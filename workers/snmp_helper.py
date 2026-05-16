"""
SNMP helper — Huawei MA5603T GPON OLT
Phase-2: Direct SNMP bulk optical polling (fastest method).

Key breakthrough: Huawei GPON optical MIB responds to direct SNMP GET per-ONT.
We batch all ONTs on a port into one snmpget call → entire port in <1 second.
Full OLT (2581 ONTs) in ~30 seconds vs 35-45 minutes via SSH.

Index formula (empirically validated):
    port_ifIndex = 0xFA000000 + slot * 8192 + port * 256
    ont_oid_index = port_ifIndex.ont_id

Huawei GPON optical OIDs (hwGponOntOpticalInfoTable):
    .51.1.1  Temperature   (°C, direct integer)
    .51.1.4  RX Power      (0.01 dBm — divide by 100)
    .51.1.5  TX Power      (0.01 dBm — divide by 100)
    .51.1.6  OLT-side RX   (0.01 dBm — divide by 100)
    Offline sentinel: 2147483647 (INT_MAX) = no reading
"""

from __future__ import annotations

import re
import subprocess
from typing import Dict, List, Optional

# ── Huawei GPON optical MIB constants ────────────────────────────────────────
_GPON_BASE       = 0xFA000000            # base ifIndex for GPON ports
_GPON_SLOT_STEP  = 8192                  # 0x2000 per slot
_GPON_PORT_STEP  = 256                   # 0x100 per port
_NO_READING      = 2147483647            # INT_MAX — offline / no optical data

_OID_RX_POWER  = "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4"
_OID_TEMP      = "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.1"
_OID_TX_POWER  = "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.5"
_OID_OLT_RX    = "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6"

# Max OIDs per snmpget call (stay under UDP MTU — 40 OIDs × 2 metrics = 80 per call)
_SNMP_CHUNK = 40


def _run_snmp(args, timeout=8):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "").strip()
        return True, (p.stdout or "").strip()
    except Exception as e:
        return False, str(e)


def snmp_ping(host: str, community: str, timeout=6) -> bool:
    ok, out = _run_snmp(
        [
            "snmpget",
            "-v2c",
            "-c",
            community,
            "-Oqv",
            "-t",
            "2",
            "-r",
            "1",
            host,
            "1.3.6.1.2.1.1.5.0",
        ],
        timeout=timeout,
    )
    return bool(ok and out)


def _snmp_get(host: str, community: str, oid: str, timeout=8) -> Optional[str]:
    ok, out = _run_snmp(
        [
            "snmpget",
            "-v2c",
            "-c",
            community,
            "-Oqv",
            "-t",
            "2",
            "-r",
            "1",
            host,
            oid,
        ],
        timeout=timeout,
    )
    if not ok:
        return None
    out = out.strip().strip('"')
    if out in (
        "",
        "No Such Object available on this agent at this OID",
        "No Such Instance currently exists at this OID",
    ):
        return None
    return out


def _to_float(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _to_int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"\d+", str(v))
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def _gpon_port_ifindex(slot: int, port: int) -> int:
    """Compute Huawei GPON port ifIndex from slot and port numbers."""
    return _GPON_BASE + slot * _GPON_SLOT_STEP + port * _GPON_PORT_STEP


