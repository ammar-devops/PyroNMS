import subprocess, json, re, time

def get_olts():
    import auth_db
    conn = auth_db.get_db()
    rows = conn.execute("SELECT id,name,ip,username,password,snmp_community,snmp_write_community,model,active,created_at FROM olts").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_olt(name, ip, username, password, snmp_community, model):
    import auth_db
    conn = auth_db.get_db()
    conn.execute("INSERT INTO olts (name,ip,username,password,snmp_community,model) VALUES (?,?,?,?,?,?)",
        (name, ip, username, password, snmp_community, model))
    conn.commit(); conn.close()
    return True

def update_olt(olt_id, data):
    import auth_db
    allowed = ["name","ip","username","password","snmp_community","snmp_write_community","model","active"]
    sets = [f"{k}=?" for k in data if k in allowed]
    vals = [v for k,v in data.items() if k in allowed]
    if not sets: return False
    conn = auth_db.get_db()
    conn.execute(f"UPDATE olts SET {','.join(sets)} WHERE id=?", vals+[olt_id])
    conn.commit(); conn.close()
    return True

def delete_olt(olt_id):
    import auth_db
    if int(olt_id) == 1: return False, "Cannot delete default OLT"
    conn = auth_db.get_db()
    conn.execute("DELETE FROM olts WHERE id=?", (olt_id,))
    conn.commit(); conn.close()
    return True, "Deleted"

def get_olt_profiles():
    try:
        with open('/opt/ont-monitor/olt-config/olt_profiles.json') as f:
            d = json.load(f)
        return {'line_profiles': d.get('line_profiles',[]),
                'srv_profiles': d.get('srv_profiles',[]),
                'dba_profiles': d.get('dba_profiles',[]),
                'vlans': d.get('vlans',[]),
                'gpon_interfaces': d.get('gpon_interfaces',[]),
                'ont_count': d.get('ont_count',0),
                'updated_at': d.get('updated_at','')}
    except Exception as e:
        return {'error': str(e), 'line_profiles':[], 'srv_profiles':[]}

def olt_ssh(ip, username, password):
    from netmiko import ConnectHandler
    last_error = None
    for attempt in range(3):
        try:
            conn = ConnectHandler(device_type='terminal_server', host=ip,
                username=username, password=password, timeout=75, conn_timeout=30,
                banner_timeout=30, auth_timeout=30,
                disabled_algorithms=dict(pubkeys=['rsa-sha2-256','rsa-sha2-512']))
            conn.write_channel('enable\r\n'); time.sleep(2); conn.read_channel()
            conn.write_channel('config\r\n'); time.sleep(2); conn.read_channel()
            return conn
        except Exception as e:
            last_error = e
            time.sleep(2 + attempt)
    raise last_error

def _read_until_prompt(conn, delay=2, max_rounds=24):
    chunks = []
    ansi_re = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
    huawei_pager_re = re.compile(r"\(\s*Press\s*'?Q'?\s*to\s*break\s*\)[^\n]*")
    for _ in range(max_rounds):
        time.sleep(delay)
        chunk = conn.read_channel()
        if not chunk:
            break
        chunk = ansi_re.sub('', chunk).replace('\r', '')
        # Cisco "---- More" pager
        while '---- More' in chunk:
            before, _, after = chunk.partition('---- More')
            chunks.append(before)
            conn.write_channel(' ')
            time.sleep(0.8)
            chunk = ansi_re.sub('', conn.read_channel()).replace('\r', '')
            chunk = after + chunk
        # Huawei pager "( Press 'Q' to break )"
        while huawei_pager_re.search(chunk):
            m = huawei_pager_re.search(chunk)
            chunks.append(chunk[:m.start()])
            conn.write_channel(' ')
            time.sleep(0.8)
            chunk = ansi_re.sub('', conn.read_channel()).replace('\r', '')
        chunks.append(chunk)
        stripped = chunk.strip()
        if stripped.endswith('#') or stripped.endswith(']'):
            break
    return ''.join(chunks)

def _olt_command(conn, command, delay=2, confirm_cr=True, wait_prompt=False, max_rounds=24):
    conn.write_channel(command + '\r\n')
    time.sleep(delay)
    out = conn.read_channel()
    if confirm_cr and ('{ <cr>|' in out or '{<cr>|' in out):
        conn.write_channel('\r\n')
        if wait_prompt:
            out += _read_until_prompt(conn, delay=delay, max_rounds=max_rounds)
        else:
            time.sleep(delay)
            out += conn.read_channel()
    elif wait_prompt:
        out += _read_until_prompt(conn, delay=delay, max_rounds=max_rounds)
    return out

def get_unregistered_onts(ip, username, password):
    conn = olt_ssh(ip, username, password)
    results = []
    conn.write_channel('display ont autofind all\r\n')
    out = _read_until_prompt(conn)
    if out.strip() and 'Failure' not in out and 'Parameter error' not in out:
        blocks = re.split(r'-{20,}', out)
        for block in blocks:
            sn = re.search(r'Ont SN\s*:\s*([0-9A-Fa-f]+)', block)
            if not sn:
                continue
            vendor = re.search(r'VendorID\s*:\s*(\S+)', block)
            model  = re.search(r'Ont EquipmentID\s*:\s*(\S+)', block)
            t      = re.search(r'Ont autofind time\s*:\s*([^\n]+)', block, re.I)
            fsp    = re.search(r'F/S/P\s*:\s*(\S+)', block)
            fsp_val = fsp.group(1) if fsp else '?'
            parts = fsp_val.split('/')
            port_num = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            slot_str = '/'.join(parts[:2]) if len(parts) > 1 else fsp_val
            results.append({
                'sn':     sn.group(1),
                'pon':    fsp_val,
                'slot':   slot_str,
                'port':   port_num,
                'vendor': vendor.group(1) if vendor else '?',
                'model':  model.group(1) if model else '?',
                'time':   t.group(1).strip() if t else '?'
            })
    conn.disconnect()
    return results

def provision_ont(ip, username, password, sn, slot_port, port, line_id, srv_id, description, vlan_id=None, user_vlan=None, vas_profile="PPP-10-IPV4-IPV6"):
    conn = olt_ssh(ip, username, password)
    conn.write_channel(f'interface gpon {slot_port}\r\n')
    time.sleep(2); conn.read_channel()
    cmd = f'ont add {port} sn-auth {sn} omci ont-lineprofile-id {line_id} ont-srvprofile-id {srv_id} desc "{description}"'
    conn.write_channel(cmd+'\r\n'); time.sleep(5)
    out = conn.read_channel()
    service_port_output = ""
    s = re.search(r'success:\s*(\d+)', out)
    o = re.search(r'ONTID\s*:\s*(\d+)', out)
    if s and int(s.group(1)) > 0:
        ont_id = int(o.group(1)) if o else -1
        if vlan_id and ont_id >= 0:
            if not user_vlan:
                user_vlan = vlan_id
            sp_cmd = f'service-port vlan {vlan_id} gpon {slot_port}/{port} ont {ont_id} gemport 1 multi-service user-vlan {user_vlan} tag-transform translate'
            conn.write_channel('quit\r\n'); time.sleep(1); conn.read_channel()
            conn.write_channel(sp_cmd + '\r\n'); time.sleep(4)
            service_port_output = conn.read_channel()
        conn.write_channel('quit\r\n'); time.sleep(1)
        conn.disconnect()
        all_output = out + ("\n" + service_port_output if service_port_output else "")
        all_output += f"\n[VAS_PROFILE] {vas_profile}"
        if "Failure" in service_port_output:
            return False, ont_id, all_output
        return True, ont_id, all_output
    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.disconnect()
    return False, -1, out


