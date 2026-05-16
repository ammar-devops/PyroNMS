"""
network_db.py — SQLite CRUD for the Cacti-style Network Graphs module.

DB path: /opt/pyronms/data/network_devices.db
Called by: network_poller.py, api/server.py

Tables: network_devices, network_interfaces, network_graphs,
        graph_templates, graph_tree

SNMP v3 credentials are stored as-is on the VM (never committed to Git,
never returned raw in API responses — `_PUBLIC` excludes them).
"""

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("NETWORK_DB_PATH",
                         "/opt/pyronms/data/network_devices.db")

# ── Schema ────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS network_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    hostname        TEXT DEFAULT '',
    ip              TEXT NOT NULL UNIQUE,
    vendor          TEXT DEFAULT 'generic',
    device_type     TEXT DEFAULT 'router',
    location        TEXT DEFAULT '',
    snmp_version    TEXT DEFAULT 'v2c',
    snmp_community  TEXT DEFAULT 'public',
    snmp_v3_user        TEXT DEFAULT '',
    snmp_v3_auth_proto  TEXT DEFAULT '',
    snmp_v3_auth_pass   TEXT DEFAULT '',
    snmp_v3_priv_proto  TEXT DEFAULT '',
    snmp_v3_priv_pass   TEXT DEFAULT '',
    snmp_port       INTEGER DEFAULT 161,
    snmp_timeout    INTEGER DEFAULT 3,
    snmp_retries    INTEGER DEFAULT 1,
    polling_enabled    INTEGER DEFAULT 1,
    polling_interval   INTEGER DEFAULT 60,
    notes           TEXT DEFAULT '',
    tags            TEXT DEFAULT '',
    last_poll       INTEGER DEFAULT 0,
    last_status     TEXT DEFAULT 'unknown',
    last_poll_ms    INTEGER DEFAULT 0,
    sys_name        TEXT DEFAULT '',
    sys_descr       TEXT DEFAULT '',
    sys_object_id   TEXT DEFAULT '',
    created_at      INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    updated_at      INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS network_interfaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      INTEGER NOT NULL REFERENCES network_devices(id) ON DELETE CASCADE,
    if_index       INTEGER NOT NULL,
    if_name        TEXT DEFAULT '',
    if_descr       TEXT DEFAULT '',
    if_alias       TEXT DEFAULT '',
    if_type        INTEGER DEFAULT 0,
    if_speed       INTEGER DEFAULT 0,
    if_mtu         INTEGER DEFAULT 0,
    is_vlan        INTEGER DEFAULT 0,
    vlan_id        INTEGER DEFAULT 0,
    polling_enabled INTEGER DEFAULT 1,
    oper_status    INTEGER DEFAULT 0,
    admin_status   INTEGER DEFAULT 0,
    last_seen      INTEGER DEFAULT 0,
    UNIQUE(device_id, if_index)
);

