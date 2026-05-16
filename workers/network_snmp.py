"""
network_snmp.py — SNMP helpers (net-snmp CLI wrappers) for the Network
Graphs module.

Used by network_poller.py (bulk traffic / resource polling) and
api/server.py (device test, discovery).

Uses snmpget / snmpbulkget / snmpwalk via subprocess (consistent with
existing snmp_helper.py and mikrotik_poller.py).
"""

import logging
import re
import subprocess
import time

log = logging.getLogger("net-snmp")

# ── Common OIDs ───────────────────────────────────────────────────────────
OID_SYS_DESCR      = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID  = "1.3.6.1.2.1.1.2.0"
OID_SYS_UPTIME     = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME       = "1.3.6.1.2.1.1.5.0"

OID_IF_DESCR       = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE        = "1.3.6.1.2.1.2.2.1.3"
OID_IF_MTU         = "1.3.6.1.2.1.2.2.1.4"
OID_IF_SPEED       = "1.3.6.1.2.1.2.2.1.5"
OID_IF_ADMIN       = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER        = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_ERR      = "1.3.6.1.2.1.2.2.1.14"
OID_IF_OUT_ERR     = "1.3.6.1.2.1.2.2.1.20"
OID_IF_IN_DISC     = "1.3.6.1.2.1.2.2.1.13"
OID_IF_OUT_DISC    = "1.3.6.1.2.1.2.2.1.19"

OID_IF_NAME        = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HC_IN       = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT      = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGHSPEED   = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_ALIAS       = "1.3.6.1.2.1.31.1.1.1.18"

# Vendor SNMP enterprise prefixes (under 1.3.6.1.4.1)
_VENDOR_ENTERPRISE = {
    "9":     "cisco",
    "2636":  "juniper",
    "14988": "mikrotik",
    "2011":  "huawei",
    "8072":  "linux",       # net-snmp default
    "311":   "windows",     # Microsoft
    "12356": "fortinet",
    "10002": "ubiquiti",
}


# ── Subprocess wrapper ───────────────────────────────────────────────────
def _build_auth_args(dev: dict) -> list:
    """Build SNMP auth flags from a network_devices row."""
    ver = (dev.get("snmp_version") or "v2c").lower()
    if ver == "v3":
        args = ["-v3", "-u", dev.get("snmp_v3_user") or "",
                "-l", "authPriv" if dev.get("snmp_v3_priv_pass") else
                       ("authNoPriv" if dev.get("snmp_v3_auth_pass") else "noAuthNoPriv")]
        if dev.get("snmp_v3_auth_pass"):
            args += ["-a", (dev.get("snmp_v3_auth_proto") or "SHA"),
                     "-A", dev["snmp_v3_auth_pass"]]
        if dev.get("snmp_v3_priv_pass"):
            args += ["-x", (dev.get("snmp_v3_priv_proto") or "AES"),
                     "-X", dev["snmp_v3_priv_pass"]]
        return args
    # v2c (default)
    return ["-v2c", "-c", dev.get("snmp_community") or "public"]


def _target(dev: dict) -> str:
    port = int(dev.get("snmp_port") or 161)
    ip   = dev.get("ip")
    return f"{ip}:{port}" if port != 161 else ip


def snmp_get(dev: dict, *oids, timeout: int = None) -> dict:
    """
    snmpget — returns {oid: value_str}. Empty dict on failure.
    """
    if not oids:
        return {}
    t = timeout or int(dev.get("snmp_timeout") or 3)
    r = int(dev.get("snmp_retries") or 1)
    cmd = ["snmpget", "-Oqn", f"-t{t}", f"-r{r}"] + _build_auth_args(dev) \
        + [_target(dev)] + list(oids)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=t + 3)
        out = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or "No Such" in line or "Timeout" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1].strip().strip('"')
        return out
    except Exception as e:
        log.debug(f"snmp_get {dev.get('ip')} {oids}: {e}")
        return {}