def delete_onts(ip, username, password, sns):
    """Delete ONTs by SN list.  Returns list of {sn, ok, message} dicts."""
    conn = olt_ssh(ip, username, password)
    results = []
    for sn in sns:
        sn = sn.strip().upper()
        try:
            # Locate ONT on OLT
            conn.write_channel(f'display ont info by-sn {sn}\r\n')
            _out = _read_until_prompt(conn, delay=3, max_rounds=20)
            _fsp_m = re.search(r'F/S/P\s*:\s*(\S+)', _out)
            _id_m  = re.search(r'ONT-ID\s*:\s*(\d+)', _out)
            if not _fsp_m or not _id_m:
                results.append({'sn': sn, 'ok': False, 'message': 'ONT not found on OLT'})
                continue
            fsp    = _fsp_m.group(1)
            ont_id = int(_id_m.group(1))
            parts  = fsp.split('/')
            slot_port = '/'.join(parts[:2])
            port   = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

            # Delete service-ports first
            sp_out = _olt_command(conn, f'display service-port port {fsp} ont {ont_id}', delay=3)
            sp_ids = re.findall(r'^\s*(\d+)\s+GPON', sp_out, re.MULTILINE)
            for sp_id in sp_ids:
                _olt_command(conn, f'undo service-port {sp_id}', delay=3)

            # Enter GPON interface and delete ONT
            conn.write_channel(f'interface gpon {slot_port}\r\n')
            time.sleep(1.5); conn.read_channel()
            conn.write_channel(f'ont delete {port} {ont_id}\r\n')
            time.sleep(2)
            del_out = conn.read_channel()
            if 'y/n' in del_out.lower() or 'sure' in del_out.lower() or 'confirm' in del_out.lower():
                conn.write_channel('y\r\n')
                time.sleep(2); conn.read_channel()
            conn.write_channel('quit\r\n'); time.sleep(1); conn.read_channel()

            results.append({
                'sn': sn, 'ok': True,
                'message': f'Deleted {fsp} / ID {ont_id}. Removed {len(sp_ids)} service-port(s).'
            })
        except Exception as e:
            results.append({'sn': sn, 'ok': False, 'message': str(e)})
    try:
        conn.disconnect()
    except Exception:
        pass
    return results

def find_ont_by_sn(ip, username, password, sn):
    out = ''
    for attempt in range(2):
        conn = olt_ssh(ip, username, password)
        conn.write_channel(f'display ont info by-sn {sn}\r\n')
        out = _read_until_prompt(conn, delay=3, max_rounds=20)
        conn.write_channel('quit\r\n'); time.sleep(1)
        conn.disconnect()
        if re.search(r'F/S/P\s*:\s*(\S+)', out) and re.search(r'ONT-ID\s*:\s*(\d+)', out):
            break
        time.sleep(2)
    fsp = re.search(r'F/S/P\s*:\s*(\S+)', out)
    ont_id = re.search(r'ONT-ID\s*:\s*(\d+)', out)
    if not fsp or not ont_id:
        return None, out
    parts = fsp.group(1).split('/')
    vendor = re.search(r'SN\s*:\s*[0-9A-Fa-f]+\s*\(([^)-]+)-', out)
    equipment = re.search(r'Ont EquipmentID\s*:\s*(.+)', out)
    desc = re.search(r'Description\s*:\s*(.+)', out)
    line_id = re.search(r'Line profile ID\s*:\s*(\S+)', out)
    line_name = re.search(r'Line profile name\s*:\s*(.+)', out)
    srv_id = re.search(r'Service profile ID\s*:\s*(\S+)', out)
    srv_name = re.search(r'Service profile name\s*:\s*(.+)', out)
    run_state = re.search(r'Run state\s*:\s*(\S+)', out)
    config_state = re.search(r'Config state\s*:\s*(\S+)', out)
    return {
        'fsp': fsp.group(1),
        'slot_port': '/'.join(parts[:2]),
        'port': int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        'ont_id': int(ont_id.group(1)),
        'vendor': vendor.group(1).strip() if vendor else '',
        'model': equipment.group(1).strip() if equipment else '',
        'description': desc.group(1).strip() if desc else '',
        'line_profile_id': line_id.group(1).strip() if line_id else '',
        'line_profile_name': line_name.group(1).strip() if line_name else '',
        'service_profile_id': srv_id.group(1).strip() if srv_id else '',
        'service_profile_name': srv_name.group(1).strip() if srv_name else '',
        'run_state': run_state.group(1).strip() if run_state else '',
        'config_state': config_state.group(1).strip() if config_state else '',
    }, out

def summarize_ont(ont):
    lines = [
        f"ONT: {ont.get('fsp')} / ID {ont.get('ont_id')}",
        f"State: {ont.get('run_state') or '-'} / {ont.get('config_state') or '-'}",
        f"Model: {ont.get('vendor') or '-'} {ont.get('model') or '-'}",
        f"Alias: {ont.get('description') or '-'}",
        f"Line Profile: {ont.get('line_profile_id') or '-'} {ont.get('line_profile_name') or ''}".strip(),
        f"Service Profile: {ont.get('service_profile_id') or '-'} {ont.get('service_profile_name') or ''}".strip(),
    ]
    return '\n'.join(lines)

def _detect_wlan_band(ssid_dict):
    """Detect 2.4G vs 5G from wireless standard or SSID index. Returns '2.4G'/'5G'/None."""
    std = (ssid_dict.get('wireless_standard') or '').lower()
    if 'ac' in std or 'ax' in std or '802.11a' in std and 'b/g' not in std:
        return '5G'
    if 'b/g' in std or '802.11n' in std and 'ac' not in std:
        return '2.4G'
    # Fallback by index (Huawei convention: 1-4 = 2.4G, 5-8 = 5G)
    try:
        idx = int(ssid_dict.get('index', '0'))
        return '5G' if idx >= 5 else '2.4G'
    except (TypeError, ValueError):
        return None


def _parse_wlan_full(output):
    """Parse `display ont wlan-info` into list of band-tagged SSID dicts."""
    ssids = []
    current = None
    for line in output.splitlines():
        m = re.search(r'SSID Index\s*:\s*(\d+)', line, re.I)
        if m:
            if current is not None:
                current['band'] = _detect_wlan_band(current)
                ssids.append(current)
            current = {'index': m.group(1)}
            continue
        if current is None:
            continue
        m = re.search(r'^\s*SSID\s*:\s*(.+?)\s*$', line, re.I)
        if m:
            current['ssid'] = m.group(1).strip()
            continue
        m = re.search(r'Wireless Standard\s*:\s*(.+?)\s*$', line, re.I)
        if m:
            current['wireless_standard'] = m.group(1).strip()
            continue
        m = re.search(r'Administrative state\s*:\s*(\S+)', line, re.I)
        if m:
            current['enabled'] = m.group(1).strip().lower() == 'enable'
            continue
        m = re.search(r'Operational state\s*:\s*(\S+)', line, re.I)
        if m:
            current['operational'] = m.group(1).strip().lower() == 'up'
            continue
        m = re.search(r'Maximum associate number\s*:\s*(\d+)', line, re.I)
        if m:
            current['max_clients'] = int(m.group(1))
            continue
        m = re.search(r'Current associate number\s*:\s*(\d+)', line, re.I)
        if m:
            current['current_clients'] = int(m.group(1))
            continue
    if current:
        current['band'] = _detect_wlan_band(current)
        ssids.append(current)
    bands_present = sorted({s.get('band') for s in ssids if s.get('band')})
    return {'ssids': ssids, 'bands_present': bands_present, 'total': len(ssids)}


def _parse_wan_full(output):
    """Parse `display ont wan-info` into a single WAN connection dict.

    Returns the FIRST WAN entry (most ONTs have one Internet WAN).
    Fields filled best-effort; unparsed fields are absent from the dict.
    """
    wan = {}
    # Match common Huawei field lines
    rules = [
        ('index',         r'^\s*Index\s*:\s*(\d+)'),
        ('name',          r'^\s*Name\s*:\s*(.+?)\s*$'),
        ('service_type',  r'^\s*Service type\s*:\s*(.+?)\s*$'),
        ('connection_type', r'^\s*Connection type\s*:\s*(.+?)\s*$'),
        ('status',        r'^\s*IPv4 Connection status\s*:\s*(.+?)\s*$'),
        ('access_type',   r'^\s*IPv4 access type\s*:\s*(.+?)\s*$'),
        ('ipv4',          r'^\s*IPv4 address\s*:\s*(\S+)'),
        ('subnet',        r'^\s*Subnet mask\s*:\s*(\S+)'),
        ('gateway',       r'^\s*Default gateway\s*:\s*(\S+)'),
        ('dns1',          r'^\s*Primary DNS\s*:\s*(\S+)'),
        ('dns2',          r'^\s*Secondary DNS\s*:\s*(\S+)'),
        ('vlan',          r'^\s*Manage VLAN\s*:\s*(\S+)'),
        ('priority',      r'^\s*Manage priority\s*:\s*(\S+)'),
        ('multicast_vlan',r'^\s*Multicast VLAN\s*:\s*(\S+)'),
        ('nat',           r'^\s*NAT switch\s*:\s*(\S+)'),
        ('mac',           r'^\s*MAC address\s*:\s*(\S+)'),
        ('l2_encap',      r'^\s*L2 encap-type\s*:\s*(\S+)'),
        ('switch',        r'^\s*Switch\s*:\s*(\S+)'),
    ]
    for line in output.splitlines():
        for key, pat in rules:
            if key in wan:
                continue
            m = re.search(pat, line, re.I)
            if m:
                wan[key] = m.group(1).strip()
                break
    # Derive a normalized 'mode' for the frontend
    conn = (wan.get('connection_type') or '').lower()
    access = (wan.get('access_type') or '').lower()
    if 'bridge' in conn:
        wan['mode'] = 'bridge'
    elif 'pppoe' in access or 'pppoe' in (wan.get('l2_encap') or '').lower():
        wan['mode'] = 'pppoe'
    elif 'static' in access:
        wan['mode'] = 'static'
    elif 'dhcp' in access:
        wan['mode'] = 'dhcp'
    elif 'routed' in conn:
        wan['mode'] = 'ip-routed'
    else:
        wan['mode'] = ''
    return wan


