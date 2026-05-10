#!/usr/bin/env python3
"""
ONT Slot Worker
Polls one slot continuously. Run one instance per slot.

Usage:
    python3 slot_worker.py --slot 1
    python3 slot_worker.py --slot 2
    python3 slot_worker.py --slot 4
    python3 slot_worker.py --slot 5
"""

import sys
import time
import logging
import argparse
from datetime import datetime, timezone

sys.path.insert(0, "/opt/ont-monitor")

from config.config import (
    OLT_HOST, OLT_PORT, SLOT_USERS, SLOT_PORTS,
    INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET,
    POLL_INTERVAL, OLT_NAME
)
from workers.olt_helper import connect_olt, get_ont_list, get_optical

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"/opt/ont-monitor/logs/slot%(slot)s.log")
    ]
)


def get_logger(slot):
    logger = logging.getLogger(f"slot{slot}")
    if not logger.handlers:
        fmt = logging.Formatter(f"%(asctime)s [SLOT{slot}] %(levelname)s %(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        fh = logging.FileHandler(f"/opt/ont-monitor/logs/slot{slot}.log")
        fh.setFormatter(fmt)
        logger.addHandler(sh)
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
    return logger


def write_points(points):
    if not points:
        return
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    client.write_api(write_options=SYNCHRONOUS).write(bucket=INFLUX_BUCKET, record=points)
    client.close()


def poll_slot(slot, log):
    user   = SLOT_USERS[slot]
    ports  = SLOT_PORTS[slot]
    points = []
    conn   = None

    try:
        log.info(f"Connecting as {user['username']}...")
        conn = connect_olt(OLT_HOST, user["username"], user["password"], OLT_PORT)
        log.info("Connected.")
        now = datetime.now(timezone.utc)

        for port_num in ports:
            pon = f"0/{slot}/{port_num}"
            try:
                onts = get_ont_list(conn, slot, port_num)
                if not onts:
                    continue

                log.info(f"  {pon}: {len(onts)} ONTs")

                # Write status first (fast)
                status_points = []
                for ont in onts:
                    p = (
                        Point("ont_status")
                        .tag("olt",         OLT_NAME)
                        .tag("pon",         pon)
                        .tag("ont_id",      str(ont["ont_id"]))
                        .tag("sn",          ont["sn"])
                        .tag("description", ont["desc"])
                        .field("online",    1 if ont["state"] == "online" else 0)
                        .field("state",     ont["state"])
                        .time(now, "s")
                    )
                    status_points.append(p)

                write_points(status_points)

                # Get down cause for offline ONTs
                for ont in onts:
                    if ont["state"] == "online":
                        continue
                    try:
                        from workers.olt_helper import get_down_cause
                        cause = get_down_cause(conn, slot, port_num, ont["ont_id"])
                        dc = (
                            Point("ont_status")
                            .tag("olt",         OLT_NAME)
                            .tag("pon",         pon)
                            .tag("ont_id",      str(ont["ont_id"]))
                            .tag("sn",          ont["sn"])
                            .tag("description", ont["desc"])
                            .field("down_cause", cause)
                            .time(now, "s")
                        )
                        write_points([dc])
                        log.info(f"    [{ont['ont_id']:3d}] OFFLINE cause={cause}")
                    except Exception as e:
                        log.warning(f"down_cause error: {e}")

                # Then get optical data for online ONTs
                for ont in onts:
                    if ont["state"] != "online":
                        continue
                    optical = get_optical(conn, slot, port_num, ont["ont_id"])
                    if optical:
                        op = Point("ont_optical") \
                            .tag("olt",         OLT_NAME) \
                            .tag("pon",         pon) \
                            .tag("ont_id",      str(ont["ont_id"])) \
                            .tag("sn",          ont["sn"]) \
                            .tag("description", ont["desc"])
                        for field, val in optical.items():
                            if val is not None:
                                op = op.field(field, val)
                        op = op.time(now, "s")
                        points.append(op)
                        log.info(f"    [{ont['ont_id']:3d}] {ont['desc'][:30]:30s} RX={optical.get('rx_power')}")
                    # Get VLAN for online ONTs (every 5th poll to avoid SSH overload)
                    try:
                        from workers.olt_helper import get_vlan
                        vlan = get_vlan(conn, slot, port_num, ont["ont_id"])
                        if vlan:
                            vp = (Point("ont_status")
                                .tag("olt", OLT_NAME).tag("pon", pon)
                                .tag("ont_id", str(ont["ont_id"])).tag("sn", ont["sn"])
                                .tag("description", ont["desc"])
                                .field("vlan", vlan).time(now, "s"))
                            write_points([vp])
                    except Exception as e:
                        log.warning(f"vlan error: {e}")

                        # Write every 50 optical points
                        if len(points) >= 50:
                            write_points(points)
                            points = []

            except Exception as e:
                log.error(f"  Error on {pon}: {e}")
                continue

    except Exception as e:
        log.error(f"Poll failed: {e}")
    finally:
        if conn:
            try:
                conn.disconnect()
            except:
                pass

    if points:
        write_points(points)
    log.info(f"Slot {slot} poll complete.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", type=int, required=True, choices=[1, 2, 4, 5])
    args = parser.parse_args()
    slot = args.slot
    log  = get_logger(slot)

    log.info(f"=== Slot {slot} Worker Started ===")

    # Stagger startup: spread polls across the POLL_INTERVAL window
    # so all 4 workers never hold OLT SSH sessions simultaneously.
    _STAGGER = {1: 0, 2: 1800, 4: 3600, 5: 5400}
    _delay = _STAGGER.get(slot, 0)
    if _delay > 0:
        log.info(f"Stagger delay: sleeping {_delay}s before first poll...")
        time.sleep(_delay)

    while True:
        try:
            poll_slot(slot, log)
        except Exception as e:
            log.error(f"Unexpected error: {e}")
        log.info(f"Sleeping {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
