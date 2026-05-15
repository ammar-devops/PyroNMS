#!/usr/bin/env python3
"""
PyroNMS SNMP Poller v1.1
Huawei MA5603T/MA5608T — Production SNMP-first monitoring

Verified OIDs (live SNMP test against Slot 1/Port 0/ONT 0 and Slot 5/Port 10/ONT 0):
  .43.1.3  = ONT serial number (Hex-STRING 8 bytes)
  .43.1.9  = ONT customer description
  .43.1.10 = ONT online status (1=online, 2=offline)
  .51.1.1  = ONT temperature (direct °C integer, e.g. 60 = 60°C)
  .51.1.4  = ONT RX optical power (÷100 for dBm, e.g. -2796 = -27.96 dBm)
  .51.1.5  = ONT TX optical power (÷1000 for dBm, e.g. 3360 = 3.360 dBm)
  .51.1.6  = OLT-side RX power (÷1000 for dBm) — NOT temperature
  .51.1.7  = INT_MAX sentinel (2147483647 = no reading)
  ifHCInOctets  / ifHCOutOctets at PON ifIndex = PON traffic bytes (64-bit)

ifIndex formula (verified on V800R018 firmware):
  BASE = 0xFA000000
  slot = (ifindex - BASE) >> 13
  port = ((ifindex - BASE) & 0x1F00) >> 8
  reverse: ifindex = BASE | (slot << 13) | (port << 8)

v1.1 Changes:
  - Fixed OID_OPT_TEMP: was .51.1.6 (OLT-side RX), corrected to .51.1.1 (ONT temp)
  - Fixed temperature scale: was ÷100, now direct integer (°C)
  - Fixed field names: rx_power_dbm→rx_power, temperature_c→temp (matches slot_worker schema)
  - Fixed ont_status tags: added sn, pon tags; olt tag uses name not IP (matches API queries)
"""

import time
import logging
import threading
import sqlite3
import json
from pathlib import Path
from datetime import datetime

from pysnmp.hlapi import (
    SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity, bulkCmd, getCmd
)
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# ── Configuration ─────────────────────────────────────────────────────────
INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "my-super-secret-token"
INFLUX_ORG    = "myisp"
INFLUX_BUCKET = "olt_monitoring"
DB_PATH       = "/opt/pyronms/data/ont_map.db"
LOG_PATH      = "/opt/pyronms/logs/poller.log"
OLT_CONFIG    = "/opt/pyronms/config/olts.json"

# Poll intervals (seconds)
INTERVAL_TRAFFIC  = 120   # PON traffic bytes
INTERVAL_STATUS   = 60    # ONT online/offline
INTERVAL_OPTICAL  = 300   # RX power + temperature
INTERVAL_REMAP    = 86400 # ONT index rebuild (daily)

BULK_MAX = 50             # GETBULK max-repetitions
SNMP_TIMEOUT = 10         # seconds per request
SNMP_RETRIES = 2

# ── OIDs (verified against live OLT — see module docstring) ──────────────
OID_ONT_SN     = "1.3.6.1.4.1.2011.6.128.1.1.2.43.1.3"   # Hex-STRING SN
OID_ONT_DESC   = "1.3.6.1.4.1.2011.6.128.1.1.2.43.1.9"   # customer name
OID_ONT_STATUS = "1.3.6.1.4.1.2011.6.128.1.1.2.43.1.10"  # 1=online 2=offline
OID_OPT_RX     = "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4"   # ÷100 → dBm (FIXED: was .51.1.4, confirmed correct)
OID_OPT_TEMP   = "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.1"   # direct °C (FIXED: was .51.1.6 which is OLT-side RX)
OID_IFX_IN     = "1.3.6.1.2.1.31.1.1.1.6"                 # ifHCInOctets
OID_IFX_OUT    = "1.3.6.1.2.1.31.1.1.1.10"                # ifHCOutOctets

BULK_MAX_OPTICAL = 25   # Smaller for optical — large OctetString values

IFINDEX_BASE   = 0xFA000000
INVALID_POWER  = 2147483647  # OLT returns this when ONT is offline

# ── Logging ───────────────────────────────────────────────────────────────
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
    ]
)
log = logging.getLogger("pyronms")


# ── Helpers ───────────────────────────────────────────────────────────────
def ifindex_to_fsp(ifindex: int) -> tuple[int, int, int]:
    offset = ifindex - IFINDEX_BASE
    slot   = offset >> 13
    port   = (offset & 0x1F00) >> 8
    return 0, slot, port


