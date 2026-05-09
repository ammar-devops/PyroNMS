"""
OLT SSH Helper
Shared functions for connecting to Huawei MA5603T and parsing ONT data
"""

import re
import logging
from netmiko import ConnectHandler

log = logging.getLogger(__name__)

RE_ONT_STATUS = re.compile(
    r"\d+/\s*\d+/\s*\d+\s+(\d+)\s+([0-9A-Fa-f]{16})\s+\S+\s+(online|offline|loss|dying-gasp)",
    re.IGNORECASE
)
RE_ONT_DESC = re.compile(r"\d+/\s*\d+/\s*\d+\s+(\d+)\s+(.+?)\s*$")
RE_RX_POWER = re.compile(r"Rx optical power\(dBm\)\s*:\s*([-\d.]+|NA|-)")
RE_TX_POWER = re.compile(r"Tx optical power\(dBm\)\s*:\s*([-\d.]+|NA|-)")
RE_OLT_RX   = re.compile(r"OLT Rx ONT optical power\(dBm\)\s*:\s*([-\d.]+|NA|-)")
RE_TEMP     = re.compile(r"Temperature\(C\)\s*:\s*(\d+|NA|-)")


def to_float(val):
    try:
        return float(val)
    except:
        return None


def connect_olt(host, username, password, port=22):
    """Connect to OLT and enter config mode"""
    conn = ConnectHandler(
        device_type="huawei_smartax",
        host=host,
        username=username,
        password=password,
        port=port,
        conn_timeout=30,
        global_delay_factor=2,
        disabled_algorithms={"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]},
    )
    conn.send_command("enable", expect_string=r"[>#]", read_timeout=15)
    conn.send_command("config", expect_string=r"config\)#", read_timeout=15)
    return conn


def cmd(conn, command, expect=r"\)#", timeout=60):
    return conn.send_command(command, expect_string=expect, read_timeout=timeout)


def get_ont_list(conn, gpon_iface, port_num):
    """
    Get list of ONTs on a specific PON port
    gpon_iface = slot number (e.g. 1 for interface gpon 0/1)
    port_num   = port inside that interface (0-7 or 0-15)
    Returns list of dicts: {ont_id, sn, state, desc}
    """
    try:
        cmd(conn, f"interface gpon 0/{gpon_iface}", timeout=15)
        output = cmd(conn, f"display ont info {port_num} all", timeout=120)

        ont_map = {}
        for line in output.splitlines():
            m = RE_ONT_STATUS.search(line)
            if m:
                oid = int(m.group(1))
                ont_map[oid] = {
                    "ont_id": oid,
                    "sn":     m.group(2).upper(),
                    "state":  m.group(3).lower(),
                    "desc":   m.group(2).upper()
                }

        in_desc = False
        for line in output.splitlines():
            if "Description" in line and "ONT-ID" in line:
                in_desc = True
                continue
            if in_desc:
                if line.strip().startswith("---") or not line.strip():
                    continue
                if "total of ONTs" in line:
                    break
                m = RE_ONT_DESC.search(line)
                if m:
                    oid  = int(m.group(1))
                    desc = m.group(2).strip()
                    if desc and oid in ont_map:
                        ont_map[oid]["desc"] = desc

        cmd(conn, "quit", expect=r"config\)#", timeout=10)
        return list(ont_map.values())

    except Exception as e:
        log.error(f"get_ont_list {gpon_iface}/{port_num}: {e}")
        try:
            cmd(conn, "quit", expect=r"config\)#", timeout=10)
        except:
            pass
        return []


def get_optical(conn, gpon_iface, port_num, ont_id):
    """Get optical signal info for a single ONT"""
    try:
        cmd(conn, f"interface gpon 0/{gpon_iface}", timeout=15)
        output = cmd(conn, f"display ont optical-info {port_num} {ont_id}", timeout=20)
        cmd(conn, "quit", expect=r"config\)#", timeout=10)

        def ex(pat):
            m = pat.search(output)
            if m:
                v = m.group(1).strip()
                return None if v in ("-", "NA", "") else to_float(v)
            return None

        result = {
            "rx_power": ex(RE_RX_POWER),
            "tx_power": ex(RE_TX_POWER),
            "olt_rx":   ex(RE_OLT_RX),
            "temp":     ex(RE_TEMP),
        }
        if all(v is None for v in [result["rx_power"], result["tx_power"]]):
            return None
        return result

    except Exception as e:
        log.error(f"get_optical {gpon_iface}/{port_num}/{ont_id}: {e}")
        try:
            cmd(conn, "quit", expect=r"config\)#", timeout=10)
        except:
            pass
        return None


RE_DOWN_CAUSE = re.compile(r"Last down cause\s*:\s*(\S+)", re.IGNORECASE)

def get_down_cause(conn, gpon_iface, port_num, ont_id):
    """
    Get last down cause for an offline ONT.
    Returns: 'dying-gasp' (power failure), 'los' (fiber issue), or 'unknown'
    """
    try:
        cmd(conn, f"interface gpon 0/{gpon_iface}", timeout=15)
        output = cmd(conn, f"display ont info {port_num} {ont_id}", timeout=20)
        cmd(conn, "quit", expect=r"config\)#", timeout=10)
        m = RE_DOWN_CAUSE.search(output)
        if m:
            cause = m.group(1).strip().lower()
            return cause  # 'dying-gasp', 'los', 'deactivate', etc.
        return "unknown"
    except Exception as e:
        log.error(f"get_down_cause {gpon_iface}/{port_num}/{ont_id}: {e}")
        try:
            cmd(conn, "quit", expect=r"config\)#", timeout=10)
        except:
            pass
        return "unknown"


def get_wan_ip(conn, gpon_iface, port_num, ont_id):
    """Get WAN IP, VLAN and service info of ONT via OLT SSH"""
    try:
        cmd(conn, f"interface gpon 0/{gpon_iface}", timeout=15)
        output = cmd(conn, f"display ont wan-info {port_num} {ont_id}", timeout=20)
        cmd(conn, "quit", expect=r"config\)#", timeout=10)
        import re
        result = {}
        # IPv4 address
        m = re.search(r'IPv4 address\s*:\s*([\d.]+)', output)
        if m and m.group(1) != '0.0.0.0':
            result['ip'] = m.group(1)
        # Manage VLAN
        m2 = re.search(r'Manage VLAN\s*:\s*(\S+)', output)
        if m2: result['vlan'] = m2.group(1)
        # Service type
        m3 = re.search(r'Service type\s*:\s*(.+)', output)
        if m3: result['service'] = m3.group(1).strip()
        # Connection status
        m4 = re.search(r'IPv4 Connection status\s*:\s*(\S+)', output)
        if m4: result['status'] = m4.group(1).strip()
        return result if result else None
    except Exception as e:
        log.error(f"get_wan_ip {gpon_iface}/{port_num}/{ont_id}: {e}")
        return None
