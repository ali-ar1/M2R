#!/usr/bin/env python3
"""M2R - migrate Marzban / Marzneshin / 3x-ui to Rebecca (MySQL/MariaDB).

Export writes one .sql file with m2r_* staging tables.
Import loads it into Rebecca's MySQL, renames clashing usernames, then merges.
Only the Python 3 standard library is used; MySQL/PostgreSQL are reached through
their CLI clients on the host or inside a running docker container.
"""
import datetime as dt
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from urllib.parse import unquote, urlsplit

VERSION = "1.0"
DEFAULT_DIR = "/root"
PANELS = {
    "1": ("marzban", "Marzban", ["/opt/marzban/.env"], "/var/lib/marzban/db.sqlite3"),
    "2": ("marzneshin", "Marzneshin", ["/etc/opt/marzneshin/.env", "/opt/marzneshin/.env"], "/var/lib/marzneshin/db.sqlite3"),
    "3": ("3xui", "3x-ui (Sanaei)", ["/etc/default/x-ui"], "/etc/x-ui/x-ui.db"),
}
REBECCA_ENV = ["/opt/rebecca/.env"]
PROTOCOLS = ("vmess", "vless", "trojan", "shadowsocks")
USER_STATUSES = ("active", "disabled", "limited", "expired", "on_hold")
RESETS = ("no_reset", "day", "week", "month", "year")
SS_METHOD = "chacha20-ietf-poly1305"


def die(msg):
    print("\n[!] " + msg)
    sys.exit(1)


def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


# ---------------------------------------------------------------- helpers

def read_env(path):
    env = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if not m:
                continue
            v = m.group(2)
            if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) >= 2:
                v = v[1:-1]
            else:
                v = v.split(" #")[0].strip()
            env[m.group(1)] = v
    return env


def parse_db_url(url, base_dir, fallback_sqlite):
    """SQLAlchemy-style URL -> dict(kind=sqlite|mysql|postgres, ...)."""
    scheme = url.split(":", 1)[0].split("+")[0].lower()
    if scheme == "sqlite":
        path = url.split(":///", 1)[1] if ":///" in url else ""
        path = path.split("?")[0]
        if path and not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        if not path or not os.path.exists(path):
            path = fallback_sqlite if os.path.exists(fallback_sqlite) else path
        return {"kind": "sqlite", "path": path}
    u = urlsplit(url)
    c = {"host": u.hostname or "127.0.0.1", "port": u.port, "user": unquote(u.username or ""),
         "password": unquote(u.password or ""), "db": u.path.lstrip("/").split("?")[0]}
    if scheme in ("mysql", "mariadb"):
        c.update(kind="mysql", port=c["port"] or 3306)
    elif scheme in ("postgresql", "postgres"):
        c.update(kind="postgres", port=c["port"] or 5432,
                 dsn=re.sub(r"^[a-z]+(\+[a-z0-9]+)?://", "postgresql://", url))
    else:
        die("Unsupported database URL: " + url)
    return c


def docker_containers(words):
    if not shutil.which("docker"):
        return []
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    return [ln.split("\t")[0] for ln in out.splitlines()
            if any(w in ln.lower() for w in words)]