CREATE TABLE IF NOT EXISTS graph_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    graph_type        TEXT NOT NULL,
    vendor            TEXT DEFAULT 'generic',
    oid_map_json      TEXT NOT NULL,
    unit              TEXT DEFAULT '',
    default_interval  INTEGER DEFAULT 60,
    builtin           INTEGER DEFAULT 0,
    created_at        INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS network_graphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL REFERENCES network_devices(id) ON DELETE CASCADE,
    interface_id  INTEGER REFERENCES network_interfaces(id) ON DELETE CASCADE,
    template_id   INTEGER REFERENCES graph_templates(id),
    graph_name    TEXT NOT NULL,
    graph_type    TEXT NOT NULL,
    enabled       INTEGER DEFAULT 1,
    sort_order    INTEGER DEFAULT 0,
    created_at    INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS graph_tree (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES graph_tree(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    device_id   INTEGER REFERENCES network_devices(id) ON DELETE CASCADE,
    graph_id    INTEGER REFERENCES network_graphs(id) ON DELETE CASCADE,
    sort_order  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_iface_device ON network_interfaces(device_id);
CREATE INDEX IF NOT EXISTS idx_graph_device ON network_graphs(device_id);
CREATE INDEX IF NOT EXISTS idx_tree_parent  ON graph_tree(parent_id);
"""

# Fields allowed on add/update via API
_DEVICE_EDITABLE = {
    "name", "hostname", "ip", "vendor", "device_type", "location",
    "snmp_version", "snmp_community",
    "snmp_v3_user", "snmp_v3_auth_proto", "snmp_v3_auth_pass",
    "snmp_v3_priv_proto", "snmp_v3_priv_pass",
    "snmp_port", "snmp_timeout", "snmp_retries",
    "polling_enabled", "polling_interval",
    "notes", "tags",
}

# Fields returned by API list (secrets removed)
_DEVICE_PUBLIC = {
    "id", "name", "hostname", "ip", "vendor", "device_type", "location",
    "snmp_version", "snmp_port", "snmp_timeout", "snmp_retries",
    "polling_enabled", "polling_interval",
    "notes", "tags",
    "last_poll", "last_status", "last_poll_ms",
    "sys_name", "sys_descr", "sys_object_id",
    "created_at", "updated_at",
}


# ── Connection ────────────────────────────────────────────────────────────
def _ensure_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_db() -> sqlite3.Connection:
    _ensure_dir()
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _row_public(row) -> dict:
    d = dict(row)
    out = {k: v for k, v in d.items() if k in _DEVICE_PUBLIC}
    # boolean flags
    out["snmp_v3_configured"] = bool(d.get("snmp_v3_user"))
    out["community_set"]      = bool(d.get("snmp_community"))
    return out


# ── Devices ───────────────────────────────────────────────────────────────
def get_all_devices(include_disabled=True, public_only=True) -> list:
    con = get_db()
    try:
        sql = "SELECT * FROM network_devices"
        if not include_disabled:
            sql += " WHERE polling_enabled=1"
        sql += " ORDER BY id"
        rows = con.execute(sql).fetchall()
        return [_row_public(r) if public_only else dict(r) for r in rows]
    finally:
        con.close()


def get_enabled_devices_with_creds() -> list:
    """Poller-only: returns full credentials."""
    con = get_db()
    try:
        rows = con.execute(
            "SELECT * FROM network_devices WHERE polling_enabled=1 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_device(device_id: int, include_creds=False) -> dict | None:
    con = get_db()
    try:
        r = con.execute("SELECT * FROM network_devices WHERE id=?",
                        (device_id,)).fetchone()
        if r is None:
            return None
        return dict(r) if include_creds else _row_public(r)
    finally:
        con.close()


def add_device(fields: dict) -> int:
    name = (fields.get("name") or "").strip()
    ip   = (fields.get("ip")   or "").strip()
    if not name:
        raise ValueError("name is required")
    if not ip:
        raise ValueError("ip is required")
    allowed = {k: v for k, v in fields.items() if k in _DEVICE_EDITABLE}
    cols  = ", ".join(allowed.keys())
    place = ", ".join("?" * len(allowed))
    vals  = list(allowed.values())
    con = get_db()
    try:
        cur = con.execute(
            f"INSERT INTO network_devices ({cols}) VALUES ({place})", vals)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def update_device(device_id: int, fields: dict) -> bool:
    allowed = {k: v for k, v in fields.items() if k in _DEVICE_EDITABLE}
    if not allowed:
        return False
    allowed["updated_at"] = int(time.time())
    sets = ", ".join(f"{k}=?" for k in allowed)
    vals = list(allowed.values()) + [device_id]
    con = get_db()
    try:
        cur = con.execute(
            f"UPDATE network_devices SET {sets} WHERE id=?", vals)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_device(device_id: int) -> bool:
    con = get_db()
    try:
        cur = con.execute("DELETE FROM network_devices WHERE id=?",
                          (device_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_device_status(device_id: int, status: str, poll_ms: int = 0,
                      sys_name: str | None = None,
                      sys_descr: str | None = None,
                      sys_object_id: str | None = None):
    con = get_db()
    try:
        ts = int(time.time())
        sets = ["last_poll=?", "last_status=?", "last_poll_ms=?", "updated_at=?"]
        vals = [ts, status, poll_ms, ts]
        if sys_name is not None:
            sets.append("sys_name=?"); vals.append(sys_name)
        if sys_descr is not None:
            sets.append("sys_descr=?"); vals.append(sys_descr)
        if sys_object_id is not None:
            sets.append("sys_object_id=?"); vals.append(sys_object_id)
        vals.append(device_id)
        con.execute(f"UPDATE network_devices SET {', '.join(sets)} WHERE id=?",
                    vals)
        con.commit()
    finally:
        con.close()


# ── Interfaces ────────────────────────────────────────────────────────────
def get_interfaces(device_id: int, enabled_only=False) -> list:
    con = get_db()
    try:
        sql = "SELECT * FROM network_interfaces WHERE device_id=?"
        if enabled_only:
            sql += " AND polling_enabled=1"
        sql += " ORDER BY if_index"
        rows = con.execute(sql, (device_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_interface(iface_id: int) -> dict | None:
    con = get_db()
    try:
        r = con.execute("SELECT * FROM network_interfaces WHERE id=?",
                        (iface_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def upsert_interfaces(device_id: int, ifaces: list[dict]) -> int:
    """
    Insert or update interfaces. `ifaces` is a list of dicts with
    if_index, if_name, if_descr, if_alias, if_type, if_speed, if_mtu,
    oper_status, admin_status, is_vlan, vlan_id.
    Returns count touched.
    """
    if not ifaces:
        return 0
    ts = int(time.time())
    con = get_db()
    try:
        n = 0
        for i in ifaces:
            row = (
                device_id,
                int(i.get("if_index", 0)),
                i.get("if_name", "")  or "",
                i.get("if_descr", "") or "",
                i.get("if_alias", "") or "",
                int(i.get("if_type", 0)  or 0),
                int(i.get("if_speed", 0) or 0),
                int(i.get("if_mtu", 0)   or 0),
                int(i.get("is_vlan", 0)  or 0),
                int(i.get("vlan_id", 0)  or 0),
                int(i.get("oper_status", 0)  or 0),
                int(i.get("admin_status", 0) or 0),
                ts,
            )
            con.execute(
                """INSERT INTO network_interfaces
                   (device_id, if_index, if_name, if_descr, if_alias, if_type,
                    if_speed, if_mtu, is_vlan, vlan_id, oper_status,
                    admin_status, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_id, if_index) DO UPDATE SET
                     if_name=excluded.if_name,
                     if_descr=excluded.if_descr,
                     if_alias=excluded.if_alias,
                     if_type=excluded.if_type,
                     if_speed=excluded.if_speed,
                     if_mtu=excluded.if_mtu,
                     is_vlan=excluded.is_vlan,
                     vlan_id=excluded.vlan_id,
                     oper_status=excluded.oper_status,
                     admin_status=excluded.admin_status,
                     last_seen=excluded.last_seen""",
                row,
            )
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def toggle_interface(iface_id: int) -> bool | None:
    con = get_db()
    try:
        r = con.execute("SELECT polling_enabled FROM network_interfaces WHERE id=?",
                        (iface_id,)).fetchone()
        if r is None:
            return None
        new = 0 if r["polling_enabled"] else 1
        con.execute("UPDATE network_interfaces SET polling_enabled=? WHERE id=?",
                    (new, iface_id))
        con.commit()
        return bool(new)
    finally:
        con.close()


# ── Graph templates ──────────────────────────────────────────────────────
def get_templates(vendor: str = None, graph_type: str = None) -> list:
    con = get_db()
    try:
        sql = "SELECT * FROM graph_templates WHERE 1=1"
        args = []
        if vendor:
            sql += " AND (vendor=? OR vendor='generic')"; args.append(vendor)
        if graph_type:
            sql += " AND graph_type=?"; args.append(graph_type)
        sql += " ORDER BY builtin DESC, vendor, name"
        rows = con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:    d["oid_map"] = json.loads(d.get("oid_map_json") or "{}")
            except: d["oid_map"] = {}
            out.append(d)
        return out
    finally:
        con.close()


def get_template(tid: int) -> dict | None:
    con = get_db()
    try:
        r = con.execute("SELECT * FROM graph_templates WHERE id=?",
                        (tid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:    d["oid_map"] = json.loads(d.get("oid_map_json") or "{}")
        except: d["oid_map"] = {}
        return d
    finally:
        con.close()


def add_template(fields: dict) -> int:
    name       = (fields.get("name") or "").strip()
    graph_type = (fields.get("graph_type") or "").strip()
    if not name or not graph_type:
        raise ValueError("name and graph_type are required")
    oid_map = fields.get("oid_map") or fields.get("oid_map_json")
    if isinstance(oid_map, dict):
        oid_map = json.dumps(oid_map)
    if not oid_map:
        raise ValueError("oid_map_json is required")
    con = get_db()
    try:
        cur = con.execute(
            """INSERT INTO graph_templates
               (name, graph_type, vendor, oid_map_json, unit, default_interval,
                builtin)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, graph_type, fields.get("vendor", "generic"),
             oid_map, fields.get("unit", ""),
             int(fields.get("default_interval", 60)),
             int(fields.get("builtin", 0))))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def upsert_builtin_template(name: str, graph_type: str, vendor: str,
                            oid_map: dict, unit: str = "",
                            default_interval: int = 60):
    """Idempotent: insert if missing, leave alone if present (builtin=1)."""
    con = get_db()
    try:
        r = con.execute("SELECT id FROM graph_templates WHERE name=?",
                        (name,)).fetchone()
        if r:
            return r["id"]
        cur = con.execute(
            """INSERT INTO graph_templates
               (name, graph_type, vendor, oid_map_json, unit, default_interval,
                builtin)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (name, graph_type, vendor, json.dumps(oid_map), unit,
             default_interval))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def delete_template(tid: int) -> bool:
    con = get_db()
    try:
        r = con.execute("SELECT builtin FROM graph_templates WHERE id=?",
                        (tid,)).fetchone()
        if r is None:
            return False
        if r["builtin"]:
            raise ValueError("Cannot delete builtin template")
        cur = con.execute("DELETE FROM graph_templates WHERE id=?", (tid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# ── Graphs ────────────────────────────────────────────────────────────────
def get_graphs(device_id: int = None, graph_type: str = None) -> list:
    con = get_db()
    try:
        sql = """SELECT g.*, t.name AS template_name, t.unit, t.vendor AS template_vendor,
                        d.name AS device_name, d.vendor AS device_vendor, d.ip AS device_ip,
                        i.if_name AS interface_name, i.if_index AS if_index
                   FROM network_graphs g
                   LEFT JOIN graph_templates t ON t.id = g.template_id
                   LEFT JOIN network_devices d ON d.id = g.device_id
                   LEFT JOIN network_interfaces i ON i.id = g.interface_id
                  WHERE 1=1"""
        args = []
        if device_id is not None:
            sql += " AND g.device_id=?"; args.append(device_id)
        if graph_type:
            sql += " AND g.graph_type=?"; args.append(graph_type)
        sql += " ORDER BY g.sort_order, g.id"
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def get_graph(gid: int) -> dict | None:
    rows = get_graphs()  # join already done
    for r in rows:
        if r["id"] == gid:
            return r
    return None


def add_graph(device_id: int, template_id: int, interface_id: int = None,
              graph_name: str = None) -> int:
    con = get_db()
    try:
        t = con.execute("SELECT * FROM graph_templates WHERE id=?",
                        (template_id,)).fetchone()
        if not t:
            raise ValueError("template not found")
        if not graph_name:
            graph_name = t["name"]
        cur = con.execute(
            """INSERT INTO network_graphs
               (device_id, interface_id, template_id, graph_name, graph_type)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, interface_id, template_id, graph_name, t["graph_type"]))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def delete_graph(gid: int) -> bool:
    con = get_db()
    try:
        cur = con.execute("DELETE FROM network_graphs WHERE id=?", (gid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# ── Tree ─────────────────────────────────────────────────────────────────
def get_tree() -> list:
    con = get_db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM graph_tree ORDER BY parent_id, sort_order, id"
        ).fetchall()]
        # Build nested tree
        by_id = {r["id"]: {**r, "children": []} for r in rows}
        roots = []
        for r in rows:
            n = by_id[r["id"]]
            if r["parent_id"] and r["parent_id"] in by_id:
                by_id[r["parent_id"]]["children"].append(n)
            else:
                roots.append(n)
        return roots
    finally:
        con.close()


