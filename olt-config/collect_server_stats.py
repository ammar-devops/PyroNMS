#!/usr/bin/env python3
"""
Collect server stats (CPU, RAM, Disk) and store in InfluxDB
Run every 5 minutes via cron
"""
import psutil
import time
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# InfluxDB config
INFLUX_URL = "http://localhost:8086"
INFLUX_TOKEN = "my-super-secret-token"
INFLUX_ORG = "myisp"
INFLUX_BUCKET = "olt_monitoring"

def collect_server_stats():
    """Collect server metrics"""
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    return {
        'cpu_percent': round(cpu, 1),
        'mem_used_gb': round(mem.used / (1024**3), 2),
        'mem_total_gb': round(mem.total / (1024**3), 2),
        'mem_percent': round(mem.percent, 1),
        'disk_used_gb': round(disk.used / (1024**3), 2),
        'disk_total_gb': round(disk.total / (1024**3), 2),
        'disk_percent': round(disk.percent, 1)
    }

def write_to_influx(stats):
    """Write to InfluxDB"""
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        
        point = Point("server_stats") \
            .tag("host", "billing") \
            .field("cpu_percent", stats['cpu_percent']) \
            .field("mem_percent", stats['mem_percent']) \
            .field("mem_used_gb", stats['mem_used_gb']) \
            .field("disk_percent", stats['disk_percent']) \
            .field("disk_used_gb", stats['disk_used_gb'])
        
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        print(f"Wrote: CPU={stats['cpu_percent']}%, RAM={stats['mem_percent']}%, Disk={stats['disk_percent']}%")
        client.close()
    except Exception as e:
        print(f"InfluxDB Error: {e}")

if __name__ == "__main__":
    print(f"=== Server Stats Collector - {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    stats = collect_server_stats()
    write_to_influx(stats)
    print("Done\n")