class DB:
    """Tiny uniform reader: rows(table) / query_json(sql) -> list of dicts."""

    def __init__(self, conf):
        self.c = conf
        self.kind = conf["kind"]
        self.cmd = None
        if self.kind == "sqlite":
            if not os.path.exists(conf["path"]):
                die("SQLite file not found: %s" % conf["path"])
            self.con = sqlite3.connect(conf["path"])
            self.con.row_factory = sqlite3.Row
        else:
            self._pick_client()

    # ---- CLI client selection (host first, then a DB container)
    def _candidates(self):
        c = self.c
        if self.kind == "mysql":
            args = ["-u", c["user"], "-P", str(c["port"]), "--default-character-set=utf8mb4"]
            env = ["MYSQL_PWD=" + c["password"]]
            for b in ("mysql", "mariadb"):
                if shutil.which(b):
                    yield [b, "-h", c["host"]] + args, env
            for ctr in docker_containers(("mysql", "mariadb")):
                yield (["docker", "exec", "-i", "-e", env[0], ctr, "sh", "-c",
                        'exec "$(command -v mysql || command -v mariadb)" "$@"', "sh", "-h", "127.0.0.1"] + args, [])
        else:
            dsn = c["dsn"]
            if shutil.which("psql"):
                yield ["psql", dsn], []
            local = re.sub(r"@[^/:?]+", "@127.0.0.1", dsn) if "://" in dsn else re.sub(r"host=\S+", "host=127.0.0.1", dsn)
            for ctr in docker_containers(("postgres", "timescale")):
                yield ["docker", "exec", "-i", ctr, "psql", local], []

    def _pick_client(self):
        err = "no mysql/psql client found on the host or in a docker container"
        for cmd, env in self._candidates():
            self.cmd, self.env = cmd, env
            try:
                self.query_json("SELECT JSON_OBJECT('ok', 1);" if self.kind == "mysql"
                                else "SELECT row_to_json(t) FROM (SELECT 1 AS ok) t;")
                return
            except RuntimeError as e:
                err = str(e)
        die("Could not connect to the %s database: %s" % (self.kind, err))

    def _run(self, sql, extra=()):
        env = dict(os.environ)
        for kv in self.env:
            k, v = kv.split("=", 1)
            env[k] = v
        if self.kind == "mysql":
            cmd = self.cmd + ["-N", "-B", "-r"] + list(extra) + [self.c["db"]]
        else:
            cmd = self.cmd + ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"] + list(extra)
        p = subprocess.run(cmd, input=sql, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "database command failed")
        return p.stdout

    def query_json(self, sql):
        out = self._run(sql)
        return [json.loads(ln) for ln in out.splitlines() if ln.strip()]

    def tables(self):
        if self.kind == "sqlite":
            return {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if self.kind == "mysql":
            sql = "SELECT JSON_OBJECT('n', table_name) FROM information_schema.tables WHERE table_schema = DATABASE();"
        else:
            sql = "SELECT row_to_json(t) FROM (SELECT table_name AS n FROM information_schema.tables WHERE table_schema = current_schema()) t;"
        return {r["n"] for r in self.query_json(sql)}

    def rows(self, table):
        if self.kind == "sqlite":
            return [dict(r) for r in self.con.execute('SELECT * FROM "%s"' % table)]
        if self.kind == "postgres":
            return self.query_json('SELECT row_to_json(t) FROM "%s" t;' % table)
        cols = [r["c"] for r in self.query_json(
            "SELECT JSON_OBJECT('c', column_name) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = '%s' ORDER BY ordinal_position;" % table)]
        obj = ", ".join("'%s', `%s`" % (c, c) for c in cols)
        return self.query_json("SELECT JSON_OBJECT(%s) FROM `%s`;" % (obj, table))

    def execute(self, sql):
        return self._run(sql)


def as_json(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return {}


def truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "t", "true", "yes")
    return bool(v)


def parse_time(v):
    """Anything time-like -> aware UTC datetime or None."""
    if v is None or v == "" or v == 0:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v)):
        n = float(v)
        if n <= 0:
            return None
        if n > 1e11:  # milliseconds
            n /= 1000
        return dt.datetime.fromtimestamp(n, dt.timezone.utc)
    s = str(v).strip().replace("Z", "+00:00").replace("T", " ")
    s = re.sub(r"(\.\d{1,6})\d*", r"\1", s)
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def unix(v):
    d = parse_time(v)
    return int(d.timestamp()) if d else None


def dbtime(v):
    d = parse_time(v)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def clean_name(name, fallback):
    """Rebecca usernames: up to 32 chars of [A-Za-z0-9._@-] (+ legacy '_')."""
    raw = str(name or "").strip()
    s = re.sub(r"[^A-Za-z0-9._@-]", "_", raw)
    if s != raw:  # only tidy names we had to change; untouched names keep their sub tokens valid
        s = re.sub(r"_+", "_", s).strip("_")
    return s[:32] or fallback


def unique_name(base, taken):
    cand, i = base, 1
    while cand.lower() in taken:
        i += 1
        suffix = "-%d" % i
        cand = base[:32 - len(suffix)] + suffix
    taken.add(cand.lower())
    return cand


def num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- sources
# Each reader returns (admins, users, meta). User dicts carry "creds": {proto: settings}.

