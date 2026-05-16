#!/usr/bin/env python3
"""
mikrotik_poller.py — PyroNMS MikroTik monitoring poller.

Polls all enabled MikroTik devices every 60 seconds via:
  - SNMP v2c  : CPU, RAM, uptime, temperature, interface traffic/errors
  - RouterOS API (librouteros) : PPPoE/Radius active sessions, system resource

InfluxDB measurements written:
  mikrotik_resource   — CPU, RAM, uptime, temperature (per device, 60s)
  mikrotik_iface      — per-interface traffic + errors (60s)
  mikrotik_ppp        — active PPP count (per device, 60s)
  mikrotik_ppp_session— per active PPP user snapshot (60s)

Run as: pyronms-mikrotik.service
"""

import sys
import os
import time
import threading
import logging
import subprocess
import re

# Allow importing siblings
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

import workers.mikrotik_db as mdb

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_PATH = "/opt/ont-monitor/logs/mikrotik_poller.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mikrotik")

# ── InfluxDB config (mirrors api/server.py) ───────────────────────────────────
INFLUX_URL    = os.environ.get("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN",  "my-super-secret-token")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "myisp")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "olt_monitoring")

# ── Poll intervals ────────────────────────────────────────────────────────────
INTERVAL_POLL   = 60    # seconds between full device poll cycles
INTERVAL_RELOAD = 300   # seconds between device-list reloads from SQLite
TIMEOUT_SNMP    = 5     # per SNMP call
TIMEOUT_API     = 10    # per RouterOS API connection

# ── SNMP OIDs ─────────────────────────────────────────────────────────────────
OID_SYS_UPTIME     = "1.3.6.1.2.1.1.3.0"
OID_SYS_DESCR      = "1.3.6.1.2.1.1.1.0"
OID_CPU_LOAD       = "1.3.6.1.2.1.25.3.3.1.2.1"   # hrProcessorLoad (first CPU)
OID_MEM_TOTAL      = "1.3.6.1.4.1.14988.1.1.3.5.0" # mtxrTotalMemory (bytes)
OID_MEM_FREE       = "1.3.6.1.4.1.14988.1.1.3.6.0" # mtxrFreeMemory  (bytes)
OID_TEMPERATURE    = "1.3.6.1.4.1.14988.1.1.3.10.0"# mtxrTemperature (°C ×10)
OID_IF_DESCR       = "1.3.6.1.2.1.2.2.1.2"         # ifDescr (walk)
OID_IF_OPER        = "1.3.6.1.2.1.2.2.1.8"         # ifOperStatus (walk)
OID_IF_HC_IN       = "1.3.6.1.2.1.31.1.1.1.6"      # ifHCInOctets  (walk)
OID_IF_HC_OUT      = "1.3.6.1.2.1.31.1.1.1.10"     # ifHCOutOctets (walk)
OID_IF_IN_ERR      = "1.3.6.1.2.1.2.2.1.14"        # ifInErrors  (walk)
OID_IF_OUT_ERR     = "1.3.6.1.2.1.2.2.1.20"        # ifOutErrors (walk)
OID_IF_IN_DISC     = "1.3.6.1.2.1.2.2.1.13"        # ifInDiscards  (walk)
OID_IF_OUT_DISC    = "1.3.6.1.2.1.2.2.1.19"        # ifOutDiscards (walk)
OID_IF_IN_PKTS     = "1.3.6.1.2.1.2.2.1.11"        # ifInUcastPkts (walk)
OID_IF_OUT_PKTS    = "1.3.6.1.2.1.2.2.1.17"        # ifOutUcastPkts (walk)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _snmp_get(host: str, community: str, *oids) -> dict:
    """Run snmpget for one or more OIDs. Returns {oid_suffix: value_str}."""
    cmd = ["snmpget", "-v2c", f"-c{community}", "-Oqn",
           f"-t{TIMEOUT_SNMP}", "-r1", host] + list(oids)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SNMP + 2)
        result = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or "No Such" in line or "Timeout" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                result[parts[0]] = parts[1].strip().strip('"')
        return result
    except Exception as e:
        log.debug(f"snmpget {host} error: {e}")
        return {}


