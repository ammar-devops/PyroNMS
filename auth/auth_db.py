#!/usr/bin/env python3
"""
ONT Monitor V2.2 — Auth DB
SQLite user management + token system
"""
import sqlite3, hashlib, secrets, time, json, os

DB_PATH = "/opt/ont-monitor/auth/users.db"
TOKEN_EXPIRY = 8 * 3600  # 8 hours

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL,
    full_name   TEXT DEFAULT '',
    role        TEXT DEFAULT 'viewer',
    pon_access  TEXT DEFAULT '*',
    can_edit    INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  INTEGER DEFAULT (strftime('%s','now')),
    last_login  INTEGER DEFAULT 0,
    email       TEXT DEFAULT '',
    phone       TEXT DEFAULT '',
    cnic        TEXT DEFAULT '',
    address     TEXT DEFAULT '',
    avatar      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tokens (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER DEFAULT (strftime('%s','now')),
    expires_at  INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    cur = conn.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        create_user("admin","Dell@1122","Super Admin","superadmin","*",True)
        print("[Auth] Default admin created: admin / Dell@1122")
    conn.close()

def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{pw}".encode()).hexdigest()
    return f"{salt}:{h}"

def verify_password(pw, stored):
    try:
        salt, h = stored.split(":",1)
        return hashlib.sha256(f"{salt}{pw}".encode()).hexdigest() == h
    except:
        return False

def create_user(username, password, full_name="", role="viewer", pon_access="*", can_edit=False):
    conn = get_db()
    try:
        pons = json.dumps(pon_access) if isinstance(pon_access, list) else pon_access
        conn.execute(
            "INSERT INTO users (username,password,full_name,role,pon_access,can_edit) VALUES (?,?,?,?,?,?)",
            (username, hash_password(password), full_name, role, pons, 1 if can_edit else 0)
        )
        conn.commit()
        return True, "User created"
    except sqlite3.IntegrityError:
        return False, "Username already exists"
    finally:
        conn.close()

def update_user(user_id, data):
    conn = get_db()
    try:
        allowed = ["full_name","role","pon_access","can_edit","active","email","phone","cnic","address","avatar"]
        sets, vals = [], []
        for k,v in data.items():
            if k in allowed:
                if k=="pon_access" and isinstance(v,list): v=json.dumps(v)
                sets.append(f"{k}=?"); vals.append(v)
        if data.get("password"):
            sets.append("password=?"); vals.append(hash_password(data["password"]))
        if not sets: return False,"Nothing to update"
        vals.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        return True,"Updated"
    finally:
        conn.close()

def delete_user(user_id):
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit(); conn.close()
    return True,"Deleted"

def get_all_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,username,full_name,role,pon_access,can_edit,active,last_login FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def login(username, password):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=? AND active=1",(username,)).fetchone()
    if not row or not verify_password(password, row["password"]):
        conn.close()
        return None,"Invalid username or password"
    token = secrets.token_hex(32)
    now = int(time.time())
    expires = now + TOKEN_EXPIRY
    conn.execute("INSERT INTO tokens (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",(token,row["id"],now,expires))
    conn.execute("UPDATE users SET last_login=? WHERE id=?",(now,row["id"]))
    conn.commit(); conn.close()
    user = dict(row); user.pop("password",None)
    try: user["pon_access"] = json.loads(user["pon_access"])
    except: pass
    return {"token":token,"expires_at":expires,"user":user}, None

def validate_token(token):
    if not token: return None
    conn = get_db()
    now = int(time.time())
    row = conn.execute(
        "SELECT u.* FROM tokens t JOIN users u ON t.user_id=u.id WHERE t.token=? AND t.expires_at>? AND u.active=1",
        (token,now)
    ).fetchone()
    conn.close()
    if not row: return None
    user = dict(row); user.pop("password",None)
    try: user["pon_access"] = json.loads(user["pon_access"])
    except: pass
    return user

def logout(token):
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE token=?",(token,))
    conn.commit(); conn.close()

def cleanup_expired():
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE expires_at<?",(int(time.time()),))
    conn.commit(); conn.close()

def can_access_pon(user, pon):
    pa = user.get("pon_access","*")
    if pa=="*" or pa==["*"]: return True
    if isinstance(pa,list):
        for a in pa:
            if pon==a or pon.startswith(a): return True
    return False

if __name__=="__main__":
    import sys
    init_db()
    if len(sys.argv)>1:
        cmd=sys.argv[1]
        if cmd=="list":
            for u in get_all_users():
                print(f"  [{u['id']}] {u['username']} | {u['role']} | PONs:{u['pon_access']} | edit:{u['can_edit']} | active:{u['active']}")
        elif cmd=="create" and len(sys.argv)>=4:
            ok,msg=create_user(sys.argv[2],sys.argv[3],role=sys.argv[4] if len(sys.argv)>4 else "viewer")
            print(msg)
        elif cmd=="delete" and len(sys.argv)>=3:
            ok,msg=delete_user(int(sys.argv[2])); print(msg)
        elif cmd=="passwd" and len(sys.argv)>=4:
            ok,msg=update_user(int(sys.argv[2]),{"password":sys.argv[3]}); print(msg)
    else:
        print("Commands: list | create <user> <pass> [role] | delete <id> | passwd <id> <pass>")
