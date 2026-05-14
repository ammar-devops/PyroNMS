# PyroNMS Troubleshooting Guide

---

## API Issues

### API keeps restarting (BrokenPipeError)

**Symptom:** `journalctl -u ont-api` shows repeated `BrokenPipeError: [Errno 32] Broken pipe` followed by systemd restart.

**Root cause (fixed in v4.1.0):** The `send_json()` method had no exception handling. When a browser closed a tab or request timed out before the large `/onts` response finished writing, the broken socket caused an uncaught exception that crashed the Python process.

**Fix applied:** `send_json()` now wraps `wfile.write()` in `try/except (BrokenPipeError, ConnectionResetError)`.

**If still occurring after update:** Check if the API is running the updated file:
```bash
head -5 /opt/ont-monitor/api/server.py
systemctl restart ont-api
```

---

### `GET /onts` returns 401 after update

**Expected behaviour.** `GET /onts` was previously unauthenticated (security bug, fixed v4.1.0). The browser dashboard sends an `Authorization` header automatically — if you're logged in the dashboard will still work.

If testing with `curl`:
```bash
# Get a token first
TOKEN=$(curl -s -X POST http://localhost:8088/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_PASS"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -H "Authorization: Bearer $TOKEN" http://localhost:8088/onts | python3 -m json.tool | head -20
```

---

### Bulk Actions (Enable/Disable/Reset/Delete) returning 404

**Root cause (fixed in v4.1.0):** `POST /ont/action` used `if` instead of `elif` in `do_POST()`. The preceding `else: return 404` caught it first, making bulk actions dead code.

**Fix applied:** Changed to `elif`. After update + `systemctl restart ont-api`, bulk actions work.

---

### `/olt/test` SNMP test crashing with NameError

**Root cause (fixed in v4.1.0):** Code referenced `olt_helpers.test_olt_snmp` but the module was imported as `olt` (`import olt_helpers as olt`). Any call to this endpoint would raise `NameError: name 'olt_helpers' is not defined`.

**Fix applied:** Replaced with `olt.test_olt_snmp(ip, snmp)`.

---

## Provisioning Issues

### ONT provisioned but PPPoE not connecting

**Most common cause:** `ont wan-config` step was skipped or used wrong command.

**Correct command** (inside `interface gpon` context):
```
ont wan-config <port> <ONTID> ip-index 1 profile-name PPP-10-IPV4-IPV6
```

**Wrong commands (do NOT use):**
- `ont vas-profile ...` — does not exist on MA5603T
- Running `ont wan-config` in global config — must be inside gpon interface

**Check via U2000:** ONT → WAN → should show profile `PPP-10-IPV4-IPV6` assigned.

---

### ONT provisioned but TR-069 profile not assigned (shows `--` in U2000)

**Root cause:** The `ont tr069-server-config` command uses `profile-id` with a numeric ID (e.g. `1`), not a text name like `ACS`.

**Correct command:**
```
ont tr069-server-config <port> <ONTID> profile-id 1
```

**Common mistake:** Using `profile-name ACS` or the wrong context (global config instead of inside `interface gpon`).

---

### ONT not appearing in dashboard after provision

Normal behaviour — workers poll every 7200 seconds (2 hours). After provision, a synthetic `ont_status` data point is written to InfluxDB immediately so the ONT appears in the list within 30 seconds without waiting for the next worker cycle.

If ONT still not appearing after 1 minute, check:
```bash
journalctl -u ont-api -n 50 --no-pager | grep -i "influx\|provision\|seed"
curl -s "http://localhost:8088/onts" -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; onts=json.load(sys.stdin)['onts']; print(len(onts),'ONTs')"
```

---

### `description` field causing CLI error during provision

**Root cause (fixed in v4.1.0):** Customer names with `"` or `'` characters would break the OLT CLI `desc "..."` quoting.

**Fix applied:** `provision_ont()` now strips `"` and `'` from description before sending to OLT, and caps at 64 characters.

---

## Worker Issues

### Pattern not detected / read_timeout

**Symptom:** `journalctl -u ont-worker@1` shows `Pattern not detected` for `display ont optical-info` commands.

**Root cause:** Some OLT responses take longer than Netmiko's `read_timeout`. This is OLT-side latency, not a bug. Workers use `_read_until_prompt()` with extended timeouts and retry logic to handle this.

**Action:** No fix needed. Occasional timeouts are expected on busy PON ports with 80+ ONTs.

---

## Database Issues

### `/ont/traffic` returns 404 — SN not in SNMP cache

The SNMP `pon_traffic` endpoint requires the ONT's FSP to be in `ont_map.db` (populated by the SNMP poller/worker). This file is at `/opt/pyronms/data/ont_map.db`.

Check if SN is in the cache:
```bash
sqlite3 /opt/pyronms/data/ont_map.db "SELECT * FROM ont_map WHERE sn LIKE '%LAST8CHARS%';"
```

If the table doesn't exist or is empty, the SNMP poller hasn't run yet. Check worker logs:
```bash
journalctl -u ont-worker@1 -n 100 --no-pager | grep -i "snmp\|ont_map"
```

---

## Frontend Issues

### Dashboard shows wrong server / can't connect

**Root cause (fixed in v4.1.0):** API URL was hardcoded to `http://172.20.101.160:8088`. The fix auto-detects from `window.location.hostname`.

If still seeing connection errors, clear browser cache (Ctrl+Shift+R) to reload the updated `index.html`.

---

### Open Router button does nothing / wrong IP in URL

The Open Router feature fetches WAN IP via `/ont/wan-ip`. If the ONT has no WAN IP in InfluxDB cache, it falls back to a live SSH/GenieACS lookup. Possible reasons:

1. ONT is offline — no WAN IP assigned
2. GenieACS has not contacted the ONT yet (TR-069 not configured)
3. ONT is ONU (bridge mode) — WAN IP is on downstream router, not the ONT

---

## Service Recovery

```bash
# Full service restart sequence
systemctl daemon-reload
systemctl restart ont-api
systemctl restart ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5

# Check all healthy
systemctl is-active ont-api ont-worker@1 ont-worker@2 ont-worker@4 ont-worker@5 nginx

# Verify API
curl -s http://localhost:8088/health
```