def _snmp_walk(host: str, community: str, base_oid: str) -> dict:
    """Run snmpwalk for a base OID. Returns {last_index: value_str}."""
    cmd = ["snmpwalk", "-v2c", f"-c{community}", "-Oqn",
           f"-t{TIMEOUT_SNMP}", "-r1", host, base_oid]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SNMP + 5)
        result = {}
        prefix = f".{base_oid}." if not base_oid.startswith(".") else f"{base_oid}."
        # Normalise prefix
        if not prefix.startswith("."):
            prefix = "." + prefix
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or "No Such" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            oid_full = parts[0]
            val = parts[1].strip().strip('"')
            # Extract the index (last component after base OID)
            if oid_full.startswith(prefix):
                idx = oid_full[len(prefix):]
                result[idx] = val
            else:
                # fallback: use last numeric segment
                idx = oid_full.rsplit(".", 1)[-1]
                result[idx] = val
        return result
    except Exception as e:
        log.debug(f"snmpwalk {host} {base_oid} error: {e}")
        return {}


def _iface_type(name: str) -> str:
    """Classify MikroTik interface by name prefix."""
    n = name.lower()
    if n.startswith("ether"):  return "ether"
    if n.startswith("vlan"):   return "vlan"
    if n.startswith("bridge"): return "bridge"
    if n.startswith("wlan"):   return "wlan"
    if n.startswith("pppoe"):  return "pppoe"
    if n.startswith("sfp"):    return "sfp"
    if n.startswith("bond"):   return "bond"
    if n.startswith("lo"):     return "loopback"
    return "other"


def _parse_uptime_ros(uptime_str: str) -> int:
    """Convert RouterOS uptime string '3d2h15m30s' to seconds."""
    s = 0
    for num, unit in re.findall(r"(\d+)([wdhms])", uptime_str):
        n = int(num)
        if unit == "w": s += n * 604800
        elif unit == "d": s += n * 86400
        elif unit == "h": s += n * 3600
        elif unit == "m": s += n * 60
        elif unit == "s": s += n
    return s


