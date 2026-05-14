# Phase 4: Open Router Reliability (Cache-first + Lightweight Live WAN)

Date: 2026-05-11
Target version: v3.3-phase4-open-router-reliability

## Why this phase

`Open Router` was failing with WAN-IP timeout on busy OLT periods. The old API fallback path used a full ONT settings check flow, which runs multiple CLI sections (service-port, wan, wlan, wifi, eth). That path is heavier than needed for opening router UI.

## What changed

1. Added a lightweight WAN resolver in `api/olt_helpers.py`:
   - `get_ont_wan_live(ip, username, password, sn, pon_hint='')`
   - Finds ONT by SN, reads `service-port`, then reads only `display ont wan-info`.
   - Parses:
     - `ipv4_address`
     - `connection_status`
     - `access_type`
     - `manage_vlan`

2. Hardened `_olt_command(...)`:
   - Added `wait_prompt` + `max_rounds` support.
   - Uses `_read_until_prompt(...)` when long/paginated outputs are expected.
   - Reduces partial reads on `---- More` pages.

3. Updated `/ont/wan-ip` in `api/server.py`:
   - Keep fast path: Influx `ont_wan` cache.
   - Replace heavy fallback (`apply_ont_settings(kind='check')`) with new lightweight live WAN resolver.
   - Return structured response with:
     - `ip`
     - `status`
     - `vlan`
     - `access_type`
     - `source` (`cache` or `ssh-live`)

## Expected outcome

- Faster and more reliable `Open Router`.
- Fewer SSH operations per click.
- Better behavior during worker polling load.

## Validation target ONT

- SN: `485754435D88F1AC`
- F/S/P + ID: `0/4/2` + `38`
- Expected WAN IPv4: `10.20.170.186`
- PPPoE user: `indusshop`