def decode_sn(val) -> str:
    try:
        raw = bytes(val)
        if len(raw) >= 4:
            prefix = raw[:4].decode("ascii", errors="replace")
            suffix = raw[4:].hex().upper()
            return prefix + suffix
    except Exception:
        pass
    s = str(val)
    if s.startswith("0x"):
        try:
            b = bytes.fromhex(s[2:])
            return b[:4].decode("ascii", errors="replace") + b[4:].hex().upper()
        except Exception:
            pass
    try:
        parts = s.strip().split()
        b = bytes([int(x,16) for x in parts])
        return b[:4].decode("ascii", errors="replace") + b[4:].hex().upper()
    except Exception:
        pass
    return s.replace(" ","").upper()


class SNMPWalker:
    def __init__(self, host: str, community: str):
        self.host      = host
        self.community = community

    def _target(self):
        return UdpTransportTarget(
            (self.host, 161),
            timeout=SNMP_TIMEOUT,
            retries=SNMP_RETRIES
        )

    def bulk_walk(self, oid: str) -> dict:
        """Returns {suffix: value_str}"""
        result = {}
        for (ei, es, _, varBinds) in bulkCmd(
            SnmpEngine(),
            CommunityData(self.community, mpModel=1),
            self._target(), ContextData(),
            0, BULK_MAX,
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
            lookupMib=False
        ):
            if ei:
                log.error(f"[{self.host}] SNMP error {oid}: {ei}")
                break
            if es:
                log.error(f"[{self.host}] SNMP status {oid}: {es.prettyPrint()}")
                break
            for vb in varBinds:
                oid_str = str(vb[0])
                if oid_str.startswith(oid + "."):
                    suffix = oid_str[len(oid)+1:]
                    result[suffix] = vb[1]
        return result


# ── SQLite index cache ────────────────────────────────────────────────────
class OntCache:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS ont_map (
        pon_ifindex  INTEGER,
        ont_id       INTEGER,
        sn           TEXT,
        frame        INTEGER DEFAULT 0,
        slot         INTEGER,
        port         INTEGER,
        description  TEXT DEFAULT '',
        olt_ip       TEXT,
        last_seen    INTEGER,
        PRIMARY KEY (pon_ifindex, ont_id)
    );
    CREATE INDEX IF NOT EXISTS idx_sn ON ont_map(sn);
    CREATE INDEX IF NOT EXISTS idx_olt ON ont_map(olt_ip);
    """

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def upsert_batch(self, rows: list):
        with self._lock:
            self.conn.executemany("""
                INSERT OR REPLACE INTO ont_map
                  (pon_ifindex, ont_id, sn, frame, slot, port, description, olt_ip, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, rows)
            self.conn.commit()

    def get_all(self, olt_ip: str) -> list:
        with self._lock:
            cur = self.conn.execute(
                "SELECT pon_ifindex, ont_id, sn, frame, slot, port, description FROM ont_map WHERE olt_ip=?",
                (olt_ip,)
            )
            return cur.fetchall()

    def count(self, olt_ip: str) -> int:
        with self._lock:
            cur = self.conn.execute("SELECT COUNT(*) FROM ont_map WHERE olt_ip=?", (olt_ip,))
            return cur.fetchone()[0]


# ── Traffic delta state ───────────────────────────────────────────────────
class TrafficState:
    def __init__(self):
        self._prev = {}
        self._lock = threading.Lock()

    def update(self, key: str, rx: int, tx: int) -> dict | None:
        now  = time.time()
        with self._lock:
            prev = self._prev.get(key)
            self._prev[key] = {"rx": rx, "tx": tx, "ts": now}

        if prev is None:
            return None
        elapsed = now - prev["ts"]
        if elapsed < 5:
            return None

        # Handle counter reset
        rx_delta = max(0, rx - prev["rx"]) if rx >= prev["rx"] else 0
        tx_delta = max(0, tx - prev["tx"]) if tx >= prev["tx"] else 0

        # Sanity: reject > 10 Gbps
        limit = 10_000_000_000 * elapsed / 8
        if rx_delta > limit:
            rx_delta = 0
        if tx_delta > limit:
            tx_delta = 0

        return {
            "rx_mbps":       round(rx_delta * 8 / elapsed / 1_000_000, 4),
            "tx_mbps":       round(tx_delta * 8 / elapsed / 1_000_000, 4),
            "rx_bytes_delta": int(rx_delta),
            "tx_bytes_delta": int(tx_delta),
            "interval":       round(elapsed, 1),
        }


