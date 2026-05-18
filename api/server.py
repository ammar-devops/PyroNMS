#!/usr/bin/env python3
"""
ONT Monitor API Server — port 8088
Endpoints:
  GET  /onts                          — all ONTs from InfluxDB (main table)
  GET  /ont/live?sn=SN               — live SSH refresh for one ONT row
  GET  /device?sn=SERIALNUMBER        — full ONT detail from GenieACS (modal)
  POST /device/set                    — push TR-069 param change, verify after
  GET  /health                        — health check
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, "/opt/ont-monitor/api")
import olt_helpers as olt
sys.path.insert(0, '/opt/ont-monitor/auth')
# MikroTik module path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workers"))
sys.path.insert(0, "/root/PyroNMS-repo/workers")
try:
    import auth_db
    auth_db.init_db()
    AUTH_ENABLED = True
    print("[API] Auth system loaded OK")
except Exception as e:
    AUTH_ENABLED = False
    print(f"[API] Auth disabled: {e}")
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

# ─── Config ───────────────────────────────────────────────────────────────────
GENIEACS_NBI   = "http://localhost:7557"
API_PORT       = 8088
TASK_TIMEOUT   = 30   # seconds to wait for ONT to apply change
VERIFY_RETRIES = 5    # how many times to re-fetch after task completes
VERIFY_DELAY   = 3    # seconds between verify retries

# InfluxDB (Docker on localhost)
INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "my-super-secret-token"
INFLUX_ORG    = "myisp"
INFLUX_BUCKET = "olt_monitoring"
OLT_BACKUP_DIR = Path("/opt/ont-monitor/olt-config")
ONT_SETTINGS_TEMPLATE_PATH = Path("/opt/ont-monitor/config/ont_settings_templates.json")
SNMP_OID_TEMPLATE_PATH = Path("/opt/ont-monitor/config/snmp_oid_templates.json")

# ─── InfluxDB helpers ─────────────────────────────────────────────────────────

# Server-side graph-stats cache (5-second TTL) — avoids hammering Flux
# on repeated card refreshes/re-renders
_NET_STATS_CACHE: dict = {}      # key="<graph_id>:<range>" → (expires_at, payload)
_NET_STATS_TTL_SEC = 5
import threading as _threading
_NET_STATS_LOCK = _threading.Lock()

def _net_stats_cache_get(key):
    with _NET_STATS_LOCK:
        entry = _NET_STATS_CACHE.get(key)
        if entry and entry[0] > time.time():
            return entry[1]
    return None

def _net_stats_cache_put(key, payload):
    with _NET_STATS_LOCK:
        _NET_STATS_CACHE[key] = (time.time() + _NET_STATS_TTL_SEC, payload)
        # Crude eviction — keep size bounded
        if len(_NET_STATS_CACHE) > 500:
            _NET_STATS_CACHE.clear()


def influx_query(flux):
    """Run a Flux query against InfluxDB, return list of row dicts."""
    url  = f"{INFLUX_URL}/api/v2/query?org={urllib.parse.quote(INFLUX_ORG)}"
    body = flux.encode()
    req  = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    req.add_header("Content-Type",  "application/vnd.flux")
    req.add_header("Accept",        "application/csv")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return parse_influx_csv(resp.read().decode())
    except Exception as ex:
        print(f"[InfluxDB] query error: {ex}")
        return []


def parse_influx_csv(csv_text):
    """Parse InfluxDB annotated CSV into list of dicts."""
    rows = []
    headers = []
    for line in csv_text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if not headers:
            headers = parts
            continue
        if len(parts) < len(headers):
            continue
        row = dict(zip(headers, parts))
        rows.append(row)
    return rows


# ─── MAC Vendor Cache ─────────────────────────────────────────────────────────

MAC_VENDOR_DB = "/opt/ont-monitor/data/mac_vendor_cache.db"


def _mac_db():
    """Open (and auto-create) the MAC vendor SQLite cache."""
    os.makedirs(os.path.dirname(MAC_VENDOR_DB), exist_ok=True)
    con = sqlite3.connect(MAC_VENDOR_DB, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS sn_mac (
        sn  TEXT PRIMARY KEY,
        mac TEXT,
        ts  INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS mac_vendor (
        oui          TEXT PRIMARY KEY,
        vendor       TEXT,
        last_checked INTEGER,
        source       TEXT DEFAULT 'macvendors.com')""")
    con.commit()
    return con



def normalize_mac(mac):
    """Normalize MAC to 'AA:BB:CC:DD:EE:FF' or return None if invalid."""
    if not mac or str(mac).strip() in ('', '-', 'None'):
        return None
    s = re.sub(r'[\s.\-:]', '', str(mac)).upper()
    if len(s) != 12 or not re.match(r'^[0-9A-F]{12}$', s):
        return None
    return ':'.join(s[i:i+2] for i in range(0, 12, 2))


def get_mac_vendor(mac):
    """
    Return vendor name for a MAC address.
    Checks SQLite cache first; calls macvendors.com on miss.
    Returns: vendor string | 'Unknown' | '--' | 'Lookup Pending'
    """
    mac = normalize_mac(mac)
    if not mac:
        return '--'
    oui = mac[:8]   # e.g. "AA:BB:CC"
    try:
        con = _mac_db()
        row = con.execute("SELECT vendor FROM mac_vendor WHERE oui=?", (oui,)).fetchone()
        if row:
            con.close()
            return row[0]
        # Cache miss — query macvendors.com
        req = urllib.request.Request(
            f"https://api.macvendors.com/{mac}",
            headers={"User-Agent": "PyroNMS/4.4"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                vendor = r.read().decode().strip()
                if not vendor or len(vendor) > 120:
                    vendor = "Unknown"
        except Exception:
            con.close()
            return "Lookup Pending"
        if "not found" in vendor.lower() or vendor.startswith("{"):
            vendor = "Unknown"
        con.execute(
            "INSERT OR REPLACE INTO mac_vendor(oui,vendor,last_checked,source) VALUES(?,?,?,?)",
            (oui, vendor, int(time.time()), 'macvendors.com'))
        con.commit()
        con.close()
        return vendor
    except Exception as ex:
        print(f"[MAC] vendor lookup error: {ex}")
        return "Lookup Pending"


def _prefetch_mac_vendors():
    """
    Background thread: bulk-fetch WAN MACs from GenieACS, populate sn_mac table,
    then look up vendors for any new OUI prefixes (rate-limited: 1/second).
    Runs once 10 seconds after server startup.
    """
    time.sleep(10)
    try:
        proj = ("_id,"
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1"
                ".WANPPPConnection.1.MACAddress,"
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1"
                ".WANIPConnection.1.MACAddress")
        status, devices = genie_request("GET", f"/devices?projection={proj}")
        if status != 200 or not isinstance(devices, list):
            print(f"[MAC] prefetch: GenieACS returned {status}, skipping")
            return
        con = _mac_db()
        new_ouis = []   # (oui, mac) pairs not yet in mac_vendor table
        now = int(time.time())
        for dev in devices:
            raw_id = dev.get("_id", "")
            if not raw_id:
                continue
            igd  = dev.get("InternetGatewayDevice", {})
            wan1 = igd.get("WANDevice", {}).get("1", {})
            wcd1 = wan1.get("WANConnectionDevice", {}).get("1", {})
            ppp_mac = extract_val(wcd1.get("WANPPPConnection", {}).get("1", {}), "MACAddress")
            ip_mac  = extract_val(wcd1.get("WANIPConnection",  {}).get("1", {}), "MACAddress")
            raw_mac = ppp_mac if ppp_mac != "-" else ip_mac
            mac = normalize_mac(raw_mac)
            if not mac:
                continue
            oui = mac[:8]
            # GenieACS _id format: "OUI-ProductClass-SN" (URL-encoded)
            decoded = urllib.parse.unquote(raw_id)
            parts   = decoded.split("-")
            raw_sn  = parts[-1].upper() if len(parts) > 1 else decoded.upper()
            # Normalize SN to match /onts format (HWTCFF9464B0, not 48575443FF9464B0)
            norm_sn = normalize_sn(raw_sn) if len(raw_sn) == 16 else raw_sn
            # Store both normalized and raw so lookups work regardless of format
            for store_sn in {norm_sn, raw_sn}:
                con.execute(
                    "INSERT OR REPLACE INTO sn_mac(sn,mac,ts) VALUES(?,?,?)",
                    (store_sn, mac, now))
            # Track OUIs that need vendor lookup
            cached = con.execute(
                "SELECT 1 FROM mac_vendor WHERE oui=?", (oui,)).fetchone()
            if not cached:
                new_ouis.append((oui, mac))
        con.commit()
        con.close()
        print(f"[MAC] prefetch: {len(devices)} devices, {len(new_ouis)} new OUIs to look up")
        # Rate-limited vendor lookups — 1 per second to be polite
        for oui, mac in new_ouis:
            try:
                get_mac_vendor(mac)
                time.sleep(1)
            except Exception:
                pass
        print("[MAC] prefetch complete")
        # Retry any OUIs that failed (Lookup Pending) — wait 30s then try again
        time.sleep(30)
        _retry_pending_vendors()
    except Exception as ex:
        print(f"[MAC] prefetch error: {ex}")


def _retry_pending_vendors():
    """Retry OUI lookups that previously timed out (not yet in mac_vendor table)."""
    try:
        con = _mac_db()
        # Find unique OUIs in sn_mac that have no entry in mac_vendor
        rows = con.execute("""
            SELECT DISTINCT SUBSTR(mac, 1, 8) as oui, mac
            FROM sn_mac
            WHERE mac IS NOT NULL AND mac != ''
            AND SUBSTR(mac,1,8) NOT IN (SELECT oui FROM mac_vendor)
        """).fetchall()
        con.close()
        if not rows:
            return
        print(f"[MAC] retrying {len(rows)} pending OUI lookups...")
        for oui, mac in rows:
            try:
                get_mac_vendor(mac)
                time.sleep(2)   # slightly slower for retry
            except Exception:
                pass
        print("[MAC] retry complete")
    except Exception as ex:
        print(f"[MAC] retry error: {ex}")


def influx_write(line_protocol: str):
    """Write a line-protocol string to InfluxDB."""
    url = f"{INFLUX_URL}/api/v2/write?org={urllib.parse.quote(INFLUX_ORG)}&bucket={urllib.parse.quote(INFLUX_BUCKET)}&precision=s"
    req = urllib.request.Request(url, data=line_protocol.encode(), method="POST")
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    req.add_header("Content-Type",  "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except Exception as ex:
        print(f"[InfluxDB] write error: {ex}")
        return 0


# Server-side cache for GenieACS byte-counter previous readings {dev_id: {rx, tx, ts}}
_genie_prev_readings: dict = {}

# Server-side cache for last unregistered ONT scan result
_unreg_cache: dict = {"onts": [], "count": 0, "ts": 0, "scanned": False}


def normalize_sn(sn: str) -> str:
    """
    Normalize Huawei ONT serial number to canonical vendor form (e.g. HWTC5A819F9D).

    Two formats exist in InfluxDB — written by different pollers for the same device:
      Full 16-char hex  (poller.py SNMP path):  485754435A819F9D
      Vendor short form (slot_worker SSH path):  HWTC5A819F9D

    Conversion: first 8 hex chars of the full form are the ASCII vendor prefix.
      48 57 54 43  →  "HWTC"
      5A 81 9F 9D  →  suffix "5A819F9D"
      Result: "HWTC5A819F9D"

    Other vendor examples:
      43494F5408939108  →  CIOT08939108
      434D444310CE300E  →  CMDD10CE300E

    Short form and unrecognised SNs pass through unchanged.
    """
    sn = (sn or "").strip().upper()
    if len(sn) == 16 and all(c in "0123456789ABCDEF" for c in sn):
        try:
            vendor = bytes.fromhex(sn[:8]).decode("ascii")
            if vendor.isalpha():                  # only convert if prefix is pure letters
                return vendor + sn[8:]
        except Exception:
            pass
    return sn


def get_all_onts():
    """
    Read latest ONT status + optical from InfluxDB.
    Schema:
      ont_status  tags: sn, pon, description (customer name), olt, ont_id
                  fields: online (1/0), state ("online"/"offline")
      ont_optical tags: sn, pon, olt, ont_id
                  fields: rx_power (or rx_signal), temperature (or temp)
    Returns list of dicts: {pon, name, sn, status, rx, temp}
    """
    # Get latest 'online' and 'vlan' per ONT — 48h window
    # exists r.sn excludes old poller rows (pre-v4.3.0) that lack the sn tag
    flux_status = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "ont_status" and (r._field == "online" or r._field == "vlan") and exists r.sn and r.sn != "")
  |> last()
  |> keep(columns: ["sn", "pon", "ont_id", "description", "_field", "_value"])
'''
    # down_cause only from slot_worker — use 7-day window to catch long-offline ONTs
    flux_down_cause = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -168h)
  |> filter(fn: (r) => r._measurement == "ont_status" and r._field == "down_cause" and exists r.sn and r.sn != "")
  |> last()
  |> keep(columns: ["sn", "_field", "_value"])
'''

    # Latest optical per ONT — only rx_power and temp fields
    # exists r.sn excludes old series without sn tag (prevents CSV header mismatch)
    flux_optical = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "ont_optical" and
      (r._field == "rx_power" or r._field == "temp") and exists r.sn and r.sn != "")
  |> last()
  |> keep(columns: ["sn", "_field", "_value"])
'''
    flux_wan = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -72h)
  |> filter(fn: (r) => r._measurement == "ont_wan" and
      (r._field == "ipv4_address" or r._field == "connection_status" or r._field == "network_vlan"))
  |> last()
  |> keep(columns: ["sn", "_field", "_value"])
'''

    status_rows     = influx_query(flux_status)
    down_cause_rows = influx_query(flux_down_cause)
    optical_rows    = influx_query(flux_optical)
    wan_rows        = influx_query(flux_wan)

    # Index optical by sn — collect all fields
    # normalize_sn() ensures both full-hex (poller.py) and short vendor (SSH) SNs
    # map to the same canonical key, preventing duplicate lookups.
    optical = {}
    for r in optical_rows:
        sn    = normalize_sn(r.get("sn", "").strip())
        field = r.get("_field", "").strip()
        val   = r.get("_value", "").strip()
        if not sn:
            continue
        if sn not in optical:
            optical[sn] = {}
        optical[sn][field] = val

    # Index status rows by sn — collect online + down_cause + vlan.
    # normalize_sn() deduplicates: both 485754435A819F9D and HWTC5A819F9D
    # become HWTC5A819F9D, so the same device is never counted twice.
    status_map = {}
    for r in status_rows:
        sn    = normalize_sn(r.get("sn",          "").strip())
        pon   = r.get("pon",         "").strip()
        _raw_name = r.get("description", "").strip()
        # Blank out names that are just raw SN hex strings (e.g. "48575443EE93FF9C")
        # — these appear when no customer alias is configured on the OLT
        name  = "" if re.match(r'^[0-9A-Fa-f]{12,16}$', _raw_name) else _raw_name
        field = r.get("_field",      "online").strip()
        val   = r.get("_value",      "").strip()
        if not sn:
            continue
        ont_id = r.get("ont_id", "").strip()
        if sn not in status_map:
            # Use None sentinel for online so we can distinguish "not yet set" from "0"
            status_map[sn] = {"pon": pon, "ont_id": ont_id, "name": name, "online": None, "down_cause": "", "vlan": ""}
        else:
            # Merge: prefer non-empty values so the richer source wins
            if pon    and not status_map[sn]["pon"]:    status_map[sn]["pon"]    = pon
            if ont_id and not status_map[sn]["ont_id"]: status_map[sn]["ont_id"] = ont_id
            if name   and not status_map[sn]["name"]:   status_map[sn]["name"]   = name
        if field == "online":
            # Prefer offline ("0") over online ("1"):
            # poller writes online=1 for ALL registered ONTs (including truly offline ones)
            # slot_worker writes the correct online=0 for offline ONTs.
            # Once any source marks a device offline (0), it stays offline.
            cur = status_map[sn]["online"]
            if cur is None:
                status_map[sn]["online"] = val          # first write — accept any
            elif val == "0":
                status_map[sn]["online"] = "0"          # offline always wins
            # else: cur is already "0" or val is "1" — leave as-is
        elif field == "vlan":
            if val and not status_map[sn]["vlan"]:
                status_map[sn]["vlan"] = val

    # Merge down_cause rows (7-day window, separate query)
    for r in down_cause_rows:
        sn  = normalize_sn(r.get("sn", "").strip())
        val = r.get("_value", "").strip()
        if not sn or not val:
            continue
        if sn in status_map and not status_map[sn]["down_cause"]:
            status_map[sn]["down_cause"] = val

    # Resolve None sentinel → "0" (treat unknown as offline to be safe)
    for s in status_map.values():
        if s["online"] is None:
            s["online"] = "0"

    wan_map = {}
    for r in wan_rows:
        sn = normalize_sn(r.get("sn", "").strip())
        field = r.get("_field", "").strip()
        val = r.get("_value", "").strip()
        if not sn:
            continue
        if sn not in wan_map:
            wan_map[sn] = {"wan_ip": "", "wan_status": "", "wan_vlan": ""}
        if field == "ipv4_address":
            wan_map[sn]["wan_ip"] = val
        elif field == "connection_status":
            wan_map[sn]["wan_status"] = val
        elif field == "network_vlan":
            wan_map[sn]["wan_vlan"] = val

    onts = []
    for sn, s in status_map.items():
        opt  = optical.get(sn, {})
        wan  = wan_map.get(sn, {})
        rx   = (opt.get("rx_power") or opt.get("rx_signal") or opt.get("rx") or "-")
        temp = (opt.get("temperature") or opt.get("temp") or "-")
        is_online = s["online"] in ("1", "online", "true")
        cause = s.get("down_cause", "")
        if is_online:
            detail_status = "online"
        elif "dying" in cause or "gasp" in cause or "power" in cause:
            # dying-gasp, power-off → power failure (battery/power event)
            detail_status = "power-failure"
        elif "los" in cause or "lob" in cause or "loss" in cause or "lof" in cause or "fiber" in cause:
            # losi, lobi, lofi, los, loss, fiber-cut → fiber issue
            detail_status = "fiber-issue"
        else:
            detail_status = "offline"
        # ── Device type heuristic (ONT router vs ONU bridge) ──────────
        # Source-of-truth is OLT's `display ont info by-sn` (used by /ont/info),
        # but doing that for every list refresh is too expensive. We infer from
        # WAN-cache state instead — fast for the common case:
        #   online + has WAN IP/status        → ONT (routes WAN traffic)
        #   online + empty WAN cache          → ONU (bridge, no L3 to expose)
        #   offline / power-fail / fiber-down → '?' (can't determine without OLT call)
        # When the user opens the popup, /ont/info refreshes this with the real
        # OntProductDescription → device_type override.
        _wan_ip   = (wan.get("wan_ip") or "").strip()
        _wan_stat = (wan.get("wan_status") or "").strip().lower()
        if is_online:
            if _wan_ip and _wan_ip != "-" and _wan_ip != "0.0.0.0":
                device_type = "ONT"
            elif _wan_stat in ("connected", "connecting", "disconnected") and _wan_stat:
                device_type = "ONT"   # has WAN config, just no IP yet
            else:
                device_type = "ONU"   # online with no WAN at all → bridge
        else:
            device_type = "?"

        onts.append({
            "pon":         s["pon"],
            "ont_id":      s.get("ont_id", ""),
            "name":        s["name"],
            "sn":          sn,
            "status":      detail_status,
            "down_cause":  cause,
            "rx":          rx,
            "temp":        temp,
            "vlan":        s.get("vlan", ""),
            "wan_ip":      wan.get("wan_ip", ""),
            "wan_status":  wan.get("wan_status", ""),
            "wan_vlan":    wan.get("wan_vlan", ""),
            "device_type": device_type,
            "vendor":      None,   # enriched below from MAC cache
        })

    # Enrich vendor from cache — cache-only, no live calls (keeps /onts fast)
    try:
        _mcon = _mac_db()
        for ont in onts:
            _sn = (ont.get("sn") or "").upper()
            srow = _mcon.execute(
                "SELECT mac FROM sn_mac WHERE sn=?", (_sn,)).fetchone()
            if srow and srow[0]:
                oui  = srow[0][:8]
                vrow = _mcon.execute(
                    "SELECT vendor FROM mac_vendor WHERE oui=?", (oui,)).fetchone()
                ont["vendor"] = vrow[0] if vrow else None
        _mcon.close()
    except Exception as _mex:
        print(f"[MAC] enrichment error: {_mex}")

    return onts


def get_ont_cached(sn):
    """Fast cached ONT row from Influx-backed table payload."""
    sn = normalize_sn((sn or "").strip().upper())
    if not sn:
        return None
    for row in get_all_onts():
        if normalize_sn(row.get("sn", "").strip().upper()) == sn:
            return row
    return None


def get_cached_wan_ip(sn):
    """
    Try to get WAN IP from Influx cache first.
    This is Phase-2 ready path. If measurement doesn't exist yet, returns None.
    """
    sn = (sn or "").strip()
    if not sn:
        return None
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "ont_wan" and r.sn == "{sn}")
  |> filter(fn: (r) => r._field == "ipv4_address" or r._field == "connection_status" or r._field == "network_vlan")
  |> last()
  |> keep(columns: ["_field", "_value"])