def snmp_bulk_walk(dev: dict, base_oid: str, max_rep: int = 25,
                   timeout: int = None) -> dict:
    """
    snmpbulkwalk — returns {index_suffix: value_str} for a subtree.
    Index_suffix is the part of the OID after base_oid (e.g. for
    ifDescr.42 we return {"42": "..."}).
    """
    t = timeout or int(dev.get("snmp_timeout") or 3)
    r = int(dev.get("snmp_retries") or 1)
    cmd = ["snmpbulkwalk", "-Oqn", f"-t{t}", f"-r{r}", f"-Cr{max_rep}"] \
        + _build_auth_args(dev) + [_target(dev), base_oid]
    out = {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=max(t * 4, 30))
        prefix = base_oid + "."
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or "No Such" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            oid, val = parts[0], parts[1].strip().strip('"')
            if oid.startswith(prefix):
                out[oid[len(prefix):]] = val
            elif oid.startswith(base_oid):  # exact match (uncommon)
                out[""] = val
        return out
    except Exception as e:
        log.debug(f"snmp_bulk_walk {dev.get('ip')} {base_oid}: {e}")
        return {}


def snmp_walk(dev: dict, base_oid: str, timeout: int = None) -> dict:
    """Plain snmpwalk fallback (for v1 devices that don't support bulk)."""
    t = timeout or int(dev.get("snmp_timeout") or 3)
    r = int(dev.get("snmp_retries") or 1)
    cmd = ["snmpwalk", "-Oqn", f"-t{t}", f"-r{r}"] \
        + _build_auth_args(dev) + [_target(dev), base_oid]
    out = {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=max(t * 4, 30))
        prefix = base_oid + "."
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or "No Such" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            oid, val = parts[0], parts[1].strip().strip('"')
            if oid.startswith(prefix):
                out[oid[len(prefix):]] = val
        return out
    except Exception as e:
        log.debug(f"snmp_walk {dev.get('ip')} {base_oid}: {e}")
        return {}


# ── Vendor detection ─────────────────────────────────────────────────────
def detect_vendor(sys_descr: str = "", sys_object_id: str = "") -> str:
    """Returns one of: mikrotik|cisco|juniper|huawei|linux|windows|generic."""
    desc = (sys_descr or "").lower()
    oid  = sys_object_id or ""

    # Check sysObjectID enterprise OID first
    m = re.match(r"^\.?1\.3\.6\.1\.4\.1\.(\d+)", oid)
    if m:
        ent = m.group(1)
        if ent in _VENDOR_ENTERPRISE:
            return _VENDOR_ENTERPRISE[ent]

    # Fall back to keyword match in sysDescr
    if "routeros" in desc or "mikrotik" in desc:        return "mikrotik"
    if "cisco" in desc or "ios" in desc:                return "cisco"
    if "junos" in desc or "juniper" in desc:            return "juniper"
    if "huawei" in desc or "versatile routing" in desc: return "huawei"
    if "linux" in desc:                                  return "linux"
    if "windows" in desc or "microsoft" in desc:        return "windows"
    if "fortigate" in desc or "fortinet" in desc:       return "fortinet"
    if "ubnt" in desc or "ubiquiti" in desc or "edgeos" in desc: return "ubiquiti"
    return "generic"


# ── Connectivity test ────────────────────────────────────────────────────
def test_device(dev: dict) -> dict:
    """
    Returns {snmp_ok, sys_descr, sys_name, sys_object_id, vendor_detected,
             ms, error?}. Used by /network/devices/{id}/test endpoint.
    """
    start = time.time()
    result = {"snmp_ok": False, "sys_descr": "", "sys_name": "",
              "sys_object_id": "", "vendor_detected": "generic", "ms": 0}
    try:
        res = snmp_get(dev, OID_SYS_DESCR, OID_SYS_NAME, OID_SYS_OBJECT_ID)
        result["ms"] = int((time.time() - start) * 1000)
        if not res:
            result["error"] = "SNMP timeout or wrong community/credentials"
            return result
        result["snmp_ok"] = True
        result["sys_descr"]     = res.get(OID_SYS_DESCR, "")
        result["sys_name"]      = res.get(OID_SYS_NAME, "")
        result["sys_object_id"] = res.get(OID_SYS_OBJECT_ID, "")
        result["vendor_detected"] = detect_vendor(result["sys_descr"],
                                                  result["sys_object_id"])
        return result
    except Exception as e:
        result["error"] = str(e)
        result["ms"] = int((time.time() - start) * 1000)
        return result


