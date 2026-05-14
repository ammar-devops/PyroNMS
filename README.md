<div align="center">

# <img width="100" height="100" alt="PyroNet Solutions Logo" src="https://github.com/user-attachments/assets/f8f69570-f478-4a92-8c18-7270ae926e37"/>

### Network Management System for ISPs

[![Version](https://img.shields.io/badge/version-4.1.0-blue.svg)](https://github.com/PyroNet-Solutions/PyroNMS/releases)
[![Status](https://img.shields.io/badge/status-production-success.svg)]()
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()
[![Built for](https://img.shields.io/badge/OLT-Huawei%20MA5603T-orange.svg)]()
[![Python](https://img.shields.io/badge/python-3.x-yellow.svg)](https://www.python.org/)
[![InfluxDB](https://img.shields.io/badge/InfluxDB-2.x-22ADF6.svg)](https://www.influxdata.com/)
[![TR-069](https://img.shields.io/badge/TR--069-GenieACS-9333ea.svg)](https://genieacs.com/)

**A purpose-built NMS for ISPs running Huawei GPON OLTs — real-time ONT monitoring, fault detection, TR-069 device management, and full provisioning lifecycle in one dashboard.**

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

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                          Web Browser                           │
│   (HTML5 + Vanilla JS + Chart.js  →  cached-first rendering)   │
└────────────────────────┬───────────────────────────────────────┘
                         │ HTTPS / JSON
┌────────────────────────▼───────────────────────────────────────┐
│              Python HTTP API  (port 8088)                      │
│        Auth · ONT CRUD · OLT control · Provisioning            │
└────┬────────────────┬───────────────────┬──────────────────────┘
     │ SSH (Netmiko)  │ SQLite (auth)     │ InfluxDB 2.x
     ▼                ▼                   ▼
┌──────────────┐ ┌──────────────┐ ┌─────────────────────────┐
│ Huawei OLT   │ │ users.db     │ │ Time-series metrics     │
│ MA5603T      │ │              │ │ (signal, status, perf)  │
└──────────────┘ └──────────────┘ └─────────────────────────┘
            ▲
            │ SNMP polling
┌───────────┴────────────────────────────────────────────────────┐
│              Workers  (background pollers)                     │
│   SNMP-first WAN cache · OID discovery · ONT mapper            │
└────────────────────────────────────────────────────────────────┘
```

---

## 🧰 Tech Stack

| Layer        | Tools                                                |
|--------------|------------------------------------------------------|
| 🎨 Frontend   | HTML5 · CSS3 · Vanilla JS · Chart.js                |
| ⚙️ Backend    | Python 3 · HTTP server · Netmiko · SNMP            |
| 🗄️ Storage    | SQLite (users/config) · InfluxDB 2.x (metrics)     |
| 🌐 Web Layer  | Nginx (static + reverse proxy)                      |
| 🛜 Devices    | Huawei MA5603T OLT · GPON ONTs · TR-069 routers    |

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
├── 📁 web/          # Frontend (index.html, settings.html, assets)
├── 📁 api/          # Python HTTP API + OLT helpers
├── 📁 auth/         # Authentication DB schema and helpers
├── 📁 workers/      # Background SNMP / SSH poller jobs
├── 📁 config/       # Runtime configuration
├── 📁 olt-config/   # OLT profiles + parsed config artifacts
└── 📄 *.md          # Phase docs + changelog
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

### 🔭 Current Release — `v4.0.0` (Productized)

- 🔒 **Brand-locked** — Product identity, logo, and company branding fixed to **PyroNet Solutions**. Branding edit UI removed from settings. White-label resale prevented.
- 🌐 **Open Router column** — Per-row 🛜 button in ONT list that opens the customer's router web UI (`http://<wan-ip>`). Resolves WAN IP via **OLT direct** (InfluxDB worker cache → SSH `display ont wan-info` fallback). **Zero GenieACS dependency.**
- 🏷️ **ONT/ONU column** — New Type column showing whether each device is an **ONT** (router CPE) or **ONU** (bridge L2). Derived from cached WAN state; confirmed via OLT `display ont info by-sn` when popup opens. Smart short-circuit — clicking Open Router on an ONU shows an explanatory toast instead of a wasted SSH call.
- 🎨 **Theme-aware status colors** — Fiber Down, Power Down, Weak Signal, Critical, Unregistered badges now adapt to Light / Dark / Pyro themes. Status pill colors come from per-theme CSS tokens (`--st-{state}-fg/bg/border`) — readable on every background.
- 🛰️ **GenieACS popup overhaul** (v3.3 → v3.4) — Router-admin style popup with sidebar nav (Status · Internet · LAN · Wi-Fi · Clients · Management), iOS-style toggles, security/channel/TX-power dropdowns, **single "Apply Changes" button** in header (no more per-field saves), pulsing status dot, last-seen relative time, source badge (`✓ GenieACS · editable` vs `⚠ OLT SSH · read-only`).
- 👥 **Clients tab** — Per-ONT table of every connected device (LAN ethernet + Wi-Fi), merged from `LANDevice.1.Hosts.Host.*` and `WLAN.AssociatedDevice.*`, deduped by MAC. Stat tiles (Total · Wi-Fi · Ethernet · Active), RSSI signal pill colored by strength (green ≥ -50 dBm, amber -50…-70, red < -70).
- 🔐 **ONT Web Admin Accounts** — When firmware exposes `UserInterface.X_HW_WebUserInfo`, popup shows admin/user accounts with editable password fields.
- 🌍 **3rd-party ONT SN matching** — `find_device_id()` handles both raw-hex (Huawei) and ASCII-decoded SNs (XPON, ZTEG, etc).
- 📡 **SNMP-first optical signal** — `get_ont_full_info` queries `hwGponDeviceOntOpticalInfoTable` col 4 (RxPower) via SNMP for fast read, falls back to SSH for TX power and temperature.
- 🩹 **Lightweight PPPoE write path** (`kind='pppoe_creds'`) — fixed v3.0.0 bug where editing credentials triggered full re-provisioning and broke the WAN session.

---

## 🛣️ Roadmap (post-v4.0.0)

- 📡 Alerting & on-call notifications (email / Telegram / webhook)
- 📊 Customer-facing portal (signal status, plan, support tickets)
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
