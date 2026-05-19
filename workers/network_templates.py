"""
network_templates.py — Built-in graph template seeds.

On first import (or when network_db.seed_builtins() is called), idempotently
inserts a curated list of templates so the UI has something to show out of
the box. New templates are added; existing ones are NOT overwritten — users
can safely tweak any builtin without it being clobbered on next start.

A template is a dict:
  {
    "name":            "Generic IF-MIB Traffic",
    "graph_type":      "traffic"|"cpu"|"memory"|"temperature"|"uptime"|"custom",
    "vendor":          "generic"|"mikrotik"|"cisco"|"juniper"|"huawei"|...,
    "unit":            "bps"|"%"|"°C"|"sec",
    "default_interval":60,
    "oid_map": {
      # arbitrary keys → OIDs (poller knows how to walk vs get from the
      # graph_type). For per-interface traffic the poller uses
      # OID_IF_HC_IN / OID_IF_HC_OUT directly (always available), so the
      # template here is informational only.
    },
  }
"""

import workers.network_db as ndb


BUILTIN_TEMPLATES = [
    # ── Generic IF-MIB ────────────────────────────────────────────────────
    {
        "name":     "Generic IF-MIB Traffic",
        "graph_type": "traffic",
        "vendor":   "generic",
        "unit":     "bps",
        "oid_map":  {
            "rx_octets":  "1.3.6.1.2.1.31.1.1.1.6",   # ifHCInOctets
            "tx_octets":  "1.3.6.1.2.1.31.1.1.1.10",  # ifHCOutOctets
        },
    },
    {
        "name":     "Generic IF-MIB Errors/Drops",
        "graph_type": "errors",
        "vendor":   "generic",
        "unit":     "pkt/s",
        "oid_map":  {
            "rx_err":  "1.3.6.1.2.1.2.2.1.14",
            "tx_err":  "1.3.6.1.2.1.2.2.1.20",
            "rx_disc": "1.3.6.1.2.1.2.2.1.13",
            "tx_disc": "1.3.6.1.2.1.2.2.1.19",
        },
    },
    {
        "name":     "Generic sysUpTime",
        "graph_type": "uptime",
        "vendor":   "generic",
        "unit":     "sec",
        "oid_map":  {"uptime": "1.3.6.1.2.1.1.3.0"},
    },

    # ── Host Resources MIB (Linux, Windows, generic SNMPd) ───────────────
    {
        "name":     "Host Resources CPU",
        "graph_type": "cpu",
        "vendor":   "generic",
        "unit":     "%",
        "oid_map":  {
            "cpu_walk": "1.3.6.1.2.1.25.3.3.1.2",     # hrProcessorLoad walk
        },
    },
    {
        "name":     "Host Resources Memory",
        "graph_type": "memory",
        "vendor":   "generic",
        "unit":     "%",
        "oid_map":  {
            "storage_descr":      "1.3.6.1.2.1.25.2.3.1.3",
            "storage_alloc_unit": "1.3.6.1.2.1.25.2.3.1.4",
            "storage_size":       "1.3.6.1.2.1.25.2.3.1.5",
            "storage_used":       "1.3.6.1.2.1.25.2.3.1.6",
        },
    },

    # ── MikroTik ──────────────────────────────────────────────────────────
    {
        "name":     "MikroTik CPU Load",
        "graph_type": "cpu",
        "vendor":   "mikrotik",
        "unit":     "%",
        "oid_map":  {"cpu": "1.3.6.1.2.1.25.3.3.1.2.1"},
    },
    {
        "name":     "MikroTik Memory",
        "graph_type": "memory",
        "vendor":   "mikrotik",
        "unit":     "%",
        "oid_map":  {
            "mem_total": "1.3.6.1.4.1.14988.1.1.3.5.0",
            "mem_free":  "1.3.6.1.4.1.14988.1.1.3.6.0",
        },
    },
    {
        "name":     "MikroTik Temperature",
        "graph_type": "temperature",
        "vendor":   "mikrotik",
        "unit":     "°C",
        "oid_map":  {"temp": "1.3.6.1.4.1.14988.1.1.3.10.0"},
    },
    {
        # PPPoE active session count. Polled via RouterOS API (librouteros)
        # not SNMP — credentials are stored as JSON in device.notes:
        #   {"ros_user":"admin","ros_pass":"secret","ros_port":8728}
        # The poller writes to InfluxDB measurement `network_pppoe_sessions`
        # with tags device_id/device_name/profile/service, field active_count.
        "name":     "MikroTik PPPoE Sessions",
        "graph_type": "pppoe",
        "vendor":   "mikrotik",
        "unit":     "sessions",
        "oid_map":  {"_source": "routeros-api", "_endpoint": "/ppp/active/print"},
    },

    # ── Cisco ─────────────────────────────────────────────────────────────
    {
        "name":     "Cisco CPU 5-min",
        "graph_type": "cpu",
        "vendor":   "cisco",
        "unit":     "%",
        "oid_map":  {
            "cpu_5min": "1.3.6.1.4.1.9.9.109.1.1.1.1.5",   # cpmCPUTotal5minRev
            "cpu_5sec": "1.3.6.1.4.1.9.9.109.1.1.1.1.3",
        },
    },
    {
        "name":     "Cisco Memory Pool",
        "graph_type": "memory",
        "vendor":   "cisco",
        "unit":     "%",
        "oid_map":  {
            "mem_used": "1.3.6.1.4.1.9.9.48.1.1.1.5",     # ciscoMemoryPoolUsed
            "mem_free": "1.3.6.1.4.1.9.9.48.1.1.1.6",
        },
    },

    # ── Juniper ───────────────────────────────────────────────────────────
    {
        "name":     "Juniper Routing-Engine CPU",
        "graph_type": "cpu",
        "vendor":   "juniper",
        "unit":     "%",
        "oid_map":  {
            "cpu_walk": "1.3.6.1.4.1.2636.3.1.13.1.8",    # jnxOperatingCPU
        },
    },
    {
        "name":     "Juniper Temperature",
        "graph_type": "temperature",
        "vendor":   "juniper",
        "unit":     "°C",
        "oid_map":  {"temp_walk": "1.3.6.1.4.1.2636.3.1.13.1.7"},
    },

    # ── Huawei ────────────────────────────────────────────────────────────
    {
        "name":     "Huawei CPU",
        "graph_type": "cpu",
        "vendor":   "huawei",
        "unit":     "%",
        "oid_map":  {
            "cpu_walk": "1.3.6.1.4.1.2011.6.3.4.1.2",    # hwCpuDevDuty
        },
    },
    {
        "name":     "Huawei Memory",
        "graph_type": "memory",
        "vendor":   "huawei",
        "unit":     "%",
        "oid_map":  {
            "mem_walk": "1.3.6.1.4.1.2011.6.3.5.1.1.2",  # hwEntityMemUsage
        },
    },
]


def seed_builtins():
    """Idempotently insert all builtin templates. Safe to call on every start."""
    inserted = 0
    for t in BUILTIN_TEMPLATES:
        try:
            existing = [x for x in ndb.get_templates() if x["name"] == t["name"]]
            if existing:
                continue
            ndb.upsert_builtin_template(
                name=t["name"],
                graph_type=t["graph_type"],
                vendor=t["vendor"],
                oid_map=t["oid_map"],
                unit=t.get("unit", ""),
                default_interval=t.get("default_interval", 60),
            )
            inserted += 1
        except Exception as e:
            import logging
            logging.getLogger("net-templates").warning(
                f"Failed to seed template {t['name']}: {e}")
    return inserted


if __name__ == "__main__":
    n = seed_builtins()
    print(f"Seeded {n} built-in graph templates.")
