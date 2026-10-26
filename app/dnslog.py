"""Client DNS queries: RouterOS sends the 'dns' log topic here via syslog (UDP).

On the router (set up by the button on /connections using admin credentials):
  /system logging action add name=mktkdns target=remote remote=<server> remote-port=5514
  /system logging add topics=dns action=mktkdns comment=mktk-dns

A line looks like e.g.: "<30>Oct 14 17:40:01 crs328 dns query from 192.168.88.40: #123 example.com. A"
Only packets from configured devices are accepted (protection against spoofing).
"""

import logging
import re
import socket
import threading
import time
from collections import deque

import store
from mikrotik import close, connect_once

log = logging.getLogger("mktk")

QUERY_RE = re.compile(r"query from ([0-9a-fA-F:.]+?):?\s+#\d+\s+(\S+?)\.?\s+([A-Za-z0-9]+)\b")
# format of the dns,packet topic (multiple lines): "--- got query from 192.168.88.40:51234:" + "question: example.com:A:IN"
GOT_RE = re.compile(r"got (query|answer) from \[?([0-9a-fA-F:.]+?)\]?:(\d+):?\s*$")
QUESTION_RE = re.compile(r"question:\s*(\S+?)\.?:([A-Za-z0-9]+):IN")
ACTION_NAME = "mktkdns"
RULE_COMMENT = "mktk-dns"
PER_CLIENT = 500
IGNORE_SUFFIX = (".arpa", ".local", ".home.arpa")


