"""
mikrotik_db.py — SQLite CRUD helpers for MikroTik device registry.

DB path: /opt/pyronms/data/mikrotik_devices.db
Called by: mikrotik_poller.py, api/server.py

Passwords are stored as-is on the VM (never committed to Git, never
returned raw in API responses — only a password_set boolean is exposed).
"""

import sqlite3
import time
import os

DB_PATH = os.environ.get("MIKROTIK_DB_PATH", "/opt/pyronms/data/mikrotik_devices.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mikrotik_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    ip              TEXT    NOT NULL UNIQUE,
    location        TEXT    DEFAULT '',
    snmp_enabled    INTEGER DEFAULT 1,
    snmp_version    TEXT    DEFAULT 'v2c',
    snmp_community  TEXT    DEFAULT 'public',
    api_enabled     INTEGER DEFAULT 1,
    api_port        INTEGER DEFAULT 8728,
    api_ssl         INTEGER DEFAULT 0,
    api_ssl_port    INTEGER DEFAULT 8729,
    username        TEXT    DEFAULT 'admin',
    password        TEXT    DEFAULT '',
    radius_role     INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    last_seen       INTEGER DEFAULT 0,
    last_status     TEXT    DEFAULT 'unknown',
    routeros_ver    TEXT    DEFAULT '',
    created_at      INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
"""

# Fields allowed in add/update (excludes id, last_seen, last_status, created_at)
_EDITABLE = {
    "name", "ip", "location",
    "snmp_enabled", "snmp_version", "snmp_community",
    "api_enabled", "api_port", "api_ssl", "api_ssl_port",
    "username", "password",
    "radius_role", "enabled", "routeros_ver",
}

# Fields returned to the API (password excluded, replaced by password_set flag)
_PUBLIC = {
    "id", "name", "ip", "location",
    "snmp_enabled", "snmp_version", "snmp_community",
    "api_enabled", "api_port", "api_ssl", "api_ssl_port",
    "username",
    "radius_role", "enabled",
    "last_seen", "last_status", "routeros_ver", "created_at",
}


def _ensure_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_db() -> sqlite3.Connection:
    """Open (and init) the SQLite database. Caller must close."""
    _ensure_dir()
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _row_to_dict(row, public_only=True) -> dict:
    d = dict(row)
    if public_only:
        d = {k: v for k, v in d.items() if k in _PUBLIC}
        d["password_set"] = bool(row["password"])
    return d


def get_all_devices(include_disabled=False) -> list:
    """Return list of all devices (passwords excluded)."""
    con = get_db()
    try:
        if include_disabled:
            rows = con.execute("SELECT * FROM mikrotik_devices ORDER BY id").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM mikrotik_devices WHERE enabled=1 ORDER BY id"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        con.close()


def get_all_devices_with_creds() -> list:
    """Return all enabled devices including credentials (for poller use only)."""
    con = get_db()
    try:
        rows = con.execute(
            "SELECT * FROM mikrotik_devices WHERE enabled=1 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_device(device_id: int, include_creds=False) -> dict | None:
    """Return single device by id."""
    con = get_db()
    try:
        row = con.execute(
            "SELECT * FROM mikrotik_devices WHERE id=?", (device_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row) if include_creds else _row_to_dict(row)
    finally:
        con.close()


def add_device(fields: dict) -> int:
    """Insert new device. Returns new row id. Raises ValueError on bad input."""
    name = (fields.get("name") or "").strip()
    ip   = (fields.get("ip")   or "").strip()
    if not name:
        raise ValueError("name is required")
    if not ip:
        raise ValueError("ip is required")

    allowed = {k: v for k, v in fields.items() if k in _EDITABLE}
    cols   = ", ".join(allowed.keys())
    placeholders = ", ".join("?" * len(allowed))
    vals   = list(allowed.values())

    con = get_db()
    try:
        cur = con.execute(
            f"INSERT INTO mikrotik_devices ({cols}) VALUES ({placeholders})", vals
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def update_device(device_id: int, fields: dict) -> bool:
    """Update allowed fields for device. Returns True if row existed."""
    allowed = {k: v for k, v in fields.items() if k in _EDITABLE}
    if not allowed:
        return False
    set_clause = ", ".join(f"{k}=?" for k in allowed)
    vals = list(allowed.values()) + [device_id]

    con = get_db()
    try:
        cur = con.execute(
            f"UPDATE mikrotik_devices SET {set_clause} WHERE id=?", vals
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_device(device_id: int) -> bool:
    """Hard-delete a device. Returns True if row existed."""
    con = get_db()
    try:
        cur = con.execute("DELETE FROM mikrotik_devices WHERE id=?", (device_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_status(device_id: int, status: str, ts: int | None = None,
               routeros_ver: str | None = None):
    """Update last_seen timestamp, last_status, and optionally routeros_ver."""
    if ts is None:
        ts = int(time.time())
    con = get_db()
    try:
        if routeros_ver is not None:
            con.execute(
                "UPDATE mikrotik_devices SET last_seen=?, last_status=?, routeros_ver=? WHERE id=?",
                (ts, status, routeros_ver, device_id),
            )
        else:
            con.execute(
                "UPDATE mikrotik_devices SET last_seen=?, last_status=? WHERE id=?",
                (ts, status, device_id),
            )
        con.commit()
    finally:
        con.close()
