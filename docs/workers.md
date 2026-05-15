# PyroNMS Worker Architecture

---

## Overview

PyroNMS uses two parallel polling systems that write to InfluxDB `olt_monitoring` bucket:

```
[ont-worker@1/2/4/5]    ──SSH + SNMP──▶ OLT ──▶ InfluxDB (ont_status, ont_optical, ont_wan)
[pyronms-poller.service] ──SNMP────────▶ OLT ──▶ InfluxDB (pon_traffic, olt_summary, ont_status)
[cron: collect_olt_stats] ──SSH ×12/hr─▶ OLT ──▶ InfluxDB (olt_cpu, olt_temperature)
```

---

## Slot Workers (`ont-worker@N`)

**File:** `/opt/ont-monitor/workers/slot_worker.py`  
**Services:** `ont-worker@1`, `ont-worker@2`, `ont-worker@4`, `ont-worker@5`  
**Poll interval:** 1800s (30 min)

Each worker handles one OLT slot (set of PON ports). For each online ONT per port:

| Data | Source | InfluxDB Measurement |
|------|--------|---------------------|
| Online/offline status | SSH `display ont info` | `ont_status` |
| RX power, temperature | SNMP (primary), SSH fallback | `ont_optical` |
| WAN IP, PPPoE state | SSH (1 of 6 cycles ~3h) | `ont_wan` |

### Tags written
`olt` (name), `pon` (0/1/0), `ont_id`, `sn`, `description`

### Stagger behavior
Workers stagger their first poll to avoid simultaneous OLT load:
- Slot 1: no delay
- Slot 2: 450s delay
- Slot 4: 900s delay
- Slot 5: 1350s delay

**Important:** Stagger applies only on first boot. Systemd restarts skip the stagger immediately (flag file `/tmp/slot{N}_stagger_done` — cleared on VM reboot).

### Restart / check status
```bash
systemctl restart ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5
systemctl is-active ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5
journalctl -u ont-worker@5 -f --no-pager
```

---

## SNMP Poller (`pyronms-poller.service`)

**File:** `/opt/pyronms/poller/poller.py`  
**Service:** `pyronms-poller.service`  
**Repo copy:** `poller/poller.py`

Runs as a systemd service (since v4.3.0 — previously an unsupervised orphan process).

| Data | Interval | InfluxDB Measurement |
|------|----------|---------------------|
| ONT online/offline | 60s | `ont_status` (with `sn` tag — API-compatible) |
| PON port traffic (Mbps) | 120s | `pon_traffic` |
| ONT RX power + temp | 300s | `ont_optical` (unreliable — GETBULK timeouts ~80%) |
| ONT index rebuild | 86400s | `ont_map.db` (SQLite) |
| OLT summary counts | 60s | `olt_summary` |

### Tags written
`olt` (name), `pon` (0/1/0), `ont_id`, `sn`, `description`

> **Note:** The `ont_optical` writes from this poller time out ~80% of the time (SNMP GETBULK for full OLT is too slow). The slot_worker SNMP optical (per-port chunked snmpget) is the reliable optical data source.

### Restart / check status
```bash
systemctl restart pyronms-poller
systemctl status pyronms-poller
journalctl -u pyronms-poller -f --no-pager
```

---

## OLT Stats Cron (`collect_olt_stats.py`)

**File:** `/opt/ont-monitor/olt-config/collect_olt_stats.py`  
**Schedule:** Every 5 minutes (`*/5 * * * *`)  
**SSH load:** ~12 sessions/hour

Collects OLT CPU, memory, temperature via SSH `display board` command.  
Writes to `olt_cpu` and `olt_temperature` measurements.

---

## Worker Health API

`GET /workers/health` (requires auth)

```bash
TOKEN=$(curl -s -X POST http://localhost:8088/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_PASS"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -s http://localhost:8088/workers/health -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Example response:
```json
{
  "ok": true,
  "workers": {
    "slot1": {"service_status": "active", "active": true},
    "slot2": {"service_status": "active", "active": true},
    "slot4": {"service_status": "active", "active": true},
    "slot5": {"service_status": "active", "active": true},
    "poller": {"service_status": "active", "active": true}
  },
  "ts": 1747334400
}
```

---

## Full Restart Sequence

```bash
systemctl daemon-reload
systemctl restart ont-api
systemctl restart ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5
systemctl restart pyronms-poller

# Verify all active
systemctl is-active ont-api ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5 pyronms-poller nginx
```

---

## Dead Schema Notes

`ont_status_v2` and `ont_optical_v2` appear in InfluxDB schema but have zero data and zero code references. These are leftover from an older poller prototype and can be ignored.
