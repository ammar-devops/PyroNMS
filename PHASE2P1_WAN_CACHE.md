# PyroNMS Phase 2.1 — Worker WAN Cache for Open Router

## Why

`Open Router` depended on on-demand SSH checks for ONT WAN IP.  
That added delay and timeout risk under worker load.

## What changed

1. Worker now fetches WAN info for online ONTs during normal polling:
   - source: `display ont wan-info {port} {ont_id}`

2. Writes new Influx measurement:
   - measurement: `ont_wan`
   - fields:
     - `ipv4_address`
     - `connection_status`
     - `network_vlan`
   - tags:
     - `olt`, `pon`, `ont_id`, `sn`, `description`

3. VLAN write path optimization:
   - uses WAN parse (and SNMP VLAN if available) in one pass
   - avoids extra redundant CLI calls for VLAN-only fetch.

## Safety

- Existing optical/status pipeline unchanged.
- Errors in WAN fetch do not stop ONT polling loop.
- API fallback to SSH remains available.

## Expected outcome

`/ont/wan-ip` can return from cache for most ONTs, making Open Router faster and more reliable.
