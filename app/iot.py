"""Management of the IoT whitelist on the router.

Reads (state, dropped attempts, DNS names) use the monitoring account and the persistent
connection. Writes (add/remove domain, IP, ports, learning) only with admin credentials
from the vault (same as updates), and each one is written to the audit log.

Conventions on the router (the rules must be created beforehand, see docs):
- /ip dns static type=FWD address-list=<allow_list> comment="IoT whitelist <tag>"
- /ip firewall filter "IoT -> cloud whitelist" (base ports), "IoT -> cloud <tag> tcp|udp"
- /ip firewall filter "<rule_drop>" with log-prefix IOT-DROP
- /ip firewall filter "<rule_temp>" (src-address-list=<temp_list>)
"""

import ipaddress
import logging
import re
import threading
import time
import unicodedata

from librouteros.query import Key

import store
from i18n import t
from mikrotik import _safe, close, connect_once, with_conn

log = logging.getLogger("mktk")

WL_PREFIX = "IoT whitelist"
RULE_BASE = "IoT -> cloud whitelist"
RULE_TAG_PREFIX = "IoT -> cloud "
# comments of the drop and temporary-access rules come from config (iot.rule_drop / iot.rule_temp)
LEARN_LOG_COMMENT = "mktk-learn"

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
_DROP_RE = re.compile(r"proto (\w+)[^,]*, (\d+\.\d+\.\d+\.\d+)(?::(\d+))?->(\d+\.\d+\.\d+\.\d+)(?::(\d+))?")
_DNSQ_RE = re.compile(r"query from (\d+\.\d+\.\d+\.\d+):\s*#\d+\s+(\S+?)\.?\s+(\w+)")
_SHARED = ("azure-devices.net", "azurewebsites.net", "windows.net", "cloudapp.net",
           "azureedge.net", "trafficmanager.net", "amazonaws.com", "cloudfront.net",
           "elasticbeanstalk.com", "googleapis.com", "appspot.com", "firebaseio.com",
           "herokuapp.com", "akamaized.net", "edgekey.net", "cloudflare.net",
           "digitaloceanspaces.com")


def base_domain(d: str) -> str:
    """Whitelist entry suggestion: the base domain, the full name for shared clouds."""
    d = d.lower().rstrip(".")
    for s in _SHARED:
        if d.endswith("." + s):
            return d
    p = d.split(".")
    if len(p) <= 2:
        return d
    if p[-2] in ("co", "com", "net", "org", "gov", "ac", "edu") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _is_private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_multicast or a.is_loopback or a.is_link_local
    except ValueError:
        return True


# --- reading -----------------------------------------------------------------

_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_S = 10


def _query(api, path: tuple, field: str, value: str, keys: tuple) -> list[dict]:
    """Router-side filter (?field=value); on error fall back to the full listing."""
    try:
        q = api.path(*path).select(*[Key(k) for k in keys]).where(Key(field) == value)
        return [dict(r) for r in q]
    except Exception:  # noqa: BLE001
        return [dict(r) for r in _safe(api, *path) if r.get(field) == value]


