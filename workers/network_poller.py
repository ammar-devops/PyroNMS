#!/usr/bin/env python3
"""
network_poller.py — PyroNMS Cacti-style Network Graphs poller.

Architecture (Spine-inspired):
  • A bounded ThreadPoolExecutor (default 8 workers) polls all enabled
    devices in parallel.
  • Each device is independent — one slow/dead device cannot block others.
  • Per-device timeout, retries, and last_poll_ms tracking.
  • Device list reloaded every 60s from SQLite.

Each cycle per device:
  1. snmpget sysName/sysDescr/sysUpTime/sysObjectID  (vendor + uptime)
  2. snmpbulkwalk ifHCInOctets/ifHCOutOctets + errors + opStatus
  3. Vendor-aware resource OIDs (CPU/RAM/temp) via graph_templates
  4. Compute rates from delta against previous counters
  5. Write all measurements to InfluxDB bucket `network_monitoring` as one
     line-protocol HTTP POST
  6. Update SQLite last_poll, last_status, last_poll_ms

Runs as systemd: pyronms-network-poller.service
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error

# Allow importing siblings (workers.*)
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

import workers.network_db   as ndb
import workers.network_snmp as nsnmp
import workers.network_templates as ntemplates

# ── Config ───────────────────────────────────────────────────────────────
INFLUX_URL    = os.environ.get("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN",  "my-super-secret-token")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "myisp")
INFLUX_BUCKET = os.environ.get("NETWORK_INFLUX_BUCKET", "network_monitoring")

MAX_WORKERS   = int(os.environ.get("NET_POLLER_THREADS", "8"))
TICK_SECS     = 15      # check schedule every N seconds
RELOAD_SECS   = 60      # reload device list every N seconds

LOG_PATH = "/opt/ont-monitor/logs/network_poller.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("net-poller")

stop_event = threading.Event()

# Counter cache for rate computation: {device_id: {if_index: (ts, in_oct, out_oct)}}
_counters_lock = threading.Lock()
_counters_cache: dict[int, dict[int, tuple]] = {}

# Poller stats (for /network/poller/status)
_stats_lock = threading.Lock()
_stats = {
    "started_at":      int(time.time()),
    "max_workers":     MAX_WORKERS,
    "cycles":          0,
    "last_cycle_ms":   0,
    "last_cycle_at":   0,
    "devices_polled":  0,
    "devices_failed":  0,
    "total_retries":   0,
    "avg_poll_ms":     0,        # rolling avg of per-device poll ms
    "active_workers":  0,        # currently in-flight devices
    "degraded_count":  0,        # devices that succeeded only after retries
    "last_success_at": 0,        # last cycle that had >=1 successful poll
    "queue_depth":     0,        # devices waiting to be polled this cycle
    "failed_devices":  [],       # list of {id, name, ip, error, ts}
    "slowest_devices": [],       # list of {id, name, ms}
    "degraded_devices": [],      # list of {id, name, retries, ts}
}

# Rolling window for avg_poll_ms — keep last 200 successful polls
_poll_ms_window = []
_poll_ms_window_lock = threading.Lock()
_POLL_MS_WINDOW_MAX = 200

def _record_poll_ms(ms: int):
    with _poll_ms_window_lock:
        _poll_ms_window.append(ms)
        if len(_poll_ms_window) > _POLL_MS_WINDOW_MAX:
            _poll_ms_window.pop(0)
        avg = sum(_poll_ms_window) // len(_poll_ms_window)
    with _stats_lock:
        _stats["avg_poll_ms"] = avg

# ── Retry helper (Spine-style exponential backoff with jitter) ──────────
MAX_SNMP_RETRIES = int(os.environ.get("NET_POLLER_RETRIES", "3"))
RETRY_BASE_MS    = int(os.environ.get("NET_POLLER_RETRY_BASE_MS", "500"))

def _snmp_retry(fn_name: str, fn, *args, **kwargs):
    """
    Call an SNMP function with exponential backoff retry.
    Treats empty/None result as a failure to retry.
    Returns (result, retries_used). Caller can detect degraded state when
    retries_used > 0 even on success.
    """
    import random
    last_result = None
    retries_used = 0
    for attempt in range(MAX_SNMP_RETRIES):
        try:
            result = fn(*args, **kwargs)
            if result:
                if attempt > 0:
                    with _stats_lock:
                        _stats["total_retries"] += attempt
                    log.info(f"{fn_name} succeeded on attempt {attempt+1} "
                             f"(after {attempt} retries)")
                retries_used = attempt
                return result, retries_used
            last_result = result
        except Exception as e:
            last_result = None
            log.debug(f"{fn_name} attempt {attempt+1} raised: {e}")
        if attempt < MAX_SNMP_RETRIES - 1:
            # Exponential backoff with jitter: base * 2^attempt + random(0..base)
            delay_ms = RETRY_BASE_MS * (2 ** attempt) + random.randint(0, RETRY_BASE_MS)
            time.sleep(delay_ms / 1000.0)
            retries_used = attempt + 1
    # All retries exhausted
    with _stats_lock:
        _stats["total_retries"] += MAX_SNMP_RETRIES - 1
    return last_result, retries_used


def _stats_set(**kv):
    with _stats_lock:
        _stats.update(kv)


def get_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


# ── InfluxDB write (line protocol) ───────────────────────────────────────
def _esc_tag(s: str) -> str:
    """Influx line-protocol tag escaping."""
    return (str(s).replace("\\", "\\\\")
                   .replace(",", "\\,")
                   .replace("=", "\\=")
                   .replace(" ", "\\ "))


def _esc_field_str(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _build_line(measurement: str, tags: dict, fields: dict,
                ts_ns: int = None) -> str:
    tag_str = ",".join(f"{_esc_tag(k)}={_esc_tag(v)}"
                       for k, v in tags.items() if v not in (None, ""))
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, bool):
            parts.append(f"{k}={'true' if v else 'false'}")
        elif isinstance(v, (int,)):
            parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            parts.append(f"{k}={v}")
        else:
            parts.append(f'{k}="{_esc_field_str(v)}"')
    field_str = ",".join(parts)
    if not field_str:
        return ""
    line = f"{measurement},{tag_str} {field_str}"
    if ts_ns is not None:
        line += f" {ts_ns}"
    return line


def write_influx(lines: list[str]) -> bool:
    if not lines:
        return True
    payload = "\n".join(l for l in lines if l).encode("utf-8")
    url = f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=ns"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type":  "text/plain; charset=utf-8",
        })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        body = ""
        try:    body = e.read().decode("utf-8", "ignore")[:300]
        except: pass
        log.warning(f"influx write http {e.code}: {body}")
        return False
    except Exception as e:
        log.warning(f"influx write error: {e}")
        return False


# ── Vendor resource probes ───────────────────────────────────────────────
def _safe_int(s, default=0):
    try:    return int(str(s).split()[0])
    except: return default


def _safe_float(s, default=None):
    try:    return float(str(s).split()[0])
    except: return default


def probe_resources(dev: dict) -> dict:
    """
    Returns {cpu_pct, mem_used, mem_total, mem_pct, uptime_sec, temp_c,
             pppoe_sessions?}.
    Vendor-aware; falls back to host-resources MIB when possible.
    For MikroTik, optionally polls PPPoE session count via RouterOS API
    using credentials stored as JSON in the device notes field:
      {"ros_user": "admin", "ros_pass": "secret", "ros_port": 8728}
    """
    out = {"cpu_pct": None, "mem_used": None, "mem_total": None,
           "mem_pct": None, "uptime_sec": None, "temp_c": None}

    vendor = (dev.get("vendor") or "generic").lower()

    # Uptime — universal
    r = nsnmp.snmp_get(dev, nsnmp.OID_SYS_UPTIME)
    if r:
        # sysUpTime is TimeTicks (centiseconds)
        v = list(r.values())[0]
        ticks = _safe_int(v)
        out["uptime_sec"] = ticks // 100 if ticks else None

    if vendor == "mikrotik":
        # CPU + try MTXR memory + temperature (older RouterOS only)
        r = nsnmp.snmp_get(dev,
            "1.3.6.1.2.1.25.3.3.1.2.1",       # hrProcessorLoad
            "1.3.6.1.4.1.14988.1.1.3.5.0",    # mtxrTotalRam (often 0 on newer ROS)
            "1.3.6.1.4.1.14988.1.1.3.6.0",    # mtxrFreeRam (often 0 on newer ROS)
            "1.3.6.1.4.1.14988.1.1.3.10.0")   # mtxrTemperature (often missing)
        if r:
            out["cpu_pct"] = _safe_float(r.get("1.3.6.1.2.1.25.3.3.1.2.1"))
            mt = _safe_int(r.get("1.3.6.1.4.1.14988.1.1.3.5.0"))
            mf = _safe_int(r.get("1.3.6.1.4.1.14988.1.1.3.6.0"))
            if mt > 0:
                out["mem_total"] = mt
                out["mem_used"]  = max(0, mt - mf)
                out["mem_pct"]   = round((mt - mf) * 100.0 / mt, 1)
            t = _safe_float(r.get("1.3.6.1.4.1.14988.1.1.3.10.0"))
            if t is not None and t > 0:
                out["temp_c"] = t / 10.0   # ×10 in tenths-degree

        # Fallback: Host Resources MIB (hrStorageTable) for memory.
        # New RouterOS (v7+) drops the MTXR memory OIDs but always serves
        # Host Resources. Walk hrStorageType (.2) to find the entry whose
        # value equals hrStorageRam (1.3.6.1.2.1.25.2.1.2), then read size
        # (.5) and used (.6) at that index. Result is in allocation units
        # (×1024 bytes per allocation_unit which is .4).
        if out["mem_pct"] is None:
            try:
                types_map = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.2.1.25.2.3.1.2", max_rep=15)
                ram_idx = None
                for idx, val in types_map.items():
                    v = (val or "").strip()
                    # Match against hrStorageRam OID in any of the common forms
                    if v.endswith("25.2.1.2") or v == "1.3.6.1.2.1.25.2.1.2":
                        ram_idx = idx
                        break
                if ram_idx:
                    size = nsnmp.snmp_get(dev,
                        f"1.3.6.1.2.1.25.2.3.1.5.{ram_idx}",
                        f"1.3.6.1.2.1.25.2.3.1.6.{ram_idx}",
                        f"1.3.6.1.2.1.25.2.3.1.4.{ram_idx}")
                    if size:
                        units = _safe_int(size.get(f"1.3.6.1.2.1.25.2.3.1.5.{ram_idx}"))
                        used  = _safe_int(size.get(f"1.3.6.1.2.1.25.2.3.1.6.{ram_idx}"))
                        au    = _safe_int(size.get(f"1.3.6.1.2.1.25.2.3.1.4.{ram_idx}"), default=1024)
                        if units > 0:
                            total_bytes = units * au
                            used_bytes  = used  * au
                            out["mem_total"] = total_bytes
                            out["mem_used"]  = used_bytes
                            out["mem_pct"]   = round(used_bytes * 100.0 / total_bytes, 1)
            except Exception as _e:
                log.debug(f"hrStorage memory probe failed for {dev.get('ip')}: {_e}")
        # Optional: PPPoE session count via RouterOS API
        # Credentials from notes JSON: {"ros_user":"admin","ros_pass":"..."}
        try:
            notes_raw = dev.get("notes") or ""
            if notes_raw and notes_raw.strip().startswith("{"):
                notes = json.loads(notes_raw)
                ros_user = notes.get("ros_user") or ""
                ros_pass = notes.get("ros_pass") or ""
                ros_port = int(notes.get("ros_port", 8728))
                if ros_user and ros_pass:
                    import librouteros
                    conn = librouteros.connect(
                        dev["ip"], ros_user, ros_pass,
                        port=ros_port, timeout=5)
                    sessions = list(conn("/ppp/active/print"))
                    conn.close()
                    out["pppoe_sessions"] = len(sessions)
        except Exception as _e:
            log.debug(f"PPPoE session poll skipped for {dev.get('ip')}: {_e}")

    elif vendor == "cisco":
        # cpmCPUTotal5minRev
        r = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.9.9.109.1.1.1.1.5", max_rep=5)
        if r:
            vals = [_safe_float(v) for v in r.values() if v]
            vals = [v for v in vals if v is not None]
            if vals:
                out["cpu_pct"] = sum(vals) / len(vals)
        used = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.9.9.48.1.1.1.5", max_rep=5)
        free = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.9.9.48.1.1.1.6", max_rep=5)
        if used:
            u = sum(_safe_int(v) for v in used.values())
            f = sum(_safe_int(v) for v in free.values())
            tot = u + f
            if tot:
                out["mem_total"] = tot
                out["mem_used"]  = u
                out["mem_pct"]   = round(u * 100.0 / tot, 1)

    elif vendor == "juniper":
        r = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.2636.3.1.13.1.8", max_rep=10)
        if r:
            vals = [_safe_float(v) for v in r.values() if v]
            vals = [v for v in vals if v is not None]
            if vals: out["cpu_pct"] = max(vals)
        r2 = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.2636.3.1.13.1.11", max_rep=10)  # jnxOperatingBuffer
        if r2:
            vals = [_safe_float(v) for v in r2.values() if v]
            vals = [v for v in vals if v is not None]
            if vals: out["mem_pct"] = max(vals)
        r3 = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.2636.3.1.13.1.7", max_rep=10)
        if r3:
            temps = [_safe_float(v) for v in r3.values() if v]
            temps = [t for t in temps if t and t > 0]
            if temps: out["temp_c"] = max(temps)

    elif vendor == "huawei":
        r = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.2011.6.3.4.1.2", max_rep=10)
        if r:
            vals = [_safe_float(v) for v in r.values() if v]
            vals = [v for v in vals if v is not None]
            if vals: out["cpu_pct"] = sum(vals) / len(vals)
        r2 = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.4.1.2011.6.3.5.1.1.2", max_rep=10)
        if r2:
            vals = [_safe_float(v) for v in r2.values() if v]
            vals = [v for v in vals if v is not None]
            if vals: out["mem_pct"] = max(vals)

    else:
        # Generic — try hrProcessorLoad and hrStorageTable
        cpus = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.2.1.25.3.3.1.2", max_rep=10)
        if cpus:
            vals = [_safe_float(v) for v in cpus.values() if v]
            vals = [v for v in vals if v is not None]
            if vals: out["cpu_pct"] = sum(vals) / len(vals)
        # Storage rows: find first row where type=hrStorageRam (.1.3.6.1.2.1.25.2.1.2)
        descrs = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.2.1.25.2.3.1.3", max_rep=15)
        sizes  = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.2.1.25.2.3.1.5", max_rep=15)
        used   = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.2.1.25.2.3.1.6", max_rep=15)
        units  = nsnmp.snmp_bulk_walk(dev, "1.3.6.1.2.1.25.2.3.1.4", max_rep=15)
        for idx, desc in descrs.items():
            if "ram" in desc.lower() or "physical memory" in desc.lower():
                sz = _safe_int(sizes.get(idx)) * max(_safe_int(units.get(idx)), 1)
                us = _safe_int(used.get(idx))  * max(_safe_int(units.get(idx)), 1)
                if sz:
                    out["mem_total"] = sz
                    out["mem_used"]  = us
                    out["mem_pct"]   = round(us * 100.0 / sz, 1)
                break

    return out


# ── Per-device polling ───────────────────────────────────────────────────
def poll_one_device(dev: dict) -> dict:
    """
    Returns a dict {id, name, ms, ok, error?} for stats.
    Writes all data to InfluxDB and updates SQLite status.
    """
    dev_id   = dev["id"]
    dev_name = dev.get("name", "")
    dev_ip   = dev.get("ip", "")
    start    = time.time()
    lines    = []
    ts_ns    = int(start * 1_000_000_000)
    base_tags = {
        "device_id":   str(dev_id),
        "device_name": dev_name,
        "vendor":      dev.get("vendor") or "generic",
        "location":    dev.get("location") or "",
    }

    total_retries = 0  # accumulated retries this poll cycle
    try:
        # 1) System info + uptime (with retry+backoff)
        sysinfo, r1 = _snmp_retry("sysinfo", nsnmp.snmp_get, dev,
            nsnmp.OID_SYS_DESCR, nsnmp.OID_SYS_NAME,
            nsnmp.OID_SYS_OBJECT_ID, nsnmp.OID_SYS_UPTIME)
        total_retries += r1
        if not sysinfo:
            ms = int((time.time() - start) * 1000)
            ndb.set_device_status(dev_id, "offline", ms)
            log.warning(f"[{dev_name}] OFFLINE after {MAX_SNMP_RETRIES} retries "
                        f"({dev_ip}) — sysinfo SNMP timeout")
            return {"id": dev_id, "name": dev_name, "ms": ms,
                    "ok": False, "error": f"SNMP timeout (after {MAX_SNMP_RETRIES} retries)",
                    "retries": r1}

        sys_descr  = sysinfo.get(nsnmp.OID_SYS_DESCR, "")
        sys_name   = sysinfo.get(nsnmp.OID_SYS_NAME, "")
        sys_obj_id = sysinfo.get(nsnmp.OID_SYS_OBJECT_ID, "")

        # 2) Resource probe
        res = probe_resources(dev)
        if any(v is not None for v in res.values()):
            fields = {}
            if res.get("cpu_pct")       is not None: fields["cpu_pct"]       = float(res["cpu_pct"])
            if res.get("mem_used")      is not None: fields["mem_used"]      = int(res["mem_used"])
            if res.get("mem_total")     is not None: fields["mem_total"]     = int(res["mem_total"])
            if res.get("mem_pct")       is not None: fields["mem_pct"]       = float(res["mem_pct"])
            if res.get("uptime_sec")    is not None: fields["uptime_sec"]    = int(res["uptime_sec"])
            if res.get("temp_c")        is not None: fields["temp_c"]        = float(res["temp_c"])
            if res.get("pppoe_sessions") is not None: fields["pppoe_sessions"] = int(res["pppoe_sessions"])
            l = _build_line("net_resource", base_tags, fields, ts_ns)
            if l: lines.append(l)

        # 3) Interface counters (only if any interface is polling_enabled, OR
        #    if no interfaces yet in DB — initial pre-discovery state)
        ifaces_db = ndb.get_interfaces(dev_id)
        enabled_idx = {i["if_index"] for i in ifaces_db if i.get("polling_enabled", 1)}
        poll_all_ifaces = (not ifaces_db)   # never discovered → poll everything

        # Interface counters — NO retry on empty.
        # An OLT with all interfaces idle legitimately returns 0 for
        # discards/errors. The retry-on-empty logic was triggering 3 retries
        # × 7 bulkwalks on devices like HP-OLT, costing 5-10 extra seconds
        # per cycle on local-network devices. fetch_interface_counters
        # internally returns {} only if ifHCInOctets/ifHCOutOctets walks
        # both fail — that's the only real failure mode and we don't need
        # the retry layer to catch it (the bulkwalk subprocess already
        # has -r1 built in).
        r2 = 0
        try:
            counters = nsnmp.fetch_interface_counters(dev) or {}
        except Exception as _e:
            log.debug(f"fetch_interface_counters {dev_name}: {_e}")
            counters = {}

        # Build lookup map for tags from SQLite (alias, type, vlan)
        iface_meta = {i["if_index"]: i for i in ifaces_db}

        # Previous counters
        with _counters_lock:
            prev = _counters_cache.get(dev_id, {})
            new_cache = {}

        for idx, c in counters.items():
            if not poll_all_ifaces and idx not in enabled_idx:
                continue
            meta = iface_meta.get(idx, {})
            iname = meta.get("if_name") or meta.get("if_descr") or str(idx)

            prev_row = prev.get(idx)
            new_cache[idx] = (start, c["in"], c["out"])

            rx_bps = 0.0; tx_bps = 0.0
            if prev_row:
                ts_prev, in_prev, out_prev = prev_row
                dt = start - ts_prev
                if dt > 0:
                    rx_delta = c["in"]  - in_prev
                    tx_delta = c["out"] - out_prev
                    # Handle 64-bit counter wrap (negative delta)
                    if rx_delta < 0: rx_delta += 2**64
                    if tx_delta < 0: tx_delta += 2**64
                    rx_bps = (rx_delta * 8) / dt
                    tx_bps = (tx_delta * 8) / dt

            tags = {
                **base_tags,
                "interface": iname,
                "if_index":  str(idx),
                "if_type":   str(meta.get("if_type", 0)),
                "vlan_id":   str(meta.get("vlan_id", 0)),
            }
            fields = {
                "rx_bps":     float(rx_bps),
                "tx_bps":     float(tx_bps),
                "rx_errors":  int(c["in_err"]),
                "tx_errors":  int(c["out_err"]),
                "rx_drops":   int(c["in_disc"]),
                "tx_drops":   int(c["out_disc"]),
                "oper_status": int(c["oper"]),
            }
            l = _build_line("net_iface", tags, fields, ts_ns)
            if l: lines.append(l)

        with _counters_lock:
            _counters_cache[dev_id] = new_cache

        # 4) Per-device poll health line (Cacti-style poller telemetry)
        ms = int((time.time() - start) * 1000)
        health_fields = {
            "poll_duration_ms": int(ms),
            "lines_written":    int(len(lines)),
            "interfaces":       int(len(counters)),
            "retries":          int(total_retries),
        }
        hl = _build_line("net_poll_health", base_tags, health_fields, ts_ns)
        if hl: lines.append(hl)

        # 5) Write to InfluxDB
        ok = write_influx(lines)

        # Status logic: online | degraded (had retries) | offline (write failed)
        if not ok:
            status = "offline"
        elif total_retries > 0:
            status = "degraded"
            with _stats_lock:
                _stats["degraded_count"] = _stats.get("degraded_count", 0) + 1
                dl = _stats.setdefault("degraded_devices", [])
                dl.insert(0, {"id": dev_id, "name": dev_name,
                              "retries": total_retries, "ts": int(time.time())})
                _stats["degraded_devices"] = dl[:20]
        else:
            status = "online"

        ndb.set_device_status(
            dev_id, status, ms,
            sys_name=sys_name, sys_descr=sys_descr, sys_object_id=sys_obj_id)

        if ok:
            _record_poll_ms(ms)
            with _stats_lock:
                _stats["last_success_at"] = int(time.time())

        return {"id": dev_id, "name": dev_name, "ms": ms, "ok": ok,
                "lines": len(lines), "retries": total_retries,
                "status": status}

    except Exception as e:
        ms = int((time.time() - start) * 1000)
        try:    ndb.set_device_status(dev_id, "offline", ms)
        except: pass
        log.exception(f"poll_one_device {dev_name} {dev_ip}: {e}")
        return {"id": dev_id, "name": dev_name, "ms": ms,
                "ok": False, "error": str(e)}


# ── Main loop ────────────────────────────────────────────────────────────
def _handle_signal(signum, frame):
    log.info(f"Received signal {signum}, shutting down")
    stop_event.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    log.info(f"PyroNMS Network Poller starting — bucket={INFLUX_BUCKET}, "
             f"workers={MAX_WORKERS}")

    # Seed builtin templates idempotently
    try:
        inserted = ntemplates.seed_builtins()
        if inserted:
            log.info(f"Seeded {inserted} built-in graph templates")
    except Exception as e:
        log.warning(f"Template seeding failed: {e}")

    last_reload = 0
    devices: list[dict] = []
    next_due: dict[int, float] = {}     # device_id → next poll time

    with ThreadPoolExecutor(max_workers=MAX_WORKERS,
                            thread_name_prefix="netpoll") as pool:
        while not stop_event.is_set():
            now = time.time()

            # Reload device list every RELOAD_SECS
            if now - last_reload >= RELOAD_SECS:
                try:
                    devices = ndb.get_enabled_devices_with_creds()
                    last_reload = now
                    log.debug(f"Reloaded {len(devices)} enabled devices")
                except Exception as e:
                    log.error(f"Failed to reload devices: {e}")

            # Schedule devices whose poll is due
            cycle_start = now
            futures = []
            due_devices = []
            for d in devices:
                interval = int(d.get("polling_interval") or 60)
                due = next_due.get(d["id"], 0)
                if now >= due:
                    futures.append(pool.submit(poll_one_device, d))
                    due_devices.append(d)
                    next_due[d["id"]] = now + interval

            if futures:
                ok_count = 0
                fail_count = 0
                slow = []
                failed = []
                # Track queue depth + active workers for /network/poller/status
                with _stats_lock:
                    _stats["queue_depth"]    = len(futures)
                    _stats["active_workers"] = min(MAX_WORKERS, len(futures))
                for f in as_completed(futures, timeout=120):
                    try:
                        r = f.result()
                        if r.get("ok"):
                            ok_count += 1
                        else:
                            fail_count += 1
                            failed.append({
                                "id":   r.get("id"),
                                "name": r.get("name"),
                                "error": r.get("error", "unknown"),
                                "ts":   int(time.time()),
                            })
                        slow.append({"id": r.get("id"),
                                     "name": r.get("name"),
                                     "ms":   r.get("ms", 0)})
                    except Exception as e:
                        fail_count += 1
                        log.warning(f"future error: {e}")

                slow.sort(key=lambda x: x.get("ms", 0), reverse=True)
                cycle_ms = int((time.time() - cycle_start) * 1000)

                _stats_set(
                    cycles=_stats["cycles"] + 1,
                    last_cycle_ms=cycle_ms,
                    last_cycle_at=int(time.time()),
                    devices_polled=ok_count,
                    devices_failed=fail_count,
                    failed_devices=failed[-50:],   # keep last 50
                    slowest_devices=slow[:10],
                    queue_depth=0,
                    active_workers=0,
                )

                if ok_count or fail_count:
                    log.info(f"cycle: polled={ok_count} failed={fail_count} "
                             f"duration={cycle_ms}ms")

                # Write status file for /network/poller/status endpoint
                try:
                    with _stats_lock:
                        snap = dict(_stats)
                    with open("/tmp/net_poller_status.json", "w") as _sf:
                        json.dump(snap, _sf)
                except Exception:
                    pass

            # Sleep until next tick (15s) or stop signal
            stop_event.wait(TICK_SECS)

    log.info("PyroNMS Network Poller stopped")


if __name__ == "__main__":
    main()
