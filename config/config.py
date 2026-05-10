# ============================================================
#  ONT Monitor - Main Configuration
#  Edit this file to match your environment
# ============================================================

# InfluxDB Settings
INFLUX_URL = "http://localhost:8086"
INFLUX_TOKEN = "my-super-secret-token"
INFLUX_ORG = "myisp"
INFLUX_BUCKET = "olt_monitoring"

# OLT Settings
OLT_HOST = "172.20.101.101"
OLT_PORT = 22

# One dedicated SSH user per slot
SLOT_USERS = {
    1: {"username": "pollerslot1", "password": "Dell@1122"},
    2: {"username": "pollerslot2", "password": "Dell@1122"},
    4: {"username": "pollerslot4", "password": "Dell@1122"},
    5: {"username": "pollerslot5", "password": "Dell@1122"},
}

# Ports per slot (gpon interface 0/X -> ports inside)
SLOT_PORTS = {
    1: list(range(8)),  # ports 0-7
    2: list(range(8)),  # ports 0-7
    4: list(range(8)),  # ports 0-7
    5: list(range(16)),  # ports 0-15
}

# Background poll interval (seconds)
POLL_INTERVAL = 7200

# API Settings
API_HOST = "0.0.0.0"
API_PORT = 8088

# OLT Name (shown in dashboard)
OLT_NAME = "HAJI-PARK-OLT"

# ------------------ Phase 1 SNMP Migration ------------------
# Why:
# - Reduce long-running SSH load on OLT
# - Keep compatibility with current system via fallback
#
# POLL_SOURCE:
#   "ssh"    => existing behavior
#   "hybrid" => SNMP-first for ONT metrics, SSH fallback (recommended)
POLL_SOURCE = "hybrid"

# SNMP v2c communities already configured on OLT
SNMP_READ_COMMUNITY = "kknread@123"
SNMP_WRITE_COMMUNITY = "kknwrite@123"

# OID templates are deployment-specific for Huawei firmware/MIB variants.
# Keep empty until validated from your MA5600T MIB mapping.
# Use "{index}" placeholder for ONT index if needed.
SNMP_OID_TEMPLATES = {
    "rx_power": "",
    "temp": "",
    "vlan": "",
}