def user_record(**kw):
    u = {"admin_id": None, "status": "active", "used_traffic": 0, "data_limit": None,
         "reset_strategy": "no_reset", "expire": None, "on_hold_expire_duration": None,
         "on_hold_timeout": None, "ip_limit": 0, "note": None, "telegram_id": None,
         "created_at": None, "sub_revoked_at": None, "edit_at": None,
         "last_status_change": None, "subscription_token": "", "flow": None, "creds": {}}
    u.update(kw)
    if u["status"] not in USER_STATUSES:
        u["status"] = "active"
    if u["reset_strategy"] not in RESETS:
        u["reset_strategy"] = "no_reset"
    if not u["data_limit"]:
        u["data_limit"] = None
    vless = u["creds"].get("vless") or {}
    if not u["flow"] and vless.get("flow"):
        u["flow"] = vless["flow"]
    return u


def admin_record(**kw):
    a = {"hashed_password": "", "plain_password": None, "role": "standard", "status": "active",
         "telegram_id": None, "users_usage": None, "data_limit": None, "created_at": None}
    a.update(kw)
    return a


def norm_creds(raw):
    creds = {}
    for proto, s in (raw or {}).items():
        proto = str(proto).lower()
        s = as_json(s)
        if proto in PROTOCOLS and s:
            s = {k: v for k, v in s.items() if v not in (None, "")}
            if proto == "shadowsocks":
                s.setdefault("method", SS_METHOD)
            creds[proto] = s
    return creds


def read_marzban(db):
    t = db.tables()
    admins = [admin_record(id=r["id"], username=r["username"], hashed_password=r.get("hashed_password") or "",
                           role="sudo" if truthy(r.get("is_sudo")) else "standard",
                           telegram_id=r.get("telegram_id"), users_usage=r.get("users_usage"),
                           created_at=r.get("created_at"))
              for r in db.rows("admins")]
    proxies = {}
    for p in db.rows("proxies"):
        proxies.setdefault(p["user_id"], {})[str(p["type"]).lower()] = p["settings"]
    users = [user_record(id=r["id"], username=r["username"], admin_id=r.get("admin_id"),
                         status=str(r.get("status") or "active").lower(), used_traffic=num(r.get("used_traffic")),
                         data_limit=r.get("data_limit"),
                         reset_strategy=str(r.get("data_limit_reset_strategy") or "no_reset").lower(),
                         expire=unix(r.get("expire")), on_hold_expire_duration=r.get("on_hold_expire_duration"),
                         on_hold_timeout=dbtime(r.get("on_hold_timeout")), note=r.get("note"),
                         created_at=dbtime(r.get("created_at")), sub_revoked_at=dbtime(r.get("sub_revoked_at")),
                         edit_at=dbtime(r.get("edit_at")), last_status_change=dbtime(r.get("last_status_change")),
                         creds=norm_creds(proxies.get(r["id"])))
             for r in db.rows("users")]
    meta = {}
    if "jwt" in t:
        j = db.rows("jwt")
        if j and j[0].get("secret_key"):
            meta["sub_secret"] = j[0]["secret_key"]
    return admins, users, meta


def marzneshin_hashes(keys, env):
    """xxh128 like app/utils/keygen.py: key -> (uuid, password)."""
    if env.get("AUTH_GENERATION_ALGORITHM", "xxh128").lower() == "plain":
        return {k: (str(uuid.UUID(k)), k) for k in keys}, True
    code = ("import sys,json,uuid,xxhash;ks=json.load(sys.stdin);"
            "print(json.dumps({k:[str(uuid.UUID(bytes=xxhash.xxh128(k.encode()).digest())),"
            "xxhash.xxh128(k.encode()).hexdigest()] for k in ks}))")
    data = json.dumps(keys)
    tries = [[sys.executable, "-c", code]] + [["docker", "exec", "-i", c, "python", "-c", code]
                                               for c in docker_containers(("marzneshin",)) if "marznode" not in c]
    for cmd in tries:
        try:
            p = subprocess.run(cmd, input=data, capture_output=True, text=True, timeout=120)
            if p.returncode == 0:
                return {k: tuple(v) for k, v in json.loads(p.stdout).items()}, True
        except Exception:
            pass
    # shortcut: without xxhash we fall back to key-as-UUID; clients must refresh their subscription.
    return {k: (str(uuid.UUID(k)) if re.fullmatch(r"[0-9a-fA-F]{32}", k) else str(uuid.uuid4()), k) for k in keys}, False


