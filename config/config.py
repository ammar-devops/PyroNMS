# ============================================================
#  ONT Monitor - Main Configuration
#  Edit this file to match your environment
# ============================================================

# InfluxDB Settings
INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "my-super-secret-token"
INFLUX_ORG    = "myisp"
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
    1: list(range(8)),   # ports 0-7
    2: list(range(8)),   # ports 0-7
    4: list(range(8)),   # ports 0-7
    5: list(range(16)),  # ports 0-15
}

# Background poll interval (seconds) - 1 hour
POLL_INTERVAL = 300

# API Settings
API_HOST = "0.0.0.0"
API_PORT = 8088

# OLT Name (shown in dashboard)
OLT_NAME = "HAJI-PARK-OLT"
