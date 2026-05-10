#!/usr/bin/env python3
"""
ONT Slot Worker
Phase 1:
- SNMP-first for per-ONT metrics where OIDs are configured
- SSH fallback for compatibility and data continuity
"""

import sys
import time
import logging
import argparse
from datetime import datetime, timezone

sys.path.insert(0, "/opt/ont-monitor")

from config.config import (
    OLT_HOST,
    OLT_PORT,
    SLOT_USERS,
    SLOT_PORTS,
    INFLUX_URL,
    INFLUX_TOKEN,
    INFLUX_ORG,
    INFLUX_BUCKET,
    POLL_INTERVAL,
    OLT_NAME,
    POLL_SOURCE,
    SNMP_READ_COMMUNITY,
    SNMP_OID_TEMPLATES,
)
from workers.olt_helper import connect_olt, get_ont_list, get_optical
from workers.snmp_helper import snmp_ping, get_ont_metrics_by_index

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Phase 2.2: throttle WAN info collection to avoid per-ONT per-poll SSH load.
WAN_CACHE_SHARDS = 6  # each poll handles ~1/6 online ONTs for WAN cache
_POLL_CYCLE = {}


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


def _ont_index(slot, port_num, ont_id):
    # Keep calculation explicit and stable for future OID map usage.
    # Common Huawei-style packed index format.
    return (int(slot) << 24) + (int(port_num) << 16) + int(ont_id)


def poll_slot(slot, log):
    user = SLOT_USERS[slot]
    ports = SLOT_PORTS[slot]
    points = []
    conn = None

    snmp_enabled = POLL_SOURCE.lower() == "hybrid"
    snmp_ok = False
    if snmp_enabled:
        snmp_ok = snmp_ping(OLT_HOST, SNMP_READ_COMMUNITY)
        log.info(f"SNMP check: {'OK' if snmp_ok else 'FAILED'} (source={POLL_SOURCE})")

    try:
        log.info(f"Connecting as {user['username']}...")
        conn = connect_olt(OLT_HOST, user["username"], user["password"], OLT_PORT)
        log.info("Connected.")
        now = datetime.now(timezone.utc)
        cycle = _POLL_CYCLE.get(slot, 0)
        _POLL_CYCLE[slot] = cycle + 1
        shard = cycle % WAN_CACHE_SHARDS

        for port_num in ports:
            pon = f"0/{slot}/{port_num}"
            try:
                onts = get_ont_list(conn, slot, port_num)
                if not onts:
                    continue

                log.info(f"  {pon}: {len(onts)} ONTs")

                status_points = []
                for ont in onts:
                    p = (
                        Point("ont_status")
                        .tag("olt", OLT_NAME)
                        .tag("pon", pon)
                        .tag("ont_id", str(ont["ont_id"]))
                        .tag("sn", ont["sn"])
                        .tag("description", ont["desc"])
                        .field("online", 1 if ont["state"] == "online" else 0)
                        .field("state", ont["state"])
                        .time(now, "s")
                    )
                    status_points.append(p)
                write_points(status_points)

                for ont in onts:
                    if ont["state"] == "online":
                        continue
                    try:
                        from workers.olt_helper import get_down_cause

                        cause = get_down_cause(conn, slot, port_num, ont["ont_id"])
                        dc = (
                            Point("ont_status")
                            .tag("olt", OLT_NAME)
                            .tag("pon", pon)
                            .tag("ont_id", str(ont["ont_id"]))
                            .tag("sn", ont["sn"])
                            .tag("description", ont["desc"])
                            .field("down_cause", cause)
                            .time(now, "s")
                        )
                        write_points([dc])
                    except Exception as e:
                        log.warning(f"down_cause error: {e}")

                for ont in onts:
                    if ont["state"] != "online":
                        continue

                    # Phase 1: SNMP-first for metrics
                    snmp_metrics = {}
                    if snmp_enabled and snmp_ok and any(SNMP_OID_TEMPLATES.values()):
                        try:
                            idx = _ont_index(slot, port_num, ont["ont_id"])
                            snmp_metrics = get_ont_metrics_by_index(
                                OLT_HOST, SNMP_READ_COMMUNITY, idx, SNMP_OID_TEMPLATES
                            )
                        except Exception as e:
                            log.warning(f"snmp metrics error ({pon}/{ont['ont_id']}): {e}")

                    # SSH fallback (or full SSH mode)
                    optical = None
                    if "rx_power" in snmp_metrics or "temp" in snmp_metrics:
                        optical = {
                            "rx_power": snmp_metrics.get("rx_power"),
                            "tx_power": None,
                            "olt_rx": None,
                            "temp": snmp_metrics.get("temp"),
                        }
                    else:
                        optical = get_optical(conn, slot, port_num, ont["ont_id"])

                    if optical:
                        op = (
                            Point("ont_optical")
                            .tag("olt", OLT_NAME)
                            .tag("pon", pon)
                            .tag("ont_id", str(ont["ont_id"]))
                            .tag("sn", ont["sn"])
                            .tag("description", ont["desc"])
                        )
                        for field, val in optical.items():
                            if val is not None:
                                op = op.field(field, val)
                        op = op.time(now, "s")
                        points.append(op)

                    try:
                        # Phase 2.2 WAN sampling:
                        # avoid expensive WAN command on every ONT every cycle.
                        want_wan_probe = (ont["ont_id"] % WAN_CACHE_SHARDS) == shard
                        wan = {}
                        wan_ip = ""
                        wan_status = ""
                        wan_vlan = ""
                        if want_wan_probe:
                            from workers.olt_helper import get_wan_ip
                            wan = get_wan_ip(conn, slot, port_num, ont["ont_id"]) or {}
                            wan_ip = (wan.get("ip") or "").strip()
                            wan_status = (wan.get("status") or "").strip()
                            wan_vlan = (wan.get("vlan") or "").strip()

                        # Prefer SNMP VLAN if available, otherwise WAN parse fallback.
                        vlan = snmp_metrics.get("vlan") or wan_vlan
                        if vlan:
                            vp = (
                                Point("ont_status")
                                .tag("olt", OLT_NAME)
                                .tag("pon", pon)
                                .tag("ont_id", str(ont["ont_id"]))
                                .tag("sn", ont["sn"])
                                .tag("description", ont["desc"])
                                .field("vlan", vlan)
                                .time(now, "s")
                            )
                            write_points([vp])

                        # Phase 2.1/2.2: cache WAN fields for fast API Open Router.
                        if want_wan_probe and (wan_ip or wan_status or vlan):
                            wp = (
                                Point("ont_wan")
                                .tag("olt", OLT_NAME)
                                .tag("pon", pon)
                                .tag("ont_id", str(ont["ont_id"]))
                                .tag("sn", ont["sn"])
                                .tag("description", ont["desc"])
                            )
                            if wan_ip:
                                wp = wp.field("ipv4_address", wan_ip)
                            if wan_status:
                                wp = wp.field("connection_status", wan_status)
                            if vlan:
                                wp = wp.field("network_vlan", str(vlan))
                            wp = wp.time(now, "s")
                            write_points([wp])
                    except Exception as e:
                        log.warning(f"wan/vlan cache error: {e}")

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
            except Exception:
                pass

    if points:
        write_points(points)
    log.info(f"Slot {slot} poll complete.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", type=int, required=True, choices=[1, 2, 4, 5])
    args = parser.parse_args()
    slot = args.slot
    log = get_logger(slot)

    log.info(f"=== Slot {slot} Worker Started ===")
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