def read_marzneshin(db, env):
    admins = [admin_record(id=r["id"], username=r["username"], hashed_password=r.get("hashed_password") or "",
                           role="sudo" if truthy(r.get("is_sudo")) else "standard",
                           status="active" if truthy(r.get("enabled", True)) else "disabled",
                           created_at=r.get("created_at"))
              for r in db.rows("admins")]
    raw = [r for r in db.rows("users") if not truthy(r.get("removed"))]
    hashes, exact = marzneshin_hashes([r["key"] for r in raw if r.get("key")], env)
    if not exact:
        print("[~] xxhash not available: UUIDs will change, users must update their subscription once.")
    now = dt.datetime.now(dt.timezone.utc)
    users = []
    for r in raw:
        used, limit = num(r.get("used_traffic")), r.get("data_limit")
        strategy = str(r.get("expire_strategy") or "never").lower()
        expire_at = parse_time(r.get("expire_date"))
        status, hold, hold_to = "active", None, None
        if strategy == "start_on_first_use" and not expire_at:
            status, hold, hold_to = "on_hold", r.get("usage_duration"), dbtime(r.get("activation_deadline"))
        if not truthy(r.get("enabled", True)):
            status = "disabled"
        elif limit and used >= num(limit):
            status = "limited"
        elif expire_at and expire_at < now:
            status = "expired"
        uid, pwd = hashes.get(r.get("key"), (None, None))
        creds = {}
        if uid:
            creds = {"vmess": {"id": uid}, "vless": {"id": uid}, "trojan": {"password": pwd},
                     "shadowsocks": {"password": pwd, "method": SS_METHOD}}
        ip = num(r.get("ip_limit"), 0)
        users.append(user_record(
            id=r["id"], username=r["username"], admin_id=r.get("admin_id"), status=status, used_traffic=used,
            data_limit=limit, reset_strategy=str(r.get("data_limit_reset_strategy") or "no_reset").lower(),
            expire=int(expire_at.timestamp()) if expire_at else None, on_hold_expire_duration=hold,
            on_hold_timeout=hold_to, ip_limit=ip if ip > 0 else 0, note=r.get("note"),
            created_at=dbtime(r.get("created_at")), sub_revoked_at=dbtime(r.get("sub_revoked_at")),
            edit_at=dbtime(r.get("edit_at")), subscription_token=r.get("key") or "", creds=creds))
    return admins, users, {}


def xui_reset(c):
    tr = str(c.get("trafficReset") or c.get("traffic_reset") or "").lower()
    if tr in ("daily", "weekly", "monthly"):
        return {"daily": "day", "weekly": "week", "monthly": "month"}[tr]
    return {1: "day", 7: "week", 30: "month", 31: "month", 365: "year"}.get(num(c.get("reset")), "no_reset")