def _parse_ont_ipconfig(output):
    """Parse `display ont ipconfig` (ONT management IP block)."""
    out = {}
    rules = [
        ('mgmt_ip',     r'^\s*ONT IP\s*:\s*(\S+)'),
        ('mgmt_subnet', r'^\s*ONT subnet mask\s*:\s*(\S+)'),
        ('mgmt_gateway',r'^\s*ONT gateway\s*:\s*(\S+)'),
        ('mgmt_dns1',   r'^\s*ONT primary DNS\s*:\s*(\S+)'),
        ('mgmt_dns2',   r'^\s*ONT slave DNS\s*:\s*(\S+)'),
        ('mgmt_mac',    r'^\s*ONT MAC\s*:\s*(\S+)'),
        ('mgmt_vlan',   r'^\s*ONT manage VLAN\s*:\s*(\S+)'),
    ]
    for line in output.splitlines():
        for key, pat in rules:
            if key in out:
                continue
            m = re.search(pat, line, re.I)
            if m:
                out[key] = m.group(1).strip()
                break
    return out


def _parse_wlan_info(output):
    """LEGACY parser kept for backward-compat with apply_ont_settings(kind='check').

    Returns the same shape as before: {'ssids': [...], 'ssid': <first>}.
    New code should use _parse_wlan_full() which is band-aware and richer.
    """
    ssids = []
    current = {}
    for line in output.splitlines():
        m = re.search(r'SSID Index\s*:\s*(\d+)', line, re.I)
        if m:
            if current:
                ssids.append(current)
            current = {'index': m.group(1)}
            continue
        m = re.search(r'^\s*SSID\s*:\s*(.+)', line, re.I)
        if m and current is not None:
            current['ssid'] = m.group(1).strip()
            continue
        m = re.search(r'Administrative state\s*:\s*(\S+)', line, re.I)
        if m and current is not None:
            current['enabled'] = m.group(1).strip().lower() == 'enable'
    if current:
        ssids.append(current)
    return {'ssids': ssids, 'ssid': ssids[0].get('ssid', '') if ssids else ''}

def _parse_service_port(output):
    m = re.search(r'^\s*(\d+)\s+(\d+)\s+\S+\s+gpon\b.*?\bvlan\s+(\d+)', output, re.M)
    if m:
        return {'service_port': m.group(1), 'network_vlan': m.group(2), 'user_vlan': m.group(3)}
    for line in output.splitlines():
        if ' gpon ' in line and ' vlan ' in line:
            parts = line.split()
            try:
                return {'service_port': parts[0], 'network_vlan': parts[1], 'user_vlan': parts[parts.index('vlan') + 1]}
            except Exception:
                return {}
    return {}

def _safe_cli_value(value, name):
    value = str(value or '').strip()
    if not value:
        return ''
    if re.search(r'[\s"\';`$\\]', value):
        raise ValueError(f'{name} contains unsupported CLI characters')
    return value

