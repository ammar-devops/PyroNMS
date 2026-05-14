# ONT Provisioning Flow

## Overview

ONT provisioning in PyroNMS sends a precise sequence of Huawei MA5603T CLI commands over SSH.
All commands run inside a single SSH session to minimize OLT connection load.

**File:** `api/olt_helpers.py` → `provision_ont()`

---

## Correct Command Sequence

```
interface gpon 0/X
```
Enter GPON interface context (X = slot number, e.g. `0/1`).

```
ont add <port> sn-auth <SN> omci ont-lineprofile-id <LINE_ID> ont-srvprofile-id <SRV_ID> desc "<CUSTOMER_NAME>"
```
Add ONT by serial number. OLT responds with the dynamically assigned `ONTID` — **never assume an ONTID; always capture it from the output**.

Response pattern: `success: 1` + `ONTID: <N>`

```
ont tr069-server-config <port> <ONTID> profile-id 1
```
Assign TR-069 ACS profile (numeric profile-id, not text). Runs **inside** `interface gpon`.

```
ont alarm-profile <port> <ONTID> profile-name alarm-profile_1
ont optical-alarm-profile <port> <ONTID> profile-name optical_alarm_profile_1
ont service-level-profile <port> <ONTID> profile-name alarm-policy_0
ont alias <port> <ONTID> alias "<CUSTOMER_NAME>"
```
Apply alarm, optical, service-level profiles and set alias.

```
ont wan-config <port> <ONTID> ip-index 1 profile-name PPP-10-IPV4-IPV6
```
Apply General ONT VAS Profile (WAN service template). **CRITICAL:**
- This command **must run inside** `interface gpon` context
- Do **NOT** use `ont vas-profile` — that command does not exist on MA5603T
- Without this step, PPPoE dial will not work even with correct credentials

```
ont ipconfig <port> <ONTID> pppoe vlan <VLAN> priority 0 user-account username <USER> password <PASS>
```
Dial PPPoE. Only issued if `conn_type == "pppoe"` and credentials are provided.
For ONU (bridge-mode) devices: skip this step — downstream router dials PPPoE.

```
quit
```
**Single `quit`** to exit GPON interface → global config. Do NOT issue two quits here.

```
service-port vlan <VLAN> gpon 0/X/<port> ont <ONTID> gemport 1 multi-service user-vlan <USER_VLAN> tag-transform translate
```
Create service-port. Runs in **global config** context (after the single `quit` above).

```
quit
save
```
Exit global config → system view, then save OLT config to flash.

---

## ONU vs ONT Detection

| Type | Models | PPPoE Dial |
|------|--------|------------|
| ONU (bridge) | HG80xx, HG81xx, EG80xx | Skip `ont ipconfig` — downstream router dials |
| ONT (router) | HG83xx, HG84xx, HG85xx, all others | Issue `ont ipconfig` |

Detection is by model prefix in the frontend (`_isONUModel()` in `web/index.html`).

---

## ONTID Capture

ONTID is **always captured dynamically** from the `ont add` response:
```python
o = re.search(r'ONTID\s*:\s*(\d+)', out)
ont_id = int(o.group(1)) if o else -1
```
If ONTID cannot be parsed, provisioning aborts. Never hardcode or assume ONTID = 0.

---

## Return Value

`provision_ont()` always returns a 4-tuple:
```python
(ok: bool, ont_id: int, output: str, verify_ok: bool | None)
```
- `ok=False, ont_id=-1, output=..., verify_ok=None` — ont add failed or no success match
- `ok=False, ont_id=N, output=..., verify_ok=False` — added but service-port failed
- `ok=True, ont_id=N, output=..., verify_ok=True/False` — added successfully; verify_ok indicates post-check result

---

## Description Sanitization

Customer name / description is sanitized before CLI send:
- Double and single quotes stripped (would break `desc "..."` quoting)
- Trimmed and capped at 64 characters

---

## Delete Flow

`delete_onts()` processes a list of SNs sequentially:

1. `display ont info by-sn <SN>` → find FSP + ONTID
2. `display service-port port <FSP> ont <ONTID>` → get service-port IDs
3. `undo service-port <ID>` per port (or fallback `undo service-port port <FSP> ont <ONTID>`)
4. `interface gpon <slot>` → `ont delete <port> <ONTID>` → confirm `y`
5. `quit`
6. **After ALL deletions**: single `save` (not per-ONT)

---

## Known Limitations

- Per-ONT SNMP traffic metrics not available on MA5603T firmware — only PON-port level via `pon_traffic` measurement
- If OLT SSH connection drops mid-provisioning, partial config may remain — always check OLT U2000 or run `display ont info` manually
- Verify step (`display ont info`) may show ONT offline immediately after add — normal, ONT needs 30–60s to register