def add_tree_node(parent_id: int | None, name: str, node_type: str,
                  device_id: int = None, graph_id: int = None,
                  sort_order: int = 0) -> int:
    if node_type not in ("folder", "device", "graph"):
        raise ValueError("node_type must be folder|device|graph")
    con = get_db()
    try:
        cur = con.execute(
            """INSERT INTO graph_tree
               (parent_id, name, node_type, device_id, graph_id, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (parent_id, name, node_type, device_id, graph_id, sort_order))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def update_tree_node(nid: int, name: str = None, parent_id=...,
                     sort_order: int = None) -> bool:
    sets = []
    vals = []
    if name is not None:
        sets.append("name=?"); vals.append(name)
    if parent_id is not ...:
        sets.append("parent_id=?"); vals.append(parent_id)
    if sort_order is not None:
        sets.append("sort_order=?"); vals.append(sort_order)
    if not sets:
        return False
    vals.append(nid)
    con = get_db()
    try:
        cur = con.execute(
            f"UPDATE graph_tree SET {', '.join(sets)} WHERE id=?", vals)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_tree_node(nid: int) -> bool:
    con = get_db()
    try:
        cur = con.execute("DELETE FROM graph_tree WHERE id=?", (nid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# ── Counts (for dashboard) ───────────────────────────────────────────────
def device_counts() -> dict:
    con = get_db()
    try:
        total   = con.execute("SELECT COUNT(*) AS n FROM network_devices").fetchone()["n"]
        online  = con.execute("SELECT COUNT(*) AS n FROM network_devices WHERE last_status='online'").fetchone()["n"]
        offline = con.execute("SELECT COUNT(*) AS n FROM network_devices WHERE last_status='offline'").fetchone()["n"]
        ifaces  = con.execute("SELECT COUNT(*) AS n FROM network_interfaces").fetchone()["n"]
        graphs  = con.execute("SELECT COUNT(*) AS n FROM network_graphs").fetchone()["n"]
        return {"total": total, "online": online, "offline": offline,
                "interfaces": ifaces, "graphs": graphs}
    finally:
        con.close()