def read_state(router: dict, user: str, pw: str, cfg: dict) -> dict:
    """Whitelist state + dropped attempts (cached 10 s so the router is not overloaded)."""
    key = (router["name"], cfg["allow_list"])
    with _cache_lock:
        c = _cache.get(key)
        if c and time.time() - c["ts"] < CACHE_S:
            return c["data"]

    def fn(api):
        dns = [dict(r) for r in _safe(api, "ip", "dns", "static")]
        al = [dict(r) for r in _safe(api, "ip", "firewall", "address-list")]
        rules = [dict(r) for r in _safe(api, "ip", "firewall", "filter")]
        logging_rules = [dict(r) for r in _safe(api, "system", "logging")]
        fw_log = _query(api, ("log",), "topics", "firewall,info", ("time", "topics", "message"))
        return dns, al, rules, logging_rules, fw_log

    dns, al, rules, logging_rules, fw_log = with_conn(router, user, pw, fn)
    allow, temp = cfg["allow_list"], cfg["temp_list"]
    data = {
        "domains": [{"id": r.get(".id"), "name": r.get("name") or r.get("regexp"),
                     "match_subdomain": bool(r.get("match-subdomain")),
                     "forward_to": r.get("forward-to"), "comment": r.get("comment"),
                     "disabled": bool(r.get("disabled")), "type": r.get("type")}
                    for r in dns if r.get("address-list") == allow],
        "addresses": [{"id": r.get(".id"), "address": r.get("address"),
                       "comment": r.get("comment"), "dynamic": bool(r.get("dynamic")),
                       "timeout": r.get("timeout"), "created": r.get("creation-time")}
                      for r in al if r.get("list") == allow],
        "temp": [{"id": r.get(".id"), "address": r.get("address"), "comment": r.get("comment"),
                  "timeout": r.get("timeout")} for r in al if r.get("list") == temp],
        "rules": [{"id": r.get(".id"), "comment": r.get("comment"), "protocol": r.get("protocol"),
                   "dst_port": r.get("dst-port"), "src_address": r.get("src-address"),
                   "packets": r.get("packets"), "bytes": r.get("bytes"),
                   "disabled": bool(r.get("disabled")), "action": r.get("action"),
                   "base": r.get("comment") == RULE_BASE,
                   "drop": r.get("comment") == cfg["rule_drop"],
                   "temp": r.get("comment") == cfg["rule_temp"]}
                  for r in rules if str(r.get("comment") or "").startswith("IoT")],
        "learn_logging": any(r.get("comment") == LEARN_LOG_COMMENT for r in logging_rules),
        "drops": _parse_drops(fw_log, cfg["drop_prefix"]),
        "fetched_at": time.time(),
    }
    with _cache_lock:
        _cache[key] = {"ts": time.time(), "data": data}
    return data


def invalidate():
    with _cache_lock:
        _cache.clear()


def _parse_drops(rows: list[dict], prefix: str) -> list[dict]:
    agg: dict[tuple, dict] = {}
    for r in rows:
        msg = str(r.get("message") or "")
        if prefix not in msg:
            continue
        m = _DROP_RE.search(msg)
        if not m:
            continue
        proto, src, _sport, dst, dport = m.groups()
        k = (src, dst, dport, proto.lower())
        a = agg.setdefault(k, {"src": src, "dst": dst, "port": dport, "proto": proto.lower(),
                               "count": 0, "first": r.get("time"), "last": r.get("time")})
        a["count"] += 1
        a["last"] = r.get("time")
    return sorted(agg.values(), key=lambda a: -a["count"])


def resolve_names(router: dict, user: str, pw: str, ips: list[str]) -> dict[str, list[str]]:
    """IP -> names from the router's DNS cache (public addresses only, max 40)."""
    ips = [i for i in dict.fromkeys(ips) if not _is_private(i)][:40]
    if not ips:
        return {}

    def fn(api):
        out = {}
        for ip in ips:
            rows = _query(api, ("ip", "dns", "cache", "all"), "data", ip, ("name", "type", "data"))
            out[ip] = sorted({str(r.get("name") or "").rstrip(".") for r in rows if r.get("name")})
        return out
    try:
        return with_conn(router, user, pw, fn)
    except Exception as e:  # noqa: BLE001
        log.info("dns cache lookup failed: %s", e)
        return {}