def _influx_write_lines(lines: list[str]):
    """Write InfluxDB line protocol points via HTTP."""
    if not lines:
        return
    import urllib.request, urllib.error
    url = f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
    body = "\n".join(lines).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    req.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                log.warning(f"InfluxDB write HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        log.warning(f"InfluxDB write error {e.code}: {e.read()[:200]}")
    except Exception as e:
        log.warning(f"InfluxDB write failed: {e}")


def _escape_tag(v: str) -> str:
    """Escape InfluxDB tag value (space, comma, equals)."""
    return str(v).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def _escape_field_str(v: str) -> str:
    """Wrap string field value in quotes, escape inner quotes."""
    return '"' + str(v).replace('"', '\\"') + '"'


# ── Per-device poller class ───────────────────────────────────────────────────

class MikroTikDevicePoller:
    """Polls one MikroTik device. Runs in its own daemon thread."""

    def __init__(self, device: dict):
        self.dev = device
        self.id  = device["id"]
        self.ip  = device["ip"]
        self.name = device["name"]
        self.location = device.get("location", "")
        self.community = device.get("snmp_community", "public")
        self.snmp_enabled = bool(device.get("snmp_enabled", 1))
        self.api_enabled  = bool(device.get("api_enabled", 1))
        self.api_port = int(device.get("api_port", 8728))
        self.username = device.get("username", "admin")
        self.password = device.get("password", "")
        # Previous byte counters for rate calculation {ifIndex: (in_bytes, out_bytes, ts)}
        self._prev: dict = {}

    # ── Tag string used in all line-protocol points ──
    @property
    def _tags(self) -> str:
        return (
            f"device_id={self.id},"
            f"device_name={_escape_tag(self.name)},"
            f"ip={_escape_tag(self.ip)},"
            f"location={_escape_tag(self.location)}"
        )

    # ── SNMP resource poll ──
    def poll_resource_snmp(self) -> dict | None:
        vals = _snmp_get(
            self.ip, self.community,
            f".{OID_SYS_UPTIME}",
            f".{OID_CPU_LOAD}",
            f".{OID_MEM_TOTAL}",
            f".{OID_MEM_FREE}",
            f".{OID_TEMPERATURE}",
        )
        if not vals:
            return None

        result = {}

        # Uptime — sysUpTime is in centiseconds
        uptime_raw = vals.get(f".{OID_SYS_UPTIME}", "")
        # SNMP output: "Timeticks: (12345600) 1:26:00.00" or just integer
        m = re.search(r"\((\d+)\)", uptime_raw)
        if m:
            result["uptime_sec"] = int(m.group(1)) // 100
        else:
            try:
                result["uptime_sec"] = int(uptime_raw) // 100
            except Exception:
                pass

        # CPU
        cpu_raw = vals.get(f".{OID_CPU_LOAD}", "")
        try:
            result["cpu_load"] = float(cpu_raw)
        except Exception:
            pass

        # Memory
        try:
            mem_total = int(vals.get(f".{OID_MEM_TOTAL}", 0))
            mem_free  = int(vals.get(f".{OID_MEM_FREE}", 0))
            result["mem_total_bytes"] = mem_total
            result["mem_free_bytes"]  = mem_free
            if mem_total > 0:
                result["mem_used_pct"] = round(100.0 * (mem_total - mem_free) / mem_total, 1)
        except Exception:
            pass

        # Temperature (optional — not all boards have it)
        temp_raw = vals.get(f".{OID_TEMPERATURE}", "")
        try:
            t_val = float(temp_raw)
            if t_val > 0:
                result["temperature_c"] = round(t_val / 10.0, 1)
        except Exception:
            pass

        return result or None

    # ── SNMP resource from RouterOS API (fallback) ──
    def poll_resource_api(self) -> dict | None:
        try:
            import librouteros
            api = librouteros.connect(
                self.ip, self.username, self.password,
                port=self.api_port, timeout=TIMEOUT_API
            )
            res = list(api("/system/resource/print"))
            api.close()
            if not res:
                return None
            r = res[0]
            mem_total = int(r.get("total-memory", 0))
            mem_free  = int(r.get("free-memory", 0))
            uptime_str = r.get("uptime", "")
            return {
                "cpu_load":       float(r.get("cpu-load", 0)),
                "mem_total_bytes": mem_total,
                "mem_free_bytes":  mem_free,
                "mem_used_pct":   round(100.0 * (mem_total - mem_free) / mem_total, 1)
                                   if mem_total > 0 else 0.0,
                "uptime_sec":     _parse_uptime_ros(uptime_str),
                "_routeros_ver":  r.get("version", ""),
            }
        except Exception as e:
            log.debug(f"[{self.name}] API resource error: {e}")
            return None

    # ── SNMP interface poll ──
    def poll_interfaces_snmp(self, ts: int) -> list[str]:
        """Returns InfluxDB line-protocol strings for each interface."""
        descr_map  = _snmp_walk(self.ip, self.community, OID_IF_DESCR)
        oper_map   = _snmp_walk(self.ip, self.community, OID_IF_OPER)
        in_map     = _snmp_walk(self.ip, self.community, OID_IF_HC_IN)
        out_map    = _snmp_walk(self.ip, self.community, OID_IF_HC_OUT)
        in_err_map = _snmp_walk(self.ip, self.community, OID_IF_IN_ERR)
        out_err_map= _snmp_walk(self.ip, self.community, OID_IF_OUT_ERR)
        in_disc_map= _snmp_walk(self.ip, self.community, OID_IF_IN_DISC)
        out_disc_map= _snmp_walk(self.ip, self.community, OID_IF_OUT_DISC)
        in_pkt_map = _snmp_walk(self.ip, self.community, OID_IF_IN_PKTS)
        out_pkt_map= _snmp_walk(self.ip, self.community, OID_IF_OUT_PKTS)

        if not descr_map:
            return []

        lines = []
        for idx, iface_name in descr_map.items():
            iface_name = iface_name.strip().strip('"')
            if not iface_name:
                continue

            oper_status = 1 if oper_map.get(idx, "2") == "1" else 0
            itype = _iface_type(iface_name)

            # Byte rate calculation
            rx_bps = tx_bps = 0.0
            try:
                in_bytes  = int(in_map.get(idx, 0))
                out_bytes = int(out_map.get(idx, 0))
                prev = self._prev.get(idx)
                if prev:
                    prev_in, prev_out, prev_ts = prev
                    elapsed = ts - prev_ts
                    if elapsed > 0:
                        rx_bps = max(0.0, (in_bytes  - prev_in)  * 8 / elapsed)
                        tx_bps = max(0.0, (out_bytes - prev_out) * 8 / elapsed)
                self._prev[idx] = (in_bytes, out_bytes, ts)
            except Exception:
                pass

            # Counters
            def _cnt(m, k):
                try: return int(m.get(k, 0))
                except: return 0

            rx_pkts    = _cnt(in_pkt_map,   idx)
            tx_pkts    = _cnt(out_pkt_map,  idx)
            rx_errors  = _cnt(in_err_map,   idx)
            tx_errors  = _cnt(out_err_map,  idx)
            rx_drops   = _cnt(in_disc_map,  idx)
            tx_drops   = _cnt(out_disc_map, idx)

            tags = (
                f"{self._tags},"
                f"interface={_escape_tag(iface_name)},"
                f"iface_type={itype}"
            )
            fields = (
                f"rx_bps={rx_bps},"
                f"tx_bps={tx_bps},"
                f"rx_pkts={rx_pkts}i,"
                f"tx_pkts={tx_pkts}i,"
                f"rx_errors={rx_errors}i,"
                f"tx_errors={tx_errors}i,"
                f"rx_drops={rx_drops}i,"
                f"tx_drops={tx_drops}i,"
                f"oper_status={oper_status}i"
            )
            lines.append(f"mikrotik_iface,{tags} {fields} {ts}")

        return lines

    # ── RouterOS API PPPoE sessions poll ──
    def poll_ppp_api(self, ts: int) -> list[str]:
        """Returns InfluxDB line-protocol strings for PPP summary + per-session."""
        try:
            import librouteros
            api = librouteros.connect(
                self.ip, self.username, self.password,
                port=self.api_port, timeout=TIMEOUT_API
            )
            sessions = list(api("/ppp/active/print"))
            api.close()
        except Exception as e:
            log.debug(f"[{self.name}] API PPP error: {e}")
            return []

        active_count = len(sessions)
        radius_count = sum(1 for s in sessions if s.get("radius") == "true")

        lines = []

        # Summary point
        lines.append(
            f"mikrotik_ppp,{self._tags} "
            f"active_ppp_count={active_count}i,"
            f"radius_ppp_count={radius_count}i "
            f"{ts}"
        )

        # Per-session points
        for s in sessions:
            username   = _escape_tag(s.get("name", "unknown"))
            service    = _escape_tag(s.get("service", ""))
            profile    = _escape_tag(s.get("caller-id", ""))    # profile may be absent
            radius_tag = "true" if s.get("radius") == "true" else "false"

            uptime_sec = _parse_uptime_ros(s.get("uptime", ""))
            caller_id  = _escape_field_str(s.get("caller-id", ""))
            address    = _escape_field_str(s.get("address", ""))

            tags = (
                f"{self._tags},"
                f"username={username},"
                f"service={service},"
                f"radius={radius_tag}"
            )
            fields = (
                f"uptime_sec={uptime_sec}i,"
                f"caller_id={caller_id},"
                f"address={address}"
            )
            lines.append(f"mikrotik_ppp_session,{tags} {fields} {ts}")

        return lines

    # ── Single full poll cycle ──
    def run_once(self):
        ts = int(time.time())
        lines = []
        ros_ver = None

        # ── Resource ──
        resource = None
        if self.snmp_enabled:
            resource = self.poll_resource_snmp()

        if resource is None and self.api_enabled:
            resource = self.poll_resource_api()
            if resource:
                ros_ver = resource.pop("_routeros_ver", None)

        if resource:
            fields_parts = []
            for k in ("cpu_load", "mem_used_pct", "mem_total_bytes",
                      "mem_free_bytes", "uptime_sec", "temperature_c"):
                v = resource.get(k)
                if v is not None:
                    if isinstance(v, int):
                        fields_parts.append(f"{k}={v}i")
                    else:
                        fields_parts.append(f"{k}={v}")
            if fields_parts:
                lines.append(
                    f"mikrotik_resource,{self._tags} "
                    + ",".join(fields_parts)
                    + f" {ts}"
                )
            mdb.set_status(self.id, "online", ts, routeros_ver=ros_ver)
        else:
            mdb.set_status(self.id, "offline", ts)
            log.warning(f"[{self.name}] {self.ip} — unreachable (resource poll failed)")

        # ── Interfaces ──
        if self.snmp_enabled:
            try:
                iface_lines = self.poll_interfaces_snmp(ts)
                lines.extend(iface_lines)
            except Exception as e:
                log.warning(f"[{self.name}] interface poll error: {e}")

        # ── PPP sessions ──
        if self.api_enabled and self.password:
            try:
                ppp_lines = self.poll_ppp_api(ts)
                lines.extend(ppp_lines)
            except Exception as e:
                log.warning(f"[{self.name}] PPP poll error: {e}")

        # ── Write to InfluxDB ──
        if lines:
            _influx_write_lines(lines)
            log.info(f"[{self.name}] wrote {len(lines)} points to InfluxDB")


# ── Main loop ─────────────────────────────────────────────────────────────────

_active_threads: dict[int, threading.Thread] = {}
_stop_event = threading.Event()


def _poll_device_loop(device: dict):
    """Continuously poll one device every INTERVAL_POLL seconds."""
    poller = MikroTikDevicePoller(device)
    dev_id = device["id"]
    log.info(f"[{device['name']}] polling thread started ({device['ip']})")
    while not _stop_event.is_set():
        try:
            poller.run_once()
        except Exception as e:
            log.exception(f"[{device['name']}] unexpected error: {e}")
        # Re-read device config (in case credentials changed)
        try:
            fresh = mdb.get_device(dev_id, include_creds=True)
            if fresh and fresh.get("enabled"):
                poller.dev = fresh
                poller.community = fresh.get("snmp_community", "public")
                poller.username  = fresh.get("username", "admin")
                poller.password  = fresh.get("password", "")
                poller.snmp_enabled = bool(fresh.get("snmp_enabled", 1))
                poller.api_enabled  = bool(fresh.get("api_enabled", 1))
            else:
                log.info(f"[{device['name']}] device disabled — stopping thread")
                break
        except Exception:
            pass
        _stop_event.wait(INTERVAL_POLL)
    log.info(f"[{device['name']}] polling thread stopped")


def main_loop():
    """
    Main manager loop.
    Starts a daemon thread per enabled device, reloads device list every
    INTERVAL_RELOAD seconds, starts threads for newly added devices.
    """
    log.info("PyroNMS MikroTik poller starting…")
    last_reload = 0

    while True:
        now = time.time()
        if now - last_reload >= INTERVAL_RELOAD:
            try:
                devices = mdb.get_all_devices_with_creds()
            except Exception as e:
                log.error(f"Failed to load device list: {e}")
                time.sleep(15)
                continue

            active_ids = set()
            for dev in devices:
                dev_id = dev["id"]
                active_ids.add(dev_id)
                t = _active_threads.get(dev_id)
                if t is None or not t.is_alive():
                    t = threading.Thread(
                        target=_poll_device_loop,
                        args=(dev,),
                        daemon=True,
                        name=f"mt-{dev_id}-{dev['name'][:12]}"
                    )
                    _active_threads[dev_id] = t
                    t.start()
                    log.info(f"Started thread for device {dev_id} ({dev['name']})")

            # Clean up threads for deleted/disabled devices
            for dev_id in list(_active_threads):
                if dev_id not in active_ids:
                    del _active_threads[dev_id]

            last_reload = now

        time.sleep(30)  # Check for new devices every 30s without busy-waiting


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Shutting down MikroTik poller")
        _stop_event.set()
