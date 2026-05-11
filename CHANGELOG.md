# Changelog

## v2.6.0 (2026-05-11)

### SNMP-First Architecture
- **Phase 1** — SNMP-first worker polling with SSH fallback.
- **Phase 2** — Cache-first WAN/live API paths; worker writes `ont_wan` cache; throttled collection with shard sampling.
- **Phase 3** — SNMP OID probe endpoint for ONT field discovery; SNMP template manager + raw get/walk API tools.
- **Phase 4** — Hardened Open Router WAN resolver (cache-first + lightweight SSH-live).
- **Phase 5** — Bounded SNMP discovery endpoint for safe OID hunting.
- **Phase 6** — ONT OID mapper endpoint with ifIndex + stride heuristics.
- Added SNMP v2 support with SSH/SNMP method selector.
- Fixed legacy RSA key negotiation for older OLT firmware.

### Dashboard & UI Overhaul
- Rebuilt dashboard as 8 sibling cards in a 4-column CSS grid (Total, Active, Offline, Power Down, Fiber Down, Weak Signal, Critical, Unregistered).
- Mobile-responsive 2-column dashboard layout.
- Sidebar defaults to collapsed; auto-collapse on mobile.
- Theme iframe bridge (settings.html → index.html via `postMessage`).
- `localStorage` cache of `/onts` response for instant first paint on subsequent loads.
- Nginx `Cache-Control: no-cache` for HTML so UI updates are picked up immediately.

### ONT List & Filtering
- VLAN column sortable (numeric).
- Removed redundant "All PONs" header dropdown.
- Consolidated PON filter logic into `buildPONFilterOptions()` with proper numeric sort (0/1/0, 0/1/1 …).
- `refreshAll()` made async with toast feedback and button busy state.
- `updateStats()` made null-safe so legacy IDs no longer halt `loadONTs()`.

### Performance
- Open Router fast path using cached PON + ONT ID (no full ONT scan).
- Cached WAN fields for instant Open Router popup.
- Removed client-side timeout aborts on long-running operations.

### Bug Fixes
- Power Down badge (pink) and Fiber Down badge (red) — distinct colors, single-line, no-wrap.
- Replaced corrupted placeholder text in Power/Fiber badges.
- Restored `signalBadge` helper used by ONT table renderer.
- Fixed ONT table render break caused by quote chars in customer names.
- Switched to safe `data-attribute` click binding (no inline `onclick`).
- Fixed SSH session exhaustion and ONT check reliability.
- Open Router robust refresh + popup-safe timeout.

### Features
- Bulk ONT delete with checkboxes.
- In-app toast notifications for router and refresh actions.
- Open Router resolves live ONT WAN IPv4 via SSH check.

### Documentation & Project
- README redesigned with badges, emojis, ASCII architecture diagram, and tech-stack tables.
- Repository migrated to `PyroNet-Solutions/PyroNMS`.

## v2.5.1
- Fixed unregistered ONT scanning flow for Huawei prompt behavior.
- Improved provisioning flow with service-port creation support.
- Added VLAN 10 defaults in provisioning UI/API.
- Added General ONT VAS Profile field in provisioning modal.
- Multiple mobile UI and table alignment fixes.

## v2.5
- Dashboard cards, filter bar, resource monitor.

## v2.4
- Side panel, OLT management, server monitor.