class DnsLog:
    def __init__(self, port: int, allowed_sources, lookup):
        self.port = port
        self.allowed_sources = allowed_sources   # () -> set(ip)
        self.lookup = lookup                     # () -> {ip: client}
        self.lock = threading.Lock()
        self.recent: dict[str, deque] = {}       # client ip -> deque[(ts, name, qtype)]
        self.asked: dict[str, dict] = {}         # client ip -> {name: last_ts}
        self.buf: dict[tuple, list] = {}         # (hour, ip, name, qtype) -> [mac, count, last]
        self.stats = {"packets": 0, "queries": 0, "unparsed": 0, "rejected": {},
                      "last_packet": None, "started": None, "error": None}
        self.on_query = None                     # (ip, name, client) -> None
        self.raw = deque(maxlen=300)
        self.parsed = deque(maxlen=300)

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="dnslog").start()
        threading.Thread(target=self._flusher, daemon=True, name="dnslog-flush").start()

    def _serve(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            self.stats["started"] = time.time()
            log.info("DNS syslog receiver on UDP %d", self.port)
        except OSError as e:
            self.stats["error"] = str(e)
            log.error("DNS syslog receiver cannot start: %s", e)
            return
        clients, clients_at = {}, 0.0
        pending: dict[str, str | None] = {}
        while True:
            data, (src, _port) = s.recvfrom(4096)
            self.stats["packets"] += 1
            self.stats["last_packet"] = time.time()
            allowed = self.allowed_sources()
            if allowed and src not in allowed:
                rj = self.stats["rejected"]
                rj[src] = rj.get(src, 0) + 1
                continue
            line = data.decode("utf-8", "replace").strip()
            self.raw.append(line)
            m = QUERY_RE.search(line)
            if m:
                ip, name, qtype = m.group(1), m.group(2).lower().rstrip("."), m.group(3).upper()
            else:
                g = GOT_RE.search(line)
                if g:          # start of a packet: query from a client / answer from upstream
                    pending[src] = g.group(2) if g.group(1) == "query" else None
                    continue
                q = QUESTION_RE.search(line)
                if not q or not pending.get(src):
                    if "query from" in line:
                        self.stats["unparsed"] += 1
                    continue
                ip, name, qtype = pending.pop(src), q.group(1).lower().rstrip("."), q.group(2).upper()
                if ip in self.router_ips():
                    continue   # the router's own queries
            self.stats["format"] = "query" if m else "packet"
            if not name or name.endswith(IGNORE_SUFFIX):
                continue
            if time.time() - clients_at > 30:
                clients = self.lookup()
                clients_at = time.time() if clients else 0.0   # don't cache an empty result (before the first poll)
            cl = clients.get(ip) or {}
            self.add(ip, name, qtype, cl.get("mac"))
            if self.on_query:
                try:
                    self.on_query(ip, name, cl.get("hostname") or cl.get("comment") or cl.get("mac"))
                except Exception:  # noqa: BLE001
                    log.exception("on_query")

    def router_ips(self) -> set:
        return self.allowed_sources() or set()

    def add(self, ip, name, qtype, mac, ts=None):
        ts = ts or time.time()
        self.parsed.append({"ts": ts, "ip": ip, "name": name, "type": qtype, "mac": mac})
        self.stats["queries"] += 1
        with self.lock:
            dq = self.recent.setdefault(ip, deque(maxlen=PER_CLIENT))
            if dq and dq[-1][1] == name and ts - dq[-1][0] < 2:
                # A + AAAA (and repeats) right after each other -> one row
                last = dq[-1]
                types = last[2] if qtype in last[2].split(",") else last[2] + "," + qtype
                dq[-1] = (last[0], name, types, last[3] + (1 if qtype in last[2].split(",") else 0))
            else:
                dq.append((ts, name, qtype, 1))
            self.asked.setdefault(ip, {})[name] = ts
            k = (int(ts // 3600 * 3600), ip, name, qtype)
            b = self.buf.setdefault(k, [mac, 0, ts])
            b[1] += 1
            b[2] = ts
            if mac:
                b[0] = mac

    def _flusher(self):
        while True:
            time.sleep(30)
            with self.lock:
                buf, self.buf = self.buf, {}
                cut = time.time() - 3600
                for ip in list(self.asked):          # names for matching with connections only for 1 h
                    a = self.asked[ip]
                    for n in [n for n, t in a.items() if t < cut]:
                        del a[n]
            if buf:
                try:
                    store.add_dns_stats([(h, v[0], ip, n, qt, v[1], v[2])
                                         for (h, ip, n, qt), v in buf.items()])
                except Exception:  # noqa: BLE001
                    log.exception("dns stats flush")

    def recent_names(self, ip: str) -> dict:
        with self.lock:
            return dict(self.asked.get(ip) or {})

    def recent_for(self, ips: list[str], limit: int = 300) -> list[dict]:
        out = []
        with self.lock:
            for ip in ips:
                for ts, name, qt, n in self.recent.get(ip) or []:
                    out.append({"ts": ts, "ip": ip, "name": name, "type": qt, "count": n})
        out.sort(key=lambda x: -x["ts"])
        return out[:limit]

    def debug(self, q: str = "") -> dict:
        ql = q.lower()
        raw = [r for r in self.raw if ql in r.lower()] if q else list(self.raw)
        parsed = [p for p in self.parsed if ql in (p["name"] + " " + p["ip"]).lower()] if q \
            else list(self.parsed)
        return {"raw": raw[-100:], "parsed": parsed[-100:], "status": self.status()}

    def status(self) -> dict:
        st = dict(self.stats)
        st["rejected"] = dict(st["rejected"])
        st["clients"] = len(self.recent)
        st["port"] = self.port
        st["raw_sample"] = list(self.raw)[-5:]
        return st


# --- router setup (admin) -----------------------------------------------------

def router_status(api_rows_logging, api_rows_actions) -> dict:
    act = next((a for a in api_rows_actions if a.get("name") == ACTION_NAME), None)
    rule = next((r for r in api_rows_logging if r.get("comment") == RULE_COMMENT), None)
    return {"configured": bool(act and rule and not rule.get("disabled")),
            "remote": f"{act.get('remote')}:{act.get('remote-port')}" if act else None,
            "topics": rule.get("topics") if rule else None}


def router_enable(router: dict, creds: tuple[str, str], target_ip: str, port: int):
    api = connect_once(router["host"], creds[0], creds[1], timeout=15)
    try:
        acts = api.path("system", "logging", "action")
        cur = [dict(r) for r in acts if r.get("name") == ACTION_NAME]
        if cur:
            acts.update(**{".id": cur[0][".id"], "remote": target_ip, "remote-port": port})
        else:
            acts.add(name=ACTION_NAME, target="remote", remote=target_ip,
                     **{"remote-port": port})
        rules = api.path("system", "logging")
        cur = [dict(r) for r in rules if r.get("comment") == RULE_COMMENT]
        # the whole dns topic (incl. dns,packet) — depending on the ROS version queries are in one or the other
        if cur:
            rules.update(**{".id": cur[0][".id"], "topics": "dns", "action": ACTION_NAME,
                            "disabled": False})
        else:
            rules.add(topics="dns", action=ACTION_NAME, comment=RULE_COMMENT)
    finally:
        close(api)


def router_disable(router: dict, creds: tuple[str, str]):
    api = connect_once(router["host"], creds[0], creds[1], timeout=15)
    try:
        rules = api.path("system", "logging")
        for r in [dict(x) for x in rules if x.get("comment") == RULE_COMMENT]:
            rules.remove(r[".id"])
        acts = api.path("system", "logging", "action")
        for a in [dict(x) for x in acts if x.get("name") == ACTION_NAME]:
            acts.remove(a[".id"])
    finally:
        close(api)
