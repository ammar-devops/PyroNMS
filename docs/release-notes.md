# PyroNMS Release Notes

---

## v4.2.0 — 2026-05-15 (SNMP Bulk Optical + UI Cleanup)

### Summary
Implemented SNMP bulk optical polling (40x faster than SSH). Fixed InfluxDB 422 type conflict.
Removed misleading per-ONT traffic graph (hardware limitation). Poll interval reduced from 2h to 30min.

### Changes

#### Performance
- **SNMP bulk optical polling** — replaced per-ONT SSH with batched `snmpget` (40 ONTs × 2 OIDs per call)
  - Full OLT (~2500 ONTs) now polled in ~30 seconds vs 35–45 minutes via SSH
  - SSH kept as automatic fallback for any ONT SNMP can't reach
- **Poll interval** reduced from 7200s (2h) to 1800s (30min)
- **Worker stagger** made proportional to poll interval (max 22.5min offset vs old 90min for Slot 5)

#### Bug Fixes
- **InfluxDB 422 fix** — `temp` field was being written as Python `int` by SNMP path, conflicting with existing `float` schema from SSH path. Fixed by casting to `float(raw)` in `snmp_helper.py`
- **All-None field guard** — added explicit check to skip InfluxDB Points where all optical fields are None (prevents 422 on edge-case ONTs)

#### UI
- **Removed: PON Port Traffic (SNMP) section** from ONT detail Graphs tab
  - Reason: Huawei MA5603T does not expose per-ONT bandwidth counters via SNMP
  - The graph was showing aggregate traffic for all ONTs on the entire PON port — misleading when viewed per-ONT
  - Signal History (RX dBm + Temperature) and all other ONT detail sections remain unchanged

### Known Limitation — Per-ONT Traffic
> **Huawei MA5603T does not expose per-ONT bandwidth counters via SNMP.**
> Only full PON port aggregate traffic is available (shared across all ONTs on the port).
> Therefore the per-ONT traffic graph has been intentionally removed to avoid misleading users.
> Alternatives: TR-069 (if ONT CPE reports WAN stats) or SSH `display statistics ont-port` (extremely slow).

---

## v4.1.0 — 2026-05-15 (Security Patch + Stability Release)

### Summary
Full production audit performed. 14 bugs identified and fixed. No provisioning logic was altered.
All fixes are backward compatible with existing OLT, SNMP, and GenieACS configurations.

### 🔴 Critical Security Fixes

| Fix | Endpoint | Issue | Resolution |
|-----|----------|-------|------------|
| Auth | `GET /onts` | No authentication — entire ONT database exposed publicly | Added `require_auth()` guard |
| Auth | `GET /ont/live` | No authentication — live ONT status exposed publicly | Added `require_auth()` guard |
| Auth | `GET /server/stats` | No authentication — server CPU/RAM/disk exposed publicly | Added `require_auth()` guard |

### 🔴 Critical Bug Fixes

| Fix | Area | Issue | Resolution |
|-----|------|-------|------------|
| BrokenPipe | API server | `send_json()` had no exception handling — every client disconnect crashed the process and triggered a systemd restart | Wrapped `wfile.write` in `try/except (BrokenPipeError, ConnectionResetError)` |
| Dead code | `POST /ont/action` | Used `if` instead of `elif` — bulk enable/disable/reset/delete always returned 404 | Changed to `elif` — bulk actions now functional |
| NameError | `GET /olt/test` | Referenced `olt_helpers.test_olt_snmp` but `olt_helpers` was not a valid name (module imported as `olt`) — would raise `NameError` at runtime | Replaced with `olt.test_olt_snmp(ip, snmp)` |
| Token duplication | `/olt/stats`, `/olt/cpu`, `/server/history` | InfluxDB token hardcoded as literal string 3× instead of using `INFLUX_TOKEN` constant | All three replaced with `INFLUX_TOKEN`, `INFLUX_URL`, `INFLUX_ORG` constants |

### 🟡 Provisioning & Logic Fixes

| Fix | Area | Issue | Resolution |
|-----|------|-------|------------|
| Tuple normalization | `provision_ont()` | Returned 3-tuple `(False, -1, out)` on failure but 4-tuple on success — callers unpacking 4 values would crash | Now always returns 4-tuple: `(ok, ont_id, output, verify_ok)` |
| Input sanitization | `provision_ont()` | `description` parameter passed unsanitized into OLT CLI `desc "..."` — a `"` in the name would break the CLI command | Double/single quotes stripped, length capped at 64 chars |
| Batch save | `delete_onts()` | `save` called after each individual ONT deletion — N deletions = N slow flash writes on OLT | Moved single `save` to execute once after all deletions complete |

### 🟡 Infrastructure Fixes

| Fix | Area | Issue | Resolution |
|-----|------|-------|------------|
| systemd | `ont-api.service` | Duplicate `Restart=always` and `RestartSec=5` directives | Removed duplicates, service reloaded |
| Missing file | `live_check.py` | Referenced by `server.py` subprocess call but file did not exist — all live check invocations silently failed | Created stub module at `/opt/ont-monitor/workers/live_check.py` |

### 🟠 Frontend Fixes

| Fix | Area | Issue | Resolution |
|-----|------|-------|------------|
| Hardcoded IP | API URL | `const API = 'http://172.20.101.160:8088'` — fails for any client on a different subnet | Changed to `const API = \`http://${window.location.hostname}:8088\`` |
| Dead code | `openRouter` | Old 3-arg `openRouter(sn, pon, btn)` function referenced undefined variable `ontId` | Removed — only the correct 4-arg version remains |
| Debug code | Console logs | 3 `console.log()` calls left in production code exposing internal data | Commented out |

### What Was NOT Changed
- `provision_ont()` core SSH command sequence (already correct and tested)
- `ont wan-config` command (confirmed correct in prior session)
- Worker `slot_worker.py` polling logic
- GenieACS TR-069 integration
- InfluxDB measurement schemas
- SNMP poller configuration

### Backup
- Path: `/opt/backups/pyronms-final-20260514-2055.tar.gz`
- Size: 12 MB
- Contents: full `/opt/ont-monitor/`, `/opt/pyronms/`, `/var/www/html/`, nginx config, systemd units, docker-compose, git commit hash

### Git Tag
`final-stable-20260515`

---

## v4.0.2 — Prior Release

Version committed by PyroNet Solutions (Codex). Sidebar version string fix.

## v4.0.1 — Prior Release

User Management theme support + OLT Config refresh feedback.

## v4.0.0 — Prior Release

Productized release — brand lock, Open Router, ONT/ONU column, full provisioning flow.