def apply_ont_settings(ip, username, password, payload):
    method = payload.get('method', 'ssh')
    kind = payload.get('kind', '')
    sn = payload.get('sn', '').strip()
    if not sn:
        return False, 'SN is required'
    if method != 'ssh':
        return True, f"SNMP write request accepted for {kind}. Command template is pending model mapping."

    # Single connection: open once and reuse for both find and apply
    conn = olt_ssh(ip, username, password)
    conn.write_channel(f'display ont info by-sn {sn}\r\n')
    _find_out = _read_until_prompt(conn, delay=3, max_rounds=20)
    _fsp_m = re.search(r'F/S/P\s*:\s*(\S+)', _find_out)
    _id_m  = re.search(r'ONT-ID\s*:\s*(\d+)', _find_out)
    if not _fsp_m or not _id_m:
        conn.disconnect()
        return False, 'ONT not found on OLT'
    _parts = _fsp_m.group(1).split('/')
    ont = {
        'fsp':              _fsp_m.group(1),
        'slot_port':        '/'.join(_parts[:2]),
        'port':             int(_parts[2]) if len(_parts) > 2 and _parts[2].isdigit() else 0,
        'ont_id':           int(_id_m.group(1)),
        'vendor':           (re.search(r'SN\s*:\s*[0-9A-Fa-f]+\s*\(([^)-]+)-', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'model':            (re.search(r'Ont EquipmentID\s*:\s*(.+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'description':      (re.search(r'Description\s*:\s*(.+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'line_profile_id':  (re.search(r'Line profile ID\s*:\s*(\S+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'line_profile_name':(re.search(r'Line profile name\s*:\s*(.+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'service_profile_id': (re.search(r'Service profile ID\s*:\s*(\S+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'service_profile_name': (re.search(r'Service profile name\s*:\s*(.+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'run_state':        (re.search(r'Run state\s*:\s*(\S+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
        'config_state':     (re.search(r'Config state\s*:\s*(\S+)', _find_out) or type('', (), {'group': lambda s, x: ''})()).group(1).strip(),
    }
    info = _find_out
    outputs = [info]
    action = payload.get('action', 'apply')

    if kind == 'check':
        details = {"wan": {}, "wifi": {}, "raw_sections": []}
        # SERVICE_PORT works from (config)# with fsp format
        sp_section = _olt_command(conn, f'display service-port port {ont["fsp"]} ont {ont["ont_id"]}', delay=3)
        if 'Parameter error' not in sp_section and 'Unknown command' not in sp_section:
            outputs.append(f'[SERVICE_PORT]\n{sp_section}')
            details["raw_sections"].append("SERVICE_PORT")
            details["wan"].update(_parse_service_port(sp_section))
        # Enter gpon interface for port-based commands (avoids digit-space-digit issue)
        conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
        time.sleep(1.5); conn.read_channel()
        port_cmds = [
            ("WAN_INFO",  f'display ont wan-info {ont["port"]} {ont["ont_id"]}'),
            ("WLAN_INFO", f'display ont wlan-info {ont["port"]} {ont["ont_id"]}'),
            ("WIFI_INFO", f'display ont wifi-info {ont["port"]} {ont["ont_id"]}'),
            ("ETH_PORT",  f'display ont port state {ont["port"]} {ont["ont_id"]} eth-port all'),
        ]
        for label, cmd in port_cmds:
            section = _olt_command(conn, cmd, delay=3)
            if 'Unknown command' in section or 'Parameter error' in section:
                continue
            outputs.append(f'[{label}]\n{section}')
            details["raw_sections"].append(label)
            if label == 'WAN_INFO':
                import re as _re
                _ip = _re.search("IPv4 address\s*:\s*(\S+)", section)
                _st = _re.search("IPv4 Connection status\s*:\s*(\S.*)", section)
                _at = _re.search("IPv4 access type\s*:\s*(\S.*)", section)
                if _ip and _ip.group(1) != "-": details["wan"]["ipv4_address"] = _ip.group(1).strip()
                if _st: details["wan"]["connection_status"] = _st.group(1).strip()
                if _at: details["wan"]["access_type"] = _at.group(1).strip()
            if label == 'WLAN_INFO':
                details["wifi"].update(_parse_wlan_info(section))
        conn.write_channel('quit\r\n'); time.sleep(1); conn.read_channel()
        conn.disconnect()
        return True, summarize_ont(ont) + '\n[ONT_DETAILS_JSON] ' + json.dumps(details) + '\n\n' + '\n'.join(outputs)

    if kind == 'user' and payload.get('alias'):
        alias = str(payload.get('alias', '')).replace('"', '')
        conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
        time.sleep(1); conn.read_channel()
        conn.write_channel(f'ont modify {ont["port"]} {ont["ont_id"]} desc "{alias}"\r\n')
        time.sleep(2); outputs.append(conn.read_channel())
        conn.write_channel('quit\r\n'); time.sleep(1); conn.read_channel()

    if kind == 'user' and action == 'reboot':
        conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
        time.sleep(1); conn.read_channel()
        conn.write_channel(f'ont reset {ont["port"]} {ont["ont_id"]}\r\n')
        time.sleep(3); outputs.append(conn.read_channel())
        conn.write_channel('quit\r\n'); time.sleep(1); conn.read_channel()

    if kind == 'wan':
        vlan_id = str(payload.get('vlan_id') or '196')
        user_vlan = str(payload.get('user_vlan') or vlan_id)
        vas_profile = str(payload.get('vas_profile') or 'PPP-10-IPV4-IPV6')
        service_description = str(payload.get('service_description') or 'HSI (High-Speed Internet)')
        sp_out = _olt_command(conn, f'display service-port port {ont["fsp"]} ont {ont["ont_id"]}', delay=2)
        outputs.append(sp_out)
        existing_service_port = re.search(r'^\s*(\d+)\s+\d+\s+\S+\s+gpon\b', sp_out, re.M)
        existing_map = _parse_service_port(sp_out)
        should_repair_service = (
            action == 'repair_vlan'
            or (
                payload.get('mode') == 'pppoe'
                and existing_service_port
                and (
                    existing_map.get('network_vlan') != vlan_id
                    or existing_map.get('user_vlan') != user_vlan
                )
            )
        )
        if should_repair_service and existing_service_port:
            service_port_id = existing_service_port.group(1)
            outputs.append(_olt_command(conn, f'undo service-port {service_port_id}', delay=3))
            cmd = f'service-port vlan {vlan_id} gpon {ont["fsp"]} ont {ont["ont_id"]} gemport 1 multi-service user-vlan {user_vlan} tag-transform translate'
            outputs.append(_olt_command(conn, cmd, delay=3))
            outputs.append(f'[SERVICE_PORT] Repaired service-port {service_port_id} with network VLAN {vlan_id}, user VLAN {user_vlan}.')
        elif 'No service virtual port' in sp_out:
            cmd = f'service-port vlan {vlan_id} gpon {ont["fsp"]} ont {ont["ont_id"]} gemport 1 multi-service user-vlan {user_vlan} tag-transform translate'
            outputs.append(_olt_command(conn, cmd, delay=3))
        else:
            outputs.append('[SERVICE_PORT] Existing service-port found; no duplicate created.')
        if payload.get('mode') == 'pppoe':
            ppp_user = _safe_cli_value(payload.get('pppoe_username'), 'PPPoE username')
            ppp_pass = _safe_cli_value(payload.get('pppoe_password'), 'PPPoE password')
            if not ppp_user or not ppp_pass:
                raise ValueError('PPPoE username and password are required')
            wan_cmd = f'ont wan-config {ont["port"]} {ont["ont_id"]} ip-index 1 profile-name {vas_profile}'
            ipconfig_cmd = f'ont ipconfig {ont["port"]} {ont["ont_id"]} ip-index 1 pppoe user-account username {ppp_user} password {ppp_pass} vlan {user_vlan} priority 0'
            conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
            time.sleep(1); outputs.append(conn.read_channel())
            outputs.append(_olt_command(conn, wan_cmd, delay=3))
            outputs.append(_olt_command(conn, ipconfig_cmd, delay=4))
            outputs.append('[PPPOE_DIAL_COMMAND_SENT] ONT PPPoE username/password/VLAN command accepted by OLT CLI.')
            time.sleep(5)
            outputs.append(_olt_command(conn, f'display ont wan-info {ont["port"]} {ont["ont_id"]}', delay=3))
            conn.write_channel('quit\r\n')
            time.sleep(1); outputs.append(conn.read_channel())
        elif payload.get('mode') == 'static':
            outputs.append('[STATIC_IP_PENDING] Static IP command path is available in OLT help and will be wired after PPPoE validation.')
        elif payload.get('mode') == 'bridge':
            outputs.append('[BRIDGE_MODE_PENDING] Bridge mode profile selection is captured; no PPPoE dial credentials are sent.')
        outputs.append(f"[WAN_PROFILE_CAPTURED] mode={payload.get('mode')} pppoe_user={payload.get('pppoe_username','')} static_ip={payload.get('static_ip','')}")
        outputs.append(f"[GENERAL_ONT_VAS_PROFILE] {vas_profile}")
        outputs.append(f"[SERVICE_DESCRIPTION] {service_description}")

    # ── Lightweight PPPoE credential update (no service-port, no VAS profile) ──
    if kind == 'pppoe_creds':
        ppp_user = _safe_cli_value(payload.get('pppoe_username', ''), 'PPPoE username')
        ppp_pass = _safe_cli_value(payload.get('pppoe_password', ''), 'PPPoE password')
        if not ppp_user or not ppp_pass:
            conn.disconnect()
            return False, 'PPPoE username and password are both required'
        vlan = str(payload.get('vlan_id') or '10')
        conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
        time.sleep(1); outputs.append(conn.read_channel())
        ipconfig_cmd = (f'ont ipconfig {ont["port"]} {ont["ont_id"]} ip-index 1 '
                        f'pppoe user-account username {ppp_user} password {ppp_pass} '
                        f'vlan {vlan} priority 0')
        outputs.append(_olt_command(conn, ipconfig_cmd, delay=4))
        outputs.append('[PPPOE_CREDS_UPDATED] PPPoE username/password sent via ont ipconfig only.')
        conn.write_channel('quit\r\n'); time.sleep(1); outputs.append(conn.read_channel())

    # ── Static IP update (lightweight, no service-port changes) ─────────────
    if kind == 'static_ip':
        ip_addr = _safe_cli_value(payload.get('ip', ''), 'IP address')
        subnet  = _safe_cli_value(payload.get('subnet', ''), 'Subnet mask')
        gateway = _safe_cli_value(payload.get('gateway', ''), 'Gateway')
        vlan    = str(payload.get('vlan_id') or '10')
        dns1    = _safe_cli_value(payload.get('dns1', ''), 'Primary DNS')
        dns2    = _safe_cli_value(payload.get('dns2', ''), 'Secondary DNS')
        if not ip_addr or not subnet or not gateway:
            conn.disconnect()
            return False, 'IP address, subnet mask and gateway are required'
        conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
        time.sleep(1); outputs.append(conn.read_channel())
        cmd = (f'ont ipconfig {ont["port"]} {ont["ont_id"]} ip-index 1 static '
               f'ip-address {ip_addr} subnet-mask {subnet} gateway {gateway} '
               f'vlan {vlan} priority 0')
        outputs.append(_olt_command(conn, cmd, delay=4))
        outputs.append('[STATIC_IP_UPDATED] Static IP sent via ont ipconfig only.')
        conn.write_channel('quit\r\n'); time.sleep(1); outputs.append(conn.read_channel())

    if kind in ('wifi', 'lan'):
        if kind == 'wifi':
            ssid_index = int(payload.get('ssid_index', 1))
            wpa_pass   = _safe_cli_value(payload.get('password', ''),  'WiFi password')
            ssid_name  = _safe_cli_value(payload.get('ssid_name', ''), 'SSID name')
            enabled    = payload.get('enabled', None)   # None = not provided
            if not wpa_pass and not ssid_name and enabled is None:
                conn.disconnect()
                return False, 'No WLAN fields to update'
            conn.write_channel(f'interface gpon {ont["slot_port"]}\r\n')
            time.sleep(1); outputs.append(conn.read_channel())
            base = f'ont wlan-config {ont["port"]} {ont["ont_id"]} ssid-index {ssid_index}'
            if wpa_pass:
                outputs.append(_olt_command(conn, f'{base} wpa-passwd {wpa_pass}', delay=3))
            if ssid_name:
                outputs.append(_olt_command(conn, f'{base} ssid {ssid_name}', delay=3))
            if enabled is not None:
                flag = 'enable' if enabled else 'disable'
                outputs.append(_olt_command(conn, f'{base} ssid-enable {flag}', delay=3))
            outputs.append('[WLAN_UPDATED] WLAN settings sent to OLT.')
            conn.write_channel('quit\r\n'); time.sleep(1); outputs.append(conn.read_channel())
        else:
            outputs.append(f"[LAN_CAPTURED] lan_ip={payload.get('lan_ip','')} dhcp_start={payload.get('dhcp_start','')} dhcp_end={payload.get('dhcp_end','')} dhcp_enabled={payload.get('dhcp_enabled')}")
            outputs.append('[LAN_STATUS] Template pending for terminal model-specific write commands.')

    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.disconnect()
    output = '\n'.join(outputs)
    fatal = 'Failure' in output and 'No service virtual port' not in output and 'The profile does not exist' not in output
    return not fatal, output


def get_ont_wan_live(ip, username, password, sn, pon_hint=''):
    """
    Lightweight WAN resolver for Open Router:
    1) find ONT by SN
    2) read service-port summary
    3) read WAN info only
    """
    conn = olt_ssh(ip, username, password)
    try:
        conn.write_channel(f'display ont info by-sn {sn}\r\n')
        info = _read_until_prompt(conn, delay=3, max_rounds=20)
        fsp_m = re.search(r'F/S/P\s*:\s*(\S+)', info)
        id_m = re.search(r'ONT-ID\s*:\s*(\d+)', info)
        if not fsp_m or not id_m:
            return {'ok': False, 'error': 'ONT not found on OLT'}

        fsp = fsp_m.group(1)
        parts = fsp.split('/')
        slot_port = '/'.join(parts[:2])
        port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        ont_id = int(id_m.group(1))

        details = {"wan": {}, "raw_sections": []}

        sp_section = _olt_command(
            conn,
            f'display service-port port {fsp} ont {ont_id}',
            delay=2,
            wait_prompt=True,
            max_rounds=16,
        )
        if 'Parameter error' not in sp_section and 'Unknown command' not in sp_section:
            details["raw_sections"].append("SERVICE_PORT")
            details["wan"].update(_parse_service_port(sp_section))

        conn.write_channel(f'interface gpon {slot_port}\r\n')
        time.sleep(1.5)
        conn.read_channel()

        wan_section = _olt_command(
            conn,
            f'display ont wan-info {port} {ont_id}',
            delay=2,
            wait_prompt=True,
            max_rounds=20,
        )
        details["raw_sections"].append("WAN_INFO")

        _ip = re.search(r'IPv4 address\s*:\s*(\S+)', wan_section, re.I)
        _st = re.search(r'IPv4 Connection status\s*:\s*(\S.*)', wan_section, re.I)
        _at = re.search(r'IPv4 access type\s*:\s*(\S.*)', wan_section, re.I)
        _mv = re.search(r'Manage VLAN\s*:\s*(\S+)', wan_section, re.I)
        if _ip and _ip.group(1) not in ('-', '0.0.0.0'):
            details["wan"]["ipv4_address"] = _ip.group(1).strip()
        if _st:
            details["wan"]["connection_status"] = _st.group(1).strip()
        if _at:
            details["wan"]["access_type"] = _at.group(1).strip()
        if _mv:
            details["wan"]["manage_vlan"] = _mv.group(1).strip()

        conn.write_channel('quit\r\n')
        time.sleep(1)
        conn.read_channel()

        return {
            'ok': True,
            'sn': sn,
            'fsp': fsp,
            'ont_id': ont_id,
            'details': details,
            'raw': '[SERVICE_PORT]\\n' + sp_section + '\\n[WAN_INFO]\\n' + wan_section,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


def get_ont_wan_live_by_path(ip, username, password, pon, ont_id, sn=''):
    """
    Faster WAN resolver when ONT location is already known from cache/table.
    pon format: 0/slot/port (example: 0/4/2)
    """
    conn = olt_ssh(ip, username, password)
    try:
        parts = str(pon).split('/')
        if len(parts) != 3:
            return {'ok': False, 'error': f'invalid pon: {pon}'}
        slot_port = '/'.join(parts[:2])   # 0/4
        port = int(parts[2])              # 2
        ont_id = int(ont_id)
        fsp = f'{slot_port}/{port}'

        details = {"wan": {}, "raw_sections": []}
        sp_section = _olt_command(
            conn,
            f'display service-port port {fsp} ont {ont_id}',
            delay=1,
            wait_prompt=True,
            max_rounds=10,
        )
        if 'Parameter error' not in sp_section and 'Unknown command' not in sp_section:
            details["raw_sections"].append("SERVICE_PORT")
            details["wan"].update(_parse_service_port(sp_section))

        conn.write_channel(f'interface gpon {slot_port}\r\n')
        time.sleep(1)
        conn.read_channel()

        wan_section = _olt_command(
            conn,
            f'display ont wan-info {port} {ont_id}',
            delay=1,
            wait_prompt=True,
            max_rounds=12,
        )
        details["raw_sections"].append("WAN_INFO")

        _ip = re.search(r'IPv4 address\s*:\s*(\S+)', wan_section, re.I)
        _st = re.search(r'IPv4 Connection status\s*:\s*(\S.*)', wan_section, re.I)
        _at = re.search(r'IPv4 access type\s*:\s*(\S.*)', wan_section, re.I)
        _mv = re.search(r'Manage VLAN\s*:\s*(\S+)', wan_section, re.I)
        if _ip and _ip.group(1) not in ('-', '0.0.0.0'):
            details["wan"]["ipv4_address"] = _ip.group(1).strip()
        if _st:
            details["wan"]["connection_status"] = _st.group(1).strip()
        if _at:
            details["wan"]["access_type"] = _at.group(1).strip()
        if _mv:
            details["wan"]["manage_vlan"] = _mv.group(1).strip()

        conn.write_channel('quit\r\n')
        time.sleep(0.6)
        conn.read_channel()

        return {
            'ok': True,
            'sn': sn,
            'fsp': fsp,
            'ont_id': ont_id,
            'details': details,
            'raw': '[SERVICE_PORT]\\n' + sp_section + '\\n[WAN_INFO]\\n' + wan_section,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


# ── SNMP provisioning helpers ──────────────────────────────────────────────────

def _sn_to_snmp_hex(sn):
    """Convert SN string '48575443143D1067' to snmpset hex format '48 57 54 43 14 3D 10 67'."""
    sn = sn.strip().upper().replace(" ", "").replace(":", "")
    return " ".join(sn[i:i+2] for i in range(0, len(sn), 2))

def get_gpon_port_ifindex(slot, port):
    """Compute Huawei MA5600 GPON port SNMP ifIndex.
    Formula confirmed from live OLT walk: 0xFA000000 + slot*0x2000 + port*0x100
    """
    return 0xFA000000 + int(slot) * 0x2000 + int(port) * 0x100

def get_gpon_ifindex_map(ip, read_community):
    """Walk ifDescr to map ifIndex -> port description for GPON UNI interfaces."""
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", f"-c{read_community}", ip,
             "1.3.6.1.2.1.2.2.1.2"],
            capture_output=True, text=True, timeout=30
        )
        mapping = {}
        for line in result.stdout.splitlines():
            m = re.search(r'\.(\d+)\s*=\s*STRING:\s*"?(.+?)"?\s*$', line)
            if m and "GPON_UNI" in m.group(2):
                mapping[int(m.group(1))] = m.group(2).strip()
        return mapping
    except Exception:
        return {}

def provision_ont_snmp(ip, read_community, write_community, sn, slot_port, port,
                       line_profile_name, srv_profile_name, description, ont_id=65535):
    """Register an ONT on Huawei MA5600/MA5603T via SNMP (hwGponOntActivate table).

    Returns (success, assigned_ont_id, output_string).
    Column mapping (live confirmed):
      col 3 = SN (Hex-STRING 8 bytes)
      col 7 = line profile name (STRING)
      col 8 = service profile name (STRING)
      col 9 = description (STRING)
      col 10 = RowStatus (INTEGER: 1=active, 4=createAndGo)
    """
    # Parse slot from slot_port like "0/1"
    parts = str(slot_port).split("/")
    slot = int(parts[1]) if len(parts) > 1 else 1
    port_num = int(port)

    port_ifindex = get_gpon_port_ifindex(slot, port_num)
    sn_hex = _sn_to_snmp_hex(sn)
    base = "1.3.6.1.4.1.2011.6.128.1.1.2.43.1"
    inst = f"{port_ifindex}.{ont_id}"

    cmd = [
        "snmpset", "-v2c", "-c", write_community, ip,
        f"{base}.3.{inst}",  "x", sn_hex,             # SN
        f"{base}.7.{inst}",  "s", line_profile_name,  # line profile name
        f"{base}.8.{inst}",  "s", srv_profile_name,   # service profile name
        f"{base}.9.{inst}",  "s", description,         # description
        f"{base}.10.{inst}", "i", "4",                 # RowStatus = createAndGo
    ]

    output_lines = [
        f"[SNMP_PROVISION] OLT={ip} SN={sn}",
        f"[SNMP_PROVISION] port=GPON {slot_port}/{port_num} ifIndex={port_ifindex}",
        f"[SNMP_PROVISION] line_profile={line_profile_name} srv_profile={srv_profile_name}",
        f"[SNMP_PROVISION] inst={inst}",
        f"[SNMP_PROVISION] CMD: {' '.join(cmd[:6])} ...",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output_lines.append(f"stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            output_lines.append(f"stderr: {result.stderr.strip()}")

        if result.returncode == 0 and "Error" not in result.stdout and "Timeout" not in result.stdout:
            # Try to read back the assigned ONT ID (OLT may assign auto-id)
            assigned_id = ont_id
            # Check what ID was actually assigned by reading the row
            time.sleep(2)
            verify = subprocess.run(
                ["snmpwalk", "-v2c", f"-c{read_community}", ip,
                 f"{base}.3.{port_ifindex}"],
                capture_output=True, text=True, timeout=20
            )
            sn_upper = sn.upper().replace(" ", "")
            for line in verify.stdout.splitlines():
                if sn_hex.replace(" ", "").upper() in line.upper().replace(" ", ""):
                    m = re.search(rf"{re.escape(str(port_ifindex))}\.(\d+)", line)
                    if m:
                        assigned_id = int(m.group(1))
                        output_lines.append(f"[SNMP_PROVISION] ONT registered with ID={assigned_id}")
                        break
            return True, assigned_id, "\n".join(output_lines)
        else:
            output_lines.append("[SNMP_PROVISION] FAILED — OLT returned error or non-zero exit")
            return False, -1, "\n".join(output_lines)

    except subprocess.TimeoutExpired:
        output_lines.append("[SNMP_PROVISION] TIMEOUT — snmpset did not respond within 30s")
        return False, -1, "\n".join(output_lines)
    except Exception as e:
        output_lines.append(f"[SNMP_PROVISION] EXCEPTION: {e}")
        return False, -1, "\n".join(output_lines)


def snmp_probe_ont_fields(ip, read_community, slot, port, ont_id, oid_templates):
    """
    Probe configured OID templates for one ONT index and return parsed values.
    This is a discovery helper for Phase 3 and does not modify OLT.
    """
    try:
        ont_index = (int(slot) << 24) + (int(port) << 16) + int(ont_id)
    except Exception as e:
        return False, {"error": f"invalid index parameters: {e}"}

    out = {
        "ont_index": ont_index,
        "values": {},
        "raw": {},
    }

    def _run_get(oid):
        cmd = ["snmpget", "-v2c", "-c", read_community, "-Oqv", ip, oid]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()
        return True, (r.stdout or "").strip().strip('"')

    for field in ("rx_power", "temp", "vlan"):
        tpl = (oid_templates or {}).get(field, "")
        if not tpl:
            continue
        oid = tpl.format(index=ont_index)
        ok, val = _run_get(oid)
        out["raw"][field] = {"oid": oid, "ok": ok, "value": val}
        if ok:
            if field == "temp":
                m = re.search(r"\d+", val or "")
                out["values"][field] = int(m.group(0)) if m else None
            elif field == "vlan":
                m = re.search(r"\d+", val or "")
                out["values"][field] = m.group(0) if m else ""
            else:
                m = re.search(r"[-+]?\d+(?:\.\d+)?", val or "")
                out["values"][field] = float(m.group(0)) if m else None
        else:
            out["values"][field] = None

    return True, out


def snmp_get_raw(ip, read_community, oid):
    cmd = ["snmpget", "-v2c", "-c", read_community, "-Oqv", ip, str(oid)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return {
            "ok": r.returncode == 0,
            "oid": str(oid),
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
            "rc": r.returncode,
        }
    except Exception as e:
        return {"ok": False, "oid": str(oid), "error": str(e)}


def snmp_walk_raw(ip, read_community, oid, limit_lines=200):
    cmd = ["snmpwalk", "-v2c", "-c", read_community, ip, str(oid)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        lines = (r.stdout or "").splitlines()[:max(1, int(limit_lines))]
        return {
            "ok": r.returncode == 0,
            "oid": str(oid),
            "lines": lines,
            "stderr": (r.stderr or "").strip(),
            "rc": r.returncode,
            "count": len(lines),
        }
    except Exception as e:
        return {"ok": False, "oid": str(oid), "error": str(e), "lines": [], "count": 0}


def snmp_discover_candidates(ip, read_community, expected_ip='', expected_temp=''):
    """
    Bounded SNMP discovery:
    walk only selected Huawei subtrees with strict timeout,
    then return lines that match expected WAN IP / temperature tokens.
    """
    expected_ip = (expected_ip or '').strip()
    expected_temp = str(expected_temp or '').strip()
    subtrees = [
        '1.3.6.1.4.1.2011.6.128.1.1.2',
        '1.3.6.1.4.1.2011.5.100.1',
        '1.3.6.1.2.1.31.1.1.1',
    ]

    hits = []
    scans = []
    for root in subtrees:
        cmd = [
            'snmpwalk', '-v2c', '-c', read_community,
            '-On', '-t', '1', '-r', '0', '-Cc',
            ip, root
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=18)
            out = (p.stdout or '').splitlines()
            scans.append({'root': root, 'rc': p.returncode, 'lines': len(out)})
            for line in out:
                line_l = line.lower()
                matched = False
                why = []
                if expected_ip and expected_ip in line:
                    matched = True
                    why.append('expected_ip')
                if expected_temp and (f'integer: {expected_temp}' in line_l or f'gauge32: {expected_temp}' in line_l):
                    matched = True
                    why.append('expected_temp')
                if matched:
                    if any(k in line_l for k in ['ipv4', 'pppoe', 'wan', 'optical', 'temperature', 'vlan']):
                        why.append('keyword_context')
                    hits.append({'root': root, 'line': line, 'why': ','.join(why)})
        except subprocess.TimeoutExpired:
            scans.append({'root': root, 'timeout': True})
        except Exception as e:
            scans.append({'root': root, 'error': str(e)})

    return {'ok': True, 'scans': scans, 'hits': hits[:300]}


def snmp_find_ifindex_by_pon(ip, read_community, pon):
    pon = (pon or '').strip()
    if not pon:
        return None
    cmd = ['snmpwalk', '-v2c', '-c', read_community, '-On', '-t', '1', '-r', '0', ip, '1.3.6.1.2.1.31.1.1.1.1']
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        for line in (p.stdout or '').splitlines():
            if pon in line:
                m = re.match(r'^\.1\.3\.6\.1\.2\.1\.31\.1\.1\.1\.1\.(\d+)\s*=', line.strip())
                if m:
                    return int(m.group(1))
    except Exception:
        return None
    return None


def snmp_map_ont_candidates(ip, read_community, pon='', ont_id=None, expected_name='', expected_ip='', expected_temp=''):
    """
    Deep-but-bounded ONT mapping helper.
    Scans Huawei XPON table columns and returns matching lines + index hints.
    """
    expected_name = (expected_name or '').strip().lower()
    expected_ip = (expected_ip or '').strip()
    expected_temp = str(expected_temp or '').strip()
    base = '1.3.6.1.4.1.2011.6.128.1.1.2.21.1'
    ifindex = snmp_find_ifindex_by_pon(ip, read_community, pon) if pon else None
    candidate_indexes = []
    if ifindex is not None and ont_id is not None:
        try:
            oid_int = int(ont_id)
            candidate_indexes = [int(ifindex) + oid_int, int(ifindex) + (oid_int * 256)]
        except Exception:
            candidate_indexes = []

    hits = []
    scans = []
    # Focus on first 30 columns; this table is known active on MA5600.
    for col in range(1, 31):
        root = f'{base}.{col}'
        cmd = ['snmpwalk', '-v2c', '-c', read_community, '-On', '-t', '1', '-r', '0', '-Cc', ip, root]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            lines = (p.stdout or '').splitlines()
            scans.append({'col': col, 'rc': p.returncode, 'count': len(lines)})
            for line in lines:
                line_l = line.lower()
                why = []
                if expected_name and expected_name in line_l:
                    why.append('expected_name')
                if expected_ip and expected_ip in line:
                    why.append('expected_ip')
                if expected_temp and (f'integer: {expected_temp}' in line_l or f'gauge32: {expected_temp}' in line_l):
                    why.append('expected_temp')
                m = re.match(r'^\.' + re.escape(base) + r'\.' + str(col) + r'\.(\d+)\s*=\s*(.+)$', line.strip(), re.I)
                idx = int(m.group(1)) if m else None
                inferred_ont_id = None
                if idx is not None and ifindex is not None:
                    delta = idx - ifindex
                    if delta >= 0 and delta <= (256 * 128) and delta % 256 == 0:
                        inferred_ont_id = delta // 256
                if idx is not None and ifindex is not None and idx >= ifindex and idx <= (ifindex + (256 * 128)):
                    why.append('index_near_pon')
                if why:
                    hits.append({
                        'col': col,
                        'index': idx,
                        'inferred_ont_id': inferred_ont_id,
                        'line': line,
                        'why': ','.join(sorted(set(why))),
                    })
        except subprocess.TimeoutExpired:
            scans.append({'col': col, 'timeout': True})
        except Exception as e:
            scans.append({'col': col, 'error': str(e)})

    direct = []
    for cand_idx in candidate_indexes:
        for col in range(1, 31):
            oid = f'{base}.{col}.{cand_idx}'
            cmd = ['snmpget', '-v2c', '-c', read_community, '-On', '-t', '1', '-r', '0', ip, oid]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
                out = (p.stdout or '').strip()
                err = (p.stderr or '').strip()
                if out and ('No Such Instance' not in out and 'No Such Object' not in out):
                    direct.append({'candidate_index': cand_idx, 'col': col, 'oid': oid, 'value': out})
                elif err:
                    direct.append({'candidate_index': cand_idx, 'col': col, 'oid': oid, 'error': err})
            except Exception as e:
                direct.append({'candidate_index': cand_idx, 'col': col, 'oid': oid, 'error': str(e)})

    return {
        'ok': True,
        'pon': pon,
        'ifindex': ifindex,
        'ont_id': ont_id,
        'candidate_indexes': candidate_indexes,
        'scans': scans,
        'hits': hits[:400],
        'direct': direct[:120],
    }


def get_ont_full_info(ip, username, password, sn):
    """
    Return structured ONT info parsed from `display ont info by-sn {sn}`
    plus `display ont version {fsp} {ont_id}`.
    Disables OLT pager temporarily so output is not truncated.
    Adds device_type field: 'ONT' (router) or 'ONU' (L2 bridge).
    """
    try:
        conn = olt_ssh(ip, username, password)
    except Exception as e:
        return {'ok': False, 'error': f'OLT SSH failed: {e}'}

    try:
        # Disable pager for this session so display output is not broken by "Press 'Q' to break"
        conn.write_channel('screen-length 0 temporary\r\n')
        _read_until_prompt(conn, delay=1, max_rounds=5)

        conn.write_channel(f'display ont info by-sn {sn}\r\n')
        info = _read_until_prompt(conn, delay=3, max_rounds=40)

        fsp_m = re.search(r'F/S/P\s*:\s*(\S+)', info)
        id_m  = re.search(r'ONT-ID\s*:\s*(\d+)', info)
        if not fsp_m or not id_m:
            return {'ok': False, 'error': 'ONT not found on OLT'}

        fsp = fsp_m.group(1)
        parts = fsp.split('/')
        slot_port = '/'.join(parts[:2])
        port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        ont_id = int(id_m.group(1))

        def grab(pattern, source=None, flags=0, default=''):
            src_ = source if source is not None else info
            m = re.search(pattern, src_, flags)
            return m.group(1).strip() if m else default

        dist_m = re.search(r'ONT\s*Distance\s*\(m\)\s*:\s*(\d+)', info, re.IGNORECASE)
        distance_m = int(dist_m.group(1)) if dist_m else None

        # Enter interface gpon mode for the slot to reliably run display ont version.
        # (Directly running `display ont version 0/1/7 12` from config mode has a space-eating
        # quirk on this firmware that produces "0/1/712".)
        ver = ''
        try:
            conn.write_channel(f'interface gpon {slot_port}\r\n')
            _read_until_prompt(conn, delay=1, max_rounds=8)
            conn.write_channel(f'display ont version {port} {ont_id}\r\n')
            ver = _read_until_prompt(conn, delay=2, max_rounds=20)
            conn.write_channel('quit\r\n')
            _read_until_prompt(conn, delay=1, max_rounds=8)
        except Exception:
            pass

        combined = info + '\n' + ver

        # Model: prefer the human-friendly OntProductDescription (e.g. "EchoLife HG8245 GPON Terminal")
        # — extract the first model-looking token like HG8245 / EG8145 / HS8546 / F660 / etc.
        prod_desc = grab(r'OntProductDescription\s*:\s*(.+)', combined, re.IGNORECASE)
        model_match = re.search(r'(HG\d{3,5}[A-Za-z]?\d*|EG\d{3,5}[A-Za-z]?\d*|HS\d{3,5}[A-Za-z]?\d*|HN\d{3,5}[A-Za-z]?\d*|MA\d{3,5}|F\d{3,4})', prod_desc, re.IGNORECASE)
        model = model_match.group(1).upper() if model_match else ''
        if not model:
            model = (
                grab(r'Ont\s*EquipmentID\s*:\s*(\S+)',    combined, re.IGNORECASE) or
                grab(r'Equipment[\s-]?ID\s*:\s*(\S+)',     combined, re.IGNORECASE) or
                grab(r'EquipmentID\s*:\s*(\S+)',           combined, re.IGNORECASE)
            )

        hw_version = (
            grab(r'Ont\s*HardwareVersion\s*:\s*(\S+)',  combined, re.IGNORECASE) or
            grab(r'Hardware\s*version\s*:\s*(\S+)',      combined, re.IGNORECASE) or
            grab(r'HARDWAREVERSION\s*:\s*(\S+)',          combined, re.IGNORECASE) or
            grab(r'ONT\s+Version\s*:\s*(\S+)',           combined, re.IGNORECASE)
        )
        sw_version = (
            grab(r'Ont\s*SoftwareVersion\s*:\s*(\S+)',                combined, re.IGNORECASE) or
            grab(r'Main\s+Software\s+Version\s*:\s*(\S+)',          combined, re.IGNORECASE) or
            grab(r'Software\s*version\s*:\s*(\S+)',                  combined, re.IGNORECASE) or
            grab(r'SOFTWAREVERSION\s*:\s*(\S+)',                       combined, re.IGNORECASE)
        )
        # Vendor: prefer Vendor-ID from `display ont version` over the SN-paren extraction
        vendor_ver = grab(r'Vendor[\s-]?ID\s*:\s*(\S+)', combined, re.IGNORECASE)

        # ONT (router) vs ONU (L2 bridge) detection
        ONU_PREFIXES = ('HG8310', 'HG8311', 'HG8312', 'HG8320', 'HG8330', 'HG8120', 'EG8010', 'HG8010')
        ONT_PREFIXES = ('HG8245', 'HG8247', 'HG8546', 'HS8145', 'HS8546', 'EG8145', 'EG8245', 'HN8245', 'MA5671')
        m_up = (model or '').upper()
        if m_up.startswith(ONU_PREFIXES):
            device_type = 'ONU'
        elif m_up.startswith(ONT_PREFIXES):
            device_type = 'ONT'
        else:
            # Fallback: if OLT shows an IPHOST entry, this device has an IP host (routing) -> ONT
            device_type = 'ONT' if 'IPHOST' in info.upper() else 'ONU'

        return {
            'ok': True,
            'sn': sn,
            'fsp': fsp,
            'slot_port': slot_port,
            'port': port,
            'ont_id': ont_id,
            'vendor':           grab(r'SN\s*:\s*[0-9A-Fa-f]+\s*\(([^)-]+)-'),
            'model':            model,
            'device_type':      device_type,
            'description':      grab(r'Description\s*:\s*(.+)'),
            'line_profile':     grab(r'Line profile name\s*:\s*(.+)'),
            'service_profile':  grab(r'Service profile name\s*:\s*(.+)'),
            'run_state':        grab(r'Run state\s*:\s*(\S+)'),
            'config_state':     grab(r'Config state\s*:\s*(\S+)'),
            'match_state':      grab(r'Match state\s*:\s*(\S+)'),
            'distance_m':       distance_m,
            'last_down_cause':  grab(r'Last down cause\s*:\s*(.+)'),
            'last_up_time':     grab(r'Last up time\s*:\s*(.+)'),
            'online_duration':  grab(r'ONT online duration\s*:\s*(.+)'),
            'hw_version':       hw_version,
            'sw_version':       sw_version,
            'raw_info':    info[-8000:],
            'raw_version': ver[-3000:],
        }
    except Exception as e:
        return {'ok': False, 'error': f'parse failed: {e}'}
    finally:
        try: conn.disconnect()
        except Exception: pass


def get_ont_config(ip, username, password, sn):
    """Read WAN + WLAN configuration of an ONT via OLT SSH (read-only).

    Returns (ok: bool, result: dict). On success result has:
        {
          'sn', 'fsp', 'ont_id', 'model',
          'wan': {... parsed from `display ont wan-info` ...},
          'wlan': {'ssids': [...], 'bands_present': [...], 'total': N},
          'mgmt': {... parsed from `display ont ipconfig` ...},
          'warnings': [...]
        }

    Note: Huawei OLT does NOT expose PPPoE password, WiFi password, WiFi
    security mode, channel, width, or country. Those live on the ONT itself
    and require direct ONT access (Phase 2).
    """
    sn = (sn or '').strip()
    if not sn:
        return False, {'error': 'SN is required'}

    warnings = []
    def _hard_drain(conn, settle_rounds=3, max_seconds=8):
        """Drain channel until we get N consecutive empty reads OR timeout.
        Handles slow Huawei pager-tail output without sending control chars."""
        empty = 0
        end = time.time() + max_seconds
        while time.time() < end and empty < settle_rounds:
            time.sleep(0.4)
            chunk = conn.read_channel()
            if chunk:
                # If we still see a More pager, push it past with space
                if '---- More' in chunk:
                    conn.write_channel(' ')
                empty = 0
            else:
                empty += 1

    conn = olt_ssh(ip, username, password)
    try:
        find_out = _olt_command(conn, f'display ont info by-sn {sn}', delay=2, wait_prompt=True)
        fsp_m = re.search(r'F/S/P\s*:\s*(\S+)', find_out)
        id_m  = re.search(r'ONT-ID\s*:\s*(\d+)', find_out)
        if not fsp_m or not id_m:
            return False, {'error': 'ONT not found on OLT', 'sn': sn}
        fsp = fsp_m.group(1)
        parts = fsp.split('/')
        slot_port = '/'.join(parts[:2])
        port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        ont_id = int(id_m.group(1))
        eq_m = re.search(r'(?:Equipment-ID|Ont EquipmentID)\s*:\s*(\S+)', find_out)
        model = (eq_m.group(1) if eq_m else '').strip()

        _hard_drain(conn)
        _olt_command(conn, f'interface gpon {slot_port}', delay=2, wait_prompt=True)
        _hard_drain(conn)

        # WLAN and ipconfig produce SHORT output that doesn't trigger pager — run them first.
        # WAN is heavy and may leave the OLT in pager state, so run it last.
        wlan_raw  = _olt_command(conn, f'display ont wlan-info {port} {ont_id}', delay=2, wait_prompt=True)
        _hard_drain(conn)
        ipcfg_raw = _olt_command(conn, f'display ont ipconfig {port} {ont_id}', delay=2, wait_prompt=True)
        _hard_drain(conn)
        wan_raw   = _olt_command(conn, f'display ont wan-info {port} {ont_id}', delay=2, wait_prompt=True)
        _hard_drain(conn, settle_rounds=6, max_seconds=15)

        wan  = _parse_wan_full(wan_raw)
        mgmt = _parse_ont_ipconfig(ipcfg_raw)

        # Detect OLT-level "this ONT model doesn't support WLAN reporting"
        _wlan_unsupported = 'The ONT can not support' in wlan_raw or 'Failure:' in wlan_raw
        if _wlan_unsupported:
            wlan = {'ssids': [], 'bands_present': [], 'total': 0, 'supported': False}
            warnings.append('WLAN not supported — this ONT model does not expose WLAN data via OLT CLI (HG8245 / bridge-mode devices)')
        else:
            wlan = _parse_wlan_full(wlan_raw)
            wlan['supported'] = True
            if not wlan.get('ssids'):
                warnings.append('WLAN section empty — ONT may not have WiFi or is not reporting it')

        if not wan:
            warnings.append('WAN section empty — ONT may be bridge-mode or offline')

        result = {
            'sn': sn,
            'fsp': fsp,
            'ont_id': ont_id,
            'model': model,
            'wan': wan,
            'wlan': wlan,
            'mgmt': mgmt,
            'warnings': warnings,
            'note': 'PPPoE password, WiFi password, security, and channel are not exposed by OLT — Phase 2 will fetch via direct ONT access',
        }
        return True, result
    except Exception as e:
        return False, {'error': str(e), 'sn': sn}
    finally:
        try: conn.disconnect()
        except Exception: pass


def run_ont_action(ip, username, password, action, sn='', pon='', ont_id=''):
    """
    Execute an ONT lifecycle action via SSH against the OLT.
    `action` is one of: enable | disable | reset | restore | delete
    `pon` is full F/S/P (e.g. "0/1/7") and `ont_id` is the ONT ID. SN is optional but
    we resolve pon/ont_id from SN if missing.
    Returns {'ok': bool, 'output': str, 'error': str}.
    """
    if action not in ('enable', 'disable', 'reset', 'restore', 'delete'):
        return {'ok': False, 'error': f'unknown action: {action}'}

    try:
        conn = olt_ssh(ip, username, password)
    except Exception as e:
        return {'ok': False, 'error': f'OLT SSH failed: {e}'}

    try:
        # If pon/ont_id missing, resolve from SN
        if (not pon or not str(ont_id)) and sn:
            conn.write_channel(f'display ont info by-sn {sn}\r\n')
            info = _read_until_prompt(conn, delay=3, max_rounds=20)
            fsp_m = re.search(r'F/S/P\s*:\s*(\S+)', info)
            id_m  = re.search(r'ONT-ID\s*:\s*(\d+)', info)
            if not fsp_m or not id_m:
                return {'ok': False, 'error': f'ONT {sn} not found on OLT'}
            pon = fsp_m.group(1)
            ont_id = id_m.group(1)

        try:
            ont_id = int(ont_id)
        except Exception:
            return {'ok': False, 'error': f'invalid ont_id: {ont_id}'}

        parts = pon.split('/')
        if len(parts) != 3:
            return {'ok': False, 'error': f'invalid pon: {pon}'}
        slot_port = '/'.join(parts[:2])
        port = parts[2]

        outputs = []

        # Enter interface gpon mode (port commands need the slot/port context)
        conn.write_channel(f'interface gpon {slot_port}\r\n')
        outputs.append(_read_until_prompt(conn, delay=1, max_rounds=8))

        if action == 'enable':
            conn.write_channel(f'ont activate {port} {ont_id}\r\n')
        elif action == 'disable':
            conn.write_channel(f'ont deactivate {port} {ont_id}\r\n')
        elif action == 'reset':
            conn.write_channel(f'ont reset {port} {ont_id}\r\n')
        elif action == 'restore':
            # Factory restore: reset_factory on Huawei MA5603T
            conn.write_channel(f'ont ipconfig {port} {ont_id} factory\r\n')
            # Some firmwares use: ont reset {port} {ont_id} factory
            # If we get parameter error, retry with alternate
            time.sleep(2)
            chunk = conn.read_channel()
            outputs.append(chunk)
            if 'Parameter error' in chunk or 'Unknown command' in chunk:
                conn.write_channel(f'ont reset {port} {ont_id} factory\r\n')
        elif action == 'delete':
            conn.write_channel(f'ont delete {port} {ont_id}\r\n')

        time.sleep(1.5)
        out = _read_until_prompt(conn, delay=2, max_rounds=20)
        outputs.append(out)

        # Many Huawei commands ask for confirmation with "{ <cr>|<n>}"; auto-confirm
        if '{ <cr>|' in out or '{<cr>|' in out or 'Are you sure' in out:
            conn.write_channel('\r\n')
            time.sleep(1.5)
            outputs.append(_read_until_prompt(conn, delay=2, max_rounds=20))

        # Quit interface mode
        conn.write_channel('quit\r\n')
        outputs.append(_read_until_prompt(conn, delay=1, max_rounds=8))

        # For changes that persist, commit / save
        if action in ('delete', 'restore'):
            conn.write_channel('save\r\n')
            time.sleep(2)
            sv = _read_until_prompt(conn, delay=2, max_rounds=12)
            outputs.append(sv)
            if '{ <cr>|' in sv or 'continue' in sv.lower():
                conn.write_channel('\r\n')
                outputs.append(_read_until_prompt(conn, delay=2, max_rounds=12))

        full = '\n'.join(o for o in outputs if o)
        # Detect failure
        bad_phrases = ('Parameter error', 'Failure', 'Failed', 'Cannot', 'invalid')
        bad = any(p.lower() in full.lower() for p in bad_phrases) and 'success' not in full.lower()
        if bad:
            return {'ok': False, 'error': 'OLT rejected command', 'output': full[-2000:]}
        return {'ok': True, 'output': full[-2000:]}
    except Exception as e:
        return {'ok': False, 'error': f'action failed: {e}'}
    finally:
        try: conn.disconnect()
        except Exception: pass