def snmp_get_optical_port(
    host: str,
    community: str,
    slot: int,
    port: int,
    ont_ids: List[int],
    chunk_size: int = _SNMP_CHUNK,
    timeout: int = 12,
) -> Dict[int, Dict[str, float]]:
    """
    Batch SNMP optical fetch for all ONTs on one PON port.

    Sends one snmpget per chunk of ONTs (rx_power + temp per ONT in same call).
    A port with 115 ONTs → 3 snmpget calls → done in ~1 second.
    vs SSH: 3 × 115 = 345 round-trips → 3+ minutes.

    Returns:
        {ont_id: {"rx_power": float_dBm, "temp": float_C, ...}}
        Missing keys = offline / no optical reading.
    """
    if not ont_ids:
        return {}

    port_ifidx = _gpon_port_ifindex(slot, port)
    results: Dict[int, Dict] = {}

    for start in range(0, len(ont_ids), chunk_size):
        chunk = ont_ids[start : start + chunk_size]

        # Build OID list: rx_power + temp for each ONT in one call
        oids = []
        for oid in chunk:
            oids.append(f"{_OID_RX_POWER}.{port_ifidx}.{oid}")
            oids.append(f"{_OID_TEMP}.{port_ifidx}.{oid}")

        ok, out = _run_snmp(
            ["snmpget", "-v2c", f"-c{community}", "-Oqn", "-t4", "-r1", host] + oids,
            timeout=timeout,
        )
        if not ok or not out:
            continue

        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue

            # RX Power  (.51.1.4.PORT_IDX.ONT_ID  RAW_VALUE)
            m = re.search(r"51\.1\.4\.\d+\.(\d+)\s+(-?\d+)", line)
            if m:
                ont_id = int(m.group(1))
                raw = int(m.group(2))
                if raw != _NO_READING:
                    results.setdefault(ont_id, {})["rx_power"] = round(raw / 100.0, 2)
                continue

            # Temperature (.51.1.1.PORT_IDX.ONT_ID  RAW_VALUE)
            m = re.search(r"51\.1\.1\.\d+\.(\d+)\s+(\d+)", line)
            if m:
                ont_id = int(m.group(1))
                raw = int(m.group(2))
                if raw != _NO_READING and raw > 0:
                    results.setdefault(ont_id, {})["temp"] = float(raw)  # direct °C, cast to float for InfluxDB schema compat
                continue

    return results


def snmp_get_optical_port_full(
    host: str,
    community: str,
    slot: int,
    port: int,
    ont_ids: List[int],
    timeout: int = 15,
) -> Dict[int, Dict[str, float]]:
    """
    Extended version: also fetches tx_power and olt_rx in additional snmpget calls.
    Use when you need full 4-field optical picture (slower than the basic version).
    """
    results = snmp_get_optical_port(host, community, slot, port, ont_ids, timeout=timeout)

    if not ont_ids:
        return results

    port_ifidx = _gpon_port_ifindex(slot, port)

    for start in range(0, len(ont_ids), _SNMP_CHUNK):
        chunk = ont_ids[start : start + _SNMP_CHUNK]
        oids = []
        for oid in chunk:
            oids.append(f"{_OID_TX_POWER}.{port_ifidx}.{oid}")
            oids.append(f"{_OID_OLT_RX}.{port_ifidx}.{oid}")

        ok, out = _run_snmp(
            ["snmpget", "-v2c", f"-c{community}", "-Oqn", "-t4", "-r1", host] + oids,
            timeout=timeout,
        )
        if not ok or not out:
            continue

        for line in out.splitlines():
            line = line.strip()
            m = re.search(r"51\.1\.5\.\d+\.(\d+)\s+(-?\d+)", line)
            if m:
                ont_id, raw = int(m.group(1)), int(m.group(2))
                if raw != _NO_READING:
                    results.setdefault(ont_id, {})["tx_power"] = round(raw / 100.0, 2)
                continue
            m = re.search(r"51\.1\.6\.\d+\.(\d+)\s+(-?\d+)", line)
            if m:
                ont_id, raw = int(m.group(1)), int(m.group(2))
                if raw != _NO_READING:
                    results.setdefault(ont_id, {})["olt_rx"] = round(raw / 100.0, 2)

    return results


def get_ont_metrics_by_index(
    host: str, community: str, ont_index: int, oid_templates: Dict[str, str]
):
    if not oid_templates:
        return {}

    out = {}

    rx_tpl = oid_templates.get("rx_power", "").strip()
    if rx_tpl:
        rx = _snmp_get(host, community, rx_tpl.format(index=ont_index))
        rx_f = _to_float(rx)
        if rx_f is not None:
            out["rx_power"] = rx_f

    temp_tpl = oid_templates.get("temp", "").strip()
    if temp_tpl:
        tv = _snmp_get(host, community, temp_tpl.format(index=ont_index))
        ti = _to_int(tv)
        if ti is not None:
            out["temp"] = float(ti)

    vlan_tpl = oid_templates.get("vlan", "").strip()
    if vlan_tpl:
        vv = _snmp_get(host, community, vlan_tpl.format(index=ont_index))
        vi = _to_int(vv)
        if vi is not None:
            out["vlan"] = str(vi)

    return out
