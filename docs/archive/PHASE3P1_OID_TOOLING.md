# PyroNMS Phase 3.1 — OID Template Management + SNMP Test Tools

## Why

We need faster and safer OID validation on live OLT without editing code for every trial.

## Added

1. `GET /snmp/templates`
   - returns active OID templates used by probe tools.

2. `POST /snmp/templates`
   - updates template file:
   - `/opt/ont-monitor/config/snmp_oid_templates.json`
   - keys: `rx_power`, `temp`, `vlan`

3. `GET /snmp/get?oid=...`
   - direct read-only `snmpget` helper from API.

4. `GET /snmp/walk?oid=...&limit=200`
   - read-only `snmpwalk` helper with output line limit.

5. `/snmp/probe-ont` now reads templates from JSON file
   - no restart-required config editing for template updates.

## Safety

- Admin/superadmin only.
- Read-only SNMP for probe/get/walk.
- Template updates only affect probe behavior.
