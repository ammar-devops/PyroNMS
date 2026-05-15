# PyroNMS SNMP Reference

---

## Huawei MA5603T — Validated OID Map

All OIDs verified via live `snmpget` against Slot 1/Port 0/ONT 0 and Slot 5/Port 10/ONT 0 on firmware V800R018.

### ONT Info (`hwGponOntInfoTable` — `.43`)

| OID Suffix | Full OID | Type | Description |
|------------|----------|------|-------------|
| `.43.1.3` | `...2.43.1.3` | Hex-STRING (8 bytes) | ONT Serial Number |
| `.43.1.9` | `...2.43.1.9` | STRING | Customer description |
| `.43.1.10` | `...2.43.1.10` | INTEGER | Status: 1=online, 2=offline |

### ONT Optical (`hwGponOntOpticalInfoTable` — `.51`)

| OID Suffix | Full OID | Raw example | Scale | Actual value | Field |
|------------|----------|-------------|-------|--------------|-------|
| `.51.1.1` | `...2.51.1.1` | 60, 43 | ×1 (direct °C) | 60°C, 43°C | `temp` |
| `.51.1.3` | `...2.51.1.3` | 226, 197 | unknown | — | (not used) |
| `.51.1.4` | `...2.51.1.4` | -2796, -1876 | ÷100 → dBm | -27.96, -18.76 dBm | `rx_power` |
| `.51.1.5` | `...2.51.1.5` | 3360, 3240 | ÷1000 → dBm | 3.360, 3.240 dBm | `tx_power` |
| `.51.1.6` | `...2.51.1.6` | 6778, 7875 | ÷1000 → dBm | 6.778, 7.875 dBm | `olt_rx` (OLT-side RX) |
| `.51.1.7` | `...2.51.1.7` | 2147483647 | — | INT_MAX = no reading | (skip) |

**Full base OID:** `1.3.6.1.4.1.2011.6.128.1.1.2`

> ⚠️ **Common mistake:** `.51.1.6` is **OLT-side RX power**, NOT temperature. Temperature is `.51.1.1` (direct °C integer, no division needed). The original `poller.py` had this wrong — fixed in v4.3.0.

### Standard IF-MIB (Traffic)

| OID | Description |
|-----|-------------|
| `1.3.6.1.2.1.31.1.1.1.6` | `ifHCInOctets` — 64-bit RX bytes |
| `1.3.6.1.2.1.31.1.1.1.10` | `ifHCOutOctets` — 64-bit TX bytes |

Used for PON port aggregate traffic only — not per-ONT (hardware limitation).

---

## ifIndex Formula

Huawei GPON PON port ifIndex (verified on V800R018):

```
BASE      = 0xFA000000  (4194304000)
ifIndex   = BASE | (slot << 13) | (port << 8)

Reverse:
  offset = ifIndex - BASE
  slot   = offset >> 13
  port   = (offset & 0x1F00) >> 8
```

Examples:
| Slot | Port | ifIndex (hex) | ifIndex (decimal) |
|------|------|--------------|-------------------|
| 1 | 0 | 0xFA002000 | 4194312192 |
| 5 | 10 | 0xFA00AA00 | 4194353664 |

> ⚠️ **Off-by-one bug (historical):** Early versions used `(slot-1) << 13` — WRONG. Use `slot << 13`.

---

## SNMP Community String

Community: `huawei123` (read-only, v2c)

Check current config:
```bash
cat /opt/pyronms/config/olts.json
```

---

## Per-ONT Traffic — Hardware Limitation

The MA5603T does **not** expose per-ONT byte counters via SNMP. The `hwGponOntOpticalInfoTable` provides only optical metrics.

Available via SNMP: PON **port** aggregate traffic (all ONTs combined on that port).  
Not available via SNMP: individual ONT download/upload bandwidth.

Alternatives (not implemented):
- TR-069/GenieACS: some CPE models report WAN traffic stats
- SSH `display statistics ont-port`: ~300 SSH commands per slot per poll — not feasible at scale

---

## SNMP Polling Architecture

### slot_worker (reliable, per-port chunked snmpget)
- Polls 40 ONTs × 2 OIDs per `snmpget` call
- ~30s for full OLT (~2500 ONTs) vs 35–45 min via SSH
- SSH optical used as fallback for any ONT SNMP can't reach

### pyronms-poller (GETBULK walk, unreliable for optical)
- `bulkCmd` GETBULK for full OLT optical → times out ~80% of attempts
- Reliable for: status (60s), PON traffic (120s)
- Unreliable for: optical (300s interval, GETBULK too slow for MA5603T)
- **Do not depend on poller.py for optical data** — use slot_worker SNMP optical
