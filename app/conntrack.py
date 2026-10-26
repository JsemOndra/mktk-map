"""Client connection tracker based on the router's connection tracking table.

- Every 10 s (while the /connections page is open), otherwise every 30 s,
  /ip/firewall/connection (+ IPv6) is read via the monitoring account and a persistent connection.
- A connection is matched to a client (IP -> MAC/hostname from DHCP and IPv6 neighbors),
  the destination is named from the router's DNS cache; if the client itself asked
  for a name (DNS log), its query takes precedence.
- Closed connections stay in memory for 5 min.
- Data increments are summed into hourly statistics in SQLite (top destinations).

Limitations: connections shorter than the poll interval may be missed; for FastTrack
connections RouterOS may not count bytes (marked with the fasttrack flag).
"""

import ipaddress
import logging
import threading
import time

from librouteros.query import Key

import services
import store
from i18n import t
from mikrotik import _safe, with_conn

log = logging.getLogger("mktk")

FIELDS = ("protocol", "src-address", "dst-address", "reply-src-address", "reply-dst-address",
          "src-port", "dst-port", "tcp-state", "timeout", "orig-bytes", "repl-bytes",
          "orig-packets", "repl-packets", "fasttrack", "assured", "seen-reply")


def _ip_ok(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def split_addr(addr: str | None, port_field=None) -> tuple[str | None, int | None]:
    """'1.2.3.4:443' / '[2a00::1]:443' / '2a00::1' (+ separate port) -> (ip, port)."""
    a = str(addr or "").strip()
    if not a:
        return None, None
    port = None
    if a.startswith("["):
        ip, _, p = a[1:].partition("]")
        port = p.lstrip(":") or None
    elif _ip_ok(a):
        ip = a
    else:
        ip, _, port = a.rpartition(":")
        if not _ip_ok(ip):
            ip, _, port = a.rpartition(".")
            if not _ip_ok(ip):
                return a, None
    if port_field not in (None, ""):
        port = port_field
    try:
        return ip, int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        return ip, None


def _is_local(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_link_local
    except ValueError:
        return False


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class Tracker:
    def __init__(self, get_router, get_creds, lookup, recent_names,
                 interval_active=10, interval_idle=30, keep_closed=300):
        self.get_router = get_router          # () -> device dict | None
        self.get_creds = get_creds            # () -> (user, pw)
        self.lookup = lookup                  # () -> {ip: client}
        self.recent_names = recent_names      # (client_ip) -> {name: ts}
        self.interval_active = interval_active
        self.interval_idle = interval_idle
        self.keep_closed = keep_closed
        self.conns: dict[tuple, dict] = {}
        self.lock = threading.Lock()
        self.last_view = 0.0
        self.last_poll = None
        self.last_error = None
        self.poll_ms = None
        self.rows_last = 0
        self._dns: dict[str, list[str]] = {}
        self._dns_at = 0.0
        self._stats: dict[tuple, list] = {}

    # -- public
    def touch(self):
        self.last_view = time.time()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="conntrack").start()

    def status(self) -> dict:
        return {"last_poll": self.last_poll, "last_error": self.last_error,
                "poll_ms": self.poll_ms, "rows": self.rows_last,
                "interval": self._interval(), "dns_cache": len(self._dns)}

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(c) for c in self.conns.values()]

    # -- loop
    def _interval(self) -> int:
        return self.interval_active if time.time() - self.last_view < 120 else self.interval_idle

    def _loop(self):
        time.sleep(8)
        while True:
            t0 = time.time()
            try:
                self.poll()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e) or e.__class__.__name__
                log.info("conntrack poll failed: %s", self.last_error)
            time.sleep(max(2.0, self._interval() - (time.time() - t0)))

    def _fetch(self, api) -> tuple[list, list, list | None]:
        t0 = time.time()
        try:
            return self._fetch_inner(api)
        finally:
            self.poll_ms = int((time.time() - t0) * 1000)

    def _fetch_inner(self, api) -> tuple[list, list, list | None]:
        def table(*path):
            try:
                return [dict(r) for r in api.path(*path).select(*[Key(k) for k in FIELDS])]
            except Exception:  # noqa: BLE001  (old ROS without .proplist / missing fields)
                return [dict(r) for r in _safe(api, *path)]
        v4 = table("ip", "firewall", "connection")
        v6 = table("ipv6", "firewall", "connection")
        cache = None
        if time.time() - self._dns_at > 60:
            cache = [dict(r) for r in _safe(api, "ip", "dns", "cache", "all")]
        return v4, v6, cache

    def _build_dns(self, rows: list[dict]):
        """IP -> names; for CNAME chains the original (queried) name is added too."""
        by_ip: dict[str, set] = {}
        cname_src: dict[str, set] = {}
        for r in rows:
            t = str(r.get("type") or "").upper()
            name = str(r.get("name") or "").rstrip(".").lower()
            data = str(r.get("data") or "").rstrip(".").lower()
            if not name or not data:
                continue
            if t in ("A", "AAAA"):
                by_ip.setdefault(data, set()).add(name)
            elif t == "CNAME":
                cname_src.setdefault(data, set()).add(name)
        out = {}
        for ip, names in by_ip.items():
            allnames, frontier = set(names), set(names)
            for _ in range(6):
                nxt = set()
                for n in frontier:
                    nxt |= cname_src.get(n, set())
                nxt -= allnames
                if not nxt:
                    break
                allnames |= nxt
                frontier = nxt
            # original names (top of the CNAME chain) first
            out[ip] = sorted(allnames, key=lambda n: (n in names, len(n)))
        self._dns = out
        self._dns_at = time.time()

    def name_for(self, ip: str, client_ip: str | None) -> str | None:
        names = self._dns.get(ip) or []
        if not names:
            return None
        if client_ip:
            asked = self.recent_names(client_ip) or {}
            hit = [n for n in names if n in asked]
            if hit:
                return max(hit, key=lambda n: asked[n])
        return names[0]

    def poll(self):
        router = self.get_router()
        if not router:
            raise RuntimeError(t("srv.no_router_dhcp"))
        user, pw = self.get_creds()
        v4, v6, cache = with_conn(router, user, pw, self._fetch)
        if cache is not None:
            self._build_dns(cache)
        clients = self.lookup()
        now = time.time()
        seen = set()
        stats_delta: dict[tuple, list] = {}
        with self.lock:
            first_poll = self.last_poll is None
            for fam, rows in (("v4", v4), ("v6", v6)):
                for r in rows:
                    proto = str(r.get("protocol") or "").lower()
                    sip, sport = split_addr(r.get("src-address"), r.get("src-port"))
                    dip, dport = split_addr(r.get("dst-address"), r.get("dst-port"))
                    if not sip or not dip:
                        continue
                    cl = clients.get(sip)
                    if not cl and not _is_local(sip):
                        continue             # connections from outside (port forward) and the router's own traffic
                    key = (proto, sip, sport, dip, dport)
                    seen.add(key)
                    up, down = _int(r.get("orig-bytes")), _int(r.get("repl-bytes"))
                    c = self.conns.get(key)
                    if c is None or c.get("closed_at"):
                        name = self.name_for(dip, sip) if not _is_local(dip) else \
                            ((clients.get(dip) or {}).get("hostname"))
                        c = {"key": "|".join(str(x) for x in key), "family": fam, "proto": proto,
                             "src": sip, "sport": sport, "dst": dip, "dport": dport,
                             "client_mac": (cl or {}).get("mac"),
                             "client": (cl or {}).get("hostname") or (cl or {}).get("comment")
                             or ((cl or {}).get("mac")) or sip,
                             "client_vlan": (cl or {}).get("vid"),
                             "dst_local": _is_local(dip), "dst_name": name,
                             "service": services.guess(proto, dport, name),
                             "first_seen": now, "first_seen_approx": first_poll,
                             "up": up, "down": down, "up_bps": 0.0, "down_bps": 0.0,
                             "_t": now}
                        self.conns[key] = c
                        prev_up, prev_down = up, down
                        if not first_poll:
                            prev_up = prev_down = 0     # new connection: everything is an increment
                    else:
                        prev_up, prev_down = c["up"], c["down"]
                        dt = max(1e-3, now - c["_t"])
                        c["up_bps"] = max(0, up - prev_up) * 8 / dt
                        c["down_bps"] = max(0, down - prev_down) * 8 / dt
                        c["up"], c["down"], c["_t"] = up, down, now
                        if not c.get("dst_name") and not c["dst_local"]:
                            c["dst_name"] = self.name_for(dip, sip)
                            c["service"] = services.guess(proto, dport, c["dst_name"])
                    if cl and not c.get("client_mac"):     # the client was resolved only later
                        c.update(client_mac=cl.get("mac"), client_vlan=cl.get("vid"),
                                 client=cl.get("hostname") or cl.get("comment") or cl.get("mac"))
                    c.update({
                        "last_seen": now, "tcp_state": r.get("tcp-state"),
                        "timeout": r.get("timeout"),
                        "packets": _int(r.get("orig-packets")) + _int(r.get("repl-packets")),
                        "fasttrack": bool(r.get("fasttrack")), "assured": bool(r.get("assured")),
                        "seen_reply": bool(r.get("seen-reply")),
                        "nat": split_addr(r.get("reply-dst-address"))[0] not in (None, sip),
                    })
                    du, dd = max(0, up - prev_up), max(0, down - prev_down)
                    is_new = c["first_seen"] == now and not first_poll
                    if (du or dd or is_new) and c.get("client_mac"):
                        sk = (int(now // 3600 * 3600), c["client_mac"], c["dst_name"] or dip,
                              proto, dport or 0)
                        acc = stats_delta.setdefault(sk, [c["service"], 0, 0, 0])
                        acc[1] += du
                        acc[2] += dd
                        acc[3] += 1 if is_new else 0
            for key, c in list(self.conns.items()):
                if key in seen:
                    continue
                if not c.get("closed_at"):
                    c["closed_at"] = now
                    c["up_bps"] = c["down_bps"] = 0.0
                elif now - c["closed_at"] > self.keep_closed:
                    del self.conns[key]
        self.rows_last = len(v4) + len(v6)
        self.last_poll = now
        try:
            store.add_poll_stat("conntrack", router["name"], self.poll_ms, self.rows_last)
        except Exception:  # noqa: BLE001
            pass
        if stats_delta:
            store.add_conn_stats([(h, mac, dst, proto, port, v[0], v[1], v[2], v[3])
                                  for (h, mac, dst, proto, port), v in stats_delta.items()])
