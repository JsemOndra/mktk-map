"""SQLite persistence — client history, signal/traffic samples, events."""

import json
import os
import sqlite3
import threading
import time

from i18n import t

DB_PATH = os.environ.get("DB_PATH", "/data/mktk.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(
  mac TEXT PRIMARY KEY,
  first_seen REAL,
  last_seen REAL,
  hostname TEXT,
  vendor TEXT,
  last_device TEXT,
  last_conn TEXT,
  last_ssid TEXT,
  last_vid TEXT,
  gone_notified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS samples(
  ts REAL, mac TEXT, device TEXT,
  signal INTEGER, tx_bytes INTEGER, rx_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples(mac, ts);
CREATE TABLE IF NOT EXISTS events(
  ts REAL, type TEXT, mac TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, created REAL, expires REAL
);
CREATE TABLE IF NOT EXISTS api_tokens(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT UNIQUE, label TEXT, created REAL, last_used REAL
);
CREATE TABLE IF NOT EXISTS update_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT, action TEXT, state TEXT, status TEXT,
  from_version TEXT, to_version TEXT,
  started REAL, finished REAL, started_by TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS update_job_log(
  job_id INTEGER, ts REAL, state TEXT, msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_log ON update_job_log(job_id, ts);
CREATE TABLE IF NOT EXISTS device_samples(
  ts REAL, device TEXT, cpu REAL, mem REAL, temp REAL,
  rx_bps REAL, tx_bps REAL, clients INTEGER, up INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dev_samples ON device_samples(device, ts);
CREATE TABLE IF NOT EXISTS poll_stats(
  ts REAL, source TEXT, device TEXT, ms REAL, rows INTEGER
);
CREATE INDEX IF NOT EXISTS idx_poll_stats ON poll_stats(device, source, ts);
CREATE TABLE IF NOT EXISTS config_backups(
  id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, ts REAL, checked REAL, sha TEXT,
  version TEXT, reason TEXT, who TEXT, summary TEXT, size INTEGER, blob BLOB
);
CREATE INDEX IF NOT EXISTS idx_backups_dev ON config_backups(device, ts);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT, type TEXT, severity TEXT,
  device TEXT, title TEXT, message TEXT, state TEXT, sent INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS audit_log(
  ts REAL, actor TEXT, area TEXT, action TEXT, detail TEXT, ok INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS conn_stats(
  hour INTEGER, mac TEXT, dst TEXT, proto TEXT, port INTEGER, service TEXT,
  up INTEGER, down INTEGER, conns INTEGER,
  PRIMARY KEY(hour, mac, dst, proto, port)
);
CREATE TABLE IF NOT EXISTS dns_stats(
  hour INTEGER, mac TEXT, ip TEXT, name TEXT, qtype TEXT, count INTEGER, last REAL,
  PRIMARY KEY(hour, ip, name, qtype)
);
CREATE INDEX IF NOT EXISTS idx_dns_mac ON dns_stats(mac, hour);
CREATE TABLE IF NOT EXISTS dns_daily(
  day INTEGER, mac TEXT, ip TEXT, name TEXT, count INTEGER, first REAL, last REAL,
  PRIMARY KEY(day, ip, name)
);
CREATE INDEX IF NOT EXISTS idx_dns_daily_name ON dns_daily(name, day);
CREATE TABLE IF NOT EXISTS tls_pins(
  host TEXT PRIMARY KEY, fingerprint TEXT, first_seen REAL
);
"""

SAMPLE_RETENTION_DAYS = 7
EVENT_RETENTION_DAYS = 30

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def init():
    global _conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(_SCHEMA)
    # migrate older DBs (executescript does not add a new column to an existing table)
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(clients)")}
    if "last_ssid" not in cols:
        _conn.execute("ALTER TABLE clients ADD COLUMN last_ssid TEXT")
    if "last_vid" not in cols:
        _conn.execute("ALTER TABLE clients ADD COLUMN last_vid TEXT")
    _conn.commit()


def _parse_bytes(b):
    """RouterOS 'tx,rx' -> (tx, rx) or (None, None)."""
    try:
        tx, rx = str(b).split(",")
        return int(tx), int(rx)
    except (ValueError, AttributeError, TypeError):
        return None, None


def process(clients: list[dict], gone_after_min: int) -> list[dict]:
    """Update DB from a snapshot, return list of new events.

    Event types: new_client, client_gone, client_back, roaming.
    """
    now = time.time()
    events: list[dict] = []
    online = [c for c in clients if c["conn"] != "lease-only"]

    with _lock:
        cur = _conn.cursor()
        for c in online:
            mac = c["mac"]
            row = cur.execute(
                "SELECT last_device, last_conn, gone_notified FROM clients WHERE mac=?",
                (mac,),
            ).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO clients(mac, first_seen, last_seen, hostname, vendor,"
                    " last_device, last_conn, last_ssid, last_vid)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (mac, now, now, c.get("hostname"), c.get("vendor"),
                     c.get("device"), c["conn"],
                     c.get("ssid") if c["conn"] == "wifi" else None,
                     c.get("vid")),
                )
                events.append({"type": "new_client", "mac": mac,
                               "detail": {"device": c.get("device"),
                                          "hostname": c.get("hostname"),
                                          "vendor": c.get("vendor"),
                                          "ip": c.get("ip"),
                                          "ssid": c.get("ssid"),
                                          "vlan": c.get("vid")}})
            else:
                prev_device, prev_conn, gone_notified = row
                if (c["conn"] == "wifi" and prev_conn == "wifi"
                        and prev_device and c.get("device")
                        and prev_device != c["device"]):
                    events.append({"type": "roaming", "mac": mac,
                                   "detail": {"from": prev_device,
                                              "to": c["device"],
                                              "hostname": c.get("hostname"),
                                              "signal": c.get("signal"),
                                              "ssid": c.get("ssid"),
                                              "vlan": c.get("vid")}})
                if gone_notified:
                    events.append({"type": "client_back", "mac": mac,
                                   "detail": {"device": c.get("device"),
                                              "hostname": c.get("hostname"),
                                              "ssid": c.get("ssid"),
                                              "vlan": c.get("vid")}})
                cur.execute(
                    "UPDATE clients SET last_seen=?, hostname=COALESCE(?,hostname),"
                    " vendor=COALESCE(?,vendor), last_device=?, last_conn=?,"
                    " last_ssid=COALESCE(?,last_ssid),"
                    " last_vid=COALESCE(?,last_vid), gone_notified=0 WHERE mac=?",
                    (now, c.get("hostname"), c.get("vendor"),
                     c.get("device"), c["conn"],
                     c.get("ssid") if c["conn"] == "wifi" else None,
                     c.get("vid"), mac),
                )
            # sample wifi metrics
            if c["conn"] == "wifi":
                tx, rx = _parse_bytes(c.get("bytes"))
                sig = None
                try:
                    sig = int(str(c.get("signal")).split("@")[0])
                except (ValueError, TypeError):
                    pass
                cur.execute(
                    "INSERT INTO samples(ts, mac, device, signal, tx_bytes, rx_bytes)"
                    " VALUES(?,?,?,?,?,?)",
                    (now, mac, c.get("device"), sig, tx, rx),
                )

        # gone detection
        cutoff = now - gone_after_min * 60
        for mac, hostname, device, last_ssid, last_vid in cur.execute(
            "SELECT mac, hostname, last_device, last_ssid, last_vid FROM clients"
            " WHERE last_seen < ? AND gone_notified = 0", (cutoff,)
        ).fetchall():
            events.append({"type": "client_gone", "mac": mac,
                           "detail": {"hostname": hostname, "device": device,
                                      "ssid": last_ssid, "vlan": last_vid}})
        cur.executemany(
            "UPDATE clients SET gone_notified=1 WHERE mac=?",
            [(e["mac"],) for e in events if e["type"] == "client_gone"],
        )

        for e in events:
            cur.execute(
                "INSERT INTO events(ts, type, mac, detail) VALUES(?,?,?,?)",
                (now, e["type"], e["mac"], json.dumps(e["detail"], ensure_ascii=False)),
            )
            e["ts"] = now

        # retention
        cur.execute("DELETE FROM samples WHERE ts < ?",
                    (now - SAMPLE_RETENTION_DAYS * 86400,))
        cur.execute("DELETE FROM events WHERE ts < ?",
                    (now - EVENT_RETENTION_DAYS * 86400,))
        _conn.commit()
    return events


def client_detail(mac: str, hours: int = 24) -> dict:
    since = time.time() - hours * 3600
    with _lock:
        cur = _conn.cursor()
        meta = cur.execute(
            "SELECT first_seen, last_seen, vendor FROM clients WHERE mac=?", (mac,)
        ).fetchone()
        samples = cur.execute(
            "SELECT ts, signal, tx_bytes, rx_bytes, device FROM samples"
            " WHERE mac=? AND ts>=? ORDER BY ts", (mac, since)
        ).fetchall()
        roaming = cur.execute(
            "SELECT ts, detail FROM events WHERE mac=? AND type='roaming'"
            " ORDER BY ts DESC LIMIT 10", (mac,)
        ).fetchall()
    return {
        "meta": {"first_seen": meta[0], "last_seen": meta[1],
                 "vendor": meta[2]} if meta else None,
        "samples": [
            {"ts": t, "signal": s, "tx": tx, "rx": rx, "device": d}
            for t, s, tx, rx, d in samples
        ],
        "roaming": [{"ts": t, **json.loads(d)} for t, d in roaming],
    }


def presence(mac: str, online_window_s: int) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT first_seen, last_seen, hostname, vendor, last_device, last_conn,"
            " last_ssid FROM clients WHERE mac=?", (mac,)
        ).fetchone()
    if not row:
        return None
    first, last, hostname, vendor, device, conn, last_ssid = row
    return {
        "mac": mac,
        "online": (time.time() - last) <= online_window_s,
        "last_seen": last,
        "first_seen": first,
        "hostname": hostname,
        "vendor": vendor,
        "device": device,
        "conn": conn,
        "last_ssid": last_ssid,
    }


def recent_events(hours: int = 24) -> list[dict]:
    since = time.time() - hours * 3600
    with _lock:
        rows = _conn.execute(
            "SELECT ts, type, mac, detail FROM events WHERE ts>=? ORDER BY ts DESC",
            (since,),
        ).fetchall()
    return [{"ts": t, "type": ty, "mac": m, **json.loads(d)} for t, ty, m, d in rows]


SESSION_TTL_DAYS = 30


def create_session() -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _conn.execute(
            "INSERT INTO sessions(token, created, expires) VALUES(?,?,?)",
            (token, now, now + SESSION_TTL_DAYS * 86400),
        )
        _conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        _conn.commit()
    return token


def check_session(token: str) -> bool:
    if not token:
        return False
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM sessions WHERE token=? AND expires > ?",
            (token, time.time()),
        ).fetchone()
    return row is not None


def delete_session(token: str):
    with _lock:
        _conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        _conn.commit()


def create_api_token(label: str) -> str:
    import hashlib
    import secrets
    token = "mktk_" + secrets.token_urlsafe(32)
    h = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        _conn.execute(
            "INSERT INTO api_tokens(token_hash, label, created) VALUES(?,?,?)",
            (h, label, time.time()),
        )
        _conn.commit()
    return token


def check_api_token(token: str) -> bool:
    if not token:
        return False
    import hashlib
    h = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        row = _conn.execute(
            "SELECT id FROM api_tokens WHERE token_hash=?", (h,)
        ).fetchone()
        if row:
            _conn.execute(
                "UPDATE api_tokens SET last_used=? WHERE id=?", (time.time(), row[0])
            )
            _conn.commit()
    return row is not None


def list_api_tokens() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, created, last_used FROM api_tokens ORDER BY id"
        ).fetchall()
    return [{"id": i, "label": l, "created": c, "last_used": u}
            for i, l, c, u in rows]


def delete_api_token(token_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
        _conn.commit()
        return cur.rowcount > 0


def client_meta_map() -> dict[str, dict]:
    """mac -> {first_seen, last_ssid} to enrich the snapshot."""
    with _lock:
        rows = _conn.execute(
            "SELECT mac, first_seen, last_ssid FROM clients"
        ).fetchall()
    return {mac: {"first_seen": fs, "last_ssid": ssid} for mac, fs, ssid in rows}


# --- TLS certificate fingerprints of the MikroTiks (TOFU) -----------------

def pin_get(host: str) -> str | None:
    with _lock:
        row = _conn.execute(
            "SELECT fingerprint FROM tls_pins WHERE host=?", (host,)).fetchone()
    return row[0] if row else None


def pin_set(host: str, fingerprint: str):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO tls_pins(host, fingerprint, first_seen) VALUES(?,?,?)",
            (host, fingerprint, time.time()))
        _conn.commit()


def pin_list() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT host, fingerprint, first_seen FROM tls_pins ORDER BY host").fetchall()
    return [{"host": h, "fingerprint": f, "first_seen": t} for h, f, t in rows]


def pin_delete(host: str) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM tls_pins WHERE host=?", (host,))
        _conn.commit()
        return cur.rowcount > 0


# --- updates: jobs and their progress (permanent record) -------------------

def job_create(device: str, action: str, started_by: str) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO update_jobs(device, action, state, status, started, started_by)"
            " VALUES(?,?,?,?,?,?)",
            (device, action, "queued", "running", time.time(), started_by))
        _conn.commit()
        return cur.lastrowid


def job_log(job_id: int, state: str, msg: str = ""):
    now = time.time()
    with _lock:
        _conn.execute("INSERT INTO update_job_log(job_id, ts, state, msg) VALUES(?,?,?,?)",
                      (job_id, now, state, msg))
        _conn.execute("UPDATE update_jobs SET state=? WHERE id=?", (state, job_id))
        _conn.commit()


def job_set(job_id: int, **fields):
    allowed = {"from_version", "to_version", "status", "finished", "error", "state"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    with _lock:
        _conn.execute(
            "UPDATE update_jobs SET " + ", ".join(f"{c}=?" for c in cols) + " WHERE id=?",
            [fields[c] for c in cols] + [job_id])
        _conn.commit()


def jobs_mark_interrupted() -> int:
    """After app start: jobs that were running will never be finished."""
    now = time.time()
    with _lock:
        ids = [r[0] for r in _conn.execute(
            "SELECT id FROM update_jobs WHERE status='running'").fetchall()]
        for i in ids:
            _conn.execute(
                "INSERT INTO update_job_log(job_id, ts, state, msg) VALUES(?,?,?,?)",
                (i, now, "interrupted", t("srv.upd.interrupted")))
            _conn.execute(
                "UPDATE update_jobs SET status='interrupted', state='interrupted',"
                " finished=? WHERE id=?", (now, i))
        _conn.commit()
    return len(ids)


def jobs_recent(limit: int = 30) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, device, action, state, status, from_version, to_version,"
            " started, finished, started_by, error FROM update_jobs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        ids = [r[0] for r in rows]
        logs: dict[int, list] = {i: [] for i in ids}
        if ids:
            q = ",".join("?" * len(ids))
            for jid, ts, st, msg in _conn.execute(
                    f"SELECT job_id, ts, state, msg FROM update_job_log"
                    f" WHERE job_id IN ({q}) ORDER BY ts, rowid", ids):
                logs[jid].append({"ts": ts, "state": st, "msg": msg})
    keys = ("id", "device", "action", "state", "status", "from_version",
            "to_version", "started", "finished", "started_by", "error")
    return [{**dict(zip(keys, r)), "log": logs[r[0]]} for r in rows]


# --- device history (charts) ----------------------------------------------

DEVICE_SAMPLE_RETENTION_DAYS = 8


def add_device_samples(rows: list[dict]):
    now = time.time()
    with _lock:
        _conn.executemany(
            "INSERT INTO device_samples(ts, device, cpu, mem, temp, rx_bps, tx_bps, clients, up)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(now, r["device"], r.get("cpu"), r.get("mem"), r.get("temp"), r.get("rx_bps"),
              r.get("tx_bps"), r.get("clients"), 1 if r.get("up") else 0) for r in rows])
        _conn.execute("DELETE FROM device_samples WHERE ts < ?",
                      (now - DEVICE_SAMPLE_RETENTION_DAYS * 86400,))
        _conn.execute("DELETE FROM poll_stats WHERE ts < ?",
                      (now - DEVICE_SAMPLE_RETENTION_DAYS * 86400,))
        _conn.commit()


def device_history(device: str, hours: float, points: int = 300) -> list[dict]:
    """Samples averaged into at most `points` buckets; max for CPU/traffic, min for up."""
    since = time.time() - hours * 3600
    bucket = max(30.0, hours * 3600 / points)
    with _lock:
        rows = _conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS b, AVG(cpu), MAX(cpu), AVG(mem), AVG(temp),"
            " AVG(rx_bps), MAX(rx_bps), AVG(tx_bps), MAX(tx_bps), AVG(clients), MIN(up)"
            " FROM device_samples WHERE device=? AND ts>=? GROUP BY b ORDER BY b",
            (bucket, bucket, device, since)).fetchall()
    keys = ("ts", "cpu", "cpu_max", "mem", "temp", "rx", "rx_max", "tx", "tx_max",
            "clients", "up")
    return [dict(zip(keys, r)) for r in rows]


# --- audit (changes made from the app) --------------------------------------

def audit(actor: str, area: str, action: str, detail: str, ok: bool = True):
    with _lock:
        _conn.execute("INSERT INTO audit_log(ts, actor, area, action, detail, ok) VALUES(?,?,?,?,?,?)",
                      (time.time(), actor, area, action, detail, 1 if ok else 0))
        _conn.commit()


def audit_recent(area: str | None = None, limit: int = 50) -> list[dict]:
    q = "SELECT ts, actor, area, action, detail, ok FROM audit_log"
    args: list = []
    if area:
        q += " WHERE area=?"
        args.append(area)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [{"ts": t, "actor": a, "area": ar, "action": ac, "detail": d, "ok": bool(o)}
            for t, a, ar, ac, d, o in rows]


# --- device renaming ------------------------------------------------------

def rename_device_refs(old: str, new: str) -> dict:
    """Rewrite the device name in all tables so the history continues."""
    out = {}
    with _lock:
        for table, col in (("clients", "last_device"), ("samples", "device"),
                           ("device_samples", "device"), ("update_jobs", "device"),
                           ("config_backups", "device"), ("alerts", "device"),
                           ("poll_stats", "device")):
            cur = _conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
            out[table] = cur.rowcount
        n = 0
        for rowid, detail in _conn.execute(
                "SELECT rowid, detail FROM events WHERE detail LIKE ?", (f"%{old}%",)).fetchall():
            try:
                d = json.loads(detail)
            except (ValueError, TypeError):
                continue
            changed = False
            for k in ("device", "from", "to"):
                if d.get(k) == old:
                    d[k] = new
                    changed = True
            if changed:
                _conn.execute("UPDATE events SET detail=? WHERE rowid=?",
                              (json.dumps(d, ensure_ascii=False), rowid))
                n += 1
        out["events"] = n
        _conn.commit()
    return out


# --- connection and DNS statistics (hourly aggregates) --------------------

STATS_RETENTION_DAYS = 7          # hourly details (connections and DNS) — overridden by main from config
DNS_DAILY_RETENTION_DAYS = 90     # daily summary of DNS queries
_last_stats_cleanup = [0.0]


def _stats_cleanup(now: float):
    if now - _last_stats_cleanup[0] < 3600:
        return
    _last_stats_cleanup[0] = now
    cut = now - STATS_RETENTION_DAYS * 86400
    _conn.execute("DELETE FROM conn_stats WHERE hour < ?", (cut,))
    _conn.execute("DELETE FROM dns_stats WHERE hour < ?", (cut,))
    _conn.execute("DELETE FROM dns_daily WHERE day < ?", (now - DNS_DAILY_RETENTION_DAYS * 86400,))


def add_conn_stats(rows: list[tuple]):
    """rows: (hour, mac, dst, proto, port, service, up, down, conns)"""
    with _lock:
        _conn.executemany(
            "INSERT INTO conn_stats(hour, mac, dst, proto, port, service, up, down, conns)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(hour, mac, dst, proto, port) DO UPDATE SET"
            " up=up+excluded.up, down=down+excluded.down, conns=conns+excluded.conns,"
            " service=COALESCE(excluded.service, service)", rows)
        _stats_cleanup(time.time())
        _conn.commit()


def add_dns_stats(rows: list[tuple]):
    """rows: (hour, mac, ip, name, qtype, count, last)"""
    with _lock:
        _conn.executemany(
            "INSERT INTO dns_stats(hour, mac, ip, name, qtype, count, last) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(hour, ip, name, qtype) DO UPDATE SET count=count+excluded.count,"
            " last=MAX(last, excluded.last), mac=COALESCE(excluded.mac, mac)", rows)
        daily: dict[tuple, list] = {}
        for hour, mac, ip, name, _qt, cnt, last in rows:
            k = (int(_local_day(hour)), ip, name)
            d = daily.setdefault(k, [mac, 0, hour, last])
            d[0] = d[0] or mac
            d[1] += cnt
            d[3] = max(d[3], last)
        _conn.executemany(
            "INSERT INTO dns_daily(day, mac, ip, name, count, first, last) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(day, ip, name) DO UPDATE SET count=count+excluded.count,"
            " last=MAX(last, excluded.last), first=MIN(first, excluded.first),"
            " mac=COALESCE(excluded.mac, mac)",
            [(k[0], v[0], k[1], k[2], v[1], v[2], v[3]) for k, v in daily.items()])
        _stats_cleanup(time.time())
        _conn.commit()


def conn_top(mac: str | None, hours: float = 24, limit: int = 30) -> list[dict]:
    since = time.time() - hours * 3600
    q = ("SELECT dst, proto, port, MAX(service), SUM(up), SUM(down), SUM(conns) FROM conn_stats"
         " WHERE hour >= ?")
    args: list = [since - 3600]
    if mac:
        q += " AND mac = ?"
        args.append(mac)
    q += " GROUP BY dst, proto, port ORDER BY SUM(up)+SUM(down) DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [{"dst": d, "proto": p, "port": po, "service": sv, "up": u, "down": dn, "conns": n}
            for d, p, po, sv, u, dn, n in rows]


def clients_top(hours: float = 24, limit: int = 20) -> list[dict]:
    since = time.time() - hours * 3600
    with _lock:
        rows = _conn.execute(
            "SELECT mac, SUM(up), SUM(down), SUM(conns), COUNT(DISTINCT dst) FROM conn_stats"
            " WHERE hour >= ? GROUP BY mac ORDER BY SUM(up)+SUM(down) DESC LIMIT ?",
            (since - 3600, limit)).fetchall()
    return [{"mac": m, "up": u, "down": d, "conns": n, "destinations": k} for m, u, d, n, k in rows]


def dns_top(mac: str | None, ip: str | None, hours: float = 24, limit: int = 100) -> list[dict]:
    since = time.time() - hours * 3600
    q = "SELECT name, GROUP_CONCAT(DISTINCT qtype), SUM(count), MAX(last) FROM dns_stats WHERE hour >= ?"
    args: list = [since - 3600]
    if mac and ip:
        q += " AND (mac = ? OR ip = ?)"
        args += [mac, ip]
    elif mac:
        q += " AND mac = ?"
        args.append(mac)
    elif ip:
        q += " AND ip = ?"
        args.append(ip)
    q += " GROUP BY name ORDER BY SUM(count) DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [{"name": n, "types": t, "count": c, "last": l} for n, t, c, l in rows]


def _local_day(ts: float) -> float:
    """Start of the local day (midnight) for the given time."""
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def dns_overview(hours: float, q: str = "") -> tuple[list, str]:
    """(name, who, count, last, types) for a period — from the hourly table, beyond 7 days from the daily one."""
    since = time.time() - hours * 3600
    like = f"%{q.lower()}%" if q else None
    if hours <= STATS_RETENTION_DAYS * 24:
        sql = ("SELECT name, mac, ip, SUM(count), MAX(last), GROUP_CONCAT(DISTINCT qtype)"
               " FROM dns_stats WHERE hour >= ?")
        args: list = [since - 3600]
        src = "hourly"
    else:
        sql = "SELECT name, mac, ip, SUM(count), MAX(last), NULL FROM dns_daily WHERE day >= ?"
        args = [since - 86400]
        src = "daily"
    if like:
        sql += " AND name LIKE ?"
        args.append(like)
    sql += " GROUP BY name, mac, ip"
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    return rows, src


def dns_first_seen(names: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    with _lock:
        for i in range(0, len(names), 500):
            chunk = names[i:i + 500]
            q = ",".join("?" * len(chunk))
            for n, f in _conn.execute(
                    f"SELECT name, MIN(first) FROM dns_daily WHERE name IN ({q}) GROUP BY name", chunk):
                out[n] = f
    return out


def dns_hourly_counts(hours: float, name: str | None = None) -> list[tuple]:
    since = time.time() - hours * 3600
    sql = "SELECT hour, SUM(count) FROM dns_stats WHERE hour >= ?"
    args: list = [since - 3600]
    if name:
        sql += " AND (name = ? OR name LIKE ?)"
        args += [name, "%." + name]
    sql += " GROUP BY hour ORDER BY hour"
    with _lock:
        return _conn.execute(sql, args).fetchall()


# --- router poll duration (load) -------------------------------------------

def add_poll_stat(source: str, device: str, ms: float, rows: int | None = None):
    with _lock:
        _conn.execute("INSERT INTO poll_stats(ts, source, device, ms, rows) VALUES(?,?,?,?,?)",
                      (time.time(), source, device, ms, rows))
        _conn.commit()


def poll_summary(device: str, hours: float = 1) -> dict:
    """avg / p95 / max poll duration per source (monitor = regular poll, conntrack = connection table)."""
    since = time.time() - hours * 3600
    with _lock:
        rows = _conn.execute("SELECT source, ms, rows FROM poll_stats WHERE device=? AND ts>=?",
                             (device, since)).fetchall()
    out = {}
    for src in sorted({r[0] for r in rows}):
        v = sorted(r[1] for r in rows if r[0] == src)
        rr = [r[2] for r in rows if r[0] == src and r[2] is not None]
        out[src] = {"count": len(v), "avg": sum(v) / len(v), "p95": v[min(len(v) - 1, int(len(v) * 0.95))],
                    "max": v[-1], "min": v[0], "rows_avg": (sum(rr) / len(rr)) if rr else None}
    return out


def poll_series(device: str, hours: float, points: int = 300) -> list[dict]:
    since = time.time() - hours * 3600
    bucket = max(30.0, hours * 3600 / points)
    with _lock:
        rows = _conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS b, source, AVG(ms), MAX(ms) FROM poll_stats"
            " WHERE device=? AND ts>=? GROUP BY b, source ORDER BY b",
            (bucket, bucket, device, since)).fetchall()
    out: dict[float, dict] = {}
    for b, src, avg, mx in rows:
        d = out.setdefault(b, {"ts": b})
        d[src] = avg
        d[src + "_max"] = mx
    return list(out.values())


# --- configuration backups ------------------------------------------------

def backup_last(device: str) -> dict | None:
    with _lock:
        r = _conn.execute("SELECT id, ts, checked, sha, version FROM config_backups WHERE device=?"
                          " ORDER BY id DESC LIMIT 1", (device,)).fetchone()
    return {"id": r[0], "ts": r[1], "checked": r[2], "sha": r[3], "version": r[4]} if r else None


def backup_touch(bid: int):
    with _lock:
        _conn.execute("UPDATE config_backups SET checked=? WHERE id=?", (time.time(), bid))
        _conn.commit()


def backup_add(device, text, sha, version, reason, who, summary) -> int:
    import backups
    now = time.time()
    with _lock:
        cur = _conn.execute(
            "INSERT INTO config_backups(device, ts, checked, sha, version, reason, who, summary, size, blob)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (device, now, now, sha, version, reason, who,
             json.dumps(summary, ensure_ascii=False) if summary else None, len(text),
             backups.pack(text)))
        _conn.commit()
        return cur.lastrowid


def backup_text(bid: int) -> str:
    import backups
    with _lock:
        r = _conn.execute("SELECT blob FROM config_backups WHERE id=?", (bid,)).fetchone()
    return backups.unpack(r[0]) if r else ""


def backup_get(bid: int) -> dict | None:
    with _lock:
        r = _conn.execute("SELECT id, device, ts, checked, sha, version, reason, who, summary, size"
                          " FROM config_backups WHERE id=?", (bid,)).fetchone()
    if not r:
        return None
    keys = ("id", "device", "ts", "checked", "sha", "version", "reason", "who", "summary", "size")
    d = dict(zip(keys, r))
    d["summary"] = json.loads(d["summary"]) if d["summary"] else None
    return d


def backup_list(device: str | None = None, limit: int = 100) -> list[dict]:
    q = ("SELECT id, device, ts, checked, sha, version, reason, who, summary, size"
         " FROM config_backups")
    args: list = []
    if device:
        q += " WHERE device=?"
        args.append(device)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    keys = ("id", "device", "ts", "checked", "sha", "version", "reason", "who", "summary", "size")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        out.append(d)
    return out


def backup_prev(bid: int) -> int | None:
    with _lock:
        r = _conn.execute("SELECT b.id FROM config_backups a JOIN config_backups b"
                          " ON b.device=a.device AND b.id<a.id WHERE a.id=? ORDER BY b.id DESC LIMIT 1",
                          (bid,)).fetchone()
    return r[0] if r else None


def backup_prune(device: str, keep: int):
    with _lock:
        _conn.execute("DELETE FROM config_backups WHERE device=? AND id NOT IN"
                      " (SELECT id FROM config_backups WHERE device=? ORDER BY id DESC LIMIT ?)",
                      (device, device, keep))
        _conn.commit()


# --- alerts ----------------------------------------------------------------

def alert_add(a: dict) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO alerts(ts, key, type, severity, device, title, message, state, sent, detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (a["ts"], a["key"], a["type"], a["severity"], a.get("device"), a["title"],
             a.get("message"), a["state"], 1 if a.get("sent") else 0,
             json.dumps(a.get("detail") or {}, ensure_ascii=False)))
        _conn.execute("DELETE FROM alerts WHERE ts < ?", (time.time() - 90 * 86400,))
        _conn.commit()
        return cur.lastrowid


def alerts_recent(hours: float = 24, limit: int = 200) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, ts, key, type, severity, device, title, message, state, sent, detail"
            " FROM alerts WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (time.time() - hours * 3600, limit)).fetchall()
    keys = ("id", "ts", "key", "type", "severity", "device", "title", "message", "state", "sent", "detail")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
        out.append(d)
    return out


def dns_known_pairs() -> list[tuple]:
    """(ip, domain, first) from the daily summaries — baseline for new-domain alerts."""
    with _lock:
        return _conn.execute("SELECT ip, name, MIN(first) FROM dns_daily GROUP BY ip, name").fetchall()