def read_3xui(db):
    t = db.tables()
    panel = db.rows("users") if "users" in t else []
    admins = []
    for r in panel:
        pw = r.get("password") or ""
        hashed = pw if pw.startswith("$2") else ""
        if pw and not hashed:
            try:
                import bcrypt
                hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
            except ImportError:
                pass
        admins.append(admin_record(id=r["id"], username=r["username"], hashed_password=hashed or "!",
                                   plain_password=None if hashed else pw, role="full_access" if not admins else "sudo"))
    owner = admins[0]["id"] if admins else None

    inbounds = {r["id"]: r for r in db.rows("inbounds")}
    traffic = {r["email"]: r for r in db.rows("client_traffics")} if "client_traffics" in t else {}

    # (client_dict, inbound_row) pairs, from the clients table (new) or inbound JSON (old)
    pairs = []
    if "clients" in t and "client_inbounds" in t:
        links = {}
        for l in db.rows("client_inbounds"):
            links.setdefault(l["client_id"], []).append(l)
        for c in db.rows("clients"):
            c = {"id": c.get("uuid"), "password": c.get("password"), "email": c.get("email"),
                 "flow": c.get("flow"), "limitIp": c.get("limit_ip"), "totalGB": c.get("total_gb"),
                 "expiryTime": c.get("expiry_time"), "enable": c.get("enable"), "tgId": c.get("tg_id"),
                 "subId": c.get("sub_id"), "comment": c.get("comment"), "reset": c.get("reset"),
                 "trafficReset": c.get("traffic_reset"), "created_at": c.get("created_at"), "_cid": c.get("id")}
            for l in links.get(c["_cid"], []):
                ib = inbounds.get(l["inbound_id"])
                if ib:
                    pairs.append((dict(c, flow=l.get("flow_override") or c["flow"]), ib))
    if not pairs:
        for ib in inbounds.values():
            for c in as_json(ib.get("settings")).get("clients") or []:
                pairs.append((c, ib))

    groups = {}  # one Rebecca user per subId (3x-ui subscription unit), else per email
    for c, ib in pairs:
        if not c.get("email"):
            continue
        groups.setdefault(c.get("subId") or "email:" + c["email"], []).append((c, ib))

    users, skipped, now = [], 0, dt.datetime.now(dt.timezone.utc)
    for n, (key, items) in enumerate(groups.items(), 1):
        creds, emails = {}, []
        for c, ib in items:
            emails.append(c["email"])
            proto = str(ib.get("protocol")).lower()
            if proto in ("vmess", "vless") and c.get("id"):
                creds.setdefault(proto, {"id": c["id"], **({"flow": c["flow"]} if c.get("flow") else {})})
            elif proto == "trojan" and c.get("password"):
                creds.setdefault("trojan", {"password": c["password"]})
            elif proto == "shadowsocks" and c.get("password"):
                m = c.get("method") or as_json(ib.get("settings")).get("method") or SS_METHOD
                creds.setdefault("shadowsocks", {"password": c["password"], "method": m})
            else:
                skipped += 1
        first = items[0][0]
        emails = list(dict.fromkeys(emails))
        used = sum(num(traffic.get(e, {}).get("up")) + num(traffic.get(e, {}).get("down")) for e in emails)
        limit = max(num(c.get("totalGB")) for c, _ in items) or None
        exp = min((num(c.get("expiryTime")) for c, _ in items), key=lambda x: (x == 0, x))
        enabled = any(truthy(c.get("enable", True)) for c, _ in items) and \
            all(truthy(traffic.get(e, {}).get("enable", True)) for e in emails)
        status, expire, hold = "active", None, None
        if exp < 0:
            status, hold = "on_hold", -exp // 1000
        elif exp > 0:
            expire = exp // 1000
        if not enabled:
            status = "disabled"
        elif limit and used >= limit:
            status = "limited"
        elif expire and expire < now.timestamp():
            status = "expired"
        note = " | ".join(x for x in [first.get("comment") or "",
                                      "3x-ui: " + ", ".join(emails) if len(emails) > 1 else ""] if x)
        users.append(user_record(
            id=n, username=first["email"], admin_id=owner, status=status, used_traffic=used, data_limit=limit,
            reset_strategy=xui_reset(first), expire=expire, on_hold_expire_duration=hold,
            ip_limit=max(num(c.get("limitIp")) for c, _ in items), note=note[:500] or None,
            telegram_id=str(first.get("tgId")) if num(first.get("tgId")) else None,
            created_at=dbtime(first.get("created_at")), subscription_token=first.get("subId") or "",
            creds=creds))
    if skipped:
        print("[~] %d client entries on protocols Rebecca can't take (wireguard, hysteria, ...) were skipped." % skipped)
    for a in admins:
        a["users_usage"] = sum(u["used_traffic"] for u in users) if a["id"] == owner else 0
    return admins, users, {}


# ---------------------------------------------------------------- SQL writer

