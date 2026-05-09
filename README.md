# PyroNMS v2.5.1
Network Management System for ISPs using Huawei GPON OLT.

Developed by PyroNet Solutions  
Client: Indus Broadband Pvt Ltd

## Highlights
- Real-time ONT monitoring and filtering
- Power failure and fiber issue detection via OLT SSH
- Mobile-friendly dashboard and ONT list UI
- OLT management, backup manager, worker manager
- SSH-based unregistered ONT scan and provisioning
- Service-port aware provisioning defaults (VLAN 10 flow)
- GenieACS TR-069 integration
- InfluxDB time-series monitoring

## Architecture
- Frontend: HTML5, CSS3, Vanilla JS, Chart.js
- Backend: Python 3 HTTP server
- Datastore: SQLite + InfluxDB 2.x
- OLT Access: Netmiko (Huawei MA5603T)

## Quick Start
1. Configure API and auth services on the server.
2. Place web files under `/var/www/html`.
3. Start API service (`ont-api.service`).
4. Open dashboard in browser and login.

## Current Release
- Tag: `v2.5.1`
- Focus:
  - unregistered ONT scanner reliability fix
  - provisioning improvements
  - VLAN 10 defaults in provisioning flow
  - General ONT VAS Profile field in UI

## Version History
- v2.1: Base dashboard
- v2.2: Authentication system
- v2.3: Themes and branding
- v2.4: Side panel, OLT management, server monitor
- v2.5: Dashboard cards and resource monitor
- v2.5.1: Provisioning and unregistered ONT reliability updates

## Repository Layout
- `web/` frontend files
- `api/` backend API and OLT helpers
- `auth/` authentication database code
- `workers/` worker scripts and jobs
- `config/` runtime configuration
- `olt-config/` OLT profiles and parsed config artifacts

## Contributing
Please read `CONTRIBUTING.md` and open issues for bugs or feature requests.

## License
Private/internal project unless otherwise stated by owner.