def read_learn(router: dict, user: str, pw: str, ip: str) -> dict:
    """What the device did while learning: DNS queries (log) and outgoing connections."""
    def fn(api):
        logs = [dict(r) for r in _safe(api, "log")]
        conns = [dict(r) for r in _safe(api, "ip", "firewall", "connection")]
        return logs, conns
    logs, conns = with_conn(router, user, pw, fn)
    domains: dict[str, int] = {}
    for r in logs:
        m = _DNSQ_RE.search(str(r.get("message") or ""))
        if m and m.group(1) == ip:
            d = m.group(2).lower().rstrip(".")
            if d.endswith((".arpa", ".local")):
                continue
            domains[d] = domains.get(d, 0) + 1
    seen = {}
    for c in conns:
        src = str(c.get("src-address") or "")
        if not src.startswith(ip + ":"):
            continue
        dst = str(c.get("dst-address") or "")
        if ":" not in dst:
            continue
        dip, dport = dst.rsplit(":", 1)
        if _is_private(dip):
            continue
        seen[(c.get("protocol"), dip, dport)] = c.get("tcp-state") or c.get("connection-state") or ""
    return {"domains": domains,
            "connections": [{"proto": p, "dst": d, "port": po, "state": s}
                            for (p, d, po), s in sorted(seen.items(), key=lambda x: (x[0][1], x[0][2]))]}


# --- writes (admin) ----------------------------------------------------------

class IotError(Exception):
    pass


def _admin(router: dict, creds: tuple[str, str]):
    return connect_once(router["host"], creds[0], creds[1], timeout=15)


def _find(api, path: tuple, **match) -> list[dict]:
    return [dict(r) for r in api.path(*path)
            if all(r.get(k) == v for k, v in match.items())]


def _write(router, creds, actor, action, detail, fn):
    api = _admin(router, creds)
    try:
        res = fn(api)
        if isinstance(res, str):          # removal returns the entry name -> readable audit
            detail = res
        store.audit(actor, "iot", action, detail, True)
        return res
    except Exception as e:  # noqa: BLE001
        store.audit(actor, "iot", action, f"{detail} — " + t("srv.error_detail", error=e), False)
        raise IotError(str(e)) from e
    finally:
        close(api)
        invalidate()


def clean_tag(tag: str) -> str:
    """Tag for the router comment — ASCII only (the API connection is ASCII, RouterOS cannot do diacritics)."""
    t = unicodedata.normalize("NFKD", tag or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9 _.\-]", "", t.strip())[:40]
    return t or "manual"


def add_domain(router, creds, actor, cfg, domain, match_subdomain, tag):
    d = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(d):
        raise IotError(t("srv.iot.bad_domain", domain=repr(domain)))
    tag = clean_tag(tag)

    def fn(api):
        if _find(api, ("ip", "dns", "static"), name=d, type="FWD"):
            raise IotError(t("srv.iot.domain_exists", domain=d))
        api.path("ip", "dns", "static").add(**{
            "name": d, "type": "FWD", "forward-to": cfg["dns_forward"],
            "match-subdomain": bool(match_subdomain), "address-list": cfg["allow_list"],
            "comment": f"{WL_PREFIX} {tag}"})
        return {"ok": True}
    return _write(router, creds, actor, t("srv.audit.domain_added"),
                  f"{d}{' (+' + t('srv.iot.subdomains') + ')' if match_subdomain else ''} [{tag}]", fn)


def remove_domain(router, creds, actor, cfg, rid):
    def fn(api):
        rows = [r for r in _find(api, ("ip", "dns", "static")) if r.get(".id") == rid]
        if not rows or rows[0].get("address-list") != cfg["allow_list"]:
            raise IotError(t("srv.iot.not_found"))
        api.path("ip", "dns", "static").remove(rid)
        return rows[0].get("name")
    return _write(router, creds, actor, t("srv.audit.domain_removed"), rid, fn)


def add_address(router, creds, actor, cfg, address, tag):
    try:
        net = ipaddress.ip_network(address.strip(), strict=False)
    except ValueError:
        raise IotError(t("srv.iot.bad_address", address=repr(address))) from None
    if net.is_private:
        raise IotError(t("srv.iot.private"))
    addr = str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)
    tag = clean_tag(tag)

    def fn(api):
        api.path("ip", "firewall", "address-list").add(
            list=cfg["allow_list"], address=addr, comment=tag)
        return {"ok": True}
    return _write(router, creds, actor, t("srv.audit.ip_added"), f"{addr} [{tag}]", fn)