# ── Interface discovery ──────────────────────────────────────────────────
def discover_interfaces(dev: dict) -> list[dict]:
    """
    Walk IF-MIB and return list of dicts:
      [{if_index, if_name, if_descr, if_alias, if_type, if_speed, if_mtu,
        oper_status, admin_status, is_vlan, vlan_id}]
    """
    descrs   = snmp_bulk_walk(dev, OID_IF_DESCR)
    if not descrs:
        return []
    names    = snmp_bulk_walk(dev, OID_IF_NAME)
    aliases  = snmp_bulk_walk(dev, OID_IF_ALIAS)
    types    = snmp_bulk_walk(dev, OID_IF_TYPE)
    speeds   = snmp_bulk_walk(dev, OID_IF_SPEED)
    hspeeds  = snmp_bulk_walk(dev, OID_IF_HIGHSPEED)   # Mbps
    mtus     = snmp_bulk_walk(dev, OID_IF_MTU)
    opers    = snmp_bulk_walk(dev, OID_IF_OPER)
    admins   = snmp_bulk_walk(dev, OID_IF_ADMIN)

    def _int(s, default=0):
        try:    return int((s or "").split()[0])
        except: return default

    out = []
    for idx, descr in sorted(descrs.items(), key=lambda kv: _int(kv[0])):
        if_name = names.get(idx) or descr
        # Detect VLAN sub-interface by name (common patterns: vlan100, eth0.100)
        vlan_id = 0
        is_vlan = 0
        m = re.search(r"(?:vlan[\s_-]*|\.)(\d+)$", if_name.lower())
        if m:
            try:
                vlan_id = int(m.group(1))
                if 1 <= vlan_id <= 4094:
                    is_vlan = 1
            except:
                pass

        # Speed in bps: prefer ifHighSpeed (Mbps) if non-zero
        sp = _int(hspeeds.get(idx))
        speed_bps = sp * 1_000_000 if sp else _int(speeds.get(idx))

        out.append({
            "if_index":     _int(idx),
            "if_name":      if_name,
            "if_descr":     descr,
            "if_alias":     aliases.get(idx, ""),
            "if_type":      _int(types.get(idx)),
            "if_speed":     speed_bps,
            "if_mtu":       _int(mtus.get(idx)),
            "is_vlan":      is_vlan,
            "vlan_id":      vlan_id,
            "oper_status":  _int(opers.get(idx)),
            "admin_status": _int(admins.get(idx)),
        })
    return out


# ── Bulk interface counters (for poller) ─────────────────────────────────
def fetch_interface_counters(dev: dict) -> dict:
    """
    Returns {if_index: {in:int, out:int, in_err:int, out_err:int,
                        in_disc:int, out_disc:int, oper:int}}
    for all interfaces with HC counters.
    """
    hin   = snmp_bulk_walk(dev, OID_IF_HC_IN)
    hout  = snmp_bulk_walk(dev, OID_IF_HC_OUT)
    if not hin or not hout:
        return {}
    in_err  = snmp_bulk_walk(dev, OID_IF_IN_ERR)
    out_err = snmp_bulk_walk(dev, OID_IF_OUT_ERR)
    in_dis  = snmp_bulk_walk(dev, OID_IF_IN_DISC)
    out_dis = snmp_bulk_walk(dev, OID_IF_OUT_DISC)
    opers   = snmp_bulk_walk(dev, OID_IF_OPER)

    def _int(s, default=0):
        try:    return int((s or "").split()[0])
        except: return default

    out = {}
    for idx in hin.keys():
        out[_int(idx)] = {
            "in":       _int(hin.get(idx)),
            "out":      _int(hout.get(idx)),
            "in_err":   _int(in_err.get(idx)),
            "out_err":  _int(out_err.get(idx)),
            "in_disc":  _int(in_dis.get(idx)),
            "out_disc": _int(out_dis.get(idx)),
            "oper":     _int(opers.get(idx)),
        }
    return out
