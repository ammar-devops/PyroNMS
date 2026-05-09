#!/usr/bin/env python3
"""
Collect OLT temperature stats and store in InfluxDB
Run every 5 minutes via cron
"""
import sys
sys.path.insert(0, '/opt/ont-monitor/api')
from olt_helpers import olt_ssh
import time
import re
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# InfluxDB config
INFLUX_URL = "http://localhost:8086"
INFLUX_TOKEN = "my-super-secret-token"
INFLUX_ORG = "myisp"
INFLUX_BUCKET = "olt_monitoring"

# OLT config
OLT_IP = "172.20.101.101"
OLT_USER = "collectolt"
OLT_PASS = "Dell@1122"
OLT_NAME = "HAJI-PARK-OLT"

# Slots to monitor
SLOTS_ALL = ['0/0', '0/1', '0/2', '0/4', '0/5', '0/6', '0/7', '0/8', '0/9']  # All installed boards

def collect_all_stats():
    """Collect CPU and temperature from OLT via SSH"""
    stats = {'cpu': {}, 'temp': {}}
    try:
        conn = olt_ssh(OLT_IP, OLT_USER, OLT_PASS)
        conn.write_channel('quit\r\n')
        time.sleep(1)
        conn.read_channel()
        
        # Collect CPU & Temperature for all boards
        for slot in SLOTS_ALL:
            # Try CPU first
            conn.write_channel(f'display cpu {slot}\r\n')
            time.sleep(1.5)
            out = conn.read_channel()
            
            match = re.search(r'CPU occupancy:\s+(\d+)%', out)
            if match:
                stats['cpu'][slot] = int(match.group(1))
                print(f"{slot} CPU: {stats['cpu'][slot]}%")
            elif 'not support' in out.lower() or 'failure' in out.lower():
                print(f"{slot} CPU: Not supported")
            
            # Then temperature
            conn.write_channel(f'display temperature {slot}\r\n')
            time.sleep(1.5)
            out = conn.read_channel()
            
            match = re.search(r'temperature.*:\s+(\d+)C', out, re.IGNORECASE)
            if match:
                stats['temp'][slot] = int(match.group(1))
                print(f"{slot} Temp: {stats['temp'][slot]}°C")
        
        conn.disconnect()
    except Exception as e:
        print(f"SSH Error: {e}")
    
    return stats

def write_to_influx(stats):
    """Write CPU and temperature data to InfluxDB"""
    if not stats['cpu'] and not stats['temp']:
        print("No data to write")
        return
    
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        
        points = []
        
        # Write CPU data
        for slot, cpu in stats['cpu'].items():
            point = Point("olt_cpu") \
                .tag("olt", OLT_NAME) \
                .tag("slot", slot) \
                .field("percent", cpu)
            points.append(point)
        
        # Write temperature data
        for slot, temp in stats['temp'].items():
            point = Point("olt_temperature") \
                .tag("olt", OLT_NAME) \
                .tag("slot", slot) \
                .field("celsius", temp)
            points.append(point)
        
        write_api.write(bucket=INFLUX_BUCKET, record=points)
        print(f"Wrote {len(points)} points to InfluxDB (CPU: {len(stats['cpu'])}, Temp: {len(stats['temp'])})")
        client.close()
    except Exception as e:
        print(f"InfluxDB Error: {e}")

if __name__ == "__main__":
    print(f"=== OLT Stats Collector - {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    stats = collect_all_stats()
    write_to_influx(stats)
    print("Done\n")