def remove_address(router, creds, actor, cfg, rid):
    def fn(api):
        rows = [r for r in _find(api, ("ip", "firewall", "address-list")) if r.get(".id") == rid]
        if not rows or rows[0].get("list") != cfg["allow_list"] or rows[0].get("dynamic"):
            raise IotError(t("srv.iot.not_found_static"))
        api.path("ip", "firewall", "address-list").remove(rid)
        return rows[0].get("address")
    return _write(router, creds, actor, t("srv.audit.ip_removed"), rid, fn)


def add_ports(router, creds, actor, cfg, proto, ports, src, tag):
    proto = (proto or "").lower()
    if proto not in ("tcp", "udp"):
        raise IotError(t("srv.iot.bad_proto"))
    plist = [p.strip() for p in str(ports).split(",") if p.strip()]
    if not plist or not all(re.fullmatch(r"\d{1,5}(-\d{1,5})?", p) for p in plist):
        raise IotError(t("srv.iot.bad_ports"))
    if src:
        try:
            ipaddress.ip_address(src)
        except ValueError:
            raise IotError(t("srv.iot.bad_src")) from None
    tag = clean_tag(tag)
    comment = f"{RULE_TAG_PREFIX}{tag} {proto}"

    def fn(api):
        rules = [dict(r) for r in api.path("ip", "firewall", "filter")]
        base = next((r for r in rules if r.get("comment") == RULE_BASE), None)
        drop = next((r for r in rules if r.get("comment") == cfg["rule_drop"]), None)
        if not base or not drop:
            raise IotError(t("srv.iot.rules_missing", base=RULE_BASE, drop=cfg["rule_drop"]))
        if any(r.get("comment") == comment for r in rules):
            raise IotError(t("srv.iot.rule_exists", rule=comment))
        kw = {"chain": "forward", "action": "accept", "in-interface": base.get("in-interface"),
              "out-interface": base.get("out-interface"),
              "dst-address-list": cfg["allow_list"], "protocol": proto,
              "dst-port": ",".join(plist), "comment": comment, "place-before": drop[".id"]}
        if src:
            kw["src-address"] = src
        api.path("ip", "firewall", "filter").add(**{k: v for k, v in kw.items() if v})
        return {"ok": True}
    return _write(router, creds, actor, t("srv.audit.ports_added"),
                  f"{proto} {','.join(plist)}{' ← ' + src if src else ''} [{tag}]", fn)


def remove_rule(router, creds, actor, rid):
    def fn(api):
        rows = [r for r in _find(api, ("ip", "firewall", "filter")) if r.get(".id") == rid]
        c = str(rows[0].get("comment") or "") if rows else ""
        if not rows or not c.startswith(RULE_TAG_PREFIX) or c == RULE_BASE:
            raise IotError(t("srv.iot.rule_not_removable"))
        api.path("ip", "firewall", "filter").remove(rid)
        return c
    return _write(router, creds, actor, t("srv.audit.rule_removed"), rid, fn)


# --- learning ----------------------------------------------------------------

_learn: dict[str, dict] = {}       # ip -> {until, tag, router, creds, actor}
_learn_lock = threading.Lock()


def learn_sessions() -> list[dict]:
    with _learn_lock:
        return [{"ip": ip, "until": s["until"], "tag": s["tag"], "started": s["started"]}
                for ip, s in _learn.items()]


