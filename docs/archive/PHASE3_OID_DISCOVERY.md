# PyroNMS Phase 3 — OID Discovery Endpoint

## Why

To replace SSH safely, we need validated per-field OIDs for your exact MA5603T firmware/MIB behavior.  
This phase adds an API tool to probe configured OID templates per ONT and show parsed/raw outputs.

## What was added

1. Helper in `api/olt_helpers.py`:
   - `snmp_probe_ont_fields(...)`
   - computes ONT packed index
   - runs `snmpget` for configured template fields (`rx_power`, `temp`, `vlan`)
   - returns parsed values + raw command outputs

2. New API endpoint in `api/server.py`:
   - `GET /snmp/probe-ont`
   - supports:
     - `?sn=<serial>` (auto-resolve slot/port/ont_id via existing lookup)
     - or explicit `?slot=1&port=1&ont_id=72`
   - auth: admin/superadmin
   - returns:
     - `ont_index`, `values`, `raw`, and active templates

## Example

`/snmp/probe-ont?sn=48575443143D1067`

## Safety

- Read-only operation (`snmpget` only).
- No provisioning or config write performed.
- Keeps existing production paths unchanged.
