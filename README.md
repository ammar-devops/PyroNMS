<div align="center">

# <img width="100" height="100" alt="PyroNet Solutions Logo" src="https://github.com/user-attachments/assets/f8f69570-f478-4a92-8c18-7270ae926e37"/>

### Network Management System for ISPs

[![Version](https://img.shields.io/badge/version-6.0.0-blue.svg)](https://github.com/PyroNet-Solutions/PyroNMS/releases)
[![Status](https://img.shields.io/badge/status-production-success.svg)]()
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()
[![Built for](https://img.shields.io/badge/OLT-Huawei%20MA5603T-orange.svg)]()
[![PyroGraphs](https://img.shields.io/badge/PyroGraphs-Cacti--style-f97316.svg)]()
[![Python](https://img.shields.io/badge/python-3.x-yellow.svg)](https://www.python.org/)
[![InfluxDB](https://img.shields.io/badge/InfluxDB-2.x-22ADF6.svg)](https://www.influxdata.com/)
[![TR-069](https://img.shields.io/badge/TR--069-GenieACS-9333ea.svg)](https://genieacs.com/)

**A purpose-built NMS for ISPs running Huawei GPON OLTs — real-time ONT monitoring, fault detection, TR-069 device management, and full provisioning lifecycle in one dashboard.**

**Now with [PyroGraphs](#-pyrographs) — a standalone Cacti-style SNMP graph engine for MikroTik / Cisco / Juniper / Huawei monitoring.**

🏢 *Developed and owned by [PyroNet Solutions](https://github.com/PyroNet-Solutions)* &nbsp;•&nbsp; 🔒 *Brand-locked productized release*

</div>

---

## ✨ Highlights

- 📊 **Real-time ONT monitoring** — RX signal, status, temperature, and VLAN at a glance
- ⚡ **Power-failure & fiber-cut detection** — automatic root-cause tagging via OLT SSH
- 📱 **Responsive UI** — desktop + mobile-friendly dashboard with auto-collapse sidebar
- 🛠️ **Full ONT lifecycle** — discover, provision, monitor, repair, decommission
- 🔍 **Unregistered ONT scanner** — SSH-based discovery with one-click registration
- 🔌 **Service-port aware provisioning** — VLAN-10 default flow with VAS Profile support
- 🪟 **Per-ONT Manager popup** — click any row for live SSH/SNMP data with model-aware theming (Huawei/ZTE/generic)
- 📈 **InfluxDB time-series** — historical metrics, trend analysis, alerting-ready

---

## 🚀 Features at a Glance

| Module                      | Capabilities                                                |
|-----------------------------|-------------------------------------------------------------|
| 🖥️ **Dashboard**             | Live counts: Total, Active, Offline, Power Down, Fiber Down, Weak Signal, Unregistered |
| 📋 **ONT List**              | Filter by PON, status, signal range, customer name, serial — sortable everywhere |
| 🛰️ **OLT Management**        | Multi-OLT inventory, config DB, profile parsing             |
| 👷 **Worker Manager**        | Background SNMP/SSH pollers, throttling, queue introspection |
| 💾 **Backup Manager**        | Snapshot OLT configs on schedule                            |
| 🖧 **Server Monitor**        | CPU, RAM, disk, network — operator-facing health view       |
| 🔒 **Auth & RBAC**           | Multi-user, role-based access (superadmin / admin / viewer) |
| 🎨 **Theming & Branding**    | Light/dark themes, custom logo and company name             |

---

## 🔥 PyroGraphs

**Cacti-style SNMP graph engine** — separate product, single repo, runs at `http://<server>/graphs`. Independent of the ONT/OLT dashboard but shares the same auth + nginx + API host.

> 🎯 Built for the moment you outgrow Cacti — same UX, modern stack, half the friction.

### What it monitors
- **MikroTik** — interface traffic, CPU, RAM, temperature, **PPPoE active sessions** (via RouterOS API)
- **Huawei OLT** — GPON port traffic, ethernet uplinks, CPU, memory
- **Cisco / Juniper / Linux / Windows / Generic** — Host Resources MIB + vendor-specific OIDs

### Spine-style poller
- ThreadPoolExecutor (8 workers, tunable) — one slow device can't block others
- Exponential backoff retry (3 attempts × 500ms × 2^attempt + jitter)
- Per-device states: `online` · `degraded` (succeeded after retries) · `offline`
- Bulk SNMP via `snmpbulkwalk -Oqn` with leading-dot OID normalization
- Counter wrap handling, 64-bit ifHCInOctets/ifHCOutOctets
- Per-cycle telemetry written to InfluxDB as `net_poll_health` measurement

### Cacti-grade UI
- 6 top tabs: **Graphs · Console · Devices · Templates · Poller · Logs**
- Console nav: Devices · Graph Management · Graph Templates · Tree Management · Poller
- **Tree Mode** toggle on Graphs — vendor-grouped view OR user-defined folder/device/graph hierarchy
- 12-point time range bar (30m · 1h · 2h · 4h · 6h · 12h · 1d · 2d · 1w · 2w · 1m · 1y)
- Hide-zero-traffic filter (server-side via Flux `max() == 0` check)
- Type-aware y-axis: bps / sessions / % / °C
- Click-to-zoom modal with full chart + range selector
- Bulk-delete in Graph Management, per-device grouping, search, filters
- Real Cacti stats per card: **Current · Avg · Max · Total In / Out** — server-computed via Flux, 5s cache
- Real-time Logs viewer tailing the poller log with level filter + auto-refresh

### InfluxDB measurements
| Measurement | Tags | Fields | Use |
|---|---|---|---|
| `net_iface` | device_id, device_name, interface, if_index, if_type, vlan_id, vendor | rx_bps, tx_bps, rx_errors, tx_errors, rx_drops, tx_drops, oper_status | Per-interface traffic |
| `net_resource` | device_id, device_name, vendor | cpu_pct, mem_pct, mem_used, mem_total, uptime_sec, temp_c, pppoe_sessions | Device health |
| `network_pppoe_sessions` | device_id, device_name, profile, service | active_count | PPPoE breakdown (total + per-profile + per-service) |
| `net_poll_health` | device_id, device_name | poll_duration_ms, lines_written, interfaces, retries | Poller telemetry |

### `/network/*` API endpoints (subset)
```
GET  /network/devices                       — list (snmp_community visible, secrets masked)
GET  /network/devices/{id}                  — single device + creds (auth required)
GET  /network/graph-preview?device_id=N&range=1h[&hide_zero=1]
GET  /network/graph-data?graph_id=N&range=1h
GET  /network/graph-stats?graph_id=N&range=1h    — real Cacti current/avg/max/total
GET  /network/templates                      — 15 builtin (Generic/MikroTik/Cisco/Juniper/Huawei + PPPoE)
GET  /network/tree                           — user-defined folder hierarchy
GET  /network/poller/status                  — cycles, failures, retries, avg_poll_ms, degraded
GET  /network/logs?level=&since=&limit=      — tails poller log
POST /network/devices                        — add
POST /network/devices/{id}                   — update
POST /network/devices/{id}/test              — live SNMP probe
POST /network/devices/{id}/discover          — walk ifTable + auto-create graphs
POST /network/devices/{id}/delete            — cascade delete
POST /network/tree                           — add folder/device/graph node
```

### Quick start
```bash
# PyroGraphs ships in the same repo — assets go to /var/www/html/graphs/
cp -r web/graphs /var/www/html/

# nginx /etc/nginx/sites-enabled/default already routes /graphs to the SPA
# (location /graphs { try_files $uri $uri/ /graphs/index.html =404; })

# Poller service
cp docs/pyronms-network-poller.service /etc/systemd/system/
systemctl enable --now pyronms-network-poller

# Open
xdg-open http://<server-ip>/graphs
```

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Web Browser                                 │
│  PyroNMS  http://host/         |  PyroGraphs  http://host/graphs     │
│  (ONT dashboard, provisioning) |  (Cacti-style SNMP graphs)          │
└────────────────────────┬─────────────────────────────────────────────┘
                         │ HTTPS / JSON
┌────────────────────────▼─────────────────────────────────────────────┐
│         Python ThreadingHTTPServer  (port 8088)                      │
│  Auth · ONT CRUD · OLT control · /network/* PyroGraphs API           │
└──┬──────────────┬───────────────────┬───────────────────────┬────────┘
   │ SSH/SNMP     │ SQLite (auth+net) │ InfluxDB 2.x          │ HTTP
   ▼              ▼                   ▼                       ▼
┌──────────┐ ┌──────────────────┐ ┌─────────────────┐ ┌───────────────┐
│ Huawei   │ │ users.db         │ │ olt_monitoring  │ │ Static assets │
│ OLT      │ │ network_devs.db  │ │ network_monit.. │ │ (nginx)       │
│ MA5603T  │ │ graph_tree       │ │ net_iface/...   │ │               │
└──────────┘ └──────────────────┘ └─────────────────┘ └───────────────┘
       ▲                                ▲
       │ legacy worker                  │ new worker
       │ (ONT/OLT polling)              │ (PyroGraphs)
┌──────┴───────────────┐ ┌──────────────┴────────────────────────────┐
│ ont-monitor workers  │ │ pyronms-network-poller.service            │
│ SNMP-first, OID disc │ │ ThreadPool 8 workers, retries+backoff,    │
│                      │ │ bulk-walk, PPPoE via librouteros API      │
└──────────────────────┘ └───────────────────────────────────────────┘
```

---

## 🧰 Tech Stack

| Layer        | Tools                                                                  |
|--------------|------------------------------------------------------------------------|
| 🎨 Frontend   | HTML5 · CSS3 · Vanilla JS · Chart.js 4                                |
| ⚙️ Backend    | Python 3 · ThreadingHTTPServer · Netmiko · net-snmp · librouteros     |
| 🗄️ Storage    | SQLite (users, OLTs, network_devices, graph_tree) · InfluxDB 2.x      |
| 🌐 Web Layer  | Nginx (static + reverse proxy + cache-control no-store)               |
| 🛜 Devices    | Huawei MA5603T OLT · GPON ONTs · TR-069 routers · MikroTik · Cisco · Juniper |

---

## ⚡ Quick Start

```bash
# 1. Place web files
cp -r web/* /var/www/html/

# 2. Install API
cp -r api /opt/ont-monitor/
systemctl enable --now ont-api.service

# 3. Start workers
cp -r workers /opt/ont-monitor/
systemctl enable --now ont-worker.service

# 4. Open dashboard
xdg-open http://<server-ip>/
```

> 💡 **Tip:** First-time setup requires `config/config.py` populated with your OLT credentials and InfluxDB token.

---

## 📂 Repository Layout

```
PyroNMS/
├── 📁 web/
│   ├── index.html          # PyroNMS — ONT / OLT dashboard
│   ├── settings.html       # admin UI
│   └── 📁 graphs/          # PyroGraphs — standalone Cacti SPA
│       └── index.html      # self-contained (CSS + JS inline)
├── 📁 api/
│   └── server.py           # ThreadingHTTPServer — ONT + /network/* endpoints
├── 📁 auth/                # JWT-style token store (users.db)
├── 📁 workers/
│   ├── slot_worker.py             # legacy ONT/OLT poller
│   ├── network_db.py              # PyroGraphs SQLite CRUD
│   ├── network_snmp.py            # net-snmp subprocess wrappers
│   ├── network_templates.py       # 15 builtin graph templates
│   └── network_poller.py          # Spine-style network poller
├── 📁 config/              # Runtime configuration (InfluxDB tokens, etc.)
├── 📁 olt-config/          # OLT profiles + parsed config artifacts
├── 📁 docs/
│   ├── pyronms-network-poller.service     # systemd unit for PyroGraphs poller
│   ├── release-notes.md
│   └── ...
└── 📄 *.md                 # Phase docs + changelog
```

---

## 📜 Version History

| Version    | Highlight                                                  |
|------------|------------------------------------------------------------|
| 🥚 v2.1    | Base dashboard                                             |
| 🔐 v2.2    | Authentication system                                      |
| 🎨 v2.3    | Themes and branding                                        |
| 🧭 v2.4    | Side panel, OLT management, server monitor                 |
| 📊 v2.5    | Dashboard cards and resource monitor                       |
| 🩹 v2.5.1  | Provisioning + unregistered ONT reliability fixes          |
| 🚀 v2.6.0  | SNMP-first architecture, dashboard rebuild, perf + UX overhaul |
| 🧹 v2.7.0  | GenieACS removed, new ONT Manager popup, slimmer table     |
| 🛠️ v2.8.0  | Bulk Actions (Enable/Disable/Reset/Restore/Delete), checkbox selection, robust ONT detail parsing, ONT/ONU detection |
| 🟢 v2.8.1  | Status overlay refinements                                 |
| ⏳ v2.8.2  | Refresh buttons overhaul — top progress bar, button spinners, fixed row Refresh icon |
| 🌐 v2.9.0  | ONT WAN + WLAN configuration view in popup (read-only, no TR-069) |
| 🩹 v2.9.1  | Hotfix: WLAN card graceful degradation for HG8245 / bridge-mode ONTs that don't expose WLAN via OLT CLI |
| 🛠️ v3.0.0  | Edit mode for PPPoE credentials and WiFi password in popup            |
| 🪟 v3.1.0  | Full-page ONT Manager (U2000-style) with 4 tabs                       |
| 📡 v3.2.0  | Extended WLAN settings — auth mode, encryption, channel dropdowns     |
| 🔁 v3.3.0  | GenieACS popup restored; SSH fallback for offline ONTs                |
| 🎯 v3.4.0  | Router-admin popup, single Apply button, Clients tab, full data parse |
| 🚀 **v4.0.0** | **Productized release** — brand lock, Open Router column, ONT/ONU type column, theme-aware status colors |
| 🔒 **v4.1.0** | **Security & stability patch** — auth on all data endpoints, BrokenPipe crash fix, bulk actions fix, dynamic API URL, provisioning hardening |
| 🔥 **v5.0.0** | **PyroGraphs launch** — standalone Cacti-style SPA at `/graphs`, multi-vendor SNMP poller, 6 top tabs, basic Console |
| ⚙️ **v5.1.0** | **Cacti-grade backend** — Spine-style poller, exponential-backoff retry, server-side current/avg/max/total via Flux, real degraded state |
| 🐛 **v5.1.x** | **Critical SNMP fix** — net-snmp `-Oqn` leading-dot OID bug silently dropped ALL interface counters for 2782 poll cycles; ThreadingHTTPServer; CORS preflight |
| 🎨 **v5.2.0** | **Tree Mode + hide-zero filter** — user-defined folder hierarchy, server-side zero-traffic filter, Graph Management bulk-delete + per-device groups |
| 📡 **v5.3.0** | **PPPoE support** — new `network_pppoe_sessions` measurement (profile + service tags), MikroTik RouterOS API integration, builtin PPPoE template |
| 🛠️ **v5.4.0** | **Device-edit persistence fix** — `_DEVICE_PUBLIC` mask was hiding `snmp_community` from GET (Edit modal always pre-filled "public"); full modal with port/timeout/retries/poll-* fields |
| ✅ **v5.4.2** | **Null-safe modal handlers + visible version pill + cache-bust meta** — Stable PyroGraphs build |
| 🌳 **v6.0.0** | **True-Cacti structure + hierarchical Tree** — top nav collapsed to Graphs + Console (everything else inside Console), real drag-drop Tree Management with cycle prevention + cascade delete, expand/collapse Tree Mode on Graphs |

### 🔭 Current Release — `v6.0.0` (Final Stable)

**Tag:** `v6.0.0` · **Branch:** `feature/network-graphs`

PyroGraphs is verified production-ready against real hardware:
- 🇲🇰 **MikroTik HP-MKT** (RouterOS x86, 103.125.179.29): polling at ~1.4s, **~2,300 active PPPoE sessions**, real-time RX/TX on sfp1/sfp2 (4-5 Gbps)
- 🇭🇼 **Huawei HP-OLT** (MA5600, 172.20.101.101): 50 graphs across 48 GPON ports + 2 ethernet uplinks
- 📊 **Poller**: 8-worker ThreadPoolExecutor, 0 retries on normal cycles, 7-second avg cycle, no degraded polls
- 🔒 **Auth**: JWT-style via `ont_token` localStorage, all endpoints behind `require_auth`
- 🎯 **API**: ThreadingHTTPServer so 6 parallel graph-stats fetches no longer serialize

### 🩹 v5.4.x specific fixes
- 🔓 **Edit-modal save persistence** — `snmp_community` (+ v3 config) added to `_DEVICE_PUBLIC` so GET returns it; modal pre-fills real DB value
- 🛡️ **Vendor preservation** — discover endpoint no longer auto-overwrites user-set vendor unless detection is more specific
- ✋ **Null-safe DOM access** — every `getElementById` in modal handlers wrapped (`_pgSet` / `_v` / `_chk`) so stale-cached HTML can't kill the click handler
- 🔄 **Cache-bust meta tags** + visible `v5.4.2` pill so users see which version loaded
- 🧪 **Better error surface** — `update_device` now returns 400 with `editable_fields[]` list instead of misleading 404
- 🐌 **Counter walk retries removed** — was wasting 5-10s/cycle on devices with idle interfaces (HP-OLT: 13.7s → 7.7s)

---

## 🛣️ Roadmap (post-v5.4.2)

- 📡 Alerting & on-call notifications (email / Telegram / webhook) for both PyroNMS and PyroGraphs
- 📊 Per-profile + per-service PPPoE breakdown graphs (data is already in InfluxDB)
- 🌳 Drag-and-drop tree builder for PyroGraphs
- 📥 Bulk device import (CSV / YAML)
- 🔑 SNMPv3 full credential UI (auth_pass + priv_pass)
- 🐳 Docker / one-line installer
- 🤖 GitHub Actions CI (lint, test, deploy preview)
- 🔑 License-key gating for commercial deployments

---

## 🤝 Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a PR.
Bug reports, feature requests, and feedback are welcome — please file an issue.

---

## 📄 License

🔒 **Private / internal project.** Unauthorized redistribution is not permitted.

---

<div align="center">

Built with ❤️ by **PyroNet Solutions** &nbsp;•&nbsp; Maintained by [@ammar-devops](https://github.com/ammar-devops)

⭐ *If this saved you a U2000 license, drop a star.*

</div>
