#!/usr/bin/env python3
"""
OLT Config Collector - Daily automated backup
Runs at 12AM via cron
Saves: full config + parsed profiles/VLANs JSON
"""
from netmiko import ConnectHandler
import time, datetime, re, json, os

OLT_HOST = "172.20.101.101"
OLT_USER = "collectolt"
OLT_PASS = "Dell@1122"
OUTPUT_DIR = "/opt/ont-monitor/olt-config"

timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_FILE = f"{OUTPUT_DIR}/olt_config_{timestamp}.txt"
LATEST_FILE = f"{OUTPUT_DIR}/olt_config_latest.txt"
PROFILES_FILE = f"{OUTPUT_DIR}/olt_profiles.json"

print(f"[{timestamp}] Connecting to OLT {OLT_HOST}...")

conn = ConnectHandler(
    device_type='terminal_server',
    host=OLT_HOST,
    username=OLT_USER,
    password=OLT_PASS,
    timeout=120,
)

print("Connected! Entering enable mode...")
conn.write_channel('enable\r\n'); time.sleep(3); conn.read_channel()

print("Running display current-configuration...")
conn.write_channel('display current-configuration\r\n')

full_output = ""
last_data_time = time.time()
pages = 0

for i in range(200):
    time.sleep(2)
    chunk = conn.read_channel()
    if chunk:
        last_data_time = time.time()
        if '---- More ----' in chunk or 'More' in chunk:
            pages += 1
            full_output += chunk.replace('---- More ----', '').replace('\x1b[42D', '')
            conn.write_channel(' ')
            if pages % 20 == 0:
                print(f"  [{pages} pages] {len(full_output)} bytes...")
            continue
        full_output += chunk
        if 'HAJI-PARK-OLT#' in chunk and len(full_output) > 2000:
            print(f"  Done! {len(full_output)} bytes, {pages} pages")
            break
    if time.time() - last_data_time > 60 and len(full_output) > 1000:
        print(f"  Timeout. {len(full_output)} bytes")
        break

conn.disconnect()

# Clean ANSI codes
full_output = re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', full_output)
full_output = re.sub(r'\x1b\[\d+D', '', full_output)

# Save full config
with open(OUTPUT_FILE, 'w', encoding='utf-8', errors='ignore') as f:
    f.write(full_output)
with open(LATEST_FILE, 'w', encoding='utf-8', errors='ignore') as f:
    f.write(full_output)

print(f"✓ Config saved: {OUTPUT_FILE}")
print(f"  Lines: {len(full_output.splitlines())}, Size: {len(full_output)} bytes")

# ── Parse and save profiles/VLANs JSON ────────────────────────────────────
print("\nParsing profiles and VLANs...")

profiles = {
    "updated_at": timestamp,
    "line_profiles": [],
    "srv_profiles": [],
    "dba_profiles": [],
    "vlans": [],
    "gpon_interfaces": [],
    "ont_count": 0,
}

# Line profiles
seen_ids = set()
for m in re.finditer(r'ont-lineprofile gpon profile-id (\d+) profile-name "([^"]+)"', full_output):
    pid, name = m.group(1), m.group(2)
    if pid not in seen_ids:
        seen_ids.add(pid)
        profiles["line_profiles"].append({"id": pid, "name": name})

# Service profiles
seen_ids = set()
for m in re.finditer(r'ont-srvprofile gpon profile-id (\d+) profile-name "([^"]+)"', full_output):
    pid, name = m.group(1), m.group(2)
    if pid not in seen_ids:
        seen_ids.add(pid)
        profiles["srv_profiles"].append({"id": pid, "name": name})

# DBA profiles
for m in re.finditer(r'dba-profile add profile-id (\d+) profile-name "([^"]+)" (\S+) max (\d+)', full_output):
    profiles["dba_profiles"].append({
        "id": m.group(1),
        "name": m.group(2),
        "type": m.group(3),
        "max": m.group(4)
    })

# VLANs
seen_vlans = set()
for m in re.finditer(r'vlan\s+(\d+)\s+(?:smart|standard|mux)', full_output):
    vid = m.group(1)
    if vid not in seen_vlans:
        seen_vlans.add(vid)
        profiles["vlans"].append(vid)

# Service port VLANs
for m in re.finditer(r'service-port\s+(?:port\s+\S+\s+)?vlan\s+(\d+)', full_output):
    vid = m.group(1)
    if vid not in seen_vlans:
        seen_vlans.add(vid)
        profiles["vlans"].append(vid)

profiles["vlans"] = sorted(set(profiles["vlans"]), key=int)

# GPON interfaces
for m in re.finditer(r'interface gpon (\S+)', full_output):
    iface = m.group(1)
    if iface not in profiles["gpon_interfaces"]:
        profiles["gpon_interfaces"].append(iface)

# ONT count
profiles["ont_count"] = len(re.findall(r'ont add \d+ \d+ sn-auth', full_output))

# Save profiles JSON
with open(PROFILES_FILE, 'w') as f:
    json.dump(profiles, f, indent=2)

print(f"✓ Profiles saved: {PROFILES_FILE}")
print(f"  Line profiles: {len(profiles['line_profiles'])}")
print(f"  Service profiles: {len(profiles['srv_profiles'])}")
print(f"  DBA profiles: {len(profiles['dba_profiles'])}")
print(f"  VLANs: {len(profiles['vlans'])}")
print(f"  GPON interfaces: {profiles['gpon_interfaces']}")
print(f"  Total ONTs in config: {profiles['ont_count']}")

# ── Keep only last 7 backups ───────────────────────────────────────────────
backups = sorted([
    f for f in os.listdir(OUTPUT_DIR)
    if f.startswith('olt_config_') and f != 'olt_config_latest.txt'
])
if len(backups) > 7:
    for old in backups[:-7]:
        os.remove(os.path.join(OUTPUT_DIR, old))
        print(f"  Deleted old backup: {old}")

print(f"\n✅ OLT backup complete!")
