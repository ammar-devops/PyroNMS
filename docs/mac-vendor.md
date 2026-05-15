# PyroNMS — MAC Vendor Lookup

## Overview

The ONT List table displays a **Vendor** column showing the CPE manufacturer name (e.g. "TP-Link Technologies", "Huawei Technologies") derived from the WAN MAC address of each ONT/router.

---

## How It Works

```
GenieACS (TR-069)
  └─▶ /devices bulk query (WAN PPP/IP MACAddress)
       └─▶ sn_mac table (SN → MAC)
            └─▶ OUI prefix (first 3 octets)
                 └─▶ mac_vendor table (OUI → Vendor)
                      └─▶  cache HIT → return vendor
                           cache MISS → macvendors.com API → store → return vendor
```

### Startup Prefetch

When `ont-api` starts, a background thread waits 10 seconds then:

1. Bulk-queries GenieACS NBI for ALL registered devices with their WAN MAC addresses (single HTTP call to local GenieACS)
2. Stores `SN → MAC` mappings in the `sn_mac` table
3. For each new OUI prefix not yet cached, calls `macvendors.com` at 1 request/second
4. Stores results in the `mac_vendor` table

This means the Vendor column is fully populated within a few minutes of first startup (depending on number of unique manufacturers).

### Per-Request Enrichment

Every `GET /onts` call:
- Reads `sn_mac` for each ONT's SN → gets MAC
- Reads `mac_vendor` for the OUI prefix → gets vendor name
- Returns `vendor` field alongside each ONT row
- **No live external calls** — cache-only at request time (keeps /onts fast)

---

## Cache Location

**SQLite database:** `/opt/ont-monitor/data/mac_vendor_cache.db`

### Tables

```sql
-- Maps ONT Serial Number to WAN MAC address
CREATE TABLE sn_mac (
    sn   TEXT PRIMARY KEY,   -- e.g. HWTC5A819F9D
    mac  TEXT,               -- e.g. AA:BB:CC:DD:EE:FF
    ts   INTEGER             -- Unix timestamp of last update
);

-- Maps OUI prefix to vendor name (shared across all devices from same manufacturer)
CREATE TABLE mac_vendor (
    oui          TEXT PRIMARY KEY,  -- e.g. AA:BB:CC (first 3 octets)
    vendor       TEXT,              -- e.g. TP-Link Technologies
    last_checked INTEGER,           -- Unix timestamp
    source       TEXT               -- 'macvendors.com'
);
```

### Inspect Cache

```bash
sqlite3 /opt/ont-monitor/data/mac_vendor_cache.db "SELECT COUNT(*) FROM sn_mac;"
sqlite3 /opt/ont-monitor/data/mac_vendor_cache.db "SELECT COUNT(*) FROM mac_vendor;"
sqlite3 /opt/ont-monitor/data/mac_vendor_cache.db "SELECT oui, vendor FROM mac_vendor LIMIT 10;"
sqlite3 /opt/ont-monitor/data/mac_vendor_cache.db \
  "SELECT s.sn, s.mac, v.vendor FROM sn_mac s LEFT JOIN mac_vendor v ON v.oui=s.mac LIMIT 10;"
```

---

## API Endpoints

### GET /onts
Each ONT object now includes:
```json
{
  "sn": "HWTC5A819F9D",
  "vendor": "TP-Link Technologies",
  ...
}
```
Possible values:
| Value | Meaning |
|-------|---------|
| `"TP-Link Technologies"` | Vendor found in cache |
| `null` | No MAC available (device not in GenieACS / ONU bridge) |
| `"Unknown"` | MAC found but OUI not in macvendors.com database |
| `"Lookup Pending"` | macvendors.com timed out during prefetch (will retry next restart) |

### GET /mac/vendor?mac=\<mac\>
On-demand vendor lookup for any MAC address. Checks cache first, calls macvendors.com on miss.

```bash
TOKEN=$(curl -s -X POST http://localhost:8088/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"YOUR_PASS"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl "http://localhost:8088/mac/vendor?mac=00:1A:2B:3C:4D:5E" \
  -H "Authorization: Bearer $TOKEN"
# {"mac": "00:1A:2B:3C:4D:5E", "vendor": "Cisco Systems"}
```

Supported MAC formats: `00:1A:2B:3C:4D:5E`, `00-1A-2B-3C-4D-5E`, `001A2B3C4D5E`, `001a.2b3c.4d5e`

---

## Rate Limit Handling

- API endpoint used: `https://api.macvendors.com/<MAC>` (not the old macvendors.com/api path)
- macvendors.com is called **at most once per OUI prefix** (not per device)
- Prefetch rate is **1 call/second** — a typical deployment with 5 unique manufacturers = 5 seconds total
- Vendor lookup results are cached **indefinitely** (no expiry — MAC OUI assignments rarely change)
- If macvendors.com times out (3s timeout), result is `"Lookup Pending"` and is NOT cached — will retry on next server restart

---

## Failure Modes

| Scenario | Frontend shows | Cached? |
|----------|---------------|---------|
| ONT not in GenieACS (ONU bridge) | `--` | N/A |
| MAC present, OUI in cache | Vendor name | Yes |
| MAC present, OUI not in macvendors.com | `Unknown` | Yes |
| macvendors.com unreachable / timeout | `Lookup Pending` | No (retried next restart) |
| Invalid MAC format | `--` | N/A |

---

## How to Clear Vendor Cache

```bash
# Remove and restart — prefetch will rebuild from GenieACS on next startup
rm /opt/ont-monitor/data/mac_vendor_cache.db
systemctl restart ont-api
```

To clear only vendor names (force re-lookup) while keeping SN→MAC mappings:
```bash
sqlite3 /opt/ont-monitor/data/mac_vendor_cache.db "DELETE FROM mac_vendor;"
systemctl restart ont-api
```

---

## Privacy / Security

- Only the MAC OUI prefix (first 3 octets) is sent to macvendors.com
- No customer names, IPs, PPPoE credentials, SN, or any other data is sent externally
- The macvendors.com call is made from the **backend server**, not the browser
- Frontend only renders the vendor string returned by `/onts` — no external calls
