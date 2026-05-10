# PyroNMS Phase 1 — SNMP-First Worker Polling

## Why we are doing this

The OLT currently spends a lot of time serving SSH polling sessions.  
With 2500+ ONTs and per-ONT CLI reads, slot workers can hold sessions for long periods and increase VTY pressure.

Phase 1 reduces SSH dependency in a safe way:

1. Keep current SSH workflow as fallback (no data loss risk).
2. Add SNMP-first read path for ONT metrics (`rx_power`, `temp`, `vlan`).
3. Enable gradual OID rollout by config (no hard-coded risky assumptions).

## What changed in Phase 1

1. Added worker SNMP helper:
   - `workers/snmp_helper.py`
   - Health check: `snmp_ping()` via `sysName.0`
   - Generic OID-template getter: `get_ont_metrics_by_index()`

2. Updated worker polling flow:
   - `workers/slot_worker.py`
   - New mode: `POLL_SOURCE = "hybrid"`
   - For each online ONT:
     - Try SNMP metrics first (if OIDs are configured)
     - Fallback to existing SSH parser automatically

3. Added config controls:
   - `config/config.py`
   - `SNMP_READ_COMMUNITY`
   - `SNMP_WRITE_COMMUNITY`
   - `SNMP_OID_TEMPLATES`

## Safety behavior

- If SNMP is unreachable or OID mapping is empty/invalid, worker continues using SSH exactly as before.
- No API behavior changes in this phase.
- Influx schema remains unchanged.

## Next step (Phase 1.1)

Populate `SNMP_OID_TEMPLATES` from validated Huawei MA5600T MIB OIDs, then observe logs and increase SNMP coverage.
