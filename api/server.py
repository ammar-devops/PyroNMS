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
import sys
from pathlib import Path
sys.path.insert(0, "/opt/ont-monitor/api")
import olt_helpers as olt
import re
import time
import subprocess
import sys
sys.path.insert(0, '/opt/ont-monitor/auth')
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
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    # Get latest 'state' field per ONT — one row per ONT with all tags
    flux_status = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "ont_status" and (r._field == "online" or r._field == "down_cause" or r._field == "vlan"))
  |> last()
  |> keep(columns: ["sn", "pon", "ont_id", "description", "_field", "_value"])
'''

    # Latest optical per ONT — only rx_power and temp fields
    flux_optical = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "ont_optical" and
      (r._field == "rx_power" or r._field == "temp"))
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

    status_rows  = influx_query(flux_status)
    optical_rows = influx_query(flux_optical)
    wan_rows     = influx_query(flux_wan)

    # Index optical by sn — collect all fields
    optical = {}
    for r in optical_rows:
        sn    = r.get("sn", "").strip()
        field = r.get("_field", "").strip()
        val   = r.get("_value", "").strip()
        if not sn:
            continue
        if sn not in optical:
            optical[sn] = {}
        optical[sn][field] = val

    # Index status rows by sn — collect online + down_cause
    status_map = {}
    for r in status_rows:
        sn    = r.get("sn",          "").strip()
        pon   = r.get("pon",         "").strip()
        name  = r.get("description", "").strip()
        field = r.get("_field",      "online").strip()
        val   = r.get("_value",      "").strip()
        if not sn:
            continue
        ont_id = r.get("ont_id", "").strip()
        if sn not in status_map:
            status_map[sn] = {"pon": pon, "ont_id": ont_id, "name": name, "online": "0", "down_cause": "", "vlan": ""}
        elif ont_id and not status_map[sn].get("ont_id"):
            status_map[sn]["ont_id"] = ont_id
        if field == "online":
            status_map[sn]["online"] = val
        elif field == "down_cause":
            status_map[sn]["down_cause"] = val
        elif field == "vlan":
            status_map[sn]["vlan"] = val

    wan_map = {}
    for r in wan_rows:
        sn = r.get("sn", "").strip()
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
        elif "dying" in cause or "gasp" in cause:
            detail_status = "power-failure"
        elif "los" in cause or "lob" in cause or "loss" in cause:
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
        })
    return onts


def get_ont_cached(sn):
    """Fast cached ONT row from Influx-backed table payload."""
    sn = (sn or "").strip().upper()
    if not sn:
        return None
    for row in get_all_onts():
        if (row.get("sn", "").strip().upper() == sn):
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
    if len(sn) == 16 and re.match(r'^[0-9A-Fa-f]{16}$', sn):
        try:
            vendor = bytes.fromhex(sn[:8]).decode('ascii')
            if vendor.isalnum() and vendor.isprintable():
                candidates.add((vendor + sn[8:]).upper())
        except (ValueError, UnicodeDecodeError):
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
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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
            ok, sysname = olt_helpers.test_olt_snmp(ip, snmp) if hasattr(olt_helpers, "test_olt_snmp") else olt.test_olt_snmp(ip, snmp)
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
            
            client = InfluxDBClient(url="http://localhost:8086", 
                                   token="my-super-secret-token", 
                                   org="myisp")
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
            
            client = InfluxDBClient(url="http://localhost:8086",
                                   token="my-super-secret-token",
                                   org="myisp")
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
            
            client = InfluxDBClient(url="http://localhost:8086",
                                   token="my-super-secret-token",
                                   org="myisp")
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
            import psutil, time
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

        elif parsed.path == "/olt/unregistered":
            user = require_auth(self)
            if not user: return
            olts = olt.get_olts()
            if not olts: return self.send_json(404, {"error": "No OLTs"})
            o = olts[0]
            try:
                onts = olt.get_unregistered_onts(o["ip"], o["username"], o["password"])
                return self.send_json(200, {"onts": onts, "count": len(onts)})
            except Exception as e:
                return self.send_json(500, {"error": str(e)})

        elif parsed.path == "/onts":
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
            if not sn:
                self.send_json(400, {'error': 'sn required'}); return
            try:
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
                    self.send_json(200, {'ip': '-', 'sn': sn, 'error': live.get('error', 'Live WAN check failed')}); return

                wan = (live.get('details') or {}).get('wan') or {}
                ip = (wan.get('ipv4_address') or '').strip() or '-'
                status = (wan.get('connection_status') or '').strip()
                vlan = (wan.get('network_vlan') or wan.get('manage_vlan') or '').strip()
                access_type = (wan.get('access_type') or '').strip()
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

            # 2) Fallback: ONT not in GenieACS → OLT SSH read-only view
            olts = olt.get_olts()
            if not olts:
                return self.send_json(404, {"error": f"Device {sn} not in GenieACS and no OLTs configured"})
            o = olts[0]
            try:
                ssh_data = olt.get_ont_full_info(o["ip"], o["username"], o["password"], sn,
                                                  snmp_community=o.get("snmp_community"))
            except Exception as e:
                return self.send_json(500, {"error": f"OLT SSH error: {e}"})

            if not ssh_data.get("ok"):
                return self.send_json(404, {
                    "error": f"Device {sn} not found in GenieACS or on OLT",
                    "detail": ssh_data.get("error", "")
                })

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
                    "rx_power":      ssh_data.get("rx_power"),
                    "tx_power":      ssh_data.get("tx_power"),
                    "temp":          ssh_data.get("temp"),
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
                    ok, ont_id, output = olt.provision_ont(
                        o["ip"],o["username"],o["password"],
                        sn,payload.get("slot_port","0/1"),
                        int(payload.get("port",0)),
                        payload.get("line_profile_id","8"),
                        payload.get("srv_profile_id","10"),desc,
                        payload.get("vlan_id","10"),
                        payload.get("user_vlan") or payload.get("vlan_id","10"),
                        payload.get("vas_profile","PPP-10-IPV4-IPV6"))
                if ok: return self.send_json(200, {"ok":True,"ont_id":ont_id,"method":method})
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

            import base64, os, re
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
        if parsed.path == "/ont/action":
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

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", API_PORT), Handler)
    print(f"[API] ONT Monitor API running on port {API_PORT}")
    print(f"[API] GenieACS NBI: {GENIEACS_NBI}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Stopped.")
