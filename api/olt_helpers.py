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
    conn = ConnectHandler(device_type='terminal_server', host=ip,
        username=username, password=password, timeout=60)
    conn.write_channel('enable\r\n'); time.sleep(2); conn.read_channel()
    conn.write_channel('config\r\n'); time.sleep(2); conn.read_channel()
    return conn

def get_unregistered_onts(ip, username, password):
    conn = olt_ssh(ip, username, password)
    results = []
    for slot_port in ['0/0','0/1','0/2','0/4','0/5']:
        conn.write_channel(f'interface gpon {slot_port}\r\n')
        time.sleep(2); conn.read_channel()
        for p in range(8):
            conn.write_channel(f'display ont autofind {p}\r\n')
            time.sleep(3)
            out = conn.read_channel()
            if 'do not exist' in out or 'Failure' in out: continue
            # OLT format: "Ont SN              : 48575443143D1067 (HWTC-143D1067)"
            sn = re.search(r'Ont SN\s*:\s*([0-9A-Fa-f]+)', out)
            vendor = re.search(r'VendorID\s*:\s*(\S+)', out)
            model = re.search(r'Ont EquipmentID\s*:\s*(\S+)', out)
            t = re.search(r'autofind time\s*:\s*([\d]{4}-[\d]{2}-[\d]{2} [\d]{2}:[\d]{2}:[\d]{2}[+\d:]*)', out)
            fsp = re.search(r'F/S/P\s*:\s*(\S+)', out)
            if sn:
                fsp_val = fsp.group(1) if fsp else f'0/{slot_port}/{p}'
                results.append({
                    'sn': sn.group(1),
                    'pon': fsp_val,
                    'slot': slot_port,
                    'port': p,
                    'vendor': vendor.group(1) if vendor else '?',
                    'model': model.group(1) if model else '?',
                    'time': t.group(1).strip() if t else '?'
                })
        conn.write_channel('quit\r\n'); time.sleep(1); conn.read_channel()
    conn.disconnect()
    return results

def provision_ont(ip, username, password, sn, slot_port, port, line_id, srv_id, description):
    conn = olt_ssh(ip, username, password)
    conn.write_channel(f'interface gpon {slot_port}\r\n')
    time.sleep(2); conn.read_channel()
    cmd = f'ont add {port} sn-auth {sn} omci ont-lineprofile-id {line_id} ont-srvprofile-id {srv_id} desc "{description}"'
    conn.write_channel(cmd+'\r\n'); time.sleep(5)
    out = conn.read_channel()
    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.write_channel('quit\r\n'); time.sleep(1)
    conn.disconnect()
    s = re.search(r'success:\s*(\d+)', out)
    o = re.search(r'ONTID\s*:\s*(\d+)', out)
    if s and int(s.group(1)) > 0:
        return True, int(o.group(1)) if o else -1, out
    return False, -1, out