def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else repr(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\0", "\\0") \
        .replace("\n", "\\n").replace("\r", "\\r").replace("\x1a", "\\Z")
    return "'" + s + "'"


SCHEMA = """SET NAMES utf8mb4;
DROP TABLE IF EXISTS m2r_meta, m2r_admins, m2r_users, m2r_creds;
CREATE TABLE m2r_meta (k VARCHAR(64) PRIMARY KEY, v TEXT) DEFAULT CHARSET=utf8mb4;
CREATE TABLE m2r_admins (id BIGINT PRIMARY KEY, username VARCHAR(64) NOT NULL, hashed_password VARCHAR(255),
  plain_password VARCHAR(255) NULL, role VARCHAR(32), status VARCHAR(32), telegram_id BIGINT NULL,
  users_usage BIGINT NOT NULL DEFAULT 0, data_limit BIGINT NULL, created_at DATETIME NULL,
  new_username VARCHAR(64) NULL, new_id BIGINT NULL) DEFAULT CHARSET=utf8mb4;
CREATE TABLE m2r_users (id BIGINT PRIMARY KEY, username VARCHAR(64) NOT NULL, admin_id BIGINT NULL,
  status VARCHAR(32), used_traffic BIGINT NOT NULL DEFAULT 0, data_limit BIGINT NULL, reset_strategy VARCHAR(32),
  expire BIGINT NULL, on_hold_expire_duration BIGINT NULL, on_hold_timeout DATETIME NULL, ip_limit INT NOT NULL DEFAULT 0,
  note VARCHAR(500) NULL, telegram_id VARCHAR(128) NULL, created_at DATETIME NULL, sub_revoked_at DATETIME NULL,
  edit_at DATETIME NULL, last_status_change DATETIME NULL, subscription_token VARCHAR(255) NOT NULL DEFAULT '',
  flow VARCHAR(128) NULL, new_username VARCHAR(64) NULL, new_id BIGINT NULL) DEFAULT CHARSET=utf8mb4;
CREATE TABLE m2r_creds (user_id BIGINT NOT NULL, type VARCHAR(32) NOT NULL, settings TEXT NOT NULL) DEFAULT CHARSET=utf8mb4;
"""
A_COLS = ["id", "username", "hashed_password", "plain_password", "role", "status", "telegram_id",
          "users_usage", "data_limit", "created_at"]
U_COLS = ["id", "username", "admin_id", "status", "used_traffic", "data_limit", "reset_strategy", "expire",
          "on_hold_expire_duration", "on_hold_timeout", "ip_limit", "note", "telegram_id", "created_at",
          "sub_revoked_at", "edit_at", "last_status_change", "subscription_token", "flow"]


def inserts(table, cols, rows, size=500):
    for i in range(0, len(rows), size):
        vals = ",\n".join("(" + ",".join(q(r.get(c)) for c in cols) + ")" for r in rows[i:i + size])
        yield "INSERT INTO %s (%s) VALUES\n%s;\n" % (table, ",".join(cols), vals)


def build_sql(panel, admins, users, meta):
    for a in admins:
        a["username"] = clean_name(a["username"], "admin%s" % a["id"])
        a["created_at"] = dbtime(a["created_at"])
        if a["users_usage"] is None:
            a["users_usage"] = sum(u["used_traffic"] for u in users if u["admin_id"] == a["id"])
    known = {a["id"] for a in admins}
    creds = []
    for u in users:
        u["username"] = clean_name(u["username"], "user%s" % u["id"])
        if u["admin_id"] not in known:
            u["admin_id"] = None
        for proto, s in u["creds"].items():
            s = dict(s)
            s.pop("flow", None)  # Rebecca keeps flow on users.flow
            creds.append({"user_id": u["id"], "type": proto, "settings": s})
    meta = dict(meta, source=panel, version=VERSION, exported_at=dbtime(dt.datetime.now(dt.timezone.utc).timestamp()))
    out = ["-- M2R %s export from %s, %d admins, %d users\n" % (VERSION, panel, len(admins), len(users)), SCHEMA]
    out += inserts("m2r_meta", ["k", "v"], [{"k": k, "v": v} for k, v in meta.items()])
    out += inserts("m2r_admins", A_COLS, admins)
    out += inserts("m2r_users", U_COLS, users)
    out += inserts("m2r_creds", ["user_id", "type", "settings"], creds)
    return "".join(out)


# ---------------------------------------------------------------- export / import

def source_db(key):
    panel, title, envs, default_db = PANELS[key]
    env_path = next((p for p in envs if os.path.exists(p)), None)
    if not env_path and not os.path.exists(default_db):
        env_path = ask("[?] %s .env / database file not found. Enter its path: " % title)
        if not os.path.exists(env_path):
            die("Not found: " + env_path)
    if env_path and re.search(r"\.(db|sqlite3?)$", env_path):
        return {"kind": "sqlite", "path": env_path}, {}
    env = read_env(env_path) if env_path else {}
    print("[+] Settings: %s" % (env_path or default_db))
    if panel == "3xui":
        if env.get("XUI_DB_TYPE", "").lower() in ("postgres", "postgresql", "pg"):
            dsn = env.get("XUI_DB_DSN") or die("XUI_DB_DSN missing in " + env_path)
            return {"kind": "postgres", "dsn": dsn}, env
        folder = env.get("XUI_DB_FOLDER")
        return {"kind": "sqlite", "path": os.path.join(folder, "x-ui.db") if folder else default_db}, env
    url = env.get("SQLALCHEMY_DATABASE_URL") or "sqlite:///" + default_db
    return parse_db_url(url, os.path.dirname(env_path or default_db), default_db), env


def do_export():
    print("\nSource panel:")
    for k, p in PANELS.items():
        print("  %s) %s" % (k, p[1]))
    key = ask("Select [1-3]: ")
    if key not in PANELS:
        die("Invalid choice.")
    out_dir = ask("Output directory [%s]: " % DEFAULT_DIR, DEFAULT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    conf, env = source_db(key)
    print("[+] Database: %s" % (conf.get("path") or "%s %s" % (conf["kind"], conf.get("db") or "")))
    db = DB(conf)
    panel = PANELS[key][0]
    reader = {"marzban": read_marzban, "3xui": read_3xui}.get(panel)
    admins, users, meta = reader(db) if reader else read_marzneshin(db, env)
    path = os.path.join(out_dir, "m2r_%s_%s.sql" % (panel, dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_sql(panel, admins, users, meta))
    os.chmod(path, 0o600)  # contains password hashes and secrets
    print("[✓] Exported %d admins, %d users -> %s" % (len(admins), len(users), path))


MERGE = """SET NAMES utf8mb4;
START TRANSACTION;
SET @maxa := (SELECT COALESCE(MAX(id), 0) FROM admins);
SET @maxu := (SELECT COALESCE(MAX(id), 0) FROM users);
INSERT INTO admins (username, hashed_password, created_at, role, permissions, telegram_id,
  users_usage, lifetime_usage, data_limit, status)
SELECT new_username, hashed_password, COALESCE(created_at, NOW()), role, '{}', telegram_id,
  users_usage, users_usage, data_limit, status FROM m2r_admins ORDER BY id;
UPDATE m2r_admins m JOIN admins a ON a.id > @maxa AND CAST(a.username AS BINARY) = CAST(m.new_username AS BINARY)
  SET m.new_id = a.id;
INSERT INTO services (name, description)
SELECT 'M2R', 'Users imported by M2R' FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM services WHERE name = 'M2R');
SET @sid := (SELECT id FROM services WHERE name = 'M2R' LIMIT 1);
INSERT IGNORE INTO service_hosts (service_id, host_id) SELECT @sid, id FROM hosts;
INSERT IGNORE INTO service_admins (admin_id, service_id) SELECT new_id, @sid FROM m2r_admins WHERE new_id IS NOT NULL;
INSERT INTO users (username, status, used_traffic, data_limit, data_limit_reset_strategy, expire, admin_id,
  service_id, created_at, note, telegram_id, on_hold_expire_duration, on_hold_timeout, ip_limit,
  sub_revoked_at, edit_at, last_status_change, subscription_token, flow)
SELECT u.new_username, u.status, u.used_traffic, u.data_limit, u.reset_strategy, u.expire, a.new_id,
  @sid, COALESCE(u.created_at, NOW()), u.note, u.telegram_id, u.on_hold_expire_duration, u.on_hold_timeout,
  u.ip_limit, u.sub_revoked_at, u.edit_at, COALESCE(u.last_status_change, NOW()), u.subscription_token, u.flow
FROM m2r_users u LEFT JOIN m2r_admins a ON a.id = u.admin_id ORDER BY u.id;
UPDATE m2r_users m JOIN users x ON x.id > @maxu AND CAST(x.username AS BINARY) = CAST(m.new_username AS BINARY)
  SET m.new_id = x.id;
INSERT INTO user_credentials (user_id, type, settings)
SELECT m.new_id, c.type, c.settings FROM m2r_creds c JOIN m2r_users m ON m.id = c.user_id WHERE m.new_id IS NOT NULL;
COMMIT;
"""


def do_import():
    src = ask("Export file or directory [%s]: " % DEFAULT_DIR, DEFAULT_DIR)
    if os.path.isdir(src):
        files = sorted(glob.glob(os.path.join(src, "m2r_*.sql")), key=os.path.getmtime)
        src = files[-1] if files else die("No m2r_*.sql file in " + src)
    if not os.path.isfile(src):
        die("Not found: " + src)
    print("[+] File: " + src)
    env_path = next((p for p in REBECCA_ENV if os.path.exists(p)), None) or \
        ask("[?] Rebecca .env not found. Enter its path: ")
    env = read_env(env_path)
    url = env.get("SQLALCHEMY_DATABASE_URL") or env.get("DATABASE_URL") or ""
    if not url or url.startswith("sqlite"):
        die("Rebecca must run on MySQL/MariaDB for import (SQLALCHEMY_DATABASE_URL in %s)." % env_path)
    db = DB(parse_db_url(url, os.path.dirname(env_path), ""))
    if "users" not in db.tables():
        die("Rebecca tables not found; start Rebecca once so it runs its migrations.")

    rebecca_users = db.query_json("SELECT JSON_OBJECT('n', COUNT(*)) FROM users WHERE status != 'deleted';")[0]["n"]
    print("[+] Loading staging tables ...")
    with open(src, encoding="utf-8") as f:
        db.execute(f.read())

    updates = []
    for table, live in (("m2r_admins", "admins"), ("m2r_users", "users")):
        taken = {r["u"].lower() for r in db.query_json(
            "SELECT JSON_OBJECT('u', username) FROM `%s` WHERE username IS NOT NULL;" % live)}
        renamed = 0
        for r in db.query_json("SELECT JSON_OBJECT('id', id, 'u', username) FROM %s ORDER BY id;" % table):
            new = unique_name(r["u"], taken)
            renamed += new != r["u"]
            updates.append("UPDATE %s SET new_username = %s WHERE id = %d;" % (table, q(new), r["id"]))
        print("[+] %s: %d renamed because the name already exists in Rebecca" % (live, renamed))
    db.execute("\n".join(updates))

    print("[+] Merging into Rebecca ...")
    db.execute(MERGE)
    for r in db.query_json("SELECT JSON_OBJECT('a', username, 'b', new_username) FROM m2r_users WHERE username != new_username;"):
        print("    user  %s -> %s" % (r["a"], r["b"]))
    for r in db.query_json("SELECT JSON_OBJECT('a', username, 'b', new_username) FROM m2r_admins WHERE username != new_username;"):
        print("    admin %s -> %s" % (r["a"], r["b"]))

    meta = {r["k"]: r["v"] for r in db.query_json("SELECT JSON_OBJECT('k', k, 'v', v) FROM m2r_meta;")}
    if meta.get("sub_secret"):
        ok = rebecca_users == 0 or ask(
            "[?] Rebecca already has %d users. Use the old panel's subscription secret so old /sub links keep "
            "working? This breaks the current Rebecca users' links. [y/N]: " % rebecca_users).lower() == "y"
        if ok:
            db.execute("UPDATE auth_secrets SET subscription_secret_key = %s ORDER BY id LIMIT 1;" % q(meta["sub_secret"]))
            print("[+] Subscription secret copied: old subscription links stay valid.")

    plain = db.query_json("SELECT JSON_OBJECT('u', new_username, 'p', plain_password) FROM m2r_admins WHERE plain_password IS NOT NULL;")
    for r in plain:
        cmd = ["rebecca", "cli", "admin", "set-password", "--username", r["u"], "--password", r["p"]]
        if not (shutil.which("rebecca") and subprocess.run(cmd, capture_output=True).returncode == 0):
            print("[~] Set the password of admin '%s': rebecca cli admin set-password --username %s" % (r["u"], r["u"]))
    counts = db.query_json("SELECT JSON_OBJECT('a', (SELECT COUNT(*) FROM m2r_admins), 'u', (SELECT COUNT(*) FROM m2r_users));")[0]
    db.execute("DROP TABLE IF EXISTS m2r_meta, m2r_admins, m2r_users, m2r_creds;")
    print("[✓] Imported %s admins and %s users (service 'M2R'). Restart Rebecca: rebecca restart" % (counts["a"], counts["u"]))


def selftest():
    taken = {"ali", "ali-2"}
    assert unique_name("Ali", taken) == "Ali-3" and unique_name("bob", taken) == "bob"
    assert clean_name("علی test@x", "u1") == "test@x" and clean_name("a", "u") == "a"
    assert clean_name("_ali_", "u") == "_ali_" and clean_name("علی", "user7") == "user7"
    assert len(unique_name("x" * 32, {"x" * 32})) == 32
    assert unix("2024-01-01T00:00:00Z") == 1704067200 == unix("2024-01-01 00:00:00.000000") == unix(1704067200000)
    assert q("it's\n") == "'it\\'s\\n'" and q(None) == "NULL" and q({"a": 1}) == "'{\"a\": 1}'"
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if os.geteuid() != 0:
        print("[~] Not running as root: reading panel files may fail.")
    while True:
        print("\n===== M2R %s : panels -> Rebecca =====\n  1) Exporting\n  2) Importing\n  3) Exit" % VERSION)
        c = ask("Select: ")
        if c == "1":
            do_export()
        elif c == "2":
            do_import()
        elif c == "3":
            return
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
    except RuntimeError as e:
        die(str(e))
