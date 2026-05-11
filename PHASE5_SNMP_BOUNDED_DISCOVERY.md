# Phase 5: Bounded SNMP Discovery Endpoint

Date: 2026-05-11  
Target version: v3.4-phase5-snmp-oid-discovery

## Why this phase

Full SNMP walks on Huawei MA5603T enterprise subtree are too heavy and frequently timeout on busy production OLTs.  
We need a safe discovery method to keep OID migration moving without impacting service.

## What changed

1. Added `snmp_discover_candidates(...)` in `/opt/ont-monitor/api/olt_helpers.py`:
   - Walks only bounded, relevant subtrees.
   - Uses strict per-walk timeout.
   - Extracts candidate lines by matching:
     - expected WAN IPv4
     - expected temperature value

2. Added `GET /snmp/discover` in `/opt/ont-monitor/api/server.py`:
   - Admin/superadmin protected.
   - Query params:
     - `expected_ip` (optional)
     - `expected_temp` (optional)
   - Returns:
     - scan summary per subtree
     - matched candidate lines

## Safe usage

Example:

`GET /snmp/discover?expected_ip=10.20.170.186&expected_temp=64`

Use with known ONT values to narrow candidate OIDs before updating templates.

## Validation reference ONT

- SN: `485754435D88F1AC`
- ONT: `0/4/2`, ID `38`
- Known WAN IPv4: `10.20.170.186`
- Known temperature: `64 C`