def learn_start(router, creds, actor, cfg, ip, minutes, tag):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise IotError(t("srv.iot.bad_ip")) from None
    if ipaddress.ip_address(ip) not in ipaddress.ip_network(cfg["subnet"]):
        raise IotError(t("srv.iot.not_in_subnet", ip=ip, subnet=cfg['subnet']))
    minutes = max(5, min(60, int(minutes or 20)))
    tag = clean_tag(tag)

    def fn(api):
        rules = [dict(r) for r in api.path("ip", "firewall", "filter")]
        if not any(r.get("comment") == cfg["rule_temp"] for r in rules):
            base = next((r for r in rules if r.get("comment") == RULE_BASE), None)
            drop = next((r for r in rules if r.get("comment") == cfg["rule_drop"]), None)
            if not base or not drop:
                raise IotError(t("srv.iot.rules_missing", base=RULE_BASE, drop=cfg["rule_drop"]))
            api.path("ip", "firewall", "filter").add(**{
                "chain": "forward", "action": "accept",
                "in-interface": base.get("in-interface"), "out-interface": base.get("out-interface"),
                "src-address-list": cfg["temp_list"], "connection-state": "new",
                "log": True, "log-prefix": "IOT-TEMP", "comment": cfg["rule_temp"],
                "place-before": drop[".id"]})
        al = api.path("ip", "firewall", "address-list")
        for r in _find(api, ("ip", "firewall", "address-list"), list=cfg["temp_list"], address=ip):
            al.remove(r[".id"])
        # timeout on the router = safety net in case the app crashes meanwhile
        al.add(list=cfg["temp_list"], address=ip, timeout=f"{minutes + 5}m",
               comment=f"mktk learn {tag}")
        if not _find(api, ("system", "logging"), comment=LEARN_LOG_COMMENT):
            api.path("system", "logging").add(topics="dns", action="memory",
                                              comment=LEARN_LOG_COMMENT)
        return {"ok": True}
    _write(router, creds, actor, t("srv.audit.learn_start"), f"{ip}, {minutes} min [{tag}]", fn)
    with _learn_lock:
        _learn[ip] = {"until": time.time() + minutes * 60, "tag": tag, "started": time.time(),
                      "router": router, "creds": creds, "actor": actor, "cfg": cfg}
    threading.Thread(target=_learn_timer, args=(ip,), daemon=True).start()
    return {"ok": True, "until": time.time() + minutes * 60}


def _learn_timer(ip):
    while True:
        with _learn_lock:
            s = _learn.get(ip)
        if not s:
            return
        if time.time() >= s["until"]:
            try:
                learn_stop(ip, s["actor"] + " (" + t("srv.iot.auto_expired") + ")")
            except Exception as e:  # noqa: BLE001
                log.warning("stopping learning for %s failed: %s", ip, e)
            return
        time.sleep(5)


def learn_stop(ip, actor, router=None, creds=None, cfg=None):
    with _learn_lock:
        s = _learn.pop(ip, None)
        others = bool(_learn)
    if s:
        router, creds, cfg = s["router"], s["creds"], s["cfg"]
    if not (router and creds and cfg):
        raise IotError(t("srv.iot.not_learning"))

    def fn(api):
        al = api.path("ip", "firewall", "address-list")
        for r in _find(api, ("ip", "firewall", "address-list"), list=cfg["temp_list"], address=ip):
            al.remove(r[".id"])
        if not others:
            lg = api.path("system", "logging")
            for r in _find(api, ("system", "logging"), comment=LEARN_LOG_COMMENT):
                lg.remove(r[".id"])
        return {"ok": True}
    return _write(router, creds, actor, t("srv.audit.learn_stop"), ip, fn)


def cleanup_logging(router, creds, actor):
    """Clean up DNS logging after learning (e.g. when the app crashed during learning)."""
    def fn(api):
        lg = api.path("system", "logging")
        for r in _find(api, ("system", "logging"), comment=LEARN_LOG_COMMENT):
            lg.remove(r[".id"])
        return {"ok": True}
    return _write(router, creds, actor, t("srv.audit.dns_cleanup"), "", fn)
