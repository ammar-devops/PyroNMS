"""
SNMP helper for Phase-1 migration.
SNMP-first design with automatic SSH fallback in worker.
"""

from __future__ import annotations

import re
import subprocess
from typing import Dict, Optional


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
            out["temp"] = ti

    vlan_tpl = oid_templates.get("vlan", "").strip()
    if vlan_tpl:
        vv = _snmp_get(host, community, vlan_tpl.format(index=ont_index))
        vi = _to_int(vv)
        if vi is not None:
            out["vlan"] = str(vi)

    return out
