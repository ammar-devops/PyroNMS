# Phase 6: ONT OID Mapper (Targeted Index Discovery)

Date: 2026-05-11  
Target version: v3.5-phase6-ont-oid-mapper

## Why this phase

Phase 5 added safe bounded discovery, but we still needed ONT-specific index mapping to identify field OIDs with confidence.

## What changed

1. Added `snmp_find_ifindex_by_pon(...)`:
   - Maps `PON` (example: `0/4/2`) to SNMP ifIndex via IF-MIB `ifName`.

2. Added `snmp_map_ont_candidates(...)`:
   - Scans Huawei XPON table columns (`...2011.6.128.1.1.2.21.1.{col}`).
   - Filters by known tokens:
     - expected name
     - expected WAN IP
     - expected temperature
   - Tags records whose index is near the mapped PON ifIndex.
   - Performs direct `snmpget` probes for candidate indexes:
     - `ifIndex + ont_id`
     - `ifIndex + (ont_id * 256)` (Huawei table stride heuristic)
   - Adds `inferred_ont_id` when index stride suggests ONT-ID encoding.

3. Added API endpoint:
   - `GET /snmp/ont-map`
   - Query params:
     - `pon` (e.g., `0/4/2`)
     - `ont_id` (e.g., `38`)
     - `expected_name` (e.g., `Indus Shop`)
     - `expected_ip` (e.g., `10.20.170.186`)
     - `expected_temp` (e.g., `64`)

## Usage example

`/snmp/ont-map?pon=0/4/2&ont_id=38&expected_name=Indus%20Shop&expected_ip=10.20.170.186&expected_temp=64`

## Goal

Use this mapper output to identify stable OIDs for:
- ONT name/alias
- state/status
- temperature
- WAN IP (if exposed by firmware)
- VLAN mapping