# ── Main Poller ───────────────────────────────────────────────────────────
class OLTPoller:
    def __init__(self, olt_cfg: dict, cache: OntCache,
                 influx_write_api, traffic_state: TrafficState):
        self.ip       = olt_cfg["ip"]
        self.name     = olt_cfg["name"]
        self.snmp     = SNMPWalker(self.ip, olt_cfg["community"])
        self.cache    = cache
        self.influx   = influx_write_api
        self.traffic  = traffic_state

    def _write(self, points: list):
        if points:
            self.influx.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)

    # ── Index mapping ─────────────────────────────────────────────────────
    def run_remap(self):
        log.info(f"[{self.name}] Starting ONT index discovery...")
        t0 = time.time()

        sn_table   = self.snmp.bulk_walk(OID_ONT_SN)
        desc_table = self.snmp.bulk_walk(OID_ONT_DESC)

        rows = []
        now  = int(time.time())

        for suffix, val in sn_table.items():
            parts = suffix.split(".")
            if len(parts) < 2:
                continue
            try:
                pon_ifindex = int(parts[0])
                ont_id      = int(parts[1])
            except ValueError:
                continue

            # Decode SN from Hex-STRING
            sn = decode_sn(val) if val is not None else f"UNKNOWN_{pon_ifindex}_{ont_id}"

            frame, slot, port = ifindex_to_fsp(pon_ifindex)
            desc = str(desc_table.get(suffix, "")).strip().strip('"')

            rows.append((pon_ifindex, ont_id, sn, frame, slot, port, desc, self.ip, now))

        self.cache.upsert_batch(rows)
        elapsed = time.time() - t0
        log.info(f"[{self.name}] Remap done: {len(rows)} ONTs in {elapsed:.1f}s")

    # ── ONT status ────────────────────────────────────────────────────────
    def run_status(self):
        t0 = time.time()
        status_table = self.snmp.bulk_walk(OID_ONT_STATUS)

        # Load SN/description map from cache for tag enrichment
        # This makes ont_status queryable by sn tag (same as slot_worker — required by API)
        sn_map = {}
        for row in self.cache.get_all(self.ip):
            pon_ifindex, ont_id, sn, frame, slot, port, desc = row
            sn_map[(pon_ifindex, ont_id)] = (sn, desc, frame, slot, port)

        points = []
        online_count = 0
        offline_count = 0

        for suffix, val in status_table.items():
            parts = suffix.split(".")
            if len(parts) < 2:
                continue
            try:
                pon_ifindex = int(parts[0])
                ont_id      = int(parts[1])
                status      = int(val)
            except (ValueError, TypeError):
                continue

            frame, slot, port = ifindex_to_fsp(pon_ifindex)
            online = 1 if status == 1 else 0
            pon = f"{frame}/{slot}/{port}"
            if online:
                online_count += 1
            else:
                offline_count += 1

            # Enrich with SN from cache (required for API /onts queries)
            sn_info = sn_map.get((pon_ifindex, ont_id))
            sn   = sn_info[0] if sn_info else ""
            desc = sn_info[1] if sn_info else ""

            p = (Point("ont_status")
                 .tag("olt",         self.name)   # Use name (matches slot_worker + API)
                 .tag("pon",         pon)          # 0/1/0 format (matches slot_worker)
                 .tag("ont_id",      str(ont_id))
                 .tag("sn",          sn)           # Critical: API queries by sn tag
                 .tag("description", desc)
                 .field("online",    online)
                 .field("state",     "online" if online else "offline"))
            points.append(p)

        self._write(points)

        # Summary per OLT
        summary = (Point("olt_summary")
                   .tag("olt",      self.name)
                   .tag("olt_ip",   self.ip)
                   .field("total_onts",   online_count + offline_count)
                   .field("online_onts",  online_count)
                   .field("offline_onts", offline_count))
        self._write([summary])

        elapsed = time.time() - t0
        log.info(f"[{self.name}] Status: {online_count} online, {offline_count} offline in {elapsed:.2f}s")

    # ── PON traffic ───────────────────────────────────────────────────────
    def run_traffic(self):
        t0 = time.time()

        rx_table = self.snmp.bulk_walk(OID_IFX_IN)
        tx_table = self.snmp.bulk_walk(OID_IFX_OUT)

        points = []
        matched = 0

        for suffix, rx_val in rx_table.items():
            try:
                ifindex = int(suffix)
            except ValueError:
                continue

            # Only care about PON port interfaces (high ifIndex, slot > 0)
            if ifindex < IFINDEX_BASE:
                continue
            frame, slot, port = ifindex_to_fsp(ifindex)
            if slot == 0:
                continue

            rx = int(rx_val)
            tx = int(tx_table.get(suffix, 0))

            key     = f"{self.ip}_{ifindex}"
            metrics = self.traffic.update(key, rx, tx)
            if metrics is None:
                continue

            matched += 1
            p = (Point("pon_traffic")
                 .tag("olt",         self.name)
                 .tag("olt_ip",      self.ip)
                 .tag("fsp",         f"{frame}/{slot}/{port}")
                 .tag("slot",        str(slot))
                 .tag("port",        str(port))
                 .field("rx_mbps",         metrics["rx_mbps"])
                 .field("tx_mbps",         metrics["tx_mbps"])
                 .field("rx_bytes_delta",  metrics["rx_bytes_delta"])
                 .field("tx_bytes_delta",  metrics["tx_bytes_delta"])
                 .field("rx_bytes_total",  rx)
                 .field("tx_bytes_total",  tx)
                 .field("poll_interval",   metrics["interval"]))
            points.append(p)

        self._write(points)
        elapsed = time.time() - t0
        log.info(f"[{self.name}] Traffic: {matched} PON ports written in {elapsed:.2f}s")

    # ── Optical power + temperature ───────────────────────────────────────
    def run_optical(self):
        t0 = time.time()

        rx_power_table = self.snmp.bulk_walk(OID_OPT_RX)
        temp_table     = self.snmp.bulk_walk(OID_OPT_TEMP)

        points = []

        for suffix, val in rx_power_table.items():
            parts = suffix.split(".")
            if len(parts) < 2:
                continue
            try:
                pon_ifindex = int(parts[0])
                ont_id      = int(parts[1])
                raw_power   = int(val)
            except (ValueError, TypeError):
                continue

            # Skip offline/invalid readings
            if raw_power == INVALID_POWER:
                continue

            frame, slot, port = ifindex_to_fsp(pon_ifindex)
            rx_dbm = round(raw_power / 100.0, 2)

            # Temperature: .51.1.1 returns direct integer °C (e.g. 60 = 60°C)
            # FIXED: was OID .51.1.6 (OLT-side RX power) with wrong ÷100 scale
            temp_raw = temp_table.get(suffix)
            if temp_raw is not None:
                try:
                    t_int = int(temp_raw)
                    temp_c = float(t_int) if t_int != INVALID_POWER and t_int > 0 else None
                except (ValueError, TypeError):
                    temp_c = None
            else:
                temp_c = None

            # Skip if no valid fields at all
            fields = {"rx_power": rx_dbm}
            if temp_c is not None:
                fields["temp"] = temp_c
            if not fields:
                continue

            p = (Point("ont_optical")
                 .tag("olt",     self.name)
                 .tag("olt_ip",  self.ip)
                 .tag("fsp",     f"{frame}/{slot}/{port}")
                 .tag("slot",    str(slot))
                 .tag("port",    str(port))
                 .tag("ont_id",  str(ont_id)))
            for field_name, field_val in fields.items():
                p = p.field(field_name, field_val)
            points.append(p)

        self._write(points)
        elapsed = time.time() - t0
        log.info(f"[{self.name}] Optical: {len(points)} ONTs written in {elapsed:.2f}s")


