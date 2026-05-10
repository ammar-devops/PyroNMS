# PyroNMS Phase 2 — API Live Paths (Cache/SNMP-first, SSH fallback)

## Why

`/ont/live` and `/ont/wan-ip` were forcing slow SSH checks on demand.  
This increased response time and could collide with worker SSH usage.

## What changed

1. Added fast cache lookup helpers in API:
   - `get_ont_cached(sn)` from existing Influx-backed ONT table data
   - `get_cached_wan_ip(sn)` from `ont_wan` measurement (when present)

2. Updated endpoints:
   - `GET /ont/live`:
     - returns cached ONT row first (`source=cache`)
     - falls back to existing SSH live check (`source=ssh`)
   - `GET /ont/wan-ip`:
     - checks cached WAN IP first (`source=cache`)
     - falls back to existing SSH check path (`source=ssh`)

## Safety

- No existing SSH logic removed.
- If cache is unavailable/incomplete, behavior remains as before.
- No frontend contract break; response now includes optional `source`.

## Next

Populate worker-side `ont_wan` measurement so `/ont/wan-ip` becomes mostly non-SSH in production.
