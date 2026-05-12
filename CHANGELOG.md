# Changelog

## v2.9.1 (2026-05-12)

### Hotfix: WLAN card graceful degradation for unsupported ONT models
- **Root cause identified**: Huawei HG8245 (and bridge-mode ONUs) return `Failure: The ONT can not support` to `display ont wlan-info` — this is an OLT-level hardware limitation, not a credential or parser issue
- **Backend** (`api/olt_helpers.py`): `get_ont_config()` now detects the failure string and returns `wlan.supported = false` with a descriptive warning instead of an empty parsed dict
- **Frontend** (`web/index.html`): `renderOntConfigCards()` now checks `wlan.supported === false` first and renders an explanatory message ("This ONT model does not expose WLAN data via OLT CLI") instead of a blank WLAN card
- Verified with probe script on live OLT — both `pollerslot5` and `pollerslot1` credentials returned identical failure; confirmed hardware-level limitation

## v2.9.0 (2026-05-12)

### ONT WAN + WLAN configuration view (read-only, no TR-069)
- Added a "Configuration" section to the ONT Details popup showing WAN and WLAN settings read via OLT SSH
- New backend endpoint `GET /ont/config?sn=<sn>` (auth-required) returns parsed WAN/WLAN/mgmt-IP/warnings JSON
- New SSH parsers in `api/olt_helpers.py`:
  - `_parse_wan_full` — parses `display ont wan-info` for mode, IPv4, subnet, gateway, DNS, VLAN, MAC, NAT, service type
  - `_parse_wlan_full` — parses `display ont wlan-info` with band detection (2.4G/5G via wireless standard + SSID index)
  - `_parse_ont_ipconfig` — parses ONT management IP block
- New helper `get_ont_config()` orchestrates ordered commands (WLAN first, WAN last) with `_hard_drain` between to handle Huawei "More" pager state cleanly
- Frontend per-band WLAN sub-cards with band chips, client counts, enabled/disabled state badges; WAN card with mode pill (PPPoE/Static/Bridge)
- Honest "stored on ONT — Phase 2" placeholders for fields not exposed by OLT (PPPoE password, WiFi password, security mode, channel)

### TR-069 / GenieACS decoupling
- All `genieacs-*` systemd services stopped and disabled on the live host (binaries preserved for emergency re-enable)
- No new code paths depend on `push_parameter()` (the old TR-069 writer); function kept in `server.py` but unused by the new endpoint

## v2.8.2 (2026-05-12)

### Refresh Buttons + Loading Indicators
- Added NProgress-style 3px top progress bar (`#pyro-bar`) — pure CSS, no library, ref-counted for concurrent loads
- Added `.btn-spinner` border-circle CSS class for consistent button loading state
- **Refresh All**: button now disables + shows inline spinner while loading, re-enables on done/error
- **Row Refresh**: top bar now fires alongside existing per-row spinning ⟳
- **Modal Refresh**: inline spinner replaces plain text "⟳ …" while reloading GenieACS data
- **Unregistered ONTs**: replaced static 🔄 emoji with animated spinning ⟳ + top bar
- **OLT Config DB**: top bar now fires on every forced refresh
- **Backup Manager**: top bar + inline spinner in loading row
- **OLT Monitor**: top bar + inline spinner in loading row
- **Worker Manager**: top bar + inline spinner in loading row
- **ONT Manager**: top bar fires on SSH info fetch (race-condition guard preserved)
- Version display updated to v2.8.2

## v2.8.0 (2026-05-12)

### ONT Manager popup
- Renamed popup heading to **"ONT Details and Configuration"** with a top title strip.
- Robust SSH parsing: Huawei pager (`( Press 'Q' to break )`) now auto-continued.
- `display ont version` runs from `interface gpon` mode (avoids the space-eating quirk on Huawei firmware).
- Model parsed from `OntProductDescription` (e.g. `EchoLife HG8245 GPON Terminal` → `HG8245`).
- Vendor read from `Vendor-ID`, HW from `ONT Version`, SW from `Main Software Version`.
- **Device type detection**: shows `ONT` (router) or `ONU` (L2 bridge) in the vendor pill with tooltip *ONT = Router | ONU = L2 Bridge*, based on model prefix + IPHOST presence.
- Hardware card now includes Device Type, Vendor, Model, HW Version, SW Version.

### ONT list — bulk Actions (U2000-style)
- Added a leftmost **checkbox column** to the ONT list (with a header "select all visible" checkbox).
- Selected rows highlight; row click still opens the popup.
- Added **`▾ Actions`** dropdown in the filter bar (enabled when ≥1 ONT is selected). Shows count: `▾ Actions (N)`.
- Actions available: **ONT Enable** / **Disable** / **Reset (Reboot)** / **Restore (Factory)** / **Delete**.
- **Confirmation modal**:
  - Enable / Disable / Reset → simple OK confirm with target list.
  - Delete / Restore → type-the-SN to enable the destructive button.
- Bulk dispatch: one request runs every action against all selected ONTs serially and reports per-target ok/fail.

### Backend
- New `POST /ont/action` endpoint (admin / superadmin only). Body: `{ action, targets: [{sn, pon, ont_id}] }`.
- New `olt_helpers.run_ont_action(ip, user, pwd, action, sn, pon, ont_id)`.
- SSH commands per action (Huawei MA5603T, from `interface gpon` mode):
  - enable → `ont activate {port} {ont_id}`
  - disable → `ont deactivate {port} {ont_id}`
  - reset → `ont reset {port} {ont_id}`
  - restore → `ont ipconfig {port} {ont_id} factory` (fallback `ont reset {port} {ont_id} factory`)
  - delete → `ont delete {port} {ont_id}` + auto-confirm + `save`
- Pon/ont_id auto-resolved from SN if missing.

### Fixes
- Popup race-condition guard: discard stale `/ont/info` responses (request token + SN check + close-bump).
- Hotfixes that landed in v2.7.x are folded in: rejoined broken `document.write` string, moved ONT Manager JS out of Chart.js script tag, removed orphan trailing JS after `</html>`.

## v2.7.0 (2026-05-11)

### Breaking
- **GenieACS TR-069 integration removed.** All ONT management now goes through SSH + SNMP directly to the Huawei OLT.
- `GET /device?sn=...` now returns HTTP 410 Gone. Use `GET /ont/info?sn=...` instead.

### ONT Manager (new popup)
- Clicking any ONT row in the list opens a new **ONT Manager** modal showing live data fetched via SSH (`display ont info by-sn`).
- Sections: Live Signal (RX, temperature, distance, online duration), Hardware (model, vendor, HW/SW version), Service (line/service profile, VLAN, WAN IP), OLT State (run/config/match state, last up/down).
- **Model-aware theming** — modal accent + vendor pill colors change per ONT model: Huawei (red-orange), ZTE (blue), Generic (neutral).
- **Refresh** button re-runs the SSH query.
- **Open Web UI** button opens the ONT's web admin in a new tab (using the WAN IP).

### UI Cleanup
- Removed **Settings** column from the ONT list (replaced by the unified ONT Manager popup).
- Removed **Router** column (its action merged into the new popup).
- Table now has 8 columns instead of 10.

### Backend
- New `GET /ont/info?sn=X` endpoint — runs `display ont info by-sn` + `display ont version` via SSH and returns clean JSON.
- New `olt_helpers.get_ont_full_info(ip, user, pwd, sn)` helper.
- `/device` returns 410 Gone with a deprecation message.

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