# ── Scheduler ─────────────────────────────────────────────────────────────
def run_scheduler(pollers: list):
    """Simple interval scheduler — runs each worker in its own thread."""

    def periodic(fn, interval, name):
        while True:
            try:
                fn()
            except Exception as e:
                log.exception(f"Worker {name} error: {e}")
            time.sleep(interval)

    threads = []
    for p in pollers:
        for fn, interval, label in [
            (p.run_remap,   INTERVAL_REMAP,   f"{p.name}/remap"),
            (p.run_status,  INTERVAL_STATUS,  f"{p.name}/status"),
            (p.run_traffic, INTERVAL_TRAFFIC, f"{p.name}/traffic"),
            (p.run_optical, INTERVAL_OPTICAL, f"{p.name}/optical"),
        ]:
            t = threading.Thread(
                target=periodic,
                args=(fn, interval, label),
                daemon=True,
                name=label
            )
            threads.append(t)

    # Run remap first (sync), then start all workers
    log.info("Running initial ONT index discovery...")
    for p in pollers:
        p.run_remap()

    log.info(f"Starting all polling workers ({len(threads)} threads)...")
    for t in threads[len(pollers):]:  # Skip remap threads (handled separately)
        t.start()

    # Remap threads — start after initial remap done
    for t in threads[:len(pollers)]:
        t.start()

    log.info("All workers running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down...")


# ── Entry point ───────────────────────────────────────────────────────────
def main():
    log.info("PyroNMS SNMP Poller starting...")

    # Load OLT config
    with open(OLT_CONFIG) as f:
        olts = json.load(f)
    active = [o for o in olts if o.get("active", True)]
    log.info(f"Loaded {len(active)} active OLTs")

    # Init InfluxDB
    influx  = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    # Init shared state
    cache   = OntCache(DB_PATH)
    traffic = TrafficState()

    # Init pollers
    pollers = [OLTPoller(o, cache, write_api, traffic) for o in active]

    run_scheduler(pollers)


if __name__ == "__main__":
    main()
