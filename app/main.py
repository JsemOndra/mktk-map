"""mktk-map — MikroTik fleet & client map."""

import hmac
import ipaddress
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from netaddr import EUI
from netaddr.core import NotRegisteredError

import alerts as alerts_mod
import auth
import backups as backups_mod
import config as cfg
import conntrack
import dnslog
import store
import i18n
import iot
import topology
import updates
import wifi
from mikrotik import (_safe, capsman_debug, drop_connection, poll_all, set_pin_store,
                      step_summary, test_connection, with_conn)
from mikrotik import close as close_api
from mikrotik import connect_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mktk")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
# MikroTik monitoring account (read-only, "monitor")
MKTK_USER = os.environ.get("MIKROTIK_USER", "monitor")
MKTK_PASS = os.environ.get("MIKROTIK_PASS", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

# web UI login — independent of the MikroTiks
APP_USER = os.environ.get("APP_USER", "")
APP_PASS_HASH = os.environ.get("APP_PASS_HASH", "")
# secret added by Caddy -> the request came through the HTTPS proxy
PROXY_SECRET = os.environ.get("PROXY_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

cfg.load(CONFIG_PATH)
i18n.set_language_source(cfg.language)
t = i18n.t
DNS_SYSLOG_PORT = int(os.environ.get("DNS_SYSLOG_PORT", "5514"))
SYSLOG_TARGET = os.environ.get("SYSLOG_TARGET", "")   # server IP as seen by the router


def _app_version() -> str:
    """Version = mtime of the newest app file (yyyymmdd-hhmmss).

    Changes automatically with every new build — used to verify
    that the freshly deployed code is really running.
    """
    newest = 0.0
    base = os.path.dirname(os.path.abspath(__file__))
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(newest))


APP_VERSION = _app_version()
log.info("mktk-map version %s", APP_VERSION)


@asynccontextmanager
async def _lifespan(_app):
    _startup()      # defined below, next to the poll loop it starts
    yield


app = FastAPI(title="mktk-map", lifespan=_lifespan)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_PUBLIC_PATHS = {"/login", "/api/login", "/api/i18n", "/static/i18n.js",
                 "/static/logo.svg"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# without these the UI would be open over plain HTTP or nobody could log in;
# refuse to start unless explicitly running in development mode
DEV_MODE = os.environ.get("MKTK_DEV", "") == "1"
_missing_env = [k for k, v in (("PROXY_SECRET", PROXY_SECRET), ("APP_USER", APP_USER),
                               ("APP_PASS_HASH", APP_PASS_HASH)) if not v]
if _missing_env and not DEV_MODE:
    log.critical("%s not set - refusing to start (set MKTK_DEV=1 for development only)",
                 ", ".join(_missing_env))
    raise SystemExit(1)
if not PROXY_SECRET:
    log.warning("PROXY_SECRET is not set - the UI is reachable over plain HTTP (development only!)")


def _via_proxy(request: Request) -> bool:
    if not PROXY_SECRET:
        return True
    return hmac.compare_digest(request.headers.get("x-mktk-proxy", ""), PROXY_SECRET)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:          # same-origin fetch() POST sends Origin; non-browsers omit it
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    via_proxy = _via_proxy(request)
    request.state.via_proxy = via_proxy
    # UI and login only via the HTTPS proxy; directly on :8080 only API tokens (HA)
    if via_proxy:
        if path in _PUBLIC_PATHS:
            if request.method not in _SAFE_METHODS and not _same_origin(request):
                return JSONResponse({"detail": t("srv.cross_origin")}, status_code=403)
            return await call_next(request)
        if store.check_session(request.cookies.get("mktk_session", "")):
            if request.method not in _SAFE_METHODS and not _same_origin(request):
                return JSONResponse({"detail": t("srv.cross_origin")}, status_code=403)
            request.state.auth = "session"
            return await call_next(request)
    # API tokens: read-only (GET), never for token management
    authz = request.headers.get("authorization", "")
    bearer = authz[7:] if authz.lower().startswith("bearer ") else ""
    token = bearer or request.headers.get("x-api-token", "")
    if token and store.check_api_token(token):
        if request.method != "GET":
            return JSONResponse({"detail": t("srv.token_readonly")}, status_code=403)
        if not cfg.token_allowed(path):
            return JSONResponse({"detail": t("srv.token_path_denied")}, status_code=403)
        request.state.auth = "token"
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    if not via_proxy:
        if PUBLIC_URL:
            return RedirectResponse(PUBLIC_URL + "/", status_code=302)
        return JSONResponse({"detail": t("srv.https_only")}, status_code=403)
    return RedirectResponse("/login", status_code=302)

_state: dict = {"devices": [], "clients": [], "events": [], "updated_at": None}
_lock = threading.Lock()
_wake = threading.Event()


@lru_cache(maxsize=1024)
def _vendor(oui_prefix: str) -> str | None:
    try:
        return EUI(oui_prefix + ":00:00:00").oui.registration()["org"]
    except (NotRegisteredError, Exception):  # noqa: BLE001
        return None


def vendor(mac: str) -> str | None:
    return _vendor(mac[:8]) if mac and len(mac) >= 8 else None


def _correlate(raw: list[dict]) -> dict:
    """Merge per-device data into a fleet + client view."""
    dhcp_source = cfg.dhcp_source()
    ignore_ifaces = cfg.ignore_ifaces()
    leases: dict[str, dict] = {}
    for dev in raw:
        if dhcp_source and dev["name"] != dhcp_source:
            continue
        for l in dev.get("dhcp_leases", []):
            if l.get("mac"):
                leases[l["mac"].upper()] = l

    infra_macs = set()
    for dev in raw:
        infra_macs.update(m.upper() for m in dev.get("own_macs", []))

    # mac -> vid from ALL bridge host tables (incl. trunk sightings) so wifi
    # clients get their VLAN too
    mac_vid: dict[str, str] = {}
    for dev in raw:
        for h in dev.get("bridge_hosts", []):
            if h.get("mac") and h.get("vid") is not None:
                mac_vid.setdefault(h["mac"].upper(), h["vid"])

    clients: dict[str, dict] = {}

    def lease_fields(mac):
        l = leases.get(mac, {})
        return {
            "ip": l.get("ip"), "hostname": l.get("hostname"),
            "comment": l.get("comment"), "lease_status": l.get("status"),
            "lease_last_seen": l.get("last_seen"), "lease_expires": l.get("expires"),
            "lease_static": l.get("static"), "lease_server": l.get("server"),
        }

    # device identity -> name in config (CAP identity often == name);
    # matching ignores case and -/_ (cap_ap1 == cap-AP1)
    def _norm(s: str) -> str:
        return s.lower().replace("_", "-")

    ident_dev: dict[str, str] = {}
    for dev in raw:
        if dev.get("identity"):
            ident_dev.setdefault(_norm(dev["identity"]), dev["name"])
        ident_dev.setdefault(_norm(dev["name"]), dev["name"])

    def cap_device(dev: dict, iface: str | None) -> str:
        """CAPsMAN: registrations on the controller belong to the AP, not the controller.

        Resolution: 1) radio->identity map, 2) interface name prefix
        (master and slave, e.g. 'cap-AP1-1'), 3) fallback = controller.
        """
        radio_caps = dev.get("radio_caps") or {}
        ident = None
        if iface:
            ident = radio_caps.get(iface)
            if not ident:  # safety: only at a name boundary (cap6 != cap65)
                for k, v in radio_caps.items():
                    if iface.startswith(k + "-") or iface.startswith(k + "."):
                        ident = v
                        break
            if not ident:
                for i in sorted(dev.get("remote_caps") or [],
                                key=len, reverse=True):
                    if iface == i or iface.startswith(i + "-") \
                            or iface.startswith(i + "."):
                        ident = i
                        break
        return ident_dev.get(_norm(ident), ident) if ident else dev["name"]

    # 1) wifi clients — authoritative attribution to their AP
    for dev in raw:
        for c in dev.get("wifi_clients", []):
            mac = (c.get("mac") or "").upper()
            if not mac:
                continue
            # duplicate entry (controller + local AP) — don't drop
            # the one with a signal in favour of one without
            prev = clients.get(mac)
            if prev and prev.get("signal") is not None \
                    and c.get("signal") is None:
                continue
            clients[mac] = {
                "mac": mac, "device": cap_device(dev, c.get("interface")),
                "conn": "wifi",
                "interface": c.get("interface"), "ssid": c.get("ssid"),
                "signal": c.get("signal"), "tx_rate": c.get("tx_rate"),
                "rx_rate": c.get("rx_rate"), "link_uptime": c.get("uptime"),
                "bytes": c.get("bytes"), "vid": mac_vid.get(mac),
                "vendor": vendor(mac), **lease_fields(mac),
            }

    # 2) wired clients from bridge host tables
    for dev in raw:
        for h in dev.get("bridge_hosts", []):
            mac = (h.get("mac") or "").upper()
            if not mac or mac in clients or mac in infra_macs:
                continue
            if (dev["name"], h.get("interface")) in ignore_ifaces:
                continue
            clients[mac] = {
                "mac": mac, "device": dev["name"], "conn": "eth",
                "interface": h.get("interface"), "vid": h.get("vid"),
                "vendor": vendor(mac), **lease_fields(mac),
            }

    # 3) leases with no L2 sighting
    for mac in leases:
        if mac in clients or mac in infra_macs:
            continue
        clients[mac] = {
            "mac": mac, "device": None, "interface": None,
            "conn": "lease-only", "vendor": vendor(mac), **lease_fields(mac),
        }

    # clients without a DHCP lease (static IP) get IPv4 from ARP
    arp_by_mac: dict[str, str] = {}
    for dev in raw:
        for a in dev.get("arp", []):
            arp_by_mac.setdefault(a["mac"], a["ip"])
    for mac, c in clients.items():
        if not c.get("ip") and mac in arp_by_mac:
            c["ip"] = arp_by_mac[mac]
            c["ip_source"] = "arp"

    # IPv6 from the neighbor tables of all devices; link-local (fe80) last
    mac_v6: dict[str, set] = {}
    for dev in raw:
        for n in dev.get("ipv6_neighbors", []):
            if n.get("mac") and n.get("ip"):
                mac_v6.setdefault(n["mac"].upper(), set()).add(n["ip"])
    for mac, c in clients.items():
        c["ipv6"] = sorted(
            mac_v6.get(mac, ()),
            key=lambda a: (a.lower().startswith("fe80"), a),
        )

    fleet = []
    for dev in raw:
        d = {k: v for k, v in dev.items()
             if k not in ("dhcp_leases", "bridge_hosts", "wifi_clients",
                          "own_macs", "ipv6_neighbors", "arp")}
        d["client_count"] = sum(1 for c in clients.values() if c["device"] == dev["name"])
        fleet.append(d)

    return {
        "devices": fleet,
        "clients": sorted(
            clients.values(),
            key=lambda c: (c["device"] or "~", c.get("interface") or "",
                           c.get("hostname") or "~"),
        ),
        "arp": {a["ip"]: a["mac"] for dev in raw for a in dev.get("arp", [])},
        "updated_at": time.time(),
    }


def _send_webhook(event: dict):
    wh = cfg.webhook()
    url = wh.get("url")
    flags = {
        "new_client": wh.get("notify_new", True),
        "client_gone": wh.get("notify_gone", True),
        "client_back": wh.get("notify_back", False),
        "roaming": wh.get("notify_roaming", False),
    }
    if not url or not flags.get(event["type"]):
        return
    try:
        requests.post(url, json=event, timeout=5)
    except requests.RequestException as e:
        log.warning("webhook failed: %s", e)


_offline_since: dict[str, float] = {}


def _annotate_fleet(fleet: list[dict]):
    """Add state for the dashboard and /updates: offline since, available versions."""
    now = time.time()
    for d in fleet:
        if d.get("reachable"):
            _offline_since.pop(d["name"], None)
        else:
            _offline_since.setdefault(d["name"], now)
        d["offline_since"] = _offline_since.get(d["name"])
        u = d.get("update") or {}
        ver = updates.short_version((d.get("resource") or {}).get("version")) \
            or u.get("installed")
        lt = updates.latest_for(u.get("channel"))
        d["latest"] = {"version": lt.get("version"), "fetched_at": lt.get("fetched_at"),
                       "error": lt.get("error"), "channel": u.get("channel") or "stable"}
        d["update_available"] = bool(
            ver and lt.get("version") and updates.vkey(lt["version"]) > updates.vkey(ver))
        rb = d.get("routerboard") or {}
        d["fw_available"] = bool(rb.get("upgrade_firmware")
                                 and rb.get("firmware") != rb.get("upgrade_firmware"))


_prev_uplink: dict[str, tuple] = {}


def _record_samples(fleet: list[dict]):
    """Sample for charts: CPU, RAM, temperature, uplink traffic, clients, availability."""
    rows = []
    now = time.time()
    for d in fleet:
        row = {"device": d["name"], "up": bool(d.get("reachable")),
               "clients": d.get("client_count")}
        if d.get("reachable"):
            r = d.get("resource") or {}
            try:
                row["cpu"] = float(r.get("cpu_load"))
            except (TypeError, ValueError):
                pass
            try:
                row["mem"] = (1 - int(r["free_memory"]) / int(r["total_memory"])) * 100
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
            row["temp"] = d.get("temp")
            up = d.get("uplink") or {}
            try:
                rx, tx = int(up.get("rx")), int(up.get("tx"))
                prev = _prev_uplink.get(d["name"])
                if prev and prev[3] == up.get("name") and now > prev[0]:
                    dt = now - prev[0]
                    if rx >= prev[1] and tx >= prev[2]:
                        row["rx_bps"] = (rx - prev[1]) * 8 / dt
                        row["tx_bps"] = (tx - prev[2]) * 8 / dt
                _prev_uplink[d["name"]] = (now, rx, tx, up.get("name"))
            except (TypeError, ValueError):
                pass
        rows.append(row)
    if rows:
        store.add_device_samples(rows)
    for d in fleet:
        if d.get("reachable") and d.get("poll_ms") is not None:
            # monitor = fast part only; the slow part (every 5 min) separately so it does not skew the average
            sm = d.get("slow_ms")
            store.add_poll_stat("monitor", d["name"], round(d["poll_ms"] - (sm or 0), 1))
            if sm is not None:
                store.add_poll_stat("slow", d["name"], sm)


def _channels() -> set[str]:
    with _lock:
        return {(d.get("update") or {}).get("channel") or "stable"
                for d in _state.get("devices", [])} or {"stable"}


def _capsman_has(identity: str) -> bool | None:
    """Is the AP with the given identity connected to CAPsMAN? None = cannot tell."""
    want = (identity or "").lower()
    seen_any = False
    for dev in cfg.devices():
        if dev.get("role") == "ap":
            continue
        try:
            idents = with_conn(dev, MKTK_USER, MKTK_PASS, lambda api: [
                (r.get("identity") or r.get("name") or "").lower()
                for r in _safe(api, "caps-man", "remote-cap")
                + _safe(api, "interface", "wifi", "capsman", "remote-cap")])
        except Exception:  # noqa: BLE001
            continue
        seen_any = True
        if want in idents:
            return True
    return False if seen_any else None


def _poll_loop():
    while True:
        try:
            devices = cfg.devices()
            gone_min = int(cfg.webhook().get("gone_after_min", 10))
            raw = poll_all(devices, MKTK_USER, MKTK_PASS) if devices else []
            merged = _correlate(raw)
            merged["topology"] = topology.build(raw, {d["name"]: d["client_count"]
                                                      for d in merged["devices"]},
                                                merged["clients"])
            events = store.process(merged["clients"], gone_min)
            meta = store.client_meta_map()
            for c in merged["clients"]:
                m = meta.get(c["mac"], {})
                c["first_seen"] = m.get("first_seen")
                # last SSID from history; for live wifi clients prefer the current one
                c["last_ssid"] = c.get("ssid") or m.get("last_ssid")
            _annotate_fleet(merged["devices"])
            _record_samples(merged["devices"])
            try:
                alerts.evaluate(merged["devices"], _offline_since)
                for d in merged["devices"]:
                    if d.get("reachable") and d.get("history_top"):
                        backups.history_hook(d, d["history_top"])
            except Exception:  # noqa: BLE001
                log.exception("alerts/backups")
            with _lock:
                _state.update(merged)
                _state["events"] = store.recent_events(24)
            for e in events:
                _send_webhook(e)
        except Exception:  # noqa: BLE001
            log.exception("poll failed")
        _wake.wait(POLL_INTERVAL)
        _wake.clear()


def _startup():
    store.init()
    backups.start()
    ret = cfg.retention()
    store.STATS_RETENTION_DAYS = ret["stats_hourly_days"]
    store.DNS_DAILY_RETENTION_DAYS = ret["dns_daily_days"]
    dns.start()
    tracker.start()
    set_pin_store(store.pin_get, store.pin_set)
    n = store.jobs_mark_interrupted()
    if n:
        log.warning("%d running update jobs marked as interrupted", n)
    updates.set_hooks(wake=_wake.set, capsman_has=_capsman_has,
                      pre_change=lambda dev, reason: backups.run(dev, reason))
    updates.start_background(_channels)
    threading.Thread(target=_poll_loop, daemon=True).start()


# login brute-force protection: too many failures from one address -> locked for a while
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW_S = 15 * 60
_login_fails: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # behind Caddy the peer is the proxy; trust X-Forwarded-For only on proxied requests
    if PROXY_SECRET and request.state.via_proxy:
        fwd = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "?"


@app.post("/api/login")
def login(body: dict, request: Request, response: Response):
    ip = _client_ip(request)
    now = time.time()
    with _login_lock:
        if len(_login_fails) > 1000:  # forget stale addresses
            for k in [k for k, v in _login_fails.items() if not v or now - v[-1] > LOGIN_WINDOW_S]:
                del _login_fails[k]
        fails = [x for x in _login_fails.get(ip, []) if now - x < LOGIN_WINDOW_S]
        _login_fails[ip] = fails
        if len(fails) >= LOGIN_MAX_FAILS:
            wait = int(LOGIN_WINDOW_S - (now - fails[0])) // 60 + 1
            raise HTTPException(429, t("srv.login_locked", min=wait))
    user = body.get("username") or ""
    pw = body.get("password") or ""
    ok = bool(APP_USER and APP_PASS_HASH) \
        and hmac.compare_digest(user, APP_USER) and auth.verify(pw, APP_PASS_HASH)
    if not ok:
        with _login_lock:
            _login_fails.setdefault(ip, []).append(time.time())
            if len(_login_fails[ip]) == LOGIN_MAX_FAILS:
                log.warning("login: %d failed attempts from %s - locked for %d min",
                            LOGIN_MAX_FAILS, ip, LOGIN_WINDOW_S // 60)
        time.sleep(1)  # slow down brute force
        raise HTTPException(401, t("srv.bad_login"))
    with _login_lock:
        _login_fails.pop(ip, None)
    token = store.create_session()
    response.set_cookie(
        "mktk_session", token, max_age=30 * 86400,
        httponly=True, samesite="strict", secure=bool(PROXY_SECRET),
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    updates.vault.clear(request.cookies.get("mktk_session", ""))
    store.delete_session(request.cookies.get("mktk_session", ""))
    response.delete_cookie("mktk_session")
    return {"ok": True}


@app.get("/api/tokens")
def list_tokens():
    return store.list_api_tokens()


@app.post("/api/tokens")
def create_token(body: dict):
    label = (body.get("label") or "").strip() or t("srv.token_unnamed")
    token = store.create_api_token(label)
    return {"token": token, "label": label,
            "note": t("srv.token_note")}


@app.delete("/api/tokens/{token_id}")
def delete_token(token_id: int):
    if not store.delete_api_token(token_id):
        raise HTTPException(404, t("srv.unknown_token"))
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(_STATIC_DIR, "login.html"))


@app.get("/api/snapshot")
def snapshot():
    with _lock:
        return {**_state, "version": APP_VERSION}


@app.get("/api/presence/{mac}")
def presence(mac: str):
    p = store.presence(mac.upper(), online_window_s=POLL_INTERVAL * 3)
    if not p:
        raise HTTPException(404, "unknown mac")
    return p


@app.get("/api/clients/{mac}/detail")
def client_detail(mac: str, hours: int = 24):
    return store.client_detail(mac.upper(), hours)


@app.get("/api/events")
def events(hours: int = 24):
    return store.recent_events(hours)


@app.get("/api/tls-pins")
def tls_pins():
    by_host = {d["host"]: d["name"] for d in cfg.devices()}
    return [{**p, "device": by_host.get(p["host"])} for p in store.pin_list()]


@app.delete("/api/tls-pins/{host}")
def tls_pin_reset(host: str):
    """Forget the fingerprint — the next connection accepts (and stores) the current certificate."""
    store.pin_delete(host)
    for d in cfg.devices():
        if d["host"] == host:
            drop_connection(d["name"])
    _wake.set()
    return {"ok": True}


def _dev(name: str) -> dict:
    dev = next((d for d in cfg.devices() if d["name"] == name), None)
    if not dev:
        raise HTTPException(404, "unknown device")
    return dev


@app.get("/api/devices/{name}/capsman-debug")
def capsman_dbg(name: str):
    """Raw CAPsMAN data + computed maps — attribution diagnostics."""
    try:
        return capsman_debug(_dev(name), MKTK_USER, MKTK_PASS)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)) from e


# --- updates ----------------------------------------------------------------

def _session(request: Request) -> str:
    if getattr(request.state, "auth", None) != "session":
        raise HTTPException(403, t("srv.session_only"))
    return request.cookies.get("mktk_session", "")


@app.get("/updates")
def updates_page():
    return FileResponse(os.path.join(_STATIC_DIR, "updates.html"))


@app.get("/api/updates/state")
def updates_state(request: Request):
    sess = _session(request)
    with _lock:
        devs = [{k: d.get(k) for k in (
            "name", "host", "role", "identity", "reachable", "error", "polled_at",
            "offline_since", "resource", "routerboard", "update", "latest",
            "update_available", "fw_available")} for d in _state.get("devices", [])]
    return {
        "now": time.time(),
        "credentials": updates.vault.info(sess),
        "cred_ttl": updates.CRED_TTL,
        "job": updates.current_job(),
        "devices": devs,
        "latest": updates.latest_all(),
        "history": store.jobs_recent(30),
    }


@app.post("/api/updates/credentials")
def updates_set_credentials(body: dict, request: Request):
    sess = _session(request)
    user = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    if not user or not pw:
        raise HTTPException(400, t("srv.creds_required"))
    results = updates.verify_credentials(cfg.devices(), user, pw)
    if not any(r["ok"] for r in results):
        raise HTTPException(401, t("srv.creds_failed_all", details="; ".join(
            f"{r['device']}: {r.get('error')}" for r in results)))
    updates.vault.put(sess, user, pw)
    log.info("updates: credentials stored (%s) from %s", user, _client_ip(request))
    return {"ok": True, "results": results, "credentials": updates.vault.info(sess)}


@app.delete("/api/updates/credentials")
def updates_clear_credentials(request: Request):
    updates.vault.clear(_session(request))
    return {"ok": True}


@app.post("/api/updates/jobs")
def updates_start_job(body: dict, request: Request):
    sess = _session(request)
    creds = updates.vault.get(sess)
    if not creds:
        raise HTTPException(401, t("srv.creds_first"))
    dev = _dev(body.get("device") or "")
    action = body.get("action") or ""
    started_by = t("srv.actor", user=APP_USER, account=creds[0], ip=_client_ip(request))
    try:
        jid = updates.start_job(dev, action, creds, started_by)
    except updates.JobBusy as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "job_id": jid}


