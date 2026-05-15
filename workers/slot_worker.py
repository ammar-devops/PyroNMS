#!/usr/bin/env python3
"""
ONT Slot Worker
Phase 1:
- SNMP-first for per-ONT metrics where OIDs are configured
- SSH fallback for compatibility and data continuity
"""

import os
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
from workers.olt_helper import connect_olt, get_ont_list, get_optical, get_optical_port
from workers.snmp_helper import snmp_ping, get_ont_metrics_by_index, snmp_get_optical_port

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

                # ── Phase 2: SNMP Bulk optical — fastest possible method ───────────
                # One snmpget call per chunk of ~40 ONTs (rx_power + temp together).
                # Port with 115 ONTs → 3 UDP packets → ~1 second.
                # Full OLT via SSH: 35-45 minutes. Via SNMP bulk: ~30 seconds total.
                # SSH is kept as fallback for ONTs SNMP can't reach.
                online_onts = [o for o in onts if o["state"] == "online"]
                online_ids  = [o["ont_id"] for o in online_onts]

                # Primary: SNMP bulk optical (rx_power + temp per ONT, one call per chunk)
                snmp_optical_map = {}
                if snmp_enabled and snmp_ok and online_ids:
                    try:
                        snmp_optical_map = snmp_get_optical_port(
                            OLT_HOST, SNMP_READ_COMMUNITY, slot, port_num, online_ids
                        )
                        if snmp_optical_map:
                            log.debug(f"    SNMP optical: {len(snmp_optical_map)}/{len(online_ids)} ONTs")
                    except Exception as e:
                        log.warning(f"snmp_optical error {pon}: {e}")

                # SSH fallback: only for ONTs with no SNMP optical reading
                need_ssh = [
                    o for o in online_onts
                    if o["ont_id"] not in snmp_optical_map
                ]
                ssh_optical_map = {}
                if need_ssh:
                    log.debug(f"    SSH optical fallback: {len(need_ssh)} ONTs")
                    ssh_optical_map = get_optical_port(
                        conn, slot, port_num, [o["ont_id"] for o in need_ssh]
                    )

                # Merge: SNMP takes priority; SSH fills the gaps
                optical_map = {**ssh_optical_map}
                for ont_id, snmp_data in snmp_optical_map.items():
                    optical_map[ont_id] = {
                        "rx_power": snmp_data.get("rx_power"),
                        "tx_power": None,
                        "olt_rx":   None,
                        "temp":     snmp_data.get("temp"),
                    }

                # Build optical InfluxDB points for every online ONT
                for ont in online_onts:
                    optical = optical_map.get(ont["ont_id"])
                    if not optical:
                        continue
                    # Guard: skip Points that would have no fields (all-None optical)
                    fields = {k: v for k, v in optical.items() if v is not None}
                    if not fields:
                        continue
                    op = (
                        Point("ont_optical")
                        .tag("olt", OLT_NAME)
                        .tag("pon", pon)
                        .tag("ont_id", str(ont["ont_id"]))
                        .tag("sn", ont["sn"])
                        .tag("description", ont["desc"])
                    )
                    for field, val in fields.items():
                        op = op.field(field, val)
                    op = op.time(now, "s")
                    points.append(op)

                # ── Phase 2.2: WAN sampling (throttled by shard) ───────────────────
                for ont in online_onts:
                    snmp_m = snmp_optical_map.get(ont["ont_id"], {})
                    try:
                        want_wan_probe = (ont["ont_id"] % WAN_CACHE_SHARDS) == shard
                        wan_ip = wan_status = wan_vlan = ""
                        if want_wan_probe:
                            from workers.olt_helper import get_wan_ip
                            wan = get_wan_ip(conn, slot, port_num, ont["ont_id"]) or {}
                            wan_ip    = (wan.get("ip") or "").strip()
                            wan_status = (wan.get("status") or "").strip()
                            wan_vlan  = (wan.get("vlan") or "").strip()

                        vlan = snmp_m.get("vlan") or wan_vlan
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

                        if want_wan_probe and (wan_ip or wan_status or vlan):
                            wp = (
                                Point("ont_wan")
                                .tag("olt", OLT_NAME)
                                .tag("pon", pon)
                                .tag("ont_id", str(ont["ont_id"]))
                                .tag("sn", ont["sn"])
                                .tag("description", ont["desc"])
                            )
                            if wan_ip:    wp = wp.field("ipv4_address", wan_ip)
                            if wan_status: wp = wp.field("connection_status", wan_status)
                            if vlan:      wp = wp.field("network_vlan", str(vlan))
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
    # Stagger only on first boot (not on systemd restarts after crash/update).
    # /tmp is cleared on reboot so reboots always get the stagger; restarts skip it.
    _flag = f"/tmp/slot{slot}_stagger_done"
    if not os.path.exists(_flag):
        _stagger_step = max(60, POLL_INTERVAL // 4)
        _STAGGER = {1: 0, 2: _stagger_step, 4: _stagger_step * 2, 5: _stagger_step * 3}
        _delay = _STAGGER.get(slot, 0)
        if _delay > 0:
            log.info(f"Stagger delay: sleeping {_delay}s before first poll...")
            time.sleep(_delay)
        open(_flag, 'w').close()
    else:
        log.info(f"Stagger skipped (restart detected, flag: {_flag})")

    while True:
        try:
            poll_slot(slot, log)
        except Exception as e:
            log.error(f"Unexpected error: {e}")
        log.info(f"Sleeping {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
