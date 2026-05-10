# PyroNMS Phase 2.2 — WAN Cache Sampling

## Why

Phase 2.1 cached WAN data for Open Router, but it queried WAN info for every online ONT each poll.  
That can increase SSH command load significantly on large ports.

## What changed

1. Worker now samples WAN probes by shard:
   - `WAN_CACHE_SHARDS = 6`
   - each poll cycle only probes about `1/6` of online ONTs
   - shard selection rotates every poll cycle per slot worker

2. VLAN handling remains safe:
   - SNMP VLAN is preferred if available
   - WAN-parsed VLAN used when WAN probe runs

3. `ont_wan` caching still works:
   - entries are updated incrementally over cycles
   - Open Router cache improves while avoiding burst SSH load

## Safety

- Optical/status polling unchanged.
- WAN failures do not break polling loop.
- Existing API SSH fallback remains.

## Impact

- Much lower WAN-command pressure per poll.
- Slightly slower full-table WAN cache convergence, but safer for OLT stability.