@app.post("/api/updates/refresh-latest")
def updates_refresh_latest(request: Request):
    _session(request)
    updates.refresh_latest(_channels())
    _wake.set()
    return updates.latest_all()


class WebhookCfg(BaseModel):
    url: str = ""
    notify_new: bool = True
    notify_gone: bool = True
    notify_back: bool = False
    notify_roaming: bool = False
    gone_after_min: int = 10


class DeviceCfg(BaseModel):
    name: str
    host: str
    role: str = "ap"
    ignore_interfaces: list[str] | str = []
    uplink_interface: str | None = None


@app.get("/api/config")
def get_config():
    return cfg.snapshot()


@app.put("/api/config/webhook")
def put_webhook(body: WebhookCfg):
    try:
        cfg.set_webhook(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return cfg.webhook()


@app.post("/api/config/webhook/test")
def test_webhook():
    url = cfg.webhook().get("url")
    if not url:
        raise HTTPException(400, t("srv.webhook_unset"))
    try:
        r = requests.post(url, json={"type": "test", "mac": None,
                                     "detail": {"msg": "mktk-map test"},
                                     "ts": time.time()}, timeout=5)
        return {"ok": r.ok, "status": r.status_code}
    except requests.RequestException as e:
        raise HTTPException(502, str(e)) from e


@app.put("/api/config/dhcp-source")
def put_dhcp_source(body: dict):
    try:
        cfg.set_dhcp_source(body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _wake.set()
    return {"dhcp_source": cfg.dhcp_source()}


@app.post("/api/config/devices")
def post_device(body: DeviceCfg):
    try:
        d = cfg.add_device(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _wake.set()
    return d


@app.put("/api/config/devices/{name}")
def put_device(name: str, body: DeviceCfg):
    try:
        d = cfg.update_device(name, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if d["name"] != name:
        store.rename_device_refs(name, d["name"])
        _prev_uplink.pop(name, None)
    drop_connection(name)
    _wake.set()
    return d


@app.delete("/api/config/devices/{name}")
def delete_device(name: str):
    try:
        cfg.remove_device(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    drop_connection(name)
    _wake.set()
    return {"ok": True}


@app.post("/api/config/devices/sync-names")
def sync_device_names():
    """Rename devices in the config after their identity (+ history in the DB)."""
    with _lock:
        idents = {d["name"]: d.get("identity") for d in _state.get("devices", [])
                  if d.get("reachable") and d.get("identity")}
    names = {d["name"] for d in cfg.devices()}
    done, skipped = [], []
    for dev in cfg.devices():
        new = idents.get(dev["name"])
        if not new or new == dev["name"]:
            continue
        if new in names:
            skipped.append({"from": dev["name"], "to": new, "why": t("srv.name_taken")})
            continue
        try:
            cfg.update_device(dev["name"], {**dev, "name": new})
        except ValueError as e:
            skipped.append({"from": dev["name"], "to": new, "why": str(e)})
            continue
        refs = store.rename_device_refs(dev["name"], new)
        names.discard(dev["name"])
        names.add(new)
        _prev_uplink.pop(dev["name"], None)
        drop_connection(dev["name"])
        done.append({"from": dev["name"], "to": new, "refs": refs})
        log.info("device renamed %s -> %s (%s)", dev["name"], new, refs)
    if done:
        _wake.set()
    return {"renamed": done, "skipped": skipped}


@app.get("/api/devices/{name}/history")
def device_history(name: str, hours: float = 24):
    hours = max(1.0, min(24 * 8, hours))
    return {"device": name, "hours": hours, "samples": store.device_history(name, hours),
            "polls": store.poll_series(name, hours), "poll_summary": store.poll_summary(name, 1),
            "poll_steps": step_summary(name, 1)}


@app.put("/api/config/ieee802154")
def put_ieee802154(body: dict):
    try:
        cfg.set_ieee802154(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return cfg.ieee802154()


# --- Wi-Fi overview --------------------------------------------------------

@app.get("/wifi")
def wifi_page():
    return FileResponse(os.path.join(_STATIC_DIR, "wifi.html"))


@app.get("/api/wifi")
def wifi_state():
    with _lock:
        fleet = list(_state.get("devices", []))
        clients = list(_state.get("clients", []))
        updated = _state.get("updated_at")
    return {**wifi.build(fleet, clients, cfg.ieee802154()), "updated_at": updated}


# --- IoT whitelist -----------------------------------------------------------

def _iot_router() -> dict:
    name = cfg.iot().get("router")
    if not name:
        raise HTTPException(400, t("srv.no_router_iot"))
    return _dev(name)


def _iot_creds(request: Request) -> tuple[tuple[str, str], str]:
    sess = _session(request)
    creds = updates.vault.get(sess)
    if not creds:
        raise HTTPException(401, t("srv.creds_first"))
    return creds, t("srv.actor", user=APP_USER, account=creds[0], ip=_client_ip(request))


def _iot_call(fn, *args):
    try:
        return fn(*args)
    except iot.IotError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/iot")
def iot_page():
    return FileResponse(os.path.join(_STATIC_DIR, "iot.html"))


@app.get("/api/iot/state")
def iot_state(request: Request):
    sess = _session(request)
    icfg = cfg.iot()
    router = _iot_router()
    try:
        st = iot.read_state(router, MKTK_USER, MKTK_PASS, icfg)
        err = None
    except Exception as e:  # noqa: BLE001
        st, err = None, str(e)
    net = ipaddress.ip_network(icfg["subnet"])
    with _lock:
        clients = [c for c in _state.get("clients", [])
                   if c.get("ip") and _in_net(c["ip"], net)]
    names = iot.resolve_names(router, MKTK_USER, MKTK_PASS,
                              [d["dst"] for d in (st or {}).get("drops", [])]) if st else {}
    return {"router": router["name"], "cfg": icfg, "state": st, "error": err,
            "devices": clients, "dst_names": names, "learn": iot.learn_sessions(),
            "credentials": updates.vault.info(sess),
            "audit": store.audit_recent("iot", 40), "now": time.time()}


def _in_net(ip: str, net) -> bool:
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


@app.post("/api/iot/domains")
def iot_add_domain(body: dict, request: Request):
    creds, actor = _iot_creds(request)
    return _iot_call(iot.add_domain, _iot_router(), creds, actor, cfg.iot(),
                     body.get("domain"), bool(body.get("match_subdomain")), body.get("tag"))


@app.delete("/api/iot/domains/{rid}")
def iot_remove_domain(rid: str, request: Request):
    creds, actor = _iot_creds(request)
    return {"removed": _iot_call(iot.remove_domain, _iot_router(), creds, actor, cfg.iot(), rid)}


@app.post("/api/iot/addresses")
def iot_add_address(body: dict, request: Request):
    creds, actor = _iot_creds(request)
    return _iot_call(iot.add_address, _iot_router(), creds, actor, cfg.iot(),
                     body.get("address") or "", body.get("tag"))


@app.delete("/api/iot/addresses/{rid}")
def iot_remove_address(rid: str, request: Request):
    creds, actor = _iot_creds(request)
    return {"removed": _iot_call(iot.remove_address, _iot_router(), creds, actor, cfg.iot(), rid)}


@app.post("/api/iot/ports")
def iot_add_ports(body: dict, request: Request):
    creds, actor = _iot_creds(request)
    return _iot_call(iot.add_ports, _iot_router(), creds, actor, cfg.iot(),
                     body.get("proto"), body.get("ports"), body.get("src"), body.get("tag"))


@app.delete("/api/iot/rules/{rid}")
def iot_remove_rule(rid: str, request: Request):
    creds, actor = _iot_creds(request)
    return {"removed": _iot_call(iot.remove_rule, _iot_router(), creds, actor, rid)}


@app.post("/api/iot/learn")
def iot_learn_start(body: dict, request: Request):
    creds, actor = _iot_creds(request)
    return _iot_call(iot.learn_start, _iot_router(), creds, actor, cfg.iot(),
                     body.get("ip") or "", body.get("minutes") or 20, body.get("tag"))


@app.delete("/api/iot/learn/{ip}")
def iot_learn_stop(ip: str, request: Request):
    creds, actor = _iot_creds(request)
    return _iot_call(iot.learn_stop, ip, actor, _iot_router(), creds, cfg.iot())


@app.get("/api/iot/learn/{ip}")
def iot_learn_view(ip: str, request: Request):
    _session(request)
    router = _iot_router()
    try:
        data = iot.read_learn(router, MKTK_USER, MKTK_PASS, ip)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)) from e
    names = iot.resolve_names(router, MKTK_USER, MKTK_PASS, [c["dst"] for c in data["connections"]])
    suggestions: dict[str, set] = {}
    for d in data["domains"]:
        suggestions.setdefault(iot.base_domain(d), set()).add(d)
    for c in data["connections"]:
        for n in names.get(c["dst"], []):
            suggestions.setdefault(iot.base_domain(n), set()).add(n)
    return {**data, "dst_names": names,
            "suggestions": [{"domain": k, "seen": sorted(v)} for k, v in sorted(suggestions.items())]}


@app.post("/api/iot/cleanup-logging")
def iot_cleanup_logging(request: Request):
    creds, actor = _iot_creds(request)
    return _iot_call(iot.cleanup_logging, _iot_router(), creds, actor)


# --- client connections and DNS queries ------------------------------------

_lookup_cache = {"ts": 0.0, "map": {}}


def _client_lookup() -> dict:
    """IP (v4 and v6) -> client from the snapshot; cached for 10 s."""
    if time.time() - _lookup_cache["ts"] < 10:
        return _lookup_cache["map"]
    m = {}
    with _lock:
        for c in _state.get("clients", []):
            info = {"mac": c["mac"], "hostname": c.get("hostname"), "comment": c.get("comment"),
                    "ip": c.get("ip"), "vid": c.get("vid"), "vendor": c.get("vendor"),
                    "device": c.get("device")}
            if c.get("ip"):
                m[c["ip"]] = info
            for a in c.get("ipv6") or []:
                m.setdefault(a, info)
        by_mac = {c["mac"]: c for c in _state.get("clients", [])}
        for ip, mac in (_state.get("arp") or {}).items():
            if ip in m:
                continue
            c = by_mac.get(mac)
            m[ip] = {"mac": mac, "hostname": (c or {}).get("hostname"),
                     "comment": (c or {}).get("comment"), "ip": ip, "vid": (c or {}).get("vid"),
                     "vendor": (c or {}).get("vendor") or vendor(mac), "device": (c or {}).get("device")}
    if m:                       # don't cache an empty snapshot (before the first poll)
        _lookup_cache.update(ts=time.time(), map=m)
    return m


def _router_dev() -> dict | None:
    name = cfg.iot().get("router")
    return next((d for d in cfg.devices() if d["name"] == name), None) if name else None


dns = dnslog.DnsLog(DNS_SYSLOG_PORT, lambda: {d["host"] for d in cfg.devices()}, _client_lookup)
tracker = conntrack.Tracker(_router_dev, lambda: (MKTK_USER, MKTK_PASS), _client_lookup,
                            dns.recent_names)
_dns_router_cache = {"ts": 0.0, "val": None}

alerts = alerts_mod.Alerts(cfg.alerts, lambda: cfg.webhook().get("url"))
dns.on_query = lambda ip, name, client: alerts.dns_query(ip, name, client, cfg.iot()["subnet"])


def _backup_changed(dev, info):
    secs = ", ".join(info.get("sections") or []) or "?"
    alerts.event(f"cfg:{dev['name']}:{info['id']}", "config_changed", dev["name"],
                 t("srv.backup_changed", added=info.get("added", 0),
                   removed=info.get("removed", 0), sections=secs)
                 + (f" — {info['who']}" if info.get("who") else ""),
                 {"backup_id": info["id"], "reason": info.get("reason")})


def _backup_result(dev, ok, err):
    k = f"backup:{dev['name']}"
    if ok:
        alerts.resolve(k, t("srv.backup_ok_again"))
    else:
        alerts.fire(k, "backup_failed", dev["name"], err)


backups = backups_mod.BackupManager(cfg.devices, lambda: (MKTK_USER, MKTK_PASS),
                                    _backup_changed, _backup_result)


def _dns_router_status() -> dict:
    if time.time() - _dns_router_cache["ts"] < 30 and _dns_router_cache["val"]:
        return _dns_router_cache["val"]
    router = _router_dev()
    val = {"configured": False, "error": None}
    if router:
        try:
            rules, acts = with_conn(router, MKTK_USER, MKTK_PASS, lambda api: (
                [dict(r) for r in _safe(api, "system", "logging")],
                [dict(r) for r in _safe(api, "system", "logging", "action")]))
            val = {**dnslog.router_status(rules, acts), "error": None}
        except Exception as e:  # noqa: BLE001
            val["error"] = str(e)
    _dns_router_cache.update(ts=time.time(), val=val)
    return val


def _router_cpu():
    r = _router_dev()
    if not r:
        return None
    with _lock:
        d = next((x for x in _state.get("devices", []) if x["name"] == r["name"]), None)
    return (d or {}).get("resource", {}).get("cpu_load") if d else None


@app.get("/api/connections/load")
def connections_load(hours: float = 24):
    """Router load: poll duration (monitor and conntrack) + CPU, for the chart."""
    r = _router_dev()
    if not r:
        raise HTTPException(400, t("srv.no_router"))
    hours = max(1.0, min(24 * 8, hours))
    return {"device": r["name"], "hours": hours, "polls": store.poll_series(r["name"], hours),
            "cpu": [{"ts": x["ts"], "cpu": x["cpu"], "cpu_max": x["cpu_max"]}
                    for x in store.device_history(r["name"], hours)],
            "summary_1h": store.poll_summary(r["name"], 1),
            "summary_24h": store.poll_summary(r["name"], 24),
            "steps_1h": step_summary(r["name"], 1)}


def _conn_public(c: dict) -> dict:
    return {k: v for k, v in c.items() if not k.startswith("_")}


@app.get("/connections")
def connections_page():
    return FileResponse(os.path.join(_STATIC_DIR, "connections.html"))


@app.get("/api/connections")
def connections(client: str = "", q: str = "", closed: int = 1, local: int = 1, limit: int = 1000):
    tracker.touch()
    conns = tracker.snapshot()
    total = len(conns)
    active = sum(1 for c in conns if not c.get("closed_at"))
    if client:
        cl = client.upper()
        conns = [c for c in conns if (c.get("client_mac") or "").upper() == cl or c["src"] == client]
    if not closed:
        conns = [c for c in conns if not c.get("closed_at")]
    if not local:
        conns = [c for c in conns if not c.get("dst_local")]
    if q:
        ql = q.lower()
        conns = [c for c in conns if ql in " ".join(str(c.get(k) or "") for k in (
            "client", "src", "dst", "dst_name", "service", "dport", "proto", "client_mac")).lower()]
    conns.sort(key=lambda c: (c.get("closed_at") is not None,
                              -(c.get("up_bps", 0) + c.get("down_bps", 0)), -c.get("last_seen", 0)))
    by_client: dict[str, dict] = {}
    for c in tracker.snapshot():
        if c.get("closed_at"):
            continue
        k = c.get("client_mac") or c["src"]
        b = by_client.setdefault(k, {"key": k, "client": c["client"], "ip": c["src"], "conns": 0,
                                     "up_bps": 0.0, "down_bps": 0.0})
        b["conns"] += 1
        b["up_bps"] += c.get("up_bps", 0)
        b["down_bps"] += c.get("down_bps", 0)
    return {
        "now": time.time(), "total": total, "active": active, "shown": min(len(conns), limit),
        "conns": [_conn_public(c) for c in conns[:limit]],
        "clients": sorted(by_client.values(), key=lambda b: -(b["up_bps"] + b["down_bps"])),
        "tracker": tracker.status(), "dns": dns.status(), "dns_router": _dns_router_status(),
        "router": (_router_dev() or {}).get("name"),
        "poll_summary": store.poll_summary((_router_dev() or {}).get("name") or "", 1),
        "router_cpu": _router_cpu(),
        "keep_closed": tracker.keep_closed,
    }


@app.get("/api/connections/client/{key}")
def connection_client(key: str, hours: float = 24):
    tracker.touch()
    lk = _client_lookup()
    k = key.upper()
    info = next((v for v in lk.values() if (v.get("mac") or "").upper() == k), None) or lk.get(key)
    ips = {ip for ip, v in lk.items() if info and v.get("mac") == info.get("mac")}
    if conntrack._ip_ok(key):
        ips.add(key)
    ips = sorted(ips)
    mac = (info or {}).get("mac")
    conns = [c for c in tracker.snapshot() if (mac and c.get("client_mac") == mac) or c["src"] in ips]
    conns.sort(key=lambda c: (c.get("closed_at") is not None, -c.get("last_seen", 0)))
    return {"client": info, "ips": ips, "conns": [_conn_public(c) for c in conns],
            "dns_recent": dns.recent_for(ips, 300),
            "dns_top": store.dns_top(mac, (info or {}).get("ip") or key, hours, 100),
            "conn_top": store.conn_top(mac, hours, 40) if mac else [], "hours": hours}


@app.get("/api/connections/top")
def connections_top(hours: float = 24):
    lk = _client_lookup()
    by_mac = {v["mac"]: v for v in lk.values()}
    tops = store.clients_top(hours, 25)
    for t in tops:
        v = by_mac.get(t["mac"]) or {}
        t["client"] = v.get("hostname") or v.get("comment") or t["mac"]
        t["ip"] = v.get("ip")
    return {"hours": hours, "clients": tops, "destinations": store.conn_top(None, hours, 30)}


@app.get("/dns")
def dns_page():
    return FileResponse(os.path.join(_STATIC_DIR, "dns.html"))


@app.get("/api/dns")
def dns_analysis(hours: float = 24, q: str = "", group: int = 0, limit: int = 500):
    """All DNS queries in a period: domain -> how often, which clients and how often."""
    hours = max(1.0, min(hours, cfg.retention()["dns_daily_days"] * 24))
    rows, src = store.dns_overview(hours, q.strip())
    lk = _client_lookup()
    by_mac = {v["mac"]: v for v in lk.values() if v.get("mac")}

    def who(mac, ip):
        c = (by_mac.get(mac) if mac else None) or lk.get(ip) or {}
        key = mac or c.get("mac") or ip
        return key, c.get("hostname") or c.get("comment") or key, c.get("ip") or ip

    doms: dict[str, dict] = {}
    clients: dict[str, dict] = {}
    total = 0
    for name, mac, ip, cnt, last, types in rows:
        dname = iot.base_domain(name) if group else name
        key, label, cip = who(mac, ip)
        d = doms.setdefault(dname, {"name": dname, "count": 0, "last": 0, "types": set(),
                                    "clients": {}, "names": set()})
        d["count"] += cnt
        d["last"] = max(d["last"], last or 0)
        if types:
            d["types"].update(types.split(","))
        d["names"].add(name)
        cc = d["clients"].setdefault(key, {"key": key, "client": label, "ip": cip, "count": 0})
        cc["count"] += cnt
        cl = clients.setdefault(key, {"key": key, "client": label, "ip": cip, "count": 0,
                                      "domains": set()})
        cl["count"] += cnt
        cl["domains"].add(dname)
        total += cnt
    out = sorted(doms.values(), key=lambda d: -d["count"])
    first = store.dns_first_seen([n for d in out[:limit] for n in d["names"]])
    res = []
    for d in out[:limit]:
        fs = [first[n] for n in d["names"] if n in first]
        res.append({"name": d["name"], "count": d["count"], "last": d["last"],
                    "types": sorted(d["types"]), "nclients": len(d["clients"]),
                    "clients": sorted(d["clients"].values(), key=lambda c: -c["count"]),
                    "subdomains": len(d["names"]) if group else None,
                    "first_seen": min(fs) if fs else None})
    top_clients = sorted(({**c, "domains": len(c["domains"])} for c in clients.values()),
                         key=lambda c: -c["count"])[:30]
    return {"hours": hours, "source": src, "total": total, "ndomains": len(doms),
            "nclients": len(clients), "shown": len(res), "domains": res,
            "top_clients": top_clients, "retention": cfg.retention(),
            "timeline": store.dns_hourly_counts(min(hours, 24 * 7), q.strip() or None)
            if src == "hourly" else [], "dns": dns.status(), "now": time.time()}


# --- configuration backups --------------------------------------------------

@app.get("/backups")
def backups_page():
    return FileResponse(os.path.join(_STATIC_DIR, "backups.html"))


@app.get("/api/backups")
def backups_state(device: str = ""):
    devs = []
    for d in cfg.devices():
        last = store.backup_last(d["name"])
        devs.append({"name": d["name"], "host": d["host"], "last": last,
                     "status": backups.status.get(d["name"]),
                     "ssh_pin": store.pin_get("ssh:" + d["host"]),
                     "queued": d["name"] in backups.pending})
    return {"devices": devs, "list": store.backup_list(device or None, 200),
            "every_h": backups_mod.EXPORT_EVERY / 3600, "now": time.time()}


@app.get("/api/backups/{bid}/diff")
def backup_diff(bid: int, against: int | None = None):
    cur = store.backup_get(bid)
    if not cur:
        raise HTTPException(404, t("srv.backup_missing"))
    other = against or store.backup_prev(bid)
    a = store.backup_text(other) if other else ""
    b = store.backup_text(bid)
    ometa = store.backup_get(other) if other else None
    d = backups_mod.diff(a, b, f"#{other}" if other else "(none)", f"#{bid}")
    return {"backup": cur, "against": ometa, **d}


@app.get("/api/backups/{bid}/download")
def backup_download(bid: int):
    meta = store.backup_get(bid)
    if not meta:
        raise HTTPException(404, t("srv.backup_missing"))
    fn = f"{meta['device']}_{time.strftime('%Y%m%d-%H%M', time.localtime(meta['ts']))}.rsc"
    return Response(store.backup_text(bid), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/backups/run")
def backups_run(body: dict, request: Request):
    _session(request)
    names = [body["device"]] if body.get("device") else [d["name"] for d in cfg.devices()]
    for n in names:
        backups.request(n, "manual")
    return {"queued": names}


@app.post("/api/backups/enable-ssh")
def backups_enable_ssh(request: Request):
    """Add the ssh policy to the monitor group on all devices (stays read-only)."""
    creds, actor = _iot_creds(request)
    out = []
    for d in cfg.devices():
        try:
            api = connect_once(d["host"], creds[0], creds[1], timeout=10)
        except Exception as e:  # noqa: BLE001
            out.append({"device": d["name"], "ok": False, "error": str(e)})
            continue
        try:
            grp = [dict(r) for r in api.path("user", "group") if r.get("name") == "monitor"]
            if not grp:
                raise RuntimeError(t("srv.no_monitor_group"))
            pol = [p for p in str(grp[0].get("policy") or "").split(",") if p and p not in ("!ssh", "ssh")]
            pol.append("ssh")
            api.path("user", "group").update(**{".id": grp[0][".id"], "policy": ",".join(pol)})
            out.append({"device": d["name"], "ok": True})
        except Exception as e:  # noqa: BLE001
            out.append({"device": d["name"], "ok": False, "error": str(e)})
        finally:
            close_api(api)
    store.audit(actor, "backups", t("srv.audit.ssh_monitor"), ", ".join(
        f"{o['device']}:{'ok' if o['ok'] else o.get('error')}" for o in out),
        all(o["ok"] for o in out))
    for d in cfg.devices():
        backups.request(d["name"], "ssh_enabled")
    return {"results": out}


# --- alerts ------------------------------------------------------------------

@app.get("/api/alerts")
def alerts_state(hours: float = 24):
    return {"active": alerts.active_list(), "recent": store.alerts_recent(hours, 200),
            "types": {k: {"title": alerts_mod.type_title(k), "severity": v} for k, v in alerts_mod.TYPES.items()},
            "config": cfg.alerts(), "webhook": bool(cfg.webhook().get("url"))}


@app.put("/api/config/alerts")
def put_alerts(body: dict):
    try:
        cfg.set_alerts(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return cfg.alerts()


@app.post("/api/alerts/test")
def alerts_test():
    alerts.event(f"test:{time.time()}", "config_changed", "mktk-map",
                 t("srv.alert_test"), {"test": True}, severity="info")
    return {"ok": True}


@app.get("/api/connections/dns-debug")
def connections_dns_debug(q: str = ""):
    """Last received syslog lines and recognised queries (mapped to clients)."""
    d = dns.debug(q)
    lk = _client_lookup()
    for p in d["parsed"]:
        c = lk.get(p["ip"]) or {}
        p["client"] = c.get("hostname") or c.get("comment") or c.get("mac")
    return d


@app.post("/api/connections/dns-enable")
def connections_dns_enable(request: Request):
    creds, actor = _iot_creds(request)
    router = _router_dev()
    if not router:
        raise HTTPException(400, t("srv.no_router_dhcp"))
    target = SYSLOG_TARGET or (PUBLIC_URL.split("//")[-1].split(":")[0] if PUBLIC_URL else "")
    if not target:
        raise HTTPException(400, t("srv.syslog_target"))
    try:
        dnslog.router_enable(router, creds, target, DNS_SYSLOG_PORT)
        store.audit(actor, "dns", t("srv.audit.dns_on"), f"{router['name']} → {target}:{DNS_SYSLOG_PORT}")
    except Exception as e:  # noqa: BLE001
        store.audit(actor, "dns", t("srv.audit.dns_on"), t("srv.error_detail", error=e), False)
        raise HTTPException(502, str(e)) from e
    _dns_router_cache["ts"] = 0
    return {"ok": True, "target": f"{target}:{DNS_SYSLOG_PORT}"}


@app.delete("/api/connections/dns-enable")
def connections_dns_disable(request: Request):
    creds, actor = _iot_creds(request)
    router = _router_dev()
    try:
        dnslog.router_disable(router, creds)
        store.audit(actor, "dns", t("srv.audit.dns_off"), router["name"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)) from e
    _dns_router_cache["ts"] = 0
    return {"ok": True}


@app.post("/api/config/devices/test")
def test_device(body: dict):
    host = (body.get("host") or "").strip()
    if not host:
        raise HTTPException(400, t("srv.host_required"))
    return test_connection(host, MKTK_USER, MKTK_PASS)


# --- language and topology ---------------------------------------------------

@app.get("/api/i18n")
def i18n_messages():
    lang = i18n.current()
    return {"lang": lang, "languages": i18n.languages(), "messages": i18n.messages(lang)}


@app.put("/api/config/language")
def put_language(body: dict):
    try:
        cfg.set_language(body.get("language") or "")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"language": cfg.language()}


@app.get("/topology")
def topology_page():
    return FileResponse(os.path.join(_STATIC_DIR, "topology.html"))


@app.get("/api/topology")
def topology_state():
    with _lock:
        topo = _state.get("topology") or {"nodes": [], "edges": [], "root": None,
                                           "has_neighbors": False}
        clients = [{k: c.get(k) for k in ("mac", "device", "conn", "interface", "ssid",
                                          "signal", "ip", "hostname", "comment", "vendor")}
                   for c in _state.get("clients", []) if c.get("device")]
        return {**topo, "clients": clients, "updated_at": _state.get("updated_at")}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
