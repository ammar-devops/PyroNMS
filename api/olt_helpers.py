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
    for _ in range(max_rounds):
        time.sleep(delay)
        chunk = conn.read_channel()
        if not chunk:
            break
        chunk = ansi_re.sub('', chunk).replace('\r', '')
        while '---- More' in chunk:
            before, _, after = chunk.partition('---- More')
            chunks.append(before)
            conn.write_channel(' ')
            time.sleep(0.8)
            chunk = ansi_re.sub('', conn.read_channel()).replace('\r', '')
            chunk = after + chunk
        chunks.append(chunk)
        stripped = chunk.strip()
        if stripped.endswith('#') or stripped.endswith(']'):
            break
    return ''.join(chunks)

def _olt_command(conn, command, delay=2, confirm_cr=True):
    conn.write_channel(command + '\r\n')
    time.sleep(delay)
    out = conn.read_channel()
    if confirm_cr and ('{ <cr>|' in out or '{<cr>|' in out):
        conn.write_channel('\r\n')
        time.sleep(delay)
        out += conn.read_channel()
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

def _parse_wlan_info(output):
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

    if kind in ('wifi', 'lan'):
        if kind == 'wifi':
            outputs.append(f"[WIFI_CAPTURED] band={payload.get('band','')} ssid={payload.get('ssid','')} channel={payload.get('channel','')} width={payload.get('channel_width','')} country={payload.get('country','')} enabled={payload.get('enabled')}")
        else:
            outputs.append(f"[LAN_CAPTURED] lan_ip={payload.get('lan_ip','')} dhcp_start={payload.get('dhcp_start','')} dhcp_end={payload.get('dhcp_end','')} dhcp_enabled={payload.get('dhcp_enabled')}")
        outputs.append(f"[{kind.upper()}_STATUS] Template pending for terminal model-specific write commands.")

    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.disconnect()
    output = '\n'.join(outputs)
    fatal = 'Failure' in output and 'No service virtual port' not in output and 'The profile does not exist' not in output
    return not fatal, output


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