'''
    rows = influx_query(flux)
    if not rows:
        return None
    out = {"ip": "-", "status": "", "vlan": ""}
    for r in rows:
        field = (r.get("_field") or "").strip()
        val = (r.get("_value") or "").strip()
        if field == "ipv4_address" and val and val != "0.0.0.0":
            out["ip"] = val
        elif field == "connection_status":
            out["status"] = val
        elif field == "network_vlan":
            out["vlan"] = val
    return out


def load_snmp_oid_templates():
    base = {"rx_power": "", "temp": "", "vlan": ""}
    try:
        if SNMP_OID_TEMPLATE_PATH.is_file():
            d = json.loads(SNMP_OID_TEMPLATE_PATH.read_text())
            if isinstance(d, dict):
                for k in base:
                    if k in d:
                        base[k] = str(d[k]).strip()
    except Exception:
        pass
    return base


def save_snmp_oid_templates(payload):
    base = {"rx_power": "", "temp": "", "vlan": ""}
    for k in base:
        base[k] = str((payload or {}).get(k, "")).strip()
    SNMP_OID_TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNMP_OID_TEMPLATE_PATH.write_text(json.dumps(base, indent=2))
    return base


def live_check_ont(sn):
    """
    Run live_check.py for a single ONT via subprocess.
    Returns updated ont dict or None.
    """
    try:
        result = subprocess.run(
            ["python3", "/opt/ont-monitor/workers/live_check.py", "--sn", sn],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception as ex:
        print(f"[live_check] error for {sn}: {ex}")
    return None


# ─── GenieACS helpers ─────────────────────────────────────────────────────────

def genie_request(method, path, body=None):
    url = GENIEACS_NBI + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, raw
    except Exception as ex:
        return 0, str(ex)


def resolve_sn_in_cache(sn, db_path="/opt/pyronms/data/ont_map.db"):
    """Resolve OLT-reported SN to the form stored in ont_map.db.

    The OLT reports SN as full hex (e.g. '48575443FF9464B0').
    The SNMP poller decodes it to ASCII-prefix form (e.g. 'HWTCFF9464B0').
    Try both; return (row, matched_sn) or (None, None).
    """
    import sqlite3 as _sq
    candidates = [sn.upper()]
    # If 16 hex chars, try ASCII-decoding first 4 bytes (8 hex chars)
    if len(sn) == 16 and re.match(r'^[0-9A-Fa-f]{16}$', sn):
        try:
            prefix = bytes.fromhex(sn[:8]).decode('ascii', errors='strict')
            if prefix.isalnum():
                candidates.append((prefix + sn[8:]).upper())
        except Exception:
            pass
    # Also try stripping leading zeros or other normalizations
    try:
        _db = _sq.connect(db_path, timeout=5)
        for cand in candidates:
            row = _db.execute(
                "SELECT frame, slot, port, olt_ip FROM ont_map WHERE sn=?", (cand,)
            ).fetchone()
            if row:
                _db.close()
                return row, cand
        # Last resort: LIKE search (handles minor formatting differences)
        row = _db.execute(
            "SELECT frame, slot, port, olt_ip FROM ont_map WHERE sn LIKE ?",
            (f"%{sn[-8:].upper()}",)
        ).fetchone()
        _db.close()
        return (row, sn) if row else (None, None)
    except Exception:
        return None, None


def find_device_id(sn):
    """Find GenieACS _id matching the OLT-reported SN.

    Handles 3rd-party ONTs where the OLT shows SN as full hex
    (e.g. '58504F4E05845A00') but GenieACS stores it ASCII-prefixed
    (e.g. 'XPON05845A00') because the firmware sends it that way via TR-069.
    """
    if not sn:
        return None
    sn_upper = sn.upper()
    candidates = {sn_upper}

    # If 16 hex chars (8 bytes), try ASCII-decode of the first 4 bytes
    # to handle 3rd-party ONTs where vendor prefix is sent as ASCII
    # e.g. OLT gives '58504F4E05845A00' → GenieACS stores 'XPON05845A00'
    if len(sn) == 16 and re.match(r'^[0-9A-Fa-f]{16}$', sn):
        try:
            vendor = bytes.fromhex(sn[:8]).decode('ascii')
            if vendor.isalnum() and vendor.isprintable():
                candidates.add((vendor + sn[8:]).upper())
        except (ValueError, UnicodeDecodeError):
            pass

    # If 12-char Huawei format (e.g. 'HWTCFF9464B0'), also try 16-hex form
    # GenieACS stores the SN as full hex: HWTC→48575443, so HWTCFF9464B0→48575443FF9464B0
    if len(sn) == 12 and re.match(r'^[A-Za-z]{4}[0-9A-Fa-f]{8}$', sn):
        try:
            hex16 = sn[:4].encode('ascii').hex().upper() + sn[4:].upper()
            candidates.add(hex16)
        except Exception:
            pass

    # Fetch all device IDs
    status, data = genie_request("GET", "/devices/?projection=_id")
    if status != 200 or not isinstance(data, list):
        return None

    for dev in data:
        dev_id = dev.get("_id", "")
        # GenieACS stores: OUI-ProductClass-SerialNumber (URL-encoded)
        decoded = urllib.parse.unquote(dev_id).upper()
        raw     = dev_id.upper()
        for cand in candidates:
            if decoded.endswith(cand) or raw.endswith(cand) or cand in decoded:
                return dev_id
    return None


def _genie_wan_ip(sn):
    """Fetch WAN IP from GenieACS for any ONT/ONU — PPPoE or static.
    Reuses fetch_device_data() which is the same path as the popup Internet tab.
    Returns IP string or None.
    """
    try:
        dev_id = find_device_id(sn)
        if not dev_id:
            return None
        raw = fetch_device_data(dev_id)
        if not raw:
            return None
        igd = raw.get("InternetGatewayDevice", {})
        wan_root = igd.get("WANDevice", {}).get("1", {}).get("WANConnectionDevice", {})
        # Walk all connections — PPPoE first (preferred), then IPoE
        for ctype_key in ("WANPPPConnection", "WANIPConnection"):
            for wcd_key in sorted(wan_root.keys()):
                if not wcd_key.isdigit():
                    continue
                wcd = wan_root.get(wcd_key, {})
                ctype = wcd.get(ctype_key, {})
                for conn_key in sorted(ctype.keys()):
                    if not conn_key.isdigit():
                        continue
                    conn = ctype.get(conn_key, {})
                    if not isinstance(conn, dict):
                        continue
                    ip_obj = conn.get("ExternalIPAddress", {})
                    ip = (ip_obj.get("_value", "") if isinstance(ip_obj, dict) else str(ip_obj or "")).strip()
                    if ip and ip != "-" and ip != "0.0.0.0":
                        return ip
    except Exception:
        pass
    return None


def fetch_device_data(device_id):
    """Fetch full parameter projection for a device.

    NOTE: Using whole-subtree projections (LANDevice, WANDevice) instead of leaf paths
    so we capture nested collections (Hosts.Host.*, WANPPPConnection.*, WLANConfiguration.*)
    without enumerating every index.
    """
    projection = ",".join([
        "_id",
        "_lastInform",
        "DeviceID",
        "summary",
        # Summary
        "InternetGatewayDevice.DeviceInfo",
        "InternetGatewayDevice.ManagementServer",
        # WAN — whole subtree to capture all connections + sub-params
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice",
        # LAN — whole subtree (catches WLAN bands, Hosts, IPInterface)
        "InternetGatewayDevice.LANDevice.1",
        # User accounts (Huawei) — best-effort
        "InternetGatewayDevice.UserInterface",
    ])
    # Use query filter to avoid URL double-encoding issues with device _id
    query = json.dumps({"_id": device_id})
    path = f"/devices/?query={urllib.parse.quote(query)}&projection={urllib.parse.quote(projection)}"
    status, data = genie_request("GET", path)
    if status != 200:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


def extract_val(obj, *keys):
    """Safely extract nested GenieACS value: obj['key1']['key2']['_value']"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return "-"
        cur = cur.get(k, {})
    if isinstance(cur, dict):
        v = cur.get("_value")
        return str(v) if v is not None and str(v).strip() not in ("","None") else "-"
    return str(cur) if cur is not None and str(cur).strip() not in ("","None") else "-"


def parse_device(raw):
    """Parse raw GenieACS device doc into clean structured dict."""
    igd = raw.get("InternetGatewayDevice", {})
    dev_info = igd.get("DeviceInfo", {})
    mgmt     = igd.get("ManagementServer", {})
    wan_dev  = igd.get("WANDevice", {}).get("1", {})
    wan_conn = wan_dev.get("WANConnectionDevice", {}).get("1", {})
    ppp      = wan_conn.get("WANPPPConnection", {}).get("1", {})
    ip_conn  = wan_conn.get("WANIPConnection", {}).get("1", {})
    lan_dev  = igd.get("LANDevice", {}).get("1", {})
    lan_cfg  = lan_dev.get("LANHostConfigManagement", {})
    wlan_all = lan_dev.get("WLANConfiguration", {})

    def v(node, key):
        return extract_val(node, key)

    # Last inform from _lastInform field
    _li_raw = raw.get("_lastInform", "")
    if _li_raw:
        try:
            # Convert "2026-05-06T17:25:09.582Z" to "06/05/2026, 17:25:09"
            from datetime import datetime
            _li_dt = datetime.strptime(_li_raw[:19], "%Y-%m-%dT%H:%M:%S")
            last_inform = _li_dt.strftime("%d/%m/%Y, %H:%M:%S")
        except Exception:
            last_inform = _li_raw
    else:
        last_inform = "-"
    # Parse OUI/Model/SN from _id: "OUI-ProductClass-SN"
    dev_id_str = raw.get("_id", "")
    if dev_id_str:
        _decoded = urllib.parse.unquote(dev_id_str)
        _parts   = _decoded.split("-")
        id_oui   = _parts[0] if _parts else "-"
        id_sn    = _parts[-1] if len(_parts) > 1 else "-"
        id_model = "-".join(_parts[1:-1]) if len(_parts) > 2 else (_parts[1] if len(_parts)>1 else "-")
    else:
        id_oui = id_sn = id_model = "-"

    # Uptime seconds → human
    uptime_sec = v(dev_info, "UpTime")
    try:
        secs = int(uptime_sec)
        h = secs // 3600
        uptime_str = f"{h} hours" if h < 48 else f"{h//24} days"
    except Exception:
        uptime_str = uptime_sec

    # WLAN bands — now includes BSSID, AutoChannel, TX power
    wlan_bands = []
    for band_num in sorted(wlan_all.keys(), key=lambda x: int(x) if x.isdigit() else 99):
        band = wlan_all[band_num]
        if not isinstance(band, dict):
            continue
        ssid = extract_val(band, "SSID")
        if ssid == "-":
            continue
        wlan_bands.append({
            "band_index":   band_num,
            "ssid":         ssid,
            "password":     extract_val(band, "KeyPassphrase") or extract_val(band, "PreSharedKey"),
            "band":         extract_val(band, "OperatingFrequencyBand") or extract_val(band, "Standard"),
            "channel":      extract_val(band, "Channel"),
            "security":     extract_val(band, "BeaconType") or extract_val(band, "BasicAuthenticationMode"),
            "enabled":      extract_val(band, "Enable"),
            "clients":      extract_val(band, "TotalAssociations"),
            "bssid":        extract_val(band, "BSSID"),
            "auto_channel": extract_val(band, "AutoChannelEnable"),
            "tx_power":     extract_val(band, "X_HW_TxPower") or extract_val(band, "TransmitPowerSupported"),
            "ssid_broadcast": extract_val(band, "SSIDAdvertisementEnabled"),
        })

    # WAN — collect all connections (PPPoE + IPoE) from all WANConnectionDevice instances
    wan_connections = []
    wan_root = igd.get("WANDevice", {}).get("1", {}).get("WANConnectionDevice", {})
    for wcd_key in sorted(wan_root.keys()):
        if not wcd_key.isdigit():
            continue
        wcd = wan_root.get(wcd_key, {})
        for ctype_key, ctype_name in [("WANPPPConnection", "PPPoE"), ("WANIPConnection", "IPoE")]:
            ctype = wcd.get(ctype_key, {})
            for conn_key in sorted(ctype.keys()):
                if not conn_key.isdigit():
                    continue
                conn = ctype.get(conn_key, {})
                if not isinstance(conn, dict):
                    continue
                wan_connections.append({
                    "kind":        ctype_name,
                    "wcd_index":   wcd_key,
                    "conn_index":  conn_key,
                    "name":        extract_val(conn, "Name"),
                    "enabled":     extract_val(conn, "Enable"),
                    "status":      extract_val(conn, "ConnectionStatus"),
                    "conn_type":   extract_val(conn, "ConnectionType"),
                    "service_list": extract_val(conn, "X_HW_SERVICELIST") or extract_val(conn, "X_HW_ServiceList"),
                    "vlan":        extract_val(conn, "X_HW_VLAN"),
                    "lan_bind":    extract_val(conn, "X_HW_LANBIND"),
                    "ssid_bind":   extract_val(conn, "X_HW_SSIDBIND") or extract_val(conn, "SSID_BIND"),
                    "nat":         extract_val(conn, "NATEnabled"),
                    "ip":          extract_val(conn, "ExternalIPAddress"),
                    "gateway":     extract_val(conn, "DefaultGateway"),
                    "dns":         extract_val(conn, "DNSServers"),
                    "mac":         extract_val(conn, "MACAddress"),
                    "uptime":      extract_val(conn, "Uptime"),
                    "username":    extract_val(conn, "Username"),
                })

    # Primary WAN — first connection or PPPoE-preferred for top-level summary
    primary = next((c for c in wan_connections if c["kind"] == "PPPoE"), None) or (wan_connections[0] if wan_connections else None) or {}

    # LAN
    ip_iface = lan_cfg.get("IPInterface", {}).get("1", {})
    lan_enabled       = extract_val(ip_iface, "Enable") if ip_iface else "-"
    lan_addr_type     = extract_val(ip_iface, "IPInterfaceAddressingType") if ip_iface else "-"
    dhcp_server_on    = v(lan_cfg, "DHCPServerEnable")

    # ── Connected hosts (LAN + Wi-Fi clients) ─────────────────────────
    clients = []
    hosts_root = lan_dev.get("Hosts", {}).get("Host", {})
    for hk in sorted(hosts_root.keys()):
        if not hk.isdigit():
            continue
        h = hosts_root.get(hk, {})
        if not isinstance(h, dict):
            continue
        clients.append({
            "host_name":      extract_val(h, "HostName"),
            "ip":             extract_val(h, "IPAddress"),
            "mac":            extract_val(h, "MACAddress"),
            "active":         extract_val(h, "Active"),
            "interface_type": extract_val(h, "InterfaceType"),
            "rssi":           extract_val(h, "X_HW_RSSI"),
            "lease":          extract_val(h, "LeaseTimeRemaining"),
            "rate":           extract_val(h, "X_HW_NegotiatedRate"),
            "source":         "host",
        })

    # Also pull from WLAN.AssociatedDevice (some firmwares only report wifi clients here)
    for band_num, band in wlan_all.items():
        if not band_num.isdigit() or not isinstance(band, dict):
            continue
        ad_root = band.get("AssociatedDevice", {})
        for ak in sorted(ad_root.keys()):
            if not ak.isdigit():
                continue
            ad = ad_root.get(ak, {})
            if not isinstance(ad, dict):
                continue
            mac = extract_val(ad, "AssociatedDeviceMACAddress")
            if mac == "-":
                continue
            # Skip if already in clients (by MAC)
            if any(c["mac"].upper() == mac.upper() for c in clients if c["mac"] != "-"):
                continue
            clients.append({
                "host_name":      "—",
                "ip":             extract_val(ad, "AssociatedDeviceIPAddress"),
                "mac":            mac,
                "active":         extract_val(ad, "AssociatedDeviceAuthenticationState") or "True",
                "interface_type": f"Wi-Fi (band {band_num})",
                "rssi":           extract_val(ad, "X_HW_RSSI"),
                "lease":          "-",
                "rate":           extract_val(ad, "LastDataDownlinkRate"),
                "source":         "wifi",
            })

    # ── User accounts (Huawei: X_HW_WebUserInfo) ──────────────────────
    users_root = igd.get("UserInterface", {}).get("X_HW_WebUserInfo", {})
    if not isinstance(users_root, dict):
        users_root = {}
    user_accounts = []
    for uk in sorted(users_root.keys()):
        if not uk.isdigit():
            continue
        u = users_root.get(uk, {})
        if not isinstance(u, dict):
            continue
        uname = extract_val(u, "UserName")
        if uname == "-":
            continue
        user_accounts.append({
            "index":    uk,
            "username": uname,
            "level":    extract_val(u, "UserLevel"),
        })

    conn_req_url = v(mgmt, "ConnectionRequestURL")

    return {
        "summary": {
            "serial_number": id_sn,
            "manufacturer":  (dev_info.get("Manufacturer",{}).get("_value") or "Huawei Technologies") if isinstance(dev_info.get("Manufacturer",{}),dict) else "Huawei Technologies",
            "model":         (dev_info.get("ModelName",{}).get("_value") or id_model) if isinstance(dev_info.get("ModelName",{}),dict) else id_model,
            "oui":           id_oui,
            "hw_version":    v(dev_info, "HardwareVersion"),
            "sw_version":    v(dev_info, "SoftwareVersion"),
            "uptime":        uptime_str,
            "last_inform":   last_inform,
            "conn_req_url":  conn_req_url,
            "periodic_interval": v(mgmt, "PeriodicInformInterval"),
        },
        # Compact top-level WAN (primary connection) + full connections list
        "wan": {
            "ip":         primary.get("ip", "-"),
            "gateway":    primary.get("gateway", "-"),
            "dns":        primary.get("dns", "-"),
            "pppoe_user": primary.get("username", "-"),
            "uptime":     primary.get("uptime", "-"),
            "conn_type":  primary.get("conn_type", "-"),
            "service":    primary.get("service_list", "-"),
            "vlan":       primary.get("vlan", "-"),
            "nat":        primary.get("nat", "-"),
            "mac":        primary.get("mac", "-"),
            "status":     primary.get("status", "-"),
            "name":       primary.get("name", "-"),
            "kind":       primary.get("kind", "-"),
        },
        "wan_connections": wan_connections,
        "lan": {
            "enabled":      lan_enabled,
            "type":         lan_addr_type,
            "dhcp_enabled": dhcp_server_on,
            "gateway_ip":   v(lan_cfg, "IPRouters"),
            "dhcp_min":     v(lan_cfg, "MinAddress"),
            "dhcp_max":     v(lan_cfg, "MaxAddress"),
            "subnet_mask":  v(lan_cfg, "SubnetMask"),
            "lease_time":   v(lan_cfg, "DHCPLeaseTime"),
            "dns":          v(lan_cfg, "DNSServers"),
        },
        "wlan":    wlan_bands,
        "clients": clients,
        "users":   user_accounts,
        "tr069": {
            "acs_url":      v(mgmt, "URL"),
            "username":     v(mgmt, "Username"),
            "password":     v(mgmt, "Password"),
            "conn_req_url": conn_req_url,
            "interval":     v(mgmt, "PeriodicInformInterval"),
        },
    }


# ─── Parameter path resolver ──────────────────────────────────────────────────
# Maps our logical field names to TR-069 parameter paths.
# band_index is substituted at runtime for WLAN params.

PARAM_MAP = {
    # WLAN — band_index placeholder = {b}
    "wlan.{b}.ssid":     "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.SSID",
    "wlan.{b}.password": "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.KeyPassphrase",
    "wlan.{b}.channel":  "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.Channel",
    "wlan.{b}.enabled":  "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.Enable",
    "wlan.{b}.security": "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.BeaconType",
    "wlan.{b}.auto_channel":   "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.AutoChannelEnable",
    "wlan.{b}.ssid_broadcast": "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.SSIDAdvertisementEnabled",
    "wlan.{b}.tx_power":       "InternetGatewayDevice.LANDevice.1.WLANConfiguration.{b}.X_HW_TxPower",
    # User accounts (Huawei)
    "user.1.password":         "InternetGatewayDevice.UserInterface.X_HW_WebUserInfo.1.Password",
    "user.2.password":         "InternetGatewayDevice.UserInterface.X_HW_WebUserInfo.2.Password",
    # WAN PPPoE
    "wan.pppoe_user":    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
    "wan.pppoe_pass":    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
    # LAN
    "lan.gateway_ip":    "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPRouters",
    "lan.dhcp_min":      "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress",
    "lan.dhcp_max":      "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress",
    "lan.subnet_mask":   "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.SubnetMask",
    "lan.lease_time":    "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPLeaseTime",
    # TR-069
    "tr069.acs_url":     "InternetGatewayDevice.ManagementServer.URL",
    "tr069.interval":    "InternetGatewayDevice.ManagementServer.PeriodicInformInterval",
    "tr069.username":    "InternetGatewayDevice.ManagementServer.Username",
    "tr069.password":    "InternetGatewayDevice.ManagementServer.Password",
}


def resolve_param_path(field, band_index=None):
    """Resolve logical field name to TR-069 parameter path."""
    # Try direct match first
    if field in PARAM_MAP:
        path = PARAM_MAP[field]
        if band_index:
            path = path.replace("{b}", str(band_index))
        return path
    # Try with band placeholder
    if band_index:
        templ = field  # e.g. "wlan.1.ssid" → try "wlan.{b}.ssid"
        parts = field.split(".")
        if len(parts) >= 3 and parts[0] == "wlan":
            generic = f"wlan.{{b}}.{parts[2]}"
            if generic in PARAM_MAP:
                return PARAM_MAP[generic].replace("{b}", parts[1])
    return None


def push_parameter(device_id, tr069_path, value):
    """
    Push a SetParameterValues task to GenieACS NBI.
    Uses connection request with timeout to wait for ONT to apply.
    Returns (success: bool, message: str)
    """
    enc_id = urllib.parse.quote(device_id, safe="")

    # Determine value type
    val_type = "xsd:string"
    try:
        int(value)
        val_type = "xsd:unsignedInt"
    except (ValueError, TypeError):
        pass
    if str(value).lower() in ("true", "false"):
        val_type = "xsd:boolean"
        value = str(value).lower()

    task_body = {
        "name": "setParameterValues",
        "parameterValues": [
            [tr069_path, value, val_type]
        ]
    }

    # POST task with connection request + timeout
    path = f"/devices/{enc_id}/tasks?timeout={TASK_TIMEOUT}&connection_request"
    status, resp = genie_request("POST", path, task_body)

    if status in (200, 202):
        return True, "Task accepted and applied"
    elif status == 504:
        return False, "ONT did not respond within timeout (device may be offline)"
    else:
        return False, f"GenieACS returned {status}: {resp}"


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

def get_token(handler):
    auth = handler.headers.get("Authorization","")
    if auth.startswith("Bearer "):
        return auth[7:]
    # Also accept ?token= query param (used for direct download links)
    try:
        parsed_url = urllib.parse.urlparse(handler.path)
        qs = urllib.parse.parse_qs(parsed_url.query)
        t = qs.get("token", [None])[0]
        if t:
            return t
    except Exception:
        pass
    return None

def require_auth(handler):
    if not AUTH_ENABLED:
        return {"id":0,"username":"admin","role":"superadmin","can_edit":1,"pon_access":"*"}
    token = get_token(handler)
    user = auth_db.validate_token(token)
    if not user:
        handler.send_json(401, {"error": "Unauthorized — please login"})
        return None
    return user

def backup_file_info(path):
    stat = path.stat()
    size = stat.st_size
    if size >= 1024 * 1024:
        size_text = f"{size / (1024 * 1024):.1f} MB"
    elif size >= 1024:
        size_text = f"{size / 1024:.1f} KB"
    else:
        size_text = f"{size} B"

    label = "Latest"
    m = re.search(r"olt_config_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.txt$", path.name)
    if m:
        label = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"

    return {
        "name": path.name,
        "label": label,
        "size": size,
        "size_human": size_text,
        "modified": int(stat.st_mtime),
        "modified_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "is_latest": path.name == "olt_config_latest.txt",
    }

def append_ont_settings_audit(user, kind, payload, ok, output):
    try:
        safe_payload = dict(payload)
        for key in ("pppoe_password", "password", "admin_password"):
            if key in safe_payload and safe_payload[key]:
                safe_payload[key] = "***"
        row = {
            "ts": int(time.time()),
            "user": user.get("username") or user.get("full_name") or user.get("id"),
            "kind": kind,
            "sn": safe_payload.get("sn"),
            "method": safe_payload.get("method"),
            "ok": bool(ok),
            "payload": safe_payload,
            "output": str(output)[-2000:],
        }
        with open("/var/log/pyronms_ont_settings_audit.log", "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        print(f"[ONT Settings Audit] failed: {e}")

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[API] {self.address_string()} - {format % args}")

    def send_json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected before response completed

    def send_download(self, path):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # ── GET /auth/me ──────────────────────────────────────────────────────
        if parsed.path == "/auth/me":
            user = require_auth(self)
            if not user: return
            return self.send_json(200, user)

        # ── GET /auth/profile — full profile with extra fields ────────────────
        elif parsed.path == "/auth/profile":
            user = require_auth(self)
            if not user: return
            conn = auth_db.get_db()
            row = conn.execute("SELECT id,username,full_name,role,pon_access,can_edit,email,phone,cnic,address,avatar,last_login,created_at FROM users WHERE id=?", (user["id"],)).fetchone()
            conn.close()
            if not row: return self.send_json(404,{"error":"User not found"})
            result = dict(row)
            result['active'] = int(result.get('active', 0))
            result['can_edit'] = int(result.get('can_edit', 0))
            return self.send_json(200, result)

        # ── GET /admin/users ──────────────────────────────────────────────────
        elif parsed.path == "/admin/users":
            user = require_auth(self)
            if not user: return
            if user.get("role") != "superadmin":
                return self.send_json(403, {"error": "Superadmin only"})
            return self.send_json(200, {"users": auth_db.get_all_users()})

        # ── GET /onts — full ONT list from InfluxDB ───────────────────────────
        elif parsed.path == "/olts":
            user = require_auth(self)
            if not user: return
            return self.send_json(200, {"olts": olt.get_olts()})

        elif parsed.path == "/olt/test":
            user = require_auth(self)
            if not user: return
            ip = params.get("ip",[""]) [0]
            snmp = params.get("snmp",["public"])[0]
            ok, sysname = olt.test_olt_snmp(ip, snmp)
            return self.send_json(200, {"snmp": ok, "sysname": sysname})

        elif parsed.path == "/olt/stats":
            # Get OLT temperature history from InfluxDB
            from influxdb_client import InfluxDBClient
            
            # Get time range (support both hours and days)
            days = int(params.get('days', ['0'])[0])
            hours = int(params.get('hours', ['24'])[0])
            
            if days > 0:
                range_str = f"{days}d"
                window = "30m" if days <= 7 else "2h"
            else:
                range_str = f"{hours}h"
                window = "5m"
            
            client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
            query_api = client.query_api()

            query = f'''
            from(bucket: "olt_monitoring")
              |> range(start: -{range_str})
              |> filter(fn: (r) => r["_measurement"] == "olt_temperature")
              |> filter(fn: (r) => r["_field"] == "celsius")
              |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
            '''
            
            result = query_api.query(query)
            
            data = []
            for table in result:
                for record in table.records:
                    data.append({
                        'time': record.get_time().isoformat(),
                        'slot': record.values.get('slot'),
                        'temp': round(record.get_value(), 1)
                    })
            
            client.close()
            return self.send_json(200, {'data': data, 'range': range_str})

        # ── GET /olt/uplink ───────────────────────────────────────────────────
        # range=live  → SNMP in-memory buffer (5-second resolution, last 30 min)
        # range=6h|12h|24h|3d|7d → InfluxDB historical query with aggregation
        elif parsed.path == "/olt/uplink":
            user = require_auth(self)
            if not user: return
            range_param = params.get("range", ["live"])[0].strip().lower()

            if range_param == "live":
                cutoff = time.time() - 30 * 60
                points = [
                    p for p in list(_uplink_buffer)
                    if time.mktime(time.strptime(p["time"], "%Y-%m-%dT%H:%M:%SZ")) >= cutoff
                ]
                return self.send_json(200, {
                    "ok": True, "points": points,
                    "range": "live", "interval": _UPLINK_POLL_SEC, "source": "snmp",
                })

            # Historical — map range string to Flux parameters
            range_map = {
                "6h":  ("-6h",  "1m"),
                "12h": ("-12h", "2m"),
                "24h": ("-24h", "5m"),
                "3d":  ("-3d",  "15m"),
                "7d":  ("-7d",  "30m"),
            }
            if range_param not in range_map:
                return self.send_json(400, {"error": "Invalid range"})

            flux_start, window = range_map[range_param]
            flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {flux_start})
  |> filter(fn: (r) => r._measurement == "olt_uplink" and
      (r._field == "rx_mbps" or r._field == "tx_mbps"))
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
            rows = influx_query(flux)
            points = []
            for r in rows:
                t  = r.get("_time", "")
                rx = r.get("rx_mbps")
                tx = r.get("tx_mbps")
                if t:
                    try:
                        points.append({
                            "time": t,
                            "rx":   round(float(rx), 2) if rx else 0,
                            "tx":   round(float(tx), 2) if tx else 0,
                        })
                    except (ValueError, TypeError):
                        pass
            return self.send_json(200, {
                "ok": True, "points": points,
                "range": range_param, "window": window, "source": "influxdb",
            })

        # ── GET /server/stats ─────────────────────────────────────────────────
        # Returns live CPU %, RAM %, Disk %, and uptime for the NMS server.
        # Uses /proc files — no psutil dependency required.
        elif parsed.path == "/server/stats":
            user = require_auth(self)
            if not user: return
            stats = {}
            # Uptime
            try:
                with open('/proc/uptime') as f:
                    secs = float(f.read().split()[0])
                d, rem = divmod(int(secs), 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                stats['uptime'] = f"{d}d {h}h {m}m" if d else f"{h}h {m}m"
                stats['uptime_secs'] = int(secs)
            except Exception:
                stats['uptime'] = '?'
                stats['uptime_secs'] = 0
            # CPU — two samples 0.5 s apart
            try:
                def _cpu_snap():
                    with open('/proc/stat') as f:
                        parts = f.readline().split()
                    vals = list(map(int, parts[1:8]))
                    idle = vals[3]
                    return idle, sum(vals)
                i1, t1 = _cpu_snap()
                time.sleep(0.5)
                i2, t2 = _cpu_snap()
                stats['cpu_pct'] = round(100 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1)
            except Exception:
                stats['cpu_pct'] = None
            # RAM
            try:
                mem = {}
                with open('/proc/meminfo') as f:
                    for line in f:
                        k, v = line.split(':', 1)
                        mem[k.strip()] = int(v.split()[0])
                total = mem['MemTotal']
                avail = mem.get('MemAvailable', mem.get('MemFree', 0))
                used  = total - avail
                stats['ram_pct']      = round(100 * used / max(total, 1), 1)
                stats['ram_used_gb']  = round(used  / 1024 / 1024, 1)
                stats['ram_total_gb'] = round(total / 1024 / 1024, 1)
            except Exception:
                stats['ram_pct'] = None
            # Disk (root partition)
            try:
                import shutil as _shutil
                du = _shutil.disk_usage('/')
                stats['disk_pct']      = round(100 * du.used / max(du.total, 1), 1)
                stats['disk_used_gb']  = round(du.used  / 1024**3, 1)
                stats['disk_total_gb'] = round(du.total / 1024**3, 1)
            except Exception:
                stats['disk_pct'] = None
            return self.send_json(200, stats)

        elif parsed.path == "/olt/cpu":
            # Get OLT CPU history from InfluxDB
            from influxdb_client import InfluxDBClient
            
            days = int(params.get('days', ['0'])[0])
            hours = int(params.get('hours', ['24'])[0])
            
            if days > 0:
                range_str = f"{days}d"
                window = "30m" if days <= 7 else "2h"
            else:
                range_str = f"{hours}h"
                window = "5m"
            
            client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
            query_api = client.query_api()

            query = f'''
            from(bucket: "olt_monitoring")
              |> range(start: -{range_str})
              |> filter(fn: (r) => r["_measurement"] == "olt_cpu")
              |> filter(fn: (r) => r["_field"] == "percent")
              |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
            '''
            
            result = query_api.query(query)
            
            data = []
            for table in result:
                for record in table.records:
                    data.append({
                        'time': record.get_time().isoformat(),
                        'slot': record.values.get('slot'),
                        'cpu': round(record.get_value(), 1)
                    })
            
            client.close()
            return self.send_json(200, {'data': data, 'range': range_str})

        elif parsed.path == "/server/history":
            # Get server stats history from InfluxDB
            from influxdb_client import InfluxDBClient
            
            days = int(params.get('days', ['0'])[0])
            hours = int(params.get('hours', ['24'])[0])
            
            if days > 0:
                range_str = f"{days}d"
                window = "30m" if days <= 7 else "2h"
            else:
                range_str = f"{hours}h"
                window = "5m"
            
            client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
            query_api = client.query_api()

            query = f'''
            from(bucket: "olt_monitoring")
              |> range(start: -{range_str})
              |> filter(fn: (r) => r["_measurement"] == "server_stats")
              |> filter(fn: (r) => r["_field"] == "cpu_percent" or r["_field"] == "mem_percent" or r["_field"] == "disk_percent")
              |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
            '''
            
            result = query_api.query(query)
            
            data = []
            for table in result:
                for record in table.records:
                    data.append({
                        'time': record.get_time().isoformat(),
                        'metric': record.values.get('_field'),
                        'value': round(record.get_value(), 1)
                    })
            
            client.close()
            return self.send_json(200, {'data': data, 'range': range_str})

        elif parsed.path == "/server/stats":
            user = require_auth(self)
            if not user: return
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            net = psutil.net_io_counters()
            return self.send_json(200, {
                'cpu': {'percent': cpu, 'count': psutil.cpu_count()},
                'ram': {'percent': ram.percent, 'used_gb': round(ram.used/1024**3,1), 'total_gb': round(ram.total/1024**3,1)},
                'disk': {'percent': disk.percent, 'used_gb': round(disk.used/1024**3,1), 'total_gb': round(disk.total/1024**3,1)},
                'uptime': int(time.time() - psutil.boot_time()),
                'ts': int(time.time())
            })

        elif parsed.path == "/olt/profiles":
            user = require_auth(self)
            if not user: return
            return self.send_json(200, olt.get_olt_profiles())

        elif parsed.path == "/olt/backups":
            user = require_auth(self)
            if not user: return
            try:
                files = [
                    p for p in OLT_BACKUP_DIR.glob("olt_config_*.txt")
                    if p.is_file() and p.parent == OLT_BACKUP_DIR
                ]
                files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return self.send_json(200, {"backups": [backup_file_info(p) for p in files]})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        elif parsed.path == "/olt/backups/download":
            user = require_auth(self)
            if not user: return
            name = params.get("file", [""])[0]
            if not re.match(r"^olt_config_(latest|\d{8}_\d{6})\.txt$", name):
                return self.send_json(400, {"error": "Invalid backup filename"})
            path = (OLT_BACKUP_DIR / name).resolve()
            if path.parent != OLT_BACKUP_DIR.resolve() or not path.is_file():
                return self.send_json(404, {"error": "Backup file not found"})
            return self.send_download(path)

        elif parsed.path == "/olt/unregistered/count":
            # Fast endpoint — returns cached count instantly (no SSH)
            user = require_auth(self)
            if not user: return
            import time as _t
            return self.send_json(200, {
                "count":   _unreg_cache["count"],
                "scanned": _unreg_cache["scanned"],
                "ts":      _unreg_cache["ts"],
                "age_min": round((_t.time() - _unreg_cache["ts"]) / 60, 1) if _unreg_cache["ts"] else None,
            })

        elif parsed.path == "/olt/unregistered":
            user = require_auth(self)
            if not user: return
            olts = olt.get_olts()
            if not olts: return self.send_json(404, {"error": "No OLTs"})
            o = olts[0]
            try:
                import time as _t
                onts = olt.get_unregistered_onts(o["ip"], o["username"], o["password"])
                # Update server-side cache so dashboard card stays current
                _unreg_cache["onts"]    = onts
                _unreg_cache["count"]   = len(onts)
                _unreg_cache["ts"]      = _t.time()
                _unreg_cache["scanned"] = True
                return self.send_json(200, {"onts": onts, "count": len(onts)})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        elif parsed.path == "/onts":
            user = require_auth(self)
            if not user: return
            onts = get_all_onts()
            return self.send_json(200, {"onts": onts, "count": len(onts)})

        # ── GET /ont/wan-ip?sn=XX&pon=0/1/0 — cached first, SSH fallback ───────
        elif parsed.path == '/workers':
            user = require_auth(self)
            if not user: return
            import subprocess, re as _re2
            # Read config for global + per-slot poll intervals
            global_pi = 300
            slot_pi   = {}
            try:
                cfg_txt = open('/opt/ont-monitor/config/config.py').read()
                m = _re2.search(r'(?<![_\d])POLL_INTERVAL\s*=\s*(\d+)', cfg_txt)
                global_pi = int(m.group(1)) if m else 300
                for _s in [1, 2, 4, 5]:
                    ms = _re2.search(rf'POLL_INTERVAL_{_s}\s*=\s*(\d+)', cfg_txt)
                    slot_pi[_s] = int(ms.group(1)) if ms else global_pi
            except Exception:
                for _s in [1, 2, 4, 5]:
                    slot_pi[_s] = global_pi
            workers = []
            for slot in [1, 2, 4, 5]:
                status = 'unknown'
                last_log = ''
                uptime_str = ''
                pid = ''
                try:
                    r = subprocess.run(['systemctl', 'is-active', f'ont-worker@{slot}.service'], capture_output=True, text=True)
                    status = r.stdout.strip()
                    r2 = subprocess.run(['journalctl', '-u', f'ont-worker@{slot}', '-n', '5', '--no-pager', '--output=cat'], capture_output=True, text=True)
                    lines = [l for l in r2.stdout.strip().split('\n') if l.strip()]
                    last_log = lines[-1][-120:] if lines else ''
                    r3 = subprocess.run(['systemctl', 'show', f'ont-worker@{slot}', '--property=MainPID,ActiveEnterTimestamp'], capture_output=True, text=True)
                    for prop in r3.stdout.strip().split('\n'):
                        if prop.startswith('MainPID='): pid = prop.split('=',1)[1].strip()
                        if prop.startswith('ActiveEnterTimestamp='):
                            uptime_str = prop.split('=',1)[1].strip()
                except Exception:
                    pass
                workers.append({
                    'id':           f'ont-worker@{slot}',
                    'slot':         slot,
                    'status':       status,
                    'last_log':     last_log,
                    'poll_interval': slot_pi.get(slot, global_pi),
                    'pid':          pid,
                    'since':        uptime_str,
                })
            try:
                r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
                cron_lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith('#')]
            except Exception:
                cron_lines = []
            crons = []
            for line in cron_lines:
                parts = line.split(None, 5)
                if len(parts) >= 6: crons.append({'schedule': ' '.join(parts[:5]), 'command': parts[5], 'raw': line})
                elif len(parts) == 5: crons.append({'schedule': ' '.join(parts[:4]), 'command': parts[4], 'raw': line})
            self.send_json(200, {'workers': workers, 'crons': crons, 'poll_interval': global_pi})

        elif parsed.path.startswith('/ont/wan-ip'):
            sn  = params.get('sn',  [''])[0].strip()
            pon = params.get('pon', [''])[0].strip()
            ont_id = (params.get('ont_id', [''])[0] or '').strip()
            force_genie = params.get('genie', [''])[0].strip() == '1'
            if not sn:
                self.send_json(400, {'error': 'sn required'}); return
            try:
                # ONU / force_genie: skip SSH, go straight to GenieACS
                if force_genie:
                    genie_ip = _genie_wan_ip(sn)
                    if genie_ip:
                        self.send_json(200, {'ip': genie_ip, 'sn': sn, 'source': 'genieacs'})
                    else:
                        self.send_json(200, {'ip': '-', 'sn': sn, 'error': 'No WAN IP found in GenieACS'})
                    return

                # Phase-2 fast path: Influx cache (if ont_wan measurement exists)
                cached = get_cached_wan_ip(sn)
                if cached and cached.get("ip") and cached.get("ip") != "-":
                    self.send_json(200, {
                        'ip': cached.get("ip", "-"),
                        'sn': sn,
                        'status': cached.get("status", ""),
                        'vlan': cached.get("vlan", ""),
                        'source': 'cache',
                    })
                    return

                olts = olt.get_olts()
                if not olts:
                    self.send_json(404, {'error': 'No OLTs configured'}); return
                o = olts[0]
                # Faster path when table already knows exact location
                if pon and ont_id.isdigit():
                    live = olt.get_ont_wan_live_by_path(o['ip'], o['username'], o['password'], pon, int(ont_id), sn=sn)
                else:
                    live = olt.get_ont_wan_live(o['ip'], o['username'], o['password'], sn, pon)
                if not live.get('ok'):
                    # SSH failed — try GenieACS as last resort (ONU bridge mode PPPoE IPs
                    # are only visible to TR-069, not to OLT SSH wan-info command)
                    genie_ip = _genie_wan_ip(sn)
                    if genie_ip:
                        self.send_json(200, {'ip': genie_ip, 'sn': sn, 'source': 'genieacs'})
                    else:
                        self.send_json(200, {'ip': '-', 'sn': sn, 'error': live.get('error', 'Live WAN check failed')})
                    return

                wan = (live.get('details') or {}).get('wan') or {}
                ip = (wan.get('ipv4_address') or '').strip() or '-'
                status = (wan.get('connection_status') or '').strip()
                vlan = (wan.get('network_vlan') or wan.get('manage_vlan') or '').strip()
                access_type = (wan.get('access_type') or '').strip()
                # If SSH returned no usable IP, try GenieACS fallback
                if not ip or ip == '-':
                    genie_ip = _genie_wan_ip(sn)
                    if genie_ip:
                        self.send_json(200, {'ip': genie_ip, 'sn': sn, 'status': status, 'source': 'genieacs'})
                        return
                self.send_json(200, {
                    'ip': ip,
                    'sn': sn,
                    'status': status,
                    'vlan': vlan,
                    'access_type': access_type,
                    'source': 'ssh-live'
                })
            except Exception as e:
                self.send_json(200, {'ip': '-', 'sn': sn, 'error': str(e)})

        # ── GET /ont/live?sn=XXXX — cache-first, SSH fallback ──────────────────
        elif parsed.path == "/ont/live":
            user = require_auth(self)
            if not user: return
            sn = params.get("sn", [None])[0]
            if not sn:
                return self.send_json(400, {"error": "Missing ?sn= parameter"})
            # Phase-2 fast path: Influx-backed row
            cached = get_ont_cached(sn)
            if cached:
                cached["source"] = "cache"
                return self.send_json(200, {"ont": cached})

            # Fallback to existing SSH live check
            ont = live_check_ont(sn)
            if ont:
                if isinstance(ont, dict):
                    ont["source"] = "ssh"
                return self.send_json(200, {"ont": ont})
            return self.send_json(502, {"error": "Live check failed or timed out"})

        # ── GET /ont/graph?sn=XXXX&range=1h — InfluxDB time-series for ONT charts ──
        elif parsed.path == "/ont/graph":
            user = require_auth(self)
            if not user: return
            sn = (params.get("sn", [""])[0] or "").strip()
            if not sn:
                return self.send_json(400, {"error": "Missing ?sn= parameter"})
            rng = (params.get("range", ["6h"])[0] or "6h").strip()
            if rng not in ("1h", "6h", "24h", "7d"):
                rng = "6h"
            step_map  = {"1h": "1m", "6h": "5m", "24h": "15m", "7d": "1h"}
            label_fmt = {"1h": "%H:%M", "6h": "%H:%M", "24h": "%H:%M", "7d": "%m/%d %H:%M"}
            step = step_map[rng]
            flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{rng})
  |> filter(fn: (r) => r._measurement == "ont_optical" and r.sn == "{sn}")
  |> filter(fn: (r) => r._field == "rx_power" or r._field == "tx_power" or r._field == "olt_rx" or r._field == "temp")
  |> aggregateWindow(every: {step}, fn: mean, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "rx_power", "tx_power", "olt_rx", "temp"])
  |> sort(columns: ["_time"])
'''
            try:
                rows = influx_query(flux)
            except Exception as e:
                return self.send_json(500, {"error": f"InfluxDB query failed: {e}"})
            if not rows:
                return self.send_json(200, {"ok": False, "sn": sn, "range": rng,
                                            "error": "No data for this ONT in the selected range"})
            import datetime as _dt
            fmt = label_fmt[rng]
            labels, rx_power, tx_power, olt_rx, temp = [], [], [], [], []
            for r in rows:
                t_raw = r.get("_time", "")
                try:
                    t = _dt.datetime.fromisoformat(t_raw.replace("Z", "+00:00"))
                    labels.append(t.strftime(fmt))
                except Exception:
                    labels.append(t_raw[-8:] if len(t_raw) >= 8 else t_raw)
                def _fv(key):
                    v = r.get(key, "")
                    try: return round(float(v), 2) if v not in ("", None) else None
                    except Exception: return None
                rx_power.append(_fv("rx_power"))
                tx_power.append(_fv("tx_power"))
                olt_rx.append(_fv("olt_rx"))
                temp.append(_fv("temp"))
            return self.send_json(200, {
                "ok": True, "sn": sn, "range": rng,
                "labels": labels, "rx_power": rx_power,
                "tx_power": tx_power, "olt_rx": olt_rx, "temp": temp
            })

        # ── GET /ont/info?sn=XXXX — live SSH ONT detail (replaces GenieACS) ──
        elif parsed.path == "/ont/info":
            user = require_auth(self)
            if not user: return
            sn = (params.get("sn", [""])[0] or "").strip()
            if not sn:
                return self.send_json(400, {"error": "Missing ?sn= parameter"})
            try:
                olts = olt.get_olts()
                if not olts:
                    return self.send_json(404, {"error": "No OLTs configured"})
                o = olts[0]
                data = olt.get_ont_full_info(o["ip"], o["username"], o["password"], sn,
                                             snmp_community=o.get("snmp_community"))
                return self.send_json(200 if data.get("ok") else 404, data)
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # ── GET /ont/config?sn=XXXX — WAN + WLAN config read via OLT SSH ─────
        elif parsed.path == "/ont/config":
            user = require_auth(self)
            if not user: return
            sn = (params.get("sn", [""])[0] or "").strip()
            if not sn:
                return self.send_json(400, {"error": "Missing ?sn= parameter"})
            try:
                olts = olt.get_olts()
                if not olts:
                    return self.send_json(404, {"error": "No OLTs configured"})
                o = olts[0]
                ok, data = olt.get_ont_config(o["ip"], o["username"], o["password"], sn)
                payload = {"ok": ok, **(data if isinstance(data, dict) else {})}
                return self.send_json(200 if ok else 404, payload)
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /device?sn=XXXX — restored in v3.3.0 with SSH fallback ───────
        elif parsed.path == "/device":
            user = require_auth(self)
            if not user: return
            sn = (params.get("sn", [""])[0] or "").strip()
            if not sn:
                return self.send_json(400, {"error": "Missing ?sn= parameter"})

            # 1) Try GenieACS first (full editable view)
            try:
                device_id = find_device_id(sn)
            except Exception as e:
                device_id = None
                print(f"[device] GenieACS lookup error for {sn}: {e}")

            if device_id:
                raw = fetch_device_data(device_id)
                if raw:
                    result = parse_device(raw)
                    result["_device_id"] = device_id
                    result["_source"]    = "genieacs"
                    result["_editable"]  = True
                    return self.send_json(200, result)

            # 2) Fallback: ONT not in GenieACS → try InfluxDB cache first (no SSH needed)
            #    InfluxDB has status, pon, ont_id, rx_power, temp for every polled ONT.
            #    This avoids SSH session exhaustion (slot workers hold 4 permanent sessions).
            #    SSH is only attempted if InfluxDB has absolutely no data for this SN.
            _flux_cache = f'''
from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-2h)
  |> filter(fn:(r)=>(r._measurement=="ont_status" or r._measurement=="ont_optical")
      and r.sn=="{sn}"
      and (r._field=="online" or r._field=="rx_power" or r._field=="temp"
           or r._field=="down_cause"))
  |> last()
  |> keep(columns:["_measurement","_field","_value","pon","ont_id","description"])
'''
            _cache_rows = []
            try:
                _cache_rows = influx_query(_flux_cache)
            except Exception:
                pass

            if _cache_rows:
                # Build modal response from InfluxDB — no SSH, instant load
                _cd = {}   # field → value
                _pon = _ont_id = _name = ""
                for _r in _cache_rows:
                    _cd[_r.get("_field", "")] = _r.get("_value", "")
                    if not _pon    and _r.get("pon"):         _pon    = _r["pon"]
                    if not _ont_id and _r.get("ont_id"):      _ont_id = _r["ont_id"]
                    if not _name   and _r.get("description"): _name   = _r["description"]
                _is_online = str(_cd.get("online", "0")) == "1"
                try:    _rx = round(float(_cd["rx_power"]), 2) if "rx_power" in _cd else None
                except: _rx = None
                try:    _tmp = round(float(_cd["temp"]), 1) if "temp" in _cd else None
                except: _tmp = None
                return self.send_json(200, {
                    "_source":   "influxdb",
                    "_editable": False,
                    "_message":  "ONT not registered with TR-069 — live data from SNMP poller cache",
                    "summary": {
                        "serial_number": sn,
                        "manufacturer":  "Huawei Technologies",
                        "model":         "-",
                        "oui":           "-",
                        "hw_version":    "-",
                        "sw_version":    "-",
                        "uptime":        "-",
                        "last_inform":   "-",
                        "rx_power":      _rx,
                        "tx_power":      None,
                        "temp":          _tmp,
                        "distance_m":    None,
                        "run_state":     "online" if _is_online else "offline",
                        "fsp":           _pon,
                        "ont_id":        _ont_id,
                    },
                    "wan":   {},
                    "lan":   {},
                    "wlan":  [],
                    "_device_id": None,
                })

            # 3) Last resort: InfluxDB has no data → try OLT SSH (may fail if sessions full)
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": f"Device {sn} not in GenieACS and no OLTs configured"})
            o = olts[0]
            try:
                import threading as _thr
                _ssh_result = [None]
                _ssh_exc    = [None]
                def _ssh_worker():
                    try:
                        _ssh_result[0] = olt.get_ont_full_info(
                            o["ip"], o["username"], o["password"], sn,
                            snmp_community=o.get("snmp_community"))
                    except Exception as _e:
                        _ssh_exc[0] = _e
                _t = _thr.Thread(target=_ssh_worker, daemon=True)
                _t.start()
                _t.join(timeout=8)   # 8-second hard limit — fail fast if OLT is busy
                if _t.is_alive():
                    return self.send_json(503, {"error": "OLT SSH timeout — too many concurrent sessions, try again shortly"})
                if _ssh_exc[0]:
                    raise _ssh_exc[0]
                ssh_data = _ssh_result[0] or {}
            except Exception as e:
                return self.send_json(500, {"error": f"OLT SSH error: {e}"})

            if not ssh_data.get("ok"):
                return self.send_json(404, {
                    "error": f"Device {sn} not found in GenieACS or on OLT",
                    "detail": ssh_data.get("error", "")
                })

            # Supplement SSH optical data with SNMP-polled InfluxDB values when available.
            # The SNMP poller reads OID .51.1.4 (ONT Rx power via OMCI) which matches U2000.
            # SSH "display ont optical-info" can return stale cached values from the OLT.
            influx_rx   = None
            influx_temp = None
            try:
                fsp_ssh  = ssh_data.get("fsp", "")      # e.g. "0/4/4"
                oid_ssh  = str(ssh_data.get("ont_id", ""))
                if fsp_ssh and oid_ssh:
                    _flux_opt = f'''
from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-48h)
  |> filter(fn:(r)=>r._measurement=="ont_optical"
      and r.fsp=="{fsp_ssh}"
      and r.ont_id=="{oid_ssh}"
      and (r._field=="rx_power" or r._field=="temp"))
  |> last()
  |> keep(columns:["_field","_value"])
'''
                    _opt_rows = influx_query(_flux_opt)
                    for _row in _opt_rows:
                        if _row.get("_field") == "rx_power":
                            influx_rx = float(_row["_value"])
                        elif _row.get("_field") == "temp":
                            influx_temp = float(_row["_value"])
            except Exception:
                pass

            return self.send_json(200, {
                "_source":   "ssh",
                "_editable": False,
                "_message":  "ONT not registered with TR-069 — read-only OLT view",
                "summary": {
                    "serial_number": sn,
                    "manufacturer":  ssh_data.get("vendor", "Huawei Technologies"),
                    "model":         ssh_data.get("model", "-"),
                    "oui":           "-",
                    "hw_version":    ssh_data.get("hw_version", "-"),
                    "sw_version":    ssh_data.get("sw_version", "-"),
                    "uptime":        ssh_data.get("online_duration", "-"),
                    "last_inform":   "-",
                    # Prefer SNMP/InfluxDB rx_power (matches U2000 ONU Optical Module Info).
                    # Fall back to SSH if InfluxDB has no recent data.
                    "rx_power":      influx_rx   if influx_rx   is not None else ssh_data.get("rx_power"),
                    "tx_power":      ssh_data.get("tx_power"),
                    "temp":          influx_temp if influx_temp is not None else ssh_data.get("temp"),
                    "distance_m":    ssh_data.get("distance_m"),
                    "run_state":     ssh_data.get("run_state"),
                    "fsp":           ssh_data.get("fsp"),
                    "ont_id":        ssh_data.get("ont_id"),
                },
                "wan":   {},
                "lan":   {},
                "wlan":  [],
                "tr069": {},
            })

        elif parsed.path == "/ont/settings/templates":
            user = require_auth(self)
            if not user: return
            try:
                if ONT_SETTINGS_TEMPLATE_PATH.is_file():
                    return self.send_json(200, json.loads(ONT_SETTINGS_TEMPLATE_PATH.read_text()))
                return self.send_json(200, {"models": {}, "profiles": {}})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # ── GET /snmp/probe-ont?sn=... (or slot/port/ont_id) ───────────────────
        elif parsed.path == "/snmp/probe-ont":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            try:
                olts = olt.get_olts()
                if not olts:
                    return self.send_json(404, {"error": "No OLTs configured"})
                o = olts[0]

                SNMP_OID_TEMPLATES = load_snmp_oid_templates()

                sn = (params.get("sn", [""])[0] or "").strip()
                slot = params.get("slot", [""])[0].strip()
                port = params.get("port", [""])[0].strip()
                ont_id = params.get("ont_id", [""])[0].strip()

                # If SN provided, resolve fsp/id using existing SSH helper.
                if sn and (not slot or not port or not ont_id):
                    ont_data, raw = olt.find_ont_by_sn(o["ip"], o["username"], o["password"], sn)
                    if not ont_data:
                        return self.send_json(404, {"error": "ONT not found by SN", "sn": sn, "details": raw[-600:] if raw else ""})
                    fsp = ont_data.get("fsp", "")
                    parts = fsp.split("/")
                    slot = parts[1] if len(parts) > 1 else ""
                    port = parts[2] if len(parts) > 2 else ""
                    ont_id = str(ont_data.get("ont_id", ""))

                if not slot or not port or not ont_id:
                    return self.send_json(400, {"error": "Provide sn=... or slot=..&port=..&ont_id=.."})

                read_comm = o.get("snmp_community") or "public"
                ok, data = olt.snmp_probe_ont_fields(
                    o["ip"], read_comm, int(slot), int(port), int(ont_id), SNMP_OID_TEMPLATES
                )
                if not ok:
                    return self.send_json(500, data)
                data["source"] = "snmp"
                data["sn"] = sn
                data["slot"] = int(slot)
                data["port"] = int(port)
                data["ont_id"] = int(ont_id)
                data["templates"] = SNMP_OID_TEMPLATES
                return self.send_json(200, data)
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        # ── GET /health ───────────────────────────────────────────────────────
        elif parsed.path == "/snmp/templates":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            return self.send_json(200, {"templates": load_snmp_oid_templates()})

        elif parsed.path == "/snmp/get":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            oid = (params.get("oid", [""])[0] or "").strip()
            if not re.match(r"^[0-9.]+$", oid):
                return self.send_json(400, {"error": "Invalid oid"})
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": "No OLTs configured"})
            o = olts[0]
            read_comm = o.get("snmp_community") or "public"
            return self.send_json(200, olt.snmp_get_raw(o["ip"], read_comm, oid))

        elif parsed.path == "/snmp/walk":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            oid = (params.get("oid", [""])[0] or "").strip()
            if not re.match(r"^[0-9.]+$", oid):
                return self.send_json(400, {"error": "Invalid oid"})
            limit = int((params.get("limit", ["200"])[0] or "200"))
            limit = max(10, min(limit, 1000))
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": "No OLTs configured"})
            o = olts[0]
            read_comm = o.get("snmp_community") or "public"
            return self.send_json(200, olt.snmp_walk_raw(o["ip"], read_comm, oid, limit_lines=limit))

        elif parsed.path == "/snmp/discover":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            expected_ip = (params.get("expected_ip", [""])[0] or "").strip()
            expected_temp = (params.get("expected_temp", [""])[0] or "").strip()
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": "No OLTs configured"})
            o = olts[0]
            read_comm = o.get("snmp_community") or "public"
            result = olt.snmp_discover_candidates(o["ip"], read_comm, expected_ip=expected_ip, expected_temp=expected_temp)
            result["expected_ip"] = expected_ip
            result["expected_temp"] = expected_temp
            return self.send_json(200, result)

        elif parsed.path == "/snmp/ont-map":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            pon = (params.get("pon", [""])[0] or "").strip()
            ont_id_s = (params.get("ont_id", [""])[0] or "").strip()
            expected_name = (params.get("expected_name", [""])[0] or "").strip()
            expected_ip = (params.get("expected_ip", [""])[0] or "").strip()
            expected_temp = (params.get("expected_temp", [""])[0] or "").strip()
            ont_id = int(ont_id_s) if ont_id_s.isdigit() else None
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": "No OLTs configured"})
            o = olts[0]
            read_comm = o.get("snmp_community") or "public"
            result = olt.snmp_map_ont_candidates(
                o["ip"],
                read_comm,
                pon=pon,
                ont_id=ont_id,
                expected_name=expected_name,
                expected_ip=expected_ip,
                expected_temp=expected_temp,
            )
            return self.send_json(200, result)

        elif parsed.path == "/health":
            return self.send_json(200, {"status": "ok", "influx": INFLUX_URL, "genie": GENIEACS_NBI})

        # ── GET /workers/health ────────────────────────────────────────────────
        elif parsed.path == "/workers/health":
            user = require_auth(self)
            if not user: return
            import subprocess
            health = {}
            for slot in [1, 2, 4, 5]:
                r = subprocess.run(
                    ["systemctl", "is-active", f"ont-worker@{slot}"],
                    capture_output=True, text=True
                )
                status = r.stdout.strip()
                health[f"slot{slot}"] = {"service_status": status, "active": status == "active"}
            r2 = subprocess.run(
                ["systemctl", "is-active", "pyronms-poller"],
                capture_output=True, text=True
            )
            p_status = r2.stdout.strip()
            health["poller"] = {"service_status": p_status, "active": p_status == "active"}
            return self.send_json(200, {"ok": True, "workers": health, "ts": int(time.time())})

        # ── GET /mac/vendor?mac=<mac> ──────────────────────────────────────────
        elif parsed.path == "/mac/vendor":
            user = require_auth(self)
            if not user: return
            raw_mac = params.get("mac", [""])[0].strip()
            vendor  = get_mac_vendor(raw_mac)
            return self.send_json(200, {
                "mac":    normalize_mac(raw_mac) or raw_mac,
                "vendor": vendor
            })

        # ── GET /ont/traffic/live?sn=XXXX ─────────────────────────────────────
        # Forces a TR-069 connection_request → CPE responds in ~8s with fresh
        # byte counters → calculates Mbps delta → writes point to InfluxDB
        # ont_traffic measurement (so history chart is also populated).
        elif parsed.path == "/ont/traffic/live":
            user = require_auth(self)
            if not user: return
            sn = (params.get("sn", [""])[0] or "").strip().upper()
            if not sn:
                return self.send_json(400, {"ok": False, "error": "Missing ?sn="})
            try:
                import time as _time, datetime as _dt

                # 1. Find device in GenieACS
                dev_id = find_device_id(sn)
                if not dev_id:
                    return self.send_json(404, {"ok": False, "error": "Device not in GenieACS"})

                enc_id = urllib.parse.quote(dev_id, safe='')
                STATS  = ("InternetGatewayDevice.WANDevice.1.WANConnectionDevice"
                          ".1.WANPPPConnection.1.Stats")
                PARAMS = [
                    f"{STATS}.EthernetBytesSent",
                    f"{STATS}.EthernetBytesReceived",
                    f"{STATS}.X_HW_EthernetBytesSentHigh",
                    f"{STATS}.X_HW_EthernetBytesSentLow",
                    f"{STATS}.X_HW_EthernetBytesReceivedHigh",
                    f"{STATS}.X_HW_EthernetBytesReceivedLow",
                ]

                # 2. Force CPE to connect and report fresh counters
                genie_request("POST",
                    f"/devices/{enc_id}/tasks?connection_request",
                    {"name": "getParameterValues", "parameterNames": PARAMS}
                )
                _time.sleep(8)   # wait for CPE TR-069 session

                # 3. Read fresh counters
                q    = urllib.parse.quote(json.dumps({"_id": dev_id}))
                proj = urllib.parse.quote(f"{STATS},_lastInform")
                st, data = genie_request("GET", f"/devices/?query={q}&projection={proj}")
                if st != 200 or not isinstance(data, list) or not data:
                    return self.send_json(502, {"ok": False, "error": "GenieACS read failed"})

                dev = data[0]
                def _gv(*keys):
                    obj = dev
                    for k in keys:
                        if not isinstance(obj, dict): return 0
                        obj = obj.get(k, {})
                    v = obj.get("_value", 0) if isinstance(obj, dict) else obj
                    try: return int(v or 0)
                    except: return 0

                B = ["InternetGatewayDevice","WANDevice","1",
                     "WANConnectionDevice","1","WANPPPConnection","1","Stats"]
                tx_hi = _gv(*B, "X_HW_EthernetBytesSentHigh")
                tx_lo = _gv(*B, "X_HW_EthernetBytesSentLow")
                rx_hi = _gv(*B, "X_HW_EthernetBytesReceivedHigh")
                rx_lo = _gv(*B, "X_HW_EthernetBytesReceivedLow")
                tx_bytes = (tx_hi*(2**32)+tx_lo) if (tx_hi or tx_lo) else _gv(*B,"EthernetBytesSent")
                rx_bytes = (rx_hi*(2**32)+rx_lo) if (rx_hi or rx_lo) else _gv(*B,"EthernetBytesReceived")
                now_ts = _time.time()

                # 4. Calculate Mbps from server-side previous reading
                rx_mbps = tx_mbps = 0.0
                prev = _genie_prev_readings.get(dev_id)
                if prev and rx_bytes >= prev["rx"] and tx_bytes >= prev["tx"]:
                    dt = now_ts - prev["ts"]
                    if dt > 1:
                        rx_mbps = max(0.0, (rx_bytes - prev["rx"]) * 8 / dt / 1e6)
                        tx_mbps = max(0.0, (tx_bytes - prev["tx"]) * 8 / dt / 1e6)
                        if rx_mbps > 10000: rx_mbps = 0.0
                        if tx_mbps > 10000: tx_mbps = 0.0
                _genie_prev_readings[dev_id] = {"rx": rx_bytes, "tx": tx_bytes, "ts": now_ts}

                # 5. Write data point to InfluxDB ont_traffic measurement
                sn_safe = sn.replace(" ", "_")
                if rx_mbps > 0 or tx_mbps > 0:
                    lp = (f'ont_traffic,sn={sn_safe},source=genieacs '
                          f'rx_mbps={rx_mbps:.4f},tx_mbps={tx_mbps:.4f},'
                          f'rx_bytes={rx_bytes}i,tx_bytes={tx_bytes}i '
                          f'{int(now_ts)}')
                    influx_write(lp)

                return self.send_json(200, {
                    "ok": True, "sn": sn,
                    "source": "genieacs",
                    "rx_mbps": round(rx_mbps, 2),
                    "tx_mbps": round(tx_mbps, 2),
                    "rx_bytes": int(rx_bytes),
                    "tx_bytes": int(tx_bytes),
                    "is_first": prev is None,
                    "ts": now_ts,
                })
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /ont/traffic?sn=XXXX&range=1h — PON port traffic via SNMP ──────────
        # Resolves ONT SN → FSP from ont_map.db, then queries pon_traffic
        # measurement (written by SNMP poller every ~2 min) for the PON port.
        # Works for ALL ONTs regardless of GenieACS enrollment.
        elif parsed.path == "/ont/traffic":
            user = require_auth(self)
            if not user: return
            sn     = (params.get("sn",    [""])[0] or "").strip().upper()
            range_ = (params.get("range", ["1h"])[0] or "1h").strip()
            if range_ not in ("1h", "6h", "24h", "7d"):
                range_ = "1h"
            if not sn:
                return self.send_json(400, {"ok": False, "error": "Missing ?sn="})
            try:
                # 1. Resolve SN → FSP (handles hex↔ASCII SN variants + LIKE fallback)
                row, matched_sn = resolve_sn_in_cache(sn)
                if not row:
                    return self.send_json(404, {"ok": False, "error": "SN not in SNMP cache"})
                frame, slot, port, olt_ip = row
                fsp = f"{frame}/{slot}/{port}"

                # 2. Query pon_traffic measurement for this PON port
                flux = f'''from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-{range_})
  |> filter(fn:(r)=>r._measurement=="pon_traffic"
      and r.fsp=="{fsp}"
      and (r._field=="rx_mbps" or r._field=="tx_mbps"))
  |> pivot(rowKey:["_time"],columnKey:["_field"],valueColumn:"_value")
  |> sort(columns:["_time"])
  |> keep(columns:["_time","rx_mbps","tx_mbps"])'''
                rows = influx_query(flux)

                # Format timestamps as HH:MM
                def _fmt_ts(t):
                    try: return t[11:16]   # "2026-05-14T14:37:16Z" → "14:37"
                    except: return t
                labels  = [_fmt_ts(r.get("_time", "")) for r in rows]
                rx_mbps = [round(float(r.get("rx_mbps") or 0), 2) for r in rows]
                tx_mbps = [round(float(r.get("tx_mbps") or 0), 2) for r in rows]

                # 3. Count ONTs sharing this PON port
                import sqlite3 as _sq3
                db2 = _sq3.connect("/opt/pyronms/data/ont_map.db", timeout=5)
                cnt = db2.execute(
                    "SELECT COUNT(*) FROM ont_map WHERE frame=? AND slot=? AND port=?",
                    (frame, slot, port)
                ).fetchone()[0]
                db2.close()

                return self.send_json(200, {
                    "ok":        True,
                    "sn":        sn,
                    "fsp":       fsp,
                    "source":    "snmp",
                    "ont_count": cnt,
                    "range":     range_,
                    "labels":    labels,
                    "rx_mbps":   rx_mbps,
                    "tx_mbps":   tx_mbps,
                    "rx_now":    rx_mbps[-1] if rx_mbps else 0,
                    "tx_now":    tx_mbps[-1] if tx_mbps else 0,
                })
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /ont/snmp?sn=XXXX — instant SNMP cache lookup (no SSH) ──────────
        elif parsed.path == "/ont/snmp":
            user = require_auth(self)
            if not user: return
            sn = (params.get("sn", [""])[0] or "").strip().upper()
            if not sn:
                return self.send_json(400, {"error": "Missing ?sn="})
            try:
                import sqlite3 as _sq3
                # Try raw SN and decoded ASCII-prefix form
                _cands = [sn]
                if len(sn) == 16 and re.match(r'^[0-9A-Fa-f]{16}$', sn):
                    try:
                        _prefix = bytes.fromhex(sn[:8]).decode('ascii', errors='strict')
                        if _prefix.isalnum():
                            _cands.append((_prefix + sn[8:]).upper())
                    except Exception:
                        pass
                db = _sq3.connect("/opt/pyronms/data/ont_map.db", timeout=5)
                row = None
                for _c in _cands:
                    row = db.execute(
                        "SELECT frame,slot,port,ont_id,description,pon_ifindex FROM ont_map WHERE sn=?",
                        (_c,)
                    ).fetchone()
                    if row: break
                if not row:
                    # LIKE fallback on last 8 hex chars
                    row = db.execute(
                        "SELECT frame,slot,port,ont_id,description,pon_ifindex FROM ont_map WHERE sn LIKE ?",
                        (f"%{sn[-8:]}",)
                    ).fetchone()
                if not row:
                    db.close()
                    return self.send_json(404, {"ok": False, "error": "SN not in SNMP cache"})
                frame, slot, port, ont_id, desc, pon_ifindex = row
                db.close()
                # Latest optical from InfluxDB
                flux_opt = f'''
from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-15m)
  |> filter(fn:(r)=>r._measurement=="ont_optical"
      and r.fsp=="{frame}/{slot}/{port}"
      and r.ont_id=="{ont_id}"
      and (r._field=="rx_power" or r._field=="temp"))
  |> last()
  |> keep(columns:["_field","_value"])
'''
                opt_rows = influx_query(flux_opt)
                rx_power = None
                temp_c   = None
                for _r in opt_rows:
                    if _r.get("_field") == "rx_power":
                        rx_power = _r.get("_value")
                    elif _r.get("_field") == "temp":
                        temp_c = _r.get("_value")
                # Latest status from InfluxDB
                flux_st = f'''
from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-3m)
  |> filter(fn:(r)=>r._measurement=="ont_status"
      and r.fsp=="{frame}/{slot}/{port}"
      and r.ont_id=="{ont_id}"
      and r._field=="online")
  |> last()
'''
                st_rows = influx_query(flux_st)
                online = int(st_rows[0].get("_value", 0)) if st_rows else None
                return self.send_json(200, {
                    "ok":          True,
                    "sn":          sn,
                    "fsp":         f"{frame}/{slot}/{port}",
                    "ont_id":      ont_id,
                    "description": desc,
                    "online":      online,
                    "rx_power_dbm": float(rx_power) if rx_power is not None else None,
                    "temperature_c": float(temp_c) if temp_c is not None else None,
                    "source":      "snmp_cache",
                })
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /pon/traffic — all PON ports latest Mbps for dashboard heatmap ──
        elif parsed.path == "/pon/traffic":
            user = require_auth(self)
            if not user: return
            try:
                flux = f'''
from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-5m)
  |> filter(fn:(r)=>r._measurement=="pon_traffic"
      and (r._field=="rx_mbps" or r._field=="tx_mbps"))
  |> last()
  |> pivot(rowKey:["fsp"],columnKey:["_field"],valueColumn:"_value")
  |> keep(columns:["fsp","rx_mbps","tx_mbps","slot","port"])
  |> sort(columns:["fsp"])
'''
                rows = influx_query(flux)
                # Also get OLT summary
                flux_sum = f'''
from(bucket:"{INFLUX_BUCKET}")
  |> range(start:-2m)
  |> filter(fn:(r)=>r._measurement=="olt_summary")
  |> last()
  |> pivot(rowKey:["olt"],columnKey:["_field"],valueColumn:"_value")
'''
                sum_rows = influx_query(flux_sum)
                total_online  = sum(int(r.get("online_onts", 0))  for r in sum_rows)
                total_offline = sum(int(r.get("offline_onts", 0)) for r in sum_rows)
                total_rx = sum(float(r.get("rx_mbps", 0)) for r in rows)
                total_tx = sum(float(r.get("tx_mbps", 0)) for r in rows)
                ports = [
                    {
                        "fsp":     r.get("fsp", ""),
                        "rx_mbps": round(float(r.get("rx_mbps") or 0), 2),
                        "tx_mbps": round(float(r.get("tx_mbps") or 0), 2),
                        "slot":    r.get("slot", ""),
                        "port":    r.get("port", ""),
                    }
                    for r in rows if r.get("fsp", "0/0/") and
                    not r.get("fsp","").startswith("0/0/")  # skip unused slot-0
                ]
                return self.send_json(200, {
                    "ok":           True,
                    "ports":        ports,
                    "total_rx_mbps": round(total_rx, 2),
                    "total_tx_mbps": round(total_tx, 2),
                    "online_onts":  total_online,
                    "offline_onts": total_offline,
                })
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/devices ──────────────────────────────────────────────
        elif parsed.path == "/network/devices":
            user = require_auth(self)
            if not user: return
            try:
                import network_db as ndb
                devices = ndb.get_all_devices(include_disabled=True)
                return self.send_json(200, {"ok": True, "devices": devices})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/devices/<id> ─────────────────────────────────────────
        elif re.match(r"^/network/devices/(\d+)$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/devices/(\d+)$", parsed.path)
            dev_id = int(m.group(1))
            try:
                import network_db as ndb
                dev = ndb.get_device(dev_id)
                if not dev:
                    return self.send_json(404, {"ok": False, "error": "Device not found"})
                return self.send_json(200, {"ok": True, "device": dev})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/interfaces?device_id=N ──────────────────────────────
        elif parsed.path == "/network/interfaces":
            user = require_auth(self)
            if not user: return
            device_id = params.get("device_id", [""])[0].strip()
            if not device_id:
                return self.send_json(400, {"error": "device_id required"})
            try:
                import network_db as ndb
                ifaces = ndb.get_interfaces(int(device_id))
                return self.send_json(200, {"ok": True, "interfaces": ifaces})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/graphs?device_id=N&graph_type=T ─────────────────────
        elif parsed.path == "/network/graphs":
            user = require_auth(self)
            if not user: return
            device_id  = params.get("device_id",  [""])[0].strip()
            graph_type = params.get("graph_type", [""])[0].strip()
            try:
                import network_db as ndb
                did = int(device_id) if device_id else None
                graphs = ndb.get_graphs(device_id=did,
                                        graph_type=graph_type or None)
                return self.send_json(200, {"ok": True, "graphs": graphs})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/templates ────────────────────────────────────────────
        elif parsed.path == "/network/templates":
            user = require_auth(self)
            if not user: return
            vendor     = params.get("vendor",     [""])[0].strip()
            graph_type = params.get("graph_type", [""])[0].strip()
            try:
                import network_db as ndb
                templates = ndb.get_templates(vendor=vendor or None,
                                              graph_type=graph_type or None)
                return self.send_json(200, {"ok": True, "templates": templates})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/logs?level=&since=&limit= ────────────────────────────
        # Tails the network poller log file. Real backend logs, not placeholder.
        #   level   — filter: ALL | DEBUG | INFO | WARNING | ERROR
        #   since   — unix ts; only return entries newer than this
        #   limit   — max entries to return (default 200, max 2000)
        elif parsed.path == "/network/logs":
            user = require_auth(self)
            if not user: return
            level_filter = (params.get("level", ["ALL"])[0] or "ALL").upper()
            since_ts     = float(params.get("since", ["0"])[0] or 0)
            try: limit   = max(1, min(2000, int(params.get("limit", ["200"])[0])))
            except: limit = 200

            LOG_PATH = "/opt/ont-monitor/logs/network_poller.log"
            try:
                if not os.path.exists(LOG_PATH):
                    return self.send_json(200, {"ok": True, "entries": [],
                                                "note": "log file not found"})
                # Read last ~512KB (enough for thousands of lines, fast)
                size = os.path.getsize(LOG_PATH)
                read_bytes = min(size, 512 * 1024)
                with open(LOG_PATH, "rb") as f:
                    if size > read_bytes:
                        f.seek(-read_bytes, 2)
                        f.readline()  # discard partial first line
                    raw = f.read().decode("utf-8", errors="replace")

                # Parse "YYYY-MM-DD HH:MM:SS,mmm [LEVEL] name: msg"
                import re as _re
                line_re = _re.compile(
                    r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:,(\d+))?"
                    r"\s+\[(\w+)\]\s+([^:]+):\s+(.*)$")
                entries = []
                for line in raw.splitlines():
                    m = line_re.match(line)
                    if m:
                        date_s, time_s, ms_s, lvl, name, msg = m.groups()
                        try:
                            ts = time.mktime(time.strptime(
                                f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S"))
                        except: ts = 0
                        entries.append({
                            "ts":      ts,
                            "date":    date_s,
                            "time":    time_s,
                            "ms":      int(ms_s) if ms_s else 0,
                            "level":   lvl,
                            "logger":  name.strip(),
                            "message": msg,
                        })
                    elif entries:
                        # Continuation line — append to previous entry
                        entries[-1]["message"] += "\n" + line

                # Apply filters
                if level_filter != "ALL":
                    entries = [e for e in entries if e["level"] == level_filter]
                if since_ts > 0:
                    entries = [e for e in entries if e["ts"] > since_ts]

                # Newest first, limit
                entries.reverse()
                entries = entries[:limit]

                return self.send_json(200, {"ok": True, "entries": entries,
                                            "log_path": LOG_PATH,
                                            "total": len(entries)})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/tree ─────────────────────────────────────────────────
        elif parsed.path == "/network/tree":
            user = require_auth(self)
            if not user: return
            try:
                import network_db as ndb
                tree = ndb.get_tree()
                return self.send_json(200, {"ok": True, "tree": tree})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/poller/status ────────────────────────────────────────
        elif parsed.path == "/network/poller/status":
            user = require_auth(self)
            if not user: return
            try:
                STATUS_FILE = "/tmp/net_poller_status.json"
                if os.path.exists(STATUS_FILE):
                    with open(STATUS_FILE) as _f:
                        st = json.load(_f)
                    st["uptime_sec"] = int(time.time()) - st.get("started_at",
                                                                  int(time.time()))
                else:
                    st = {"error": "Poller not running or no status yet"}
                return self.send_json(200, {"ok": True, "status": st})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/graph-data?graph_id=N&range=1h ───────────────────────
        elif parsed.path == "/network/graph-data":
            user = require_auth(self)
            if not user: return
            graph_id_s = params.get("graph_id", [""])[0].strip()
            range_p    = params.get("range",    ["1h"])[0].strip()
            if not graph_id_s:
                return self.send_json(400, {"error": "graph_id required"})
            try:
                import network_db as ndb
                NET_BUCKET = os.environ.get("NETWORK_INFLUX_BUCKET",
                                            "network_monitoring")
                # Cacti-style full range → Flux start/aggregation window
                _range_map = {"30m":"-30m","1h":"-1h","2h":"-2h","4h":"-4h",
                              "6h":"-6h","12h":"-12h","1d":"-24h","2d":"-48h",
                              "1w":"-7d","2w":"-14d","1m":"-30d","2m":"-60d",
                              "1y":"-365d","24h":"-24h","7d":"-7d","30d":"-30d"}
                _win_map   = {"30m":"30s","1h":"1m","2h":"2m","4h":"5m",
                              "6h":"5m","12h":"10m","1d":"15m","2d":"30m",
                              "1w":"1h","2w":"2h","1m":"6h","2m":"12h",
                              "1y":"1d","24h":"15m","7d":"1h","30d":"6h"}
                start  = _range_map.get(range_p, "-1h")
                window = _win_map.get(range_p, "5m")
                graph  = ndb.get_graph(int(graph_id_s))
                if not graph:
                    return self.send_json(404, {"error": "Graph not found"})
                gtype  = graph.get("graph_type", "")
                did    = str(graph.get("device_id", ""))
                labels = []; series = []
                if gtype == "traffic":
                    iname = graph.get("interface_name") or ""
                    flux = (f'from(bucket:"{NET_BUCKET}")'
                            f' |> range(start:{start})'
                            f' |> filter(fn:(r)=>r._measurement=="net_iface"'
                            f' and r.device_id=="{did}"'
                            f' and r.interface=="{iname}"'
                            f' and (r._field=="rx_bps" or r._field=="tx_bps"))'
                            f' |> aggregateWindow(every:{window},fn:mean,createEmpty:false)'
                            f' |> pivot(rowKey:["_time"],columnKey:["_field"],valueColumn:"_value")'
                            f' |> sort(columns:["_time"])')
                    rows = influx_query(flux)
                    labels = [r.get("_time","") for r in rows]
                    series = [
                        {"name": "RX bps",
                         "data": [float(r.get("rx_bps") or 0) for r in rows]},
                        {"name": "TX bps",
                         "data": [float(r.get("tx_bps") or 0) for r in rows]},
                    ]
                else:
                    _fmap = {"cpu": "cpu_pct", "memory": "mem_pct",
                             "temperature": "temp_c", "uptime": "uptime_sec",
                             "errors": "rx_errors"}
                    field = _fmap.get(gtype, gtype)
                    flux = (f'from(bucket:"{NET_BUCKET}")'
                            f' |> range(start:{start})'
                            f' |> filter(fn:(r)=>r._measurement=="net_resource"'
                            f' and r.device_id=="{did}"'
                            f' and r._field=="{field}")'
                            f' |> aggregateWindow(every:{window},fn:mean,createEmpty:false)'
                            f' |> sort(columns:["_time"])')
                    rows = influx_query(flux)
                    labels = [r.get("_time","") for r in rows]
                    series = [{"name": field,
                               "data": [float(r.get("_value") or 0) for r in rows]}]
                return self.send_json(200, {"ok": True, "labels": labels,
                                           "series": series, "graph": graph,
                                           "graph_name": graph.get("graph_name",""),
                                           "graph_type": gtype})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/graph-preview?device_id=N&range=1h ──────────────────
        elif parsed.path == "/network/graph-preview":
            user = require_auth(self)
            if not user: return
            device_id_s = params.get("device_id", [""])[0].strip()
            range_p     = params.get("range",     ["1h"])[0].strip()
            if not device_id_s:
                return self.send_json(400, {"error": "device_id required"})
            try:
                import network_db as ndb
                NET_BUCKET = os.environ.get("NETWORK_INFLUX_BUCKET",
                                            "network_monitoring")
                # Cacti-style full range → Flux start/aggregation window
                _range_map = {"30m":"-30m","1h":"-1h","2h":"-2h","4h":"-4h",
                              "6h":"-6h","12h":"-12h","1d":"-24h","2d":"-48h",
                              "1w":"-7d","2w":"-14d","1m":"-30d","2m":"-60d",
                              "1y":"-365d","24h":"-24h","7d":"-7d","30d":"-30d"}
                _win_map   = {"30m":"30s","1h":"1m","2h":"2m","4h":"5m",
                              "6h":"5m","12h":"10m","1d":"15m","2d":"30m",
                              "1w":"1h","2w":"2h","1m":"6h","2m":"12h",
                              "1y":"1d","24h":"15m","7d":"1h","30d":"6h"}
                start  = _range_map.get(range_p, "-1h")
                window = _win_map.get(range_p, "5m")
                did    = int(device_id_s)
                graphs = ndb.get_graphs(device_id=did)
                result = []
                _fmap = {"cpu": "cpu_pct", "memory": "mem_pct",
                         "temperature": "temp_c", "uptime": "uptime_sec"}
                for graph in graphs:
                    if not graph.get("enabled"):
                        continue
                    gtype = graph.get("graph_type", "")
                    labels = []; series = []
                    try:
                        dids = str(did)
                        if gtype == "traffic":
                            iname = graph.get("interface_name") or ""
                            flux = (f'from(bucket:"{NET_BUCKET}")'
                                    f' |> range(start:{start})'
                                    f' |> filter(fn:(r)=>r._measurement=="net_iface"'
                                    f' and r.device_id=="{dids}"'
                                    f' and r.interface=="{iname}"'
                                    f' and (r._field=="rx_bps" or r._field=="tx_bps"))'
                                    f' |> aggregateWindow(every:{window},fn:mean,createEmpty:false)'
                                    f' |> pivot(rowKey:["_time"],columnKey:["_field"],valueColumn:"_value")'
                                    f' |> sort(columns:["_time"])')
                            rows = influx_query(flux)
                            labels = [r.get("_time","") for r in rows]
                            series = [
                                {"name": "RX",
                                 "data": [float(r.get("rx_bps") or 0) for r in rows]},
                                {"name": "TX",
                                 "data": [float(r.get("tx_bps") or 0) for r in rows]},
                            ]
                        else:
                            field = _fmap.get(gtype, gtype)
                            flux = (f'from(bucket:"{NET_BUCKET}")'
                                    f' |> range(start:{start})'
                                    f' |> filter(fn:(r)=>r._measurement=="net_resource"'
                                    f' and r.device_id=="{dids}"'
                                    f' and r._field=="{field}")'
                                    f' |> aggregateWindow(every:{window},fn:mean,createEmpty:false)'
                                    f' |> sort(columns:["_time"])')
                            rows = influx_query(flux)
                            labels = [r.get("_time","") for r in rows]
                            series = [{"name": field,
                                       "data": [float(r.get("_value") or 0) for r in rows]}]
                    except Exception:
                        pass
                    result.append({"graph": graph, "labels": labels,
                                   "series": series})
                return self.send_json(200, {"ok": True, "previews": result})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── GET /network/graph-stats?graph_id=N&range=1h ──────────────────────
        # Cacti-style real stats — server-computed via separate Flux queries.
        # Returns {current, avg, max, total_in, total_out, last_updated, points}
        # 5-second server-side cache to avoid hammering Flux on rapid refreshes.
        elif parsed.path == "/network/graph-stats":
            user = require_auth(self)
            if not user: return
            graph_id_s = params.get("graph_id", [""])[0].strip()
            range_p    = params.get("range",    ["1h"])[0].strip()
            if not graph_id_s:
                return self.send_json(400, {"error": "graph_id required"})
            # Cache check
            _cache_key = f"{graph_id_s}:{range_p}"
            _cached = _net_stats_cache_get(_cache_key)
            if _cached is not None:
                _cached["_cached"] = True
                return self.send_json(200, _cached)
            try:
                import network_db as ndb
                NET_BUCKET = os.environ.get("NETWORK_INFLUX_BUCKET",
                                            "network_monitoring")
                _range_map = {"30m":"-30m","1h":"-1h","2h":"-2h","4h":"-4h",
                              "6h":"-6h","12h":"-12h","1d":"-24h","2d":"-48h",
                              "1w":"-7d","2w":"-14d","1m":"-30d","2m":"-60d",
                              "1y":"-365d","24h":"-24h","7d":"-7d","30d":"-30d"}
                # window in seconds — used to convert bps avg → total bytes
                _win_secs = {"30m":1800,"1h":3600,"2h":7200,"4h":14400,
                             "6h":21600,"12h":43200,"1d":86400,"2d":172800,
                             "1w":604800,"2w":1209600,"1m":2592000,
                             "2m":5184000,"1y":31536000,"24h":86400,
                             "7d":604800,"30d":2592000}
                start  = _range_map.get(range_p, "-1h")
                window_sec = _win_secs.get(range_p, 3600)
                graph  = ndb.get_graph(int(graph_id_s))
                if not graph:
                    return self.send_json(404, {"error": "Graph not found"})
                gtype  = graph.get("graph_type", "")
                did    = str(graph.get("device_id", ""))

                # Helper — run a Flux query, return first row's _value (or None)
                def _flux_one(flux):
                    rows = influx_query(flux)
                    if not rows: return None
                    v = rows[0].get("_value")
                    try: return float(v) if v is not None else None
                    except: return None

                stats = {"current": None, "avg": None, "max": None,
                         "total_in": None, "total_out": None,
                         "last_updated": None, "points": 0,
                         "graph_type": gtype}

                if gtype == "traffic":
                    iname = graph.get("interface_name") or ""
                    base_filter = (f'r._measurement=="net_iface"'
                                   f' and r.device_id=="{did}"'
                                   f' and r.interface=="{iname}"')
                    # RX: current/avg/max/sum
                    rx_cur = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="rx_bps")'
                        f' |> last()')
                    rx_avg = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="rx_bps")'
                        f' |> mean()')
                    rx_max = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="rx_bps")'
                        f' |> max()')
                    tx_cur = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="tx_bps")'
                        f' |> last()')
                    tx_avg = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="tx_bps")'
                        f' |> mean()')
                    tx_max = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="tx_bps")'
                        f' |> max()')
                    # Total bytes ≈ avg_bps × window_seconds / 8
                    stats["rx_current"] = rx_cur
                    stats["rx_avg"]     = rx_avg
                    stats["rx_max"]     = rx_max
                    stats["tx_current"] = tx_cur
                    stats["tx_avg"]     = tx_avg
                    stats["tx_max"]     = tx_max
                    stats["total_in"]   = (rx_avg * window_sec / 8) if rx_avg else 0
                    stats["total_out"]  = (tx_avg * window_sec / 8) if tx_avg else 0
                    stats["current"]    = (rx_cur or 0) + (tx_cur or 0)
                    stats["avg"]        = ((rx_avg or 0) + (tx_avg or 0)) / 2
                    stats["max"]        = max(rx_max or 0, tx_max or 0)
                    # last updated — most recent _time
                    rows = influx_query(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="rx_bps")'
                        f' |> last() |> keep(columns:["_time"])')
                    if rows:
                        stats["last_updated"] = rows[0].get("_time")
                    # points count
                    rows2 = influx_query(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter} and r._field=="rx_bps")'
                        f' |> count()')
                    if rows2:
                        try: stats["points"] = int(rows2[0].get("_value") or 0)
                        except: pass
                else:
                    _fmap = {"cpu": "cpu_pct", "memory": "mem_pct",
                             "temperature": "temp_c", "uptime": "uptime_sec",
                             "errors": "rx_errors"}
                    field = _fmap.get(gtype, gtype)
                    base_filter = (f'r._measurement=="net_resource"'
                                   f' and r.device_id=="{did}"'
                                   f' and r._field=="{field}"')
                    stats["current"] = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter}) |> last()')
                    stats["avg"] = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter}) |> mean()')
                    stats["max"] = _flux_one(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter}) |> max()')
                    rows = influx_query(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter}) |> last()'
                        f' |> keep(columns:["_time"])')
                    if rows: stats["last_updated"] = rows[0].get("_time")
                    rows2 = influx_query(
                        f'from(bucket:"{NET_BUCKET}") |> range(start:{start})'
                        f' |> filter(fn:(r)=>{base_filter}) |> count()')
                    if rows2:
                        try: stats["points"] = int(rows2[0].get("_value") or 0)
                        except: pass

                resp = {"ok": True, "stats": stats, "graph": graph}
                _net_stats_cache_put(_cache_key, resp)
                return self.send_json(200, resp)
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        else:
            return self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # ── POST /auth/login ──────────────────────────────────────────────────
        if parsed.path == "/olts":
            user = require_auth(self)
            if not user: return
            if user.get("role") != "superadmin":
                return self.send_json(403, {"error": "Superadmin only"})
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400, {"error":"Invalid JSON"})
            if not payload.get("name") or not payload.get("ip"):
                return self.send_json(400, {"error":"Name and IP required"})
            olt.add_olt(payload.get("name"),payload.get("ip"),
                payload.get("username",""),payload.get("password",""),
                payload.get("snmp_community","public"),payload.get("model","MA5603T"))
            return self.send_json(200, {"ok": True})

        elif parsed.path == "/olt/provision":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin","admin","pon_operator"):
                return self.send_json(403, {"error":"Not allowed"})
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400, {"error":"Invalid JSON"})
            sn = payload.get("sn","").strip()
            desc = payload.get("description","").strip()
            if not sn or not desc:
                return self.send_json(400, {"error":"SN and description required"})
            olts = olt.get_olts()
            if not olts: return self.send_json(404, {"error":"No OLTs"})
            o = olts[0]
            method = payload.get("method", "ssh").lower()
            try:
                if method == "snmp":
                    # SNMP provisioning via hwGponOntActivate table
                    profiles = olt.get_olt_profiles()
                    line_id  = str(payload.get("line_profile_id","8"))
                    srv_id   = str(payload.get("srv_profile_id","10"))
                    # Resolve profile name from ID
                    lp_name = next((p["name"] for p in profiles.get("line_profiles",[]) if str(p["id"]) == line_id), f"line-profile_{line_id}")
                    sp_name = next((p["name"] for p in profiles.get("srv_profiles",[])  if str(p["id"]) == srv_id),  f"srv-profile_{srv_id}")
                    write_comm = o.get("snmp_write_community") or o.get("snmp_community","public")
                    read_comm  = o.get("snmp_community","public")
                    ok, ont_id, output = olt.provision_ont_snmp(
                        o["ip"], read_comm, write_comm,
                        sn, payload.get("slot_port","0/1"),
                        int(payload.get("port",0)),
                        lp_name, sp_name, desc)
                else:
                    # Default: SSH provisioning
                    _prov_result = olt.provision_ont(
                        o["ip"],o["username"],o["password"],
                        sn,payload.get("slot_port","0/1"),
                        int(payload.get("port",0)),
                        payload.get("line_profile_id","8"),
                        payload.get("srv_profile_id","10"),desc,
                        payload.get("vlan_id","10"),
                        payload.get("user_vlan") or payload.get("vlan_id","10"),
                        payload.get("vas_profile","PPP-10-IPV4-IPV6"),
                        payload.get("alarm_profile","alarm-profile_1"),
                        payload.get("optical_alarm","optical_alarm_profile_1"),
                        payload.get("slevel_profile","alarm-policy_0"),
                        payload.get("tr069_profile","1"),
                        conn_type    = payload.get("conn_type","none"),
                        pppoe_user   = payload.get("pppoe_user",""),
                        pppoe_pass   = payload.get("pppoe_pass",""),
                        static_ip    = payload.get("static_ip",""),
                        static_subnet= payload.get("static_subnet",""),
                        static_gw    = payload.get("static_gw",""))
                    # provision_ont returns (ok, ont_id, output, verify_ok)
                    ok       = _prov_result[0]
                    ont_id   = _prov_result[1]
                    output   = _prov_result[2]
                    verify_ok= _prov_result[3] if len(_prov_result) > 3 else None
                if ok:
                    # Write a synthetic InfluxDB point so the ONT appears immediately
                    # in the PyroNMS list without waiting for the next poll cycle.
                    try:
                        slot_port_str = payload.get("slot_port","0/1")
                        port_str = str(int(payload.get("port",0)))
                        fsp = f"{slot_port_str}/{port_str}"
                        sn_clean = sn.replace(" ","").upper()
                        desc_esc = desc.replace('"', '\\"').replace(",","\\,").replace("=","\\=").replace(" ","\\ ")
                        sn_tag   = sn_clean.replace(",","\\,").replace("=","\\=").replace(" ","\\ ")
                        lp = (
                            f'ont_status,sn={sn_tag},pon={fsp},description={desc_esc},'
                            f'olt={o["ip"]},ont_id={ont_id}i '
                            f'online=1i,state="online" '
                            f'{int(time.time())}'
                        )
                        influx_write(lp)
                    except Exception as _iex:
                        print(f"[provision] influx seed warn: {_iex}")
                    conn_type = payload.get("conn_type", "none")
                    return self.send_json(200, {
                        "ok": True,
                        "ont_id": ont_id,
                        "method": method,
                        "conn_type": conn_type,
                        "verified": verify_ok,  # True=online+normal+match, False=not yet, None=skip
                    })
                return self.send_json(500, {"error":"Failed","output":output,"method":method})
            except Exception as e:
                return self.send_json(500, {"error":str(e),"method":method})

        elif parsed.path.startswith("/ont/settings/"):
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin","admin","pon_operator"):
                return self.send_json(403, {"error":"Not allowed"})
            kind = parsed.path.rsplit("/", 1)[-1]
            if kind not in ("check", "wan", "wifi", "lan", "user", "pppoe_creds", "static_ip", "wlan_radio"):
                return self.send_json(404, {"error":"Unknown settings section"})
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400, {"error":"Invalid JSON"})
            payload["kind"] = kind
            olts = olt.get_olts()
            if not olts: return self.send_json(404, {"error":"No OLTs"})
            o = olts[0]
            try:
                ok, output = olt.apply_ont_settings(o["ip"], o["username"], o["password"], payload)
                append_ont_settings_audit(user, kind, payload, ok, output)
                if ok:
                    return self.send_json(200, {"ok": True, "message": f"{kind.upper()} settings accepted", "output": output})
                return self.send_json(500, {"ok": False, "error": "Apply failed", "output": output})
            except Exception as e:
                append_ont_settings_audit(user, kind, payload, False, str(e))
                return self.send_json(500, {"error": str(e)})

        elif parsed.path == "/snmp/templates":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Not allowed"})
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            saved = save_snmp_oid_templates(payload)
            return self.send_json(200, {"ok": True, "templates": saved})

        elif parsed.path == "/ont/delete":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "Admin only"})
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            sns = payload.get("sns", [])
            if not sns or not isinstance(sns, list):
                return self.send_json(400, {"error": "sns list required"})
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": "No OLTs configured"})
            o = olts[0]
            try:
                results = olt.delete_onts(o["ip"], o["username"], o["password"], sns)
                return self.send_json(200, {"results": results})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        elif parsed.path == "/auth/login":
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400,{"error":"Invalid JSON"})
            result, err = auth_db.login(payload.get("username",""), payload.get("password",""))
            if err: return self.send_json(401,{"error":err})
            return self.send_json(200, result)

        # ── POST /auth/logout ─────────────────────────────────────────────────
        elif parsed.path == "/auth/logout":
            token = get_token(self)
            if token: auth_db.logout(token)
            return self.send_json(200,{"ok":True})

        # ── POST /admin/users — create user ───────────────────────────────────
        elif parsed.path == "/admin/users":
            user = require_auth(self)
            if not user: return
            if user.get("role") != "superadmin":
                return self.send_json(403,{"error":"Superadmin only"})
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400,{"error":"Invalid JSON"})
            ok, msg = auth_db.create_user(
                payload.get("username",""), payload.get("password",""),
                payload.get("full_name",""), payload.get("role","viewer"),
                payload.get("pon_access","*"), bool(payload.get("can_edit",0))
            )
            if ok: return self.send_json(200,{"ok":True,"message":msg})
            return self.send_json(400,{"error":msg})

        # ── POST /auth/avatar — upload avatar image ───────────────────────────
        elif parsed.path == "/auth/avatar":
            user = require_auth(self)
            if not user: return
            length = int(self.headers.get("Content-Length",0))
            if length > 5 * 1024 * 1024:
                return self.send_json(400,{"error":"File too large (max 5MB)"})
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400,{"error":"Invalid JSON"})

            # Note: do NOT re-import `re` here - it's at module level (line 14).
            # A local `import re` triggers Python compile-time scoping that makes
            # `re` local to the ENTIRE do_POST function, causing UnboundLocalError
            # on every earlier `re.match(...)` call. This broke every POST endpoint.
            import base64
            avatar_data = payload.get("avatar","")
            if not avatar_data:
                return self.send_json(400,{"error":"No avatar data"})

            # Parse base64 data URL
            match = re.match(r"data:image/(\w+);base64,(.+)", avatar_data)
            if not match:
                return self.send_json(400,{"error":"Invalid image format"})

            ext = match.group(1).lower()
            if ext not in ("png","jpg","jpeg","gif","webp"):
                return self.send_json(400,{"error":"Unsupported format"})
            if ext == "jpeg": ext = "jpg"

            img_data = base64.b64decode(match.group(2))
            avatar_dir = "/var/www/html/avatars"
            user_id = user["id"]

            # Delete old avatar files for this user
            for old_file in os.listdir(avatar_dir):
                if old_file.startswith(f"user_{user_id}."):
                    os.remove(os.path.join(avatar_dir, old_file))
                    print(f"[Avatar] Deleted old: {old_file}")

            # Save new avatar
            filename = f"user_{user_id}.{ext}"
            filepath = os.path.join(avatar_dir, filename)
            with open(filepath, "wb") as fh:
                fh.write(img_data)

            avatar_url = f"/avatars/{filename}"

            # Save URL to DB (not base64)
            auth_db.update_user(user_id, {"avatar": avatar_url})
            print(f"[Avatar] Saved: {filepath}")

            return self.send_json(200, {"ok": True, "avatar_url": avatar_url})

        # ── POST /workers/action ──────────────────────────────────────────────
        elif parsed.path == '/workers/action':
            user = require_auth(self)
            if not user: return
            if user.get('role') not in ('superadmin', 'admin'):
                return self.send_json(403, {'error': 'Admin only'})
            import subprocess
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            action = body.get('action', '')
            worker = body.get('worker', '')
            if not worker or action not in ['start', 'stop', 'restart']:
                return self.send_json(400, {'error': 'Invalid worker or action'})
            r = subprocess.run(['systemctl', action, worker], capture_output=True, text=True)
            return self.send_json(200, {'ok': r.returncode == 0, 'output': r.stderr or r.stdout})

        # ── POST /workers/poll-interval ───────────────────────────────────────
        elif parsed.path == '/workers/poll-interval':
            user = require_auth(self)
            if not user: return
            if user.get('role') not in ('superadmin', 'admin'):
                return self.send_json(403, {'error': 'Admin only'})
            import subprocess, re as _re
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            try:
                interval = int(body.get('interval', 300))
            except (ValueError, TypeError):
                return self.send_json(400, {'error': 'interval must be a number'})
            if interval < 60 or interval > 86400:
                return self.send_json(400, {'error': 'interval must be 60–86400 seconds'})
            try:
                cfg = open('/opt/ont-monitor/config/config.py').read()
                cfg = _re.sub(r'POLL_INTERVAL\s*=\s*\d+', f'POLL_INTERVAL = {interval}', cfg)
                open('/opt/ont-monitor/config/config.py', 'w').write(cfg)
            except Exception as e:
                return self.send_json(500, {'error': f'Failed to update config: {e}'})
            for slot in [1, 2, 4, 5]:
                subprocess.run(['systemctl', 'restart', f'ont-worker@{slot}'], capture_output=True)
            return self.send_json(200, {'ok': True, 'interval': interval})

        # ── POST /workers/cron ────────────────────────────────────────────────
        elif parsed.path == '/workers/cron':
            user = require_auth(self)
            if not user: return
            if user.get('role') not in ('superadmin', 'admin'):
                return self.send_json(403, {'error': 'Admin only'})
            import subprocess
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            old_raw = body.get('old_raw', '')
            new_raw = body.get('new_raw', '').strip()
            if not new_raw:
                return self.send_json(400, {'error': 'New schedule cannot be empty'})
            r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
            crontab = r.stdout
            if old_raw in crontab:
                crontab = crontab.replace(old_raw, new_raw)
                p = subprocess.run(['crontab', '-'], input=crontab, capture_output=True, text=True)
                return self.send_json(200, {'ok': p.returncode == 0})
            return self.send_json(400, {'error': 'Cron entry not found — please refresh and try again'})

        # ── POST /workers/slot-config ─────────────────────────────────────────
        elif parsed.path == '/workers/slot-config':
            user = require_auth(self)
            if not user: return
            if user.get('role') not in ('superadmin', 'admin'):
                return self.send_json(403, {'error': 'Admin only'})
            import subprocess, re as _re3
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            try:
                slot     = int(body.get('slot', 0))
                interval = int(body.get('interval', 300))
            except (ValueError, TypeError):
                return self.send_json(400, {'error': 'slot and interval must be numbers'})
            if slot not in [1, 2, 4, 5]:
                return self.send_json(400, {'error': f'Invalid slot: {slot}'})
            if interval < 60 or interval > 86400:
                return self.send_json(400, {'error': 'interval must be 60–86400 seconds'})
            try:
                cfg = open('/opt/ont-monitor/config/config.py').read()
                key = f'POLL_INTERVAL_{slot}'
                if _re3.search(rf'{key}\s*=\s*\d+', cfg):
                    cfg = _re3.sub(rf'{key}\s*=\s*\d+', f'{key} = {interval}', cfg)
                else:
                    cfg = _re3.sub(r'((?<![_\d])POLL_INTERVAL\s*=\s*\d+)', rf'\1\n{key} = {interval}', cfg)
                open('/opt/ont-monitor/config/config.py', 'w').write(cfg)
            except Exception as e:
                return self.send_json(500, {'error': f'Failed to update config: {e}'})
            subprocess.run(['systemctl', 'restart', f'ont-worker@{slot}'], capture_output=True)
            return self.send_json(200, {'ok': True, 'slot': slot, 'interval': interval})

        # ── POST /ont/action — bulk ONT lifecycle actions (v2.8.0) ──────────
        elif parsed.path == "/ont/action":
            user = require_auth(self)
            if not user: return
            if user.get("role") not in ("superadmin", "admin"):
                return self.send_json(403, {"error": "admin only"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or "0")).decode())
            except Exception as e:
                return self.send_json(400, {"error": f"bad JSON: {e}"})
            action = (body.get("action") or "").strip()
            targets = body.get("targets") or []
            if action not in ("enable","disable","reset","restore","delete"):
                return self.send_json(400, {"error": f"invalid action: {action}"})
            if not targets:
                return self.send_json(400, {"error": "no targets"})
            try:
                olts = olt.get_olts()
                if not olts:
                    return self.send_json(404, {"error": "No OLTs configured"})
                o = olts[0]
            except Exception as e:
                return self.send_json(500, {"error": f"olt lookup: {e}"})

            results = []
            for t in targets:
                sn     = (t.get("sn") or "").strip()
                pon    = (t.get("pon") or "").strip()
                ont_id = t.get("ont_id") or ""
                try:
                    r = olt.run_ont_action(o["ip"], o["username"], o["password"], action, sn=sn, pon=pon, ont_id=ont_id)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                r["sn"] = sn
                results.append(r)
            return self.send_json(200, {"ok": True, "action": action, "results": results})

        # ── POST /device/set ─────────────────────────────────────────────────
        elif parsed.path == "/device/set":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON body"})

            sn         = payload.get("sn")
            field      = payload.get("field")       # e.g. "wlan.1.ssid"
            value      = payload.get("value")       # new value string
            band_index = payload.get("band_index")  # e.g. "1" or "5" (for WLAN)

            if not sn or not field or value is None:
                return self.send_json(400, {"error": "Required: sn, field, value"})

            # Find device
            device_id = find_device_id(sn)
            if not device_id:
                return self.send_json(404, {"error": f"Device {sn} not found"})

            # Resolve TR-069 path
            tr069_path = resolve_param_path(field, band_index)
            if not tr069_path:
                return self.send_json(400, {"error": f"Unknown field: {field}"})

            print(f"[SET] device={device_id} path={tr069_path} value={value}")

            # Push change
            success, message = push_parameter(device_id, tr069_path, value)
            if not success:
                return self.send_json(502, {"success": False, "error": message})

            # Re-fetch to verify
            verified_value = None
            for attempt in range(VERIFY_RETRIES):
                time.sleep(VERIFY_DELAY)
                raw = fetch_device_data(device_id)
                if raw:
                    parsed_data = parse_device(raw)
                    verified_value = _extract_field(parsed_data, field, band_index)
                    if verified_value and verified_value != "-":
                        break
                print(f"[SET] Verify attempt {attempt+1}/{VERIFY_RETRIES}...")

            return self.send_json(200, {
                "success":        True,
                "message":        message,
                "field":          field,
                "tr069_path":     tr069_path,
                "value_sent":     value,
                "value_verified": verified_value,
                "verified":       str(verified_value) == str(value) if verified_value else None,
            })

        # ── POST /network/devices — add device ───────────────────────────────
        elif parsed.path == "/network/devices":
            user = require_auth(self)
            if not user: return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            try:
                import network_db as ndb
                new_id = ndb.add_device(payload)
                return self.send_json(200, {"ok": True, "id": new_id})
            except ValueError as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                msg = str(e)
                if "UNIQUE constraint failed: network_devices.ip" in msg:
                    ip = payload.get("ip", "")
                    return self.send_json(409, {"ok": False,
                        "error": f"A device with IP {ip} already exists. "
                                  "Find it in the Devices list and edit it instead."})
                return self.send_json(500, {"ok": False, "error": msg})

        # ── POST /network/devices/<id>/discover ───────────────────────────────
        elif re.match(r"^/network/devices/(\d+)/discover$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/devices/(\d+)/discover$", parsed.path)
            dev_id = int(m.group(1))
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                payload = {}
            try:
                import network_db as ndb
                import network_snmp as nsnmp
                dev = ndb.get_device(dev_id, include_creds=True)
                if not dev:
                    return self.send_json(404, {"ok": False, "error": "Device not found"})
                # Vendor + sys info
                sysinfo = nsnmp.test_device(dev)
                if sysinfo.get("snmp_ok"):
                    vendor_det = sysinfo.get("vendor_detected") or dev.get("vendor", "generic")
                    ndb.update_device(dev_id, {"vendor": vendor_det})
                    ndb.set_device_status(dev_id, "online", sysinfo.get("ms", 0),
                                          sys_name=sysinfo.get("sys_name"),
                                          sys_descr=sysinfo.get("sys_descr"),
                                          sys_object_id=sysinfo.get("sys_object_id"))
                # Interface walk
                ifaces = nsnmp.discover_interfaces(dev)
                ndb.upsert_interfaces(dev_id, ifaces)
                # Auto-create traffic graphs?
                graphs_created = 0
                if payload.get("auto_create_traffic") and ifaces:
                    templates = ndb.get_templates(graph_type="traffic")
                    traffic_tpl = next((t for t in templates
                                        if t.get("builtin") and "IF-MIB" in t["name"]),
                                       templates[0] if templates else None)
                    if traffic_tpl:
                        existing = ndb.get_graphs(device_id=dev_id, graph_type="traffic")
                        existing_iface_ids = {g.get("interface_id") for g in existing}
                        for iface_row in ndb.get_interfaces(dev_id):
                            if (iface_row.get("oper_status") == 1 and
                                    iface_row["id"] not in existing_iface_ids):
                                ndb.add_graph(
                                    device_id=dev_id,
                                    template_id=traffic_tpl["id"],
                                    interface_id=iface_row["id"],
                                    graph_name=f"Traffic — {iface_row.get('if_name','')}")
                                graphs_created += 1
                return self.send_json(200, {
                    "ok": True,
                    "interfaces": ifaces,
                    "graphs_created": graphs_created,
                    "snmp_ok": sysinfo.get("snmp_ok", False),
                })
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/devices/<id>/test — SNMP test ───────────────────────
        elif re.match(r"^/network/devices/(\d+)/test$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/devices/(\d+)/test$", parsed.path)
            dev_id = int(m.group(1))
            try:
                import network_db as ndb
                import network_snmp as nsnmp
                dev = ndb.get_device(dev_id, include_creds=True)
                if not dev:
                    return self.send_json(404, {"ok": False, "error": "Device not found"})
                result = nsnmp.test_device(dev)
                return self.send_json(200, {"ok": True, **result})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/devices/<id>/delete ────────────────────────────────
        elif re.match(r"^/network/devices/(\d+)/delete$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/devices/(\d+)/delete$", parsed.path)
            dev_id = int(m.group(1))
            try:
                import network_db as ndb
                ok = ndb.delete_device(dev_id)
                if not ok:
                    return self.send_json(404, {"ok": False, "error": "Device not found"})
                return self.send_json(200, {"ok": True})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/devices/<id> — update device ────────────────────────
        elif re.match(r"^/network/devices/(\d+)$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/devices/(\d+)$", parsed.path)
            dev_id = int(m.group(1))
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            try:
                import network_db as ndb
                ok = ndb.update_device(dev_id, payload)
                if not ok:
                    return self.send_json(404, {"ok": False, "error": "Device not found"})
                return self.send_json(200, {"ok": True})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/interfaces/<id>/toggle ──────────────────────────────
        elif re.match(r"^/network/interfaces/(\d+)/toggle$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/interfaces/(\d+)/toggle$", parsed.path)
            iface_id = int(m.group(1))
            try:
                import network_db as ndb
                enabled = ndb.toggle_interface(iface_id)
                if enabled is None:
                    return self.send_json(404, {"ok": False, "error": "Interface not found"})
                return self.send_json(200, {"ok": True, "polling_enabled": enabled})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/graphs — create graph ───────────────────────────────
        elif parsed.path == "/network/graphs":
            user = require_auth(self)
            if not user: return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            try:
                import network_db as ndb
                dev_id   = int(payload.get("device_id", 0))
                tpl_id   = int(payload.get("template_id", 0))
                iface_id = payload.get("interface_id")
                if iface_id is not None:
                    iface_id = int(iface_id)
                gname  = payload.get("graph_name") or None
                new_id = ndb.add_graph(dev_id, tpl_id, iface_id, gname)
                return self.send_json(200, {"ok": True, "id": new_id})
            except ValueError as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/graphs/<id>/delete ──────────────────────────────────
        elif re.match(r"^/network/graphs/(\d+)/delete$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/graphs/(\d+)/delete$", parsed.path)
            gid = int(m.group(1))
            try:
                import network_db as ndb
                ok = ndb.delete_graph(gid)
                if not ok:
                    return self.send_json(404, {"ok": False, "error": "Graph not found"})
                return self.send_json(200, {"ok": True})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/templates — create custom template ──────────────────
        elif parsed.path == "/network/templates":
            user = require_auth(self)
            if not user: return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            try:
                import network_db as ndb
                new_id = ndb.add_template(payload)
                return self.send_json(200, {"ok": True, "id": new_id})
            except ValueError as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/templates/<id>/delete ───────────────────────────────
        elif re.match(r"^/network/templates/(\d+)/delete$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/templates/(\d+)/delete$", parsed.path)
            tid = int(m.group(1))
            try:
                import network_db as ndb
                ok = ndb.delete_template(tid)
                if not ok:
                    return self.send_json(404, {"ok": False, "error": "Template not found"})
                return self.send_json(200, {"ok": True})
            except ValueError as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/tree — add tree node ────────────────────────────────
        elif parsed.path == "/network/tree":
            user = require_auth(self)
            if not user: return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            try:
                import network_db as ndb
                parent = payload.get("parent_id")
                if parent is not None:
                    parent = int(parent)
                new_id = ndb.add_tree_node(
                    parent_id  = parent,
                    name       = payload.get("name", "New Folder"),
                    node_type  = payload.get("node_type", "folder"),
                    device_id  = payload.get("device_id"),
                    graph_id   = payload.get("graph_id"),
                    sort_order = int(payload.get("sort_order", 0)),
                )
                return self.send_json(200, {"ok": True, "id": new_id})
            except ValueError as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/tree/<id>/delete ────────────────────────────────────
        elif re.match(r"^/network/tree/(\d+)/delete$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/tree/(\d+)/delete$", parsed.path)
            nid = int(m.group(1))
            try:
                import network_db as ndb
                ok = ndb.delete_tree_node(nid)
                if not ok:
                    return self.send_json(404, {"ok": False, "error": "Node not found"})
                return self.send_json(200, {"ok": True})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        # ── POST /network/tree/<id> — rename / reparent ───────────────────────
        elif re.match(r"^/network/tree/(\d+)$", parsed.path):
            user = require_auth(self)
            if not user: return
            m = re.match(r"^/network/tree/(\d+)$", parsed.path)
            nid = int(m.group(1))
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                return self.send_json(400, {"error": "Invalid JSON"})
            try:
                import network_db as ndb
                kw = {}
                if "name"       in payload: kw["name"]       = payload["name"]
                if "parent_id"  in payload: kw["parent_id"]  = payload["parent_id"]
                if "sort_order" in payload: kw["sort_order"] = int(payload["sort_order"])
                ok = ndb.update_tree_node(nid, **kw)
                if not ok:
                    return self.send_json(404, {"ok": False, "error": "Node not found"})
                return self.send_json(200, {"ok": True})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        else:
            return self.send_json(404, {"error": "Not found"})


    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/auth/profile":
            user = require_auth(self)
            if not user: return
            length = int(self.headers.get("Content-Length",0))
            if length > 10 * 1024 * 1024:  # 10MB limit
                return self.send_json(400,{"error":"Request too large"})
            body = self.rfile.read(length) if length > 0 else b'{}'
            try: payload = json.loads(body)
            except: return self.send_json(400,{"error":"Invalid JSON"})
            allowed = ["full_name","email","phone","cnic","address","avatar"]
            data = {k:v for k,v in payload.items() if k in allowed}
            if payload.get("new_password"):
                if not payload.get("current_password"):
                    return self.send_json(400,{"error":"Current password required"})
                conn = auth_db.get_db()
                row = conn.execute("SELECT password FROM users WHERE id=?",(user["id"],)).fetchone()
                conn.close()
                if not row or not auth_db.verify_password(payload["current_password"],row["password"]):
                    return self.send_json(401,{"error":"Current password incorrect"})
                data["password"] = payload["new_password"]
            ok,msg = auth_db.update_user(user["id"],data)
            if ok: return self.send_json(200,{"ok":True})
            return self.send_json(400,{"error":msg})

        elif parsed.path == "/admin/user":
            user = require_auth(self)
            if not user: return
            if user.get("role") != "superadmin":
                return self.send_json(403,{"error":"Superadmin only"})
            uid = int(params.get("id",[0])[0])
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            try: payload = json.loads(body)
            except: return self.send_json(400,{"error":"Invalid JSON"})
            ok, msg = auth_db.update_user(uid, payload)
            if ok: return self.send_json(200,{"ok":True})
            return self.send_json(400,{"error":msg})

        else:
            return self.send_json(404,{"error":"Not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/olts":
            user = require_auth(self)
            if not user: return
            if user.get("role") != "superadmin":
                return self.send_json(403, {"error":"Superadmin only"})
            olt_id = params.get("id",[0])[0]
            ok, msg = olt.delete_olt(olt_id)
            if ok: return self.send_json(200, {"ok":True})
            return self.send_json(400, {"error":msg})
        elif parsed.path == "/admin/user":
            user = require_auth(self)
            if not user: return
            if user.get("role") != "superadmin":
                return self.send_json(403,{"error":"Superadmin only"})
            uid = int(params.get("id",[0])[0])
            ok, msg = auth_db.delete_user(uid)
            return self.send_json(200,{"ok":True})

        else:
            return self.send_json(404,{"error":"Not found"})

def _extract_field(parsed_data, field, band_index=None):
    """Extract a field from already-parsed device data for verification."""
    parts = field.split(".")
    if parts[0] == "wlan" and len(parts) >= 3:
        idx = parts[1] if len(parts) > 2 else band_index
        key = parts[2]
        for band in parsed_data.get("wlan", []):
            if str(band.get("band_index")) == str(idx):
                return band.get(key)
    elif parts[0] in ("wan", "lan", "tr069", "summary"):
        section = parsed_data.get(parts[0], {})
        return section.get(parts[1]) if len(parts) > 1 else None
    return None


# ─── Main ─────────────────────────────────────────────────────────────────────

# ── OLT Uplink SNMP Poller ────────────────────────────────────────────────────
# Polls the OLT uplink port(s) via SNMP every 5 seconds using ifHCInOctets /
# ifHCOutOctets (64-bit byte counters). Stores a rolling 30-minute deque.
# Auto-discovers uplink ifIndex by walking ifDescr — finds GigabitEthernet /
# XGigabitEthernet interfaces (excludes GPON ports whose ifIndex > 0xFA000000).

OLT_HOST             = "172.20.101.101"
OLT_SNMP_COMMUNITY   = "kknread@123"
_IFDESCR_OID         = "1.3.6.1.2.1.2.2.1.2"          # ifDescr table
_IFHC_IN_OID         = "1.3.6.1.2.1.31.1.1.1.6"       # ifHCInOctets
_IFHC_OUT_OID        = "1.3.6.1.2.1.31.1.1.1.10"      # ifHCOutOctets
_UPLINK_POLL_SEC     = 5                                # poll interval
_UPLINK_MAXLEN       = int(30 * 60 / _UPLINK_POLL_SEC) # 360 points = 30 min

from collections import deque
_uplink_buffer  = deque(maxlen=_UPLINK_MAXLEN)          # shared, thread-safe append
_uplink_ifindex = []                                    # discovered uplink ifIndices


def _snmp_get_val(oid, host=OLT_HOST, community=OLT_SNMP_COMMUNITY, timeout=4):
    """Single snmpget — returns raw value string or None."""
    try:
        r = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-Oqv", "-t", str(timeout), "-r", "1",
             host, oid],
            capture_output=True, text=True, timeout=timeout + 2)
        out = (r.stdout or "").strip()
        return out if out and r.returncode == 0 else None
    except Exception:
        return None


