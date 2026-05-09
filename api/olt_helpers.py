import subprocess, json, re, time

def get_olts():
    import auth_db
    conn = auth_db.get_db()
    rows = conn.execute("SELECT id,name,ip,username,password,snmp_community,model,active,created_at FROM olts").fetchall()
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
    allowed = ["name","ip","username","password","snmp_community","model","active"]
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
                banner_timeout=30, auth_timeout=30)
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

def find_ont_by_sn(ip, username, password, sn):
    conn = olt_ssh(ip, username, password)
    conn.write_channel(f'display ont info by-sn {sn}\r\n')
    time.sleep(4)
    out = conn.read_channel()
    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.disconnect()
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

def apply_ont_settings(ip, username, password, payload):
    method = payload.get('method', 'ssh')
    kind = payload.get('kind', '')
    sn = payload.get('sn', '').strip()
    if not sn:
        return False, 'SN is required'
    if method != 'ssh':
        return True, f"SNMP write request accepted for {kind}. Command template is pending model mapping."

    ont, info = find_ont_by_sn(ip, username, password, sn)
    if not ont:
        return False, 'ONT not found on OLT'

    conn = olt_ssh(ip, username, password)
    outputs = [info]
    action = payload.get('action', 'apply')

    if kind == 'check':
        conn.write_channel(f'display service-port port {ont["fsp"]} ont {ont["ont_id"]}\r\n')
        time.sleep(2); outputs.append(conn.read_channel())
        conn.write_channel('quit\r\n'); time.sleep(1)
        conn.disconnect()
        return True, summarize_ont(ont) + '\n\n' + '\n'.join(outputs)

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
        vlan_id = str(payload.get('vlan_id') or '10')
        user_vlan = str(payload.get('user_vlan') or vlan_id)
        vas_profile = str(payload.get('vas_profile') or 'PPP-10-IPV4-IPV6')
        service_description = str(payload.get('service_description') or 'HSI (High-Speed Internet)')
        conn.write_channel(f'display service-port port {ont["fsp"]} ont {ont["ont_id"]}\r\n')
        time.sleep(2); sp_out = conn.read_channel()
        outputs.append(sp_out)
        existing_service_port = re.search(r'^\s*(\d+)\s+\d+\s+\S+\s+gpon\b', sp_out, re.M)
        if action == 'repair_vlan' and existing_service_port:
            service_port_id = existing_service_port.group(1)
            conn.write_channel(f'undo service-port {service_port_id}\r\n')
            time.sleep(3); outputs.append(conn.read_channel())
            cmd = f'service-port vlan {vlan_id} gpon {ont["fsp"]} ont {ont["ont_id"]} gemport 1 multi-service user-vlan {user_vlan} tag-transform translate'
            conn.write_channel(cmd + '\r\n')
            time.sleep(3); outputs.append(conn.read_channel())
            outputs.append(f'[SERVICE_PORT] Repaired service-port {service_port_id} with network VLAN {vlan_id}, user VLAN {user_vlan}.')
        elif 'No service virtual port' in sp_out:
            cmd = f'service-port vlan {vlan_id} gpon {ont["fsp"]} ont {ont["ont_id"]} gemport 1 multi-service user-vlan {user_vlan} tag-transform translate'
            conn.write_channel(cmd + '\r\n')
            time.sleep(3); outputs.append(conn.read_channel())
        else:
            outputs.append('[SERVICE_PORT] Existing service-port found; no duplicate created.')
        outputs.append(f"[WAN_PROFILE_CAPTURED] mode={payload.get('mode')} pppoe_user={payload.get('pppoe_username','')} static_ip={payload.get('static_ip','')}")
        outputs.append(f"[GENERAL_ONT_VAS_PROFILE] {vas_profile}")
        outputs.append(f"[SERVICE_DESCRIPTION] {service_description}")

    if kind in ('wifi', 'lan'):
        outputs.append(f"[{kind.upper()}_CAPTURED] Template pending for terminal model-specific write commands.")

    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.disconnect()
    output = '\n'.join(outputs)
    return 'Failure' not in output, output
