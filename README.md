<div align="center">

# <img width="100" height="100" alt="PyroNet Solutions Logo - P" src="https://github.com/user-attachments/assets/ae47fad8-2a62-41e8-b22e-156b7ad140c7" /> PyroNMS

### Network Management System for ISPs

[![Version](https://img.shields.io/badge/version-2.8.0-blue.svg)](https://github.com/PyroNet-Solutions/PyroNMS/releases)
[![Status](https://img.shields.io/badge/status-active-success.svg)]()
[![License](https://img.shields.io/badge/license-Private-red.svg)]()
[![Built for](https://img.shields.io/badge/OLT-Huawei%20MA5603T-orange.svg)]()
[![Python](https://img.shields.io/badge/python-3.x-yellow.svg)](https://www.python.org/)
[![InfluxDB](https://img.shields.io/badge/InfluxDB-2.x-22ADF6.svg)](https://www.influxdata.com/)

**A purpose-built NMS for ISPs running Huawei GPON OLTs — real-time ONT monitoring, fault detection, and full provisioning lifecycle in one dashboard.**

🏢 *Developed by [PyroNet Solutions](https://github.com/PyroNet-Solutions)* &nbsp;•&nbsp; 🌐 *Powering Indus Broadband Pvt Ltd*

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

### 🔭 Current Release — `v2.8.0`

- 🛠️ **U2000-style bulk Actions** — checkbox column + `▾ Actions` dropdown (Enable, Disable, Reset, Restore, Delete) with type-to-confirm for destructive actions
- 🪟 **Popup renamed** — "ONT Details and Configuration", with ONT/ONU device-type pill
- 🔍 **Robust SSH parsing** — handles Huawei pager, uses `interface gpon` mode for version, parses model from `OntProductDescription`
- 🧠 **Smart model detection** — Huawei HG/EG/HS/HN/MA prefixes and ZTE F-series mapped to themed UI
- 🚀 **New endpoint** — `POST /ont/action` (admin) wraps all 5 lifecycle actions

---

## 🛣️ Roadmap

- 🔌 SNMP-first polling architecture (Phases 1–6 in progress)
- 🤖 GenieACS deeper integration (TR-069 task automation)
- 📡 Alerting & on-call notifications (email / Telegram / webhook)
- 📊 Customer-facing portal (signal status, plan, support tickets)

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