def _snmp_walk_raw(oid, host=OLT_HOST, community=OLT_SNMP_COMMUNITY, timeout=10):
    """snmpwalk — yields (full_oid, value) tuples."""
    try:
        r = subprocess.run(
            ["snmpwalk", "-v2c", "-c", community, "-Oqn", "-t", str(timeout), "-r", "1",
             host, oid],
            capture_output=True, text=True, timeout=timeout + 2)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                yield parts[0], parts[1]
    except Exception:
        pass


def _discover_uplink_ifindices():
    """Walk ifDescr, return list of ifIndex for GE/10GE uplink ports."""
    indices = []
    gpon_base = 0xFA000000
    for full_oid, descr in _snmp_walk_raw(_IFDESCR_OID):
        # OID ends with .ifIndex
        try:
            idx = int(full_oid.rsplit(".", 1)[-1])
        except ValueError:
            continue
        if idx >= gpon_base:
            continue   # skip GPON PON ports
        descr_l = descr.strip().lower().strip('"')
        if "ethernet" in descr_l or "eth" in descr_l or "uplink" in descr_l:
            indices.append(idx)
            print(f"[UPLINK] discovered ifIndex={idx}  descr={descr.strip()}")
    return indices


def _uplink_poller():
    """Background thread: poll OLT uplink counters every 5 seconds."""
    global _uplink_ifindex
    # Wait for server to settle, then discover uplink interfaces
    time.sleep(8)
    _uplink_ifindex = _discover_uplink_ifindices()
    if not _uplink_ifindex:
        print("[UPLINK] no uplink interfaces discovered — retrying in 60s")
        time.sleep(60)
        _uplink_ifindex = _discover_uplink_ifindices()
    if not _uplink_ifindex:
        print("[UPLINK] SNMP uplink discovery failed — poller disabled")
        return

    prev = {}   # ifIndex → (in_bytes, out_bytes, ts)
    print(f"[UPLINK] polling {len(_uplink_ifindex)} uplink port(s) every {_UPLINK_POLL_SEC}s")

    while True:
        ts = time.time()
        total_rx_bps = 0.0
        total_tx_bps = 0.0
        valid = False

        for idx in _uplink_ifindex:
            in_raw  = _snmp_get_val(f"{_IFHC_IN_OID}.{idx}")
            out_raw = _snmp_get_val(f"{_IFHC_OUT_OID}.{idx}")
            try:
                in_bytes  = int(in_raw)
                out_bytes = int(out_raw)
            except (TypeError, ValueError):
                continue

            if idx in prev:
                p_in, p_out, p_ts = prev[idx]
                dt = ts - p_ts
                if dt > 0:
                    rx_bps = max(0, (in_bytes  - p_in)  / dt)
                    tx_bps = max(0, (out_bytes - p_out) / dt)
                    # Handle counter wrap (64-bit)
                    if rx_bps > 1e12: rx_bps = 0
                    if tx_bps > 1e12: tx_bps = 0
                    total_rx_bps += rx_bps
                    total_tx_bps += tx_bps
                    valid = True

            prev[idx] = (in_bytes, out_bytes, ts)

        if valid:
            rx_mbps = round(total_rx_bps * 8 / 1_000_000, 2)
            tx_mbps = round(total_tx_bps * 8 / 1_000_000, 2)
            _uplink_buffer.append({
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                "rx":   rx_mbps,
                "tx":   tx_mbps,
            })
            # Persist to InfluxDB for historical queries (6h / 12h / 24h / 3d / 7d)
            try:
                lp = f"olt_uplink rx_mbps={rx_mbps},tx_mbps={tx_mbps} {int(ts)}"
                influx_write(lp)
            except Exception:
                pass

        elapsed = time.time() - ts
        sleep_t = max(0.1, _UPLINK_POLL_SEC - elapsed)
        time.sleep(sleep_t)


if __name__ == "__main__":
    # ThreadingHTTPServer — each request runs in its own thread so the
    # heavy InfluxDB Flux queries from /network/graph-stats don't serialize
    # and block the whole API (which used to hang the PyroGraphs UI).
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), Handler)
    server.daemon_threads = True
    print(f"[API] ONT Monitor API running on port {API_PORT} (threaded)")
    print(f"[API] GenieACS NBI: {GENIEACS_NBI}")
    # Start background MAC vendor prefetch (runs 10s after startup)
    threading.Thread(target=_prefetch_mac_vendors, daemon=True, name="mac-prefetch").start()
    # Start OLT uplink SNMP poller
    threading.Thread(target=_uplink_poller, daemon=True, name="uplink-snmp").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Stopped.")
