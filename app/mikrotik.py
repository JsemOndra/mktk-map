"""MikroTik API polling — collects device info, clients and DHCP leases.

Connections use api-ssl (8729). Monitoring keeps one persistent connection per
device (pool below) — no login every 30 s. Certificates are self-signed, so
instead of a CA the fingerprint (SHA-256 DER) is verified: it is stored on the
first connection (TOFU) and a change is rejected until you confirm it in the UI.
"""

import hashlib
import logging
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from i18n import t
from librouteros import connect
from librouteros.exceptions import MultiTrapError, TrapError
from librouteros.query import Key

log = logging.getLogger("mktk")

API_PORT = 8729          # api-ssl
API_TIMEOUT = 8
BACKOFF_MAX = 300

# --- TLS pinning --------------------------------------------------------

_pin_get = lambda host: None        # noqa: E731  (set by main via set_pin_store)
_pin_set = lambda host, fp: None    # noqa: E731


def set_pin_store(get, put):
    global _pin_get, _pin_set
    _pin_get, _pin_set = get, put


class PinMismatch(Exception):
    """The device certificate differs from the stored fingerprint."""


def _ssl_wrapper(host: str):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # self-signed -> we verify the fingerprint ourselves
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def wrap(sock):
        s = ctx.wrap_socket(sock)
        der = s.getpeercert(binary_form=True)
        if not der:
            s.close()
            raise PinMismatch(t("srv.tls.no_cert", host=host))
        fp = hashlib.sha256(der).hexdigest()
        known = _pin_get(host)
        if known is None:
            _pin_set(host, fp)
            log.warning("TLS: first connection to %s, stored fingerprint %s", host, fp)
        elif known != fp:
            s.close()
            raise PinMismatch(t("srv.tls.changed", host=host, known=known[:16], current=fp[:16]))
        return s
    return wrap


def _connect(host: str, user: str, password: str, timeout: float = API_TIMEOUT):
    return connect(
        host=host,
        username=user,
        password=password,
        port=API_PORT,
        timeout=timeout,
        ssl_wrapper=_ssl_wrapper(host),
        # latin-1: reading never fails on a non-ASCII comment from the router
        encoding="latin-1",
    )


def connect_once(host: str, user: str, password: str, timeout: float = 60):
    """One-off connection (outside the pool) — for actions with another account, e.g. updates."""
    return _connect(host, user, password, timeout)


def close(api):
    _close(api)


def _close(api):
    try:
        api.close()
    except Exception:  # noqa: BLE001
        pass


# --- persistent connections ---------------------------------------------

class _Conn:
    def __init__(self):
        self.lock = threading.Lock()
        self.api = None
        self.key = None
        self.fails = 0
        self.next_try = 0.0
        self.last_error = None
        self.connected_at = None


_pool: dict[str, _Conn] = {}
_pool_lock = threading.Lock()


def _slot(name: str) -> _Conn:
    with _pool_lock:
        return _pool.setdefault(name, _Conn())


def drop_connection(name: str | None = None):
    """Close connections (one or all) and reset the backoff — the next poll reconnects."""
    invalidate_slow(name)
    with _pool_lock:
        names = [name] if name else list(_pool)
    for n in names:
        c = _slot(n)
        with c.lock:
            if c.api is not None:
                _close(c.api)
            c.api, c.key, c.fails, c.next_try, c.connected_at = None, None, 0, 0.0, None


def with_conn(dev: dict, user: str, password: str, fn):
    """Call fn(api) over the persistent connection to the device.

    The connection is opened on first use and closed on error. If a reused
    (possibly dead) connection fails, one fresh attempt is made. A failed
    connect enables a backoff 5 s -> 10 s -> ... -> 5 min so the router
    is not flooded with failed logins.
    """
    c = _slot(dev["name"])
    key = (dev["host"], user, password)
    with c.lock:
        if c.api is not None and c.key != key:
            _close(c.api)
            c.api = None
        for attempt in (1, 2):
            fresh = False
            if c.api is None:
                now = time.time()
                if now < c.next_try:
                    raise ConnectionError(
                        t("srv.conn.retry", s=int(c.next_try - now), error=c.last_error))
                try:
                    c.api = _connect(dev["host"], user, password)
                except Exception as e:  # noqa: BLE001
                    c.fails += 1
                    wait = BACKOFF_MAX if isinstance(e, PinMismatch) \
                        else min(BACKOFF_MAX, 5 * 2 ** (c.fails - 1))
                    c.next_try = now + wait
                    c.last_error = str(e) or e.__class__.__name__
                    raise
                c.key, c.fails, c.connected_at, c.last_error = key, 0, now, None
                fresh = True
            try:
                return fn(c.api)
            except (TrapError, MultiTrapError):
                raise                      # command error, the connection is fine
            except Exception:  # noqa: BLE001
                _close(c.api)
                c.api, c.connected_at = None, None
                if fresh or attempt == 2:
                    raise
                log.info("%s: connection dropped, retrying", dev["name"])


def connection_info(name: str) -> dict:
    c = _slot(name)
    return {"connected_since": c.connected_at, "fails": c.fails,
            "next_try": c.next_try or None, "last_error": c.last_error}


_PERM_MARKERS = ("not enough permissions", "permission")


def _safe(api, *path, warn: list | None = None):
    """Read a path, return [] if the command fails (missing package, old ROS, ...).

    Connection errors are NOT swallowed — they fall through to with_conn, which
    closes the connection. Missing permissions are added to `warn` so they show in the UI.
    """
    try:
        return list(api.path(*path))
    except (TrapError, MultiTrapError) as e:
        msg = str(e)
        if warn is not None and any(m in msg.lower() for m in _PERM_MARKERS):
            warn.append(f"/{'/'.join(path)}: {msg}")
        log.debug("path %s failed: %s", "/".join(path), msg)
        return []


def _capsman_maps(api) -> tuple[dict, dict]:
    """Maps for assigning CAPsMAN registrations to APs.

    Returns (radio_caps, masters):
    - radio_caps: interface on the controller -> CAP identity
    - masters: slave interface -> master interface
    A master is recognised in two ways (interface name from /caps-man/radio
    and radio-mac between /caps-man/interface and /caps-man/radio); a slave
    (virtual AP) inherits the identity via master-interface.
    """
    radio_caps: dict[str, str] = {}
    mac_ident: dict[str, str] = {}

    for r in _safe(api, "caps-man", "radio"):
        ident = (r.get("remote-ap-ident")
                 or r.get("remote-cap-identity")
                 or r.get("remote-cap-name"))
        if not ident:
            continue
        if r.get("interface"):
            radio_caps[r["interface"]] = ident
        rm = str(r.get("radio-mac") or "").upper()
        if rm and rm != "00:00:00:00:00:00":
            mac_ident[rm] = ident

    for r in _safe(api, "interface", "wifi", "radio"):
        ident = str(r.get("cap") or "").split("@")[0]  # "identity@address"
        if not ident:
            continue
        if r.get("interface"):
            radio_caps[r["interface"]] = ident
        rm = str(r.get("radio-mac") or "").upper()
        if rm and rm != "00:00:00:00:00:00":
            mac_ident[rm] = ident

    masters: dict[str, str] = {}
    iface_rows = list(_safe(api, "caps-man", "interface")) \
        + list(_safe(api, "interface", "wifi"))
    for i in iface_rows:
        name = i.get("name")
        if not name:
            continue
        m = i.get("master-interface")
        if m and str(m).lower() not in ("none", "false", ""):
            masters[name] = str(m)
        # second way: master via radio-mac (independent of names)
        rm = str(i.get("radio-mac") or "").upper()
        if rm in mac_ident:
            radio_caps.setdefault(name, mac_ident[rm])

    # a slave inherits the master's identity (2 levels to be safe)
    for name, m in masters.items():
        if name not in radio_caps:
            ident = radio_caps.get(m) \
                or radio_caps.get(masters.get(m, ""))
            if ident:
                radio_caps[name] = ident
    return radio_caps, masters


def capsman_debug(dev: dict, user: str, password: str) -> dict:
    """Raw CAPsMAN data from the API + computed maps — for diagnostics."""
    return with_conn(dev, user, password, _capsman_debug)


def _capsman_debug(api) -> dict:
    def rows(*path):
        return [dict(r) for r in _safe(api, *path)]

    radio_caps, masters = _capsman_maps(api)
    reg_ifaces = sorted(
        {e.get("interface")
         for e in _safe(api, "caps-man", "registration-table")}
        | {e.get("interface")
           for e in _safe(api, "interface", "wifi",
                          "registration-table")}
        - {None}
    )
    return {
        "caps_man_radio": rows("caps-man", "radio"),
        "caps_man_interface": [
            {k: i.get(k) for k in
             ("name", "master-interface", "radio-mac", "bound",
              "inactive", "dynamic", "configuration.ssid")}
            for i in _safe(api, "caps-man", "interface")
        ],
        "caps_man_remote_cap": rows("caps-man", "remote-cap"),
        "wifi_radio": rows("interface", "wifi", "radio"),
        "wifi_interfaces": [
            {k: i.get(k) for k in
             ("name", "master-interface", "radio-mac", "bound",
              "inactive", "dynamic", "configuration.ssid")}
            for i in _safe(api, "interface", "wifi")
        ],
        "wifi_remote_cap": rows("interface", "wifi", "capsman",
                                "remote-cap"),
        "registration_interfaces": reg_ifaces,
        "computed_radio_caps": radio_caps,
        "computed_masters": masters,
    }


def poll_device(dev: dict, user: str, password: str) -> dict:
    """Poll one device. Returns a snapshot dict, never raises."""
    out = {
        "name": dev["name"],
        "host": dev["host"],
        "role": dev.get("role", "ap"),
        "reachable": False,
        "error": None,
        "polled_at": time.time(),
    }
    def run(api):
        t0 = time.time()          # measured inside — without waiting for the connection lock
        d = _collect(api, dev)
        d["poll_ms"] = round((time.time() - t0) * 1000, 1)
        return d
    try:
        data = with_conn(dev, user, password, run)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e) or e.__class__.__name__
        out["error_kind"] = "tls_pin" if isinstance(e, PinMismatch) else "conn"
        out["conn"] = connection_info(dev["name"])
        return out
    out.update(data)
    out["reachable"] = True
    out["conn"] = connection_info(dev["name"])
    return out


def _num(v):
    try:
        return float(str(v).split()[0].rstrip("C%"))
    except (TypeError, ValueError, IndexError):
        return None


def _uplink(ifaces: list, configured: str | None) -> dict | None:
    """Uplink traffic counters: the one set in config, else PPPoE (router), else ether1."""
    by_name = {i.get("name"): i for i in ifaces if i.get("name")}
    pick = by_name.get(configured) if configured else None
    if pick is None:
        pick = next((i for i in ifaces if i.get("type") == "pppoe-out"
                     and i.get("running")), None)
    if pick is None:
        pick = by_name.get("ether1")
    if pick is None:
        return None
    return {"name": pick.get("name"), "rx": pick.get("rx-byte"), "tx": pick.get("tx-byte"),
            "type": pick.get("type"), "running": pick.get("running"),
            "link_downs": pick.get("link-downs")}


def _temperature(rows: list) -> float | None:
    vals: dict[str, float | None] = {}
    for h in rows:
        if "name" in h:                      # v7: name/value rows
            vals[str(h["name"])] = _num(h.get("value"))
        else:                                # v6: one row with fields
            for k, v in h.items():
                vals[k] = _num(v)
    for k in ("cpu-temperature", "temperature", "board-temperature1",
              "switch-temperature", "sfp-temperature"):
        if vals.get(k) is not None:
            return vals[k]
    return None


def _monitor(api, *path, name: str) -> dict:
    try:
        rows = list(api.path(*path)("monitor", numbers=name, once=True))
        return dict(rows[0]) if rows else {}
    except (TrapError, MultiTrapError):
        return {}


def _local_radios(api, wl_rows: list, wifi_rows: list) -> list[dict]:
    """Radios on the device itself (including those managed by CAPsMAN) + monitor (noise, CCQ)."""
    out = []
    for w in wl_rows:
        if not w.get("name") or w.get("disabled") or w.get("interface-type") == "virtual":
            continue
        if w.get("master-interface"):
            continue
        m = _monitor(api, "interface", "wireless", name=w["name"]) if w.get("running") else {}
        out.append({
            "name": w["name"], "pkg": "wireless", "band": w.get("band"),
            "mac": str(w.get("mac-address") or "").upper() or None,
            "frequency": m.get("frequency") or w.get("frequency"),
            "channel": m.get("channel"),
            "width": w.get("channel-width"),
            "running": bool(w.get("running")),
            "noise_floor": _num(m.get("noise-floor")),
            "ccq": _num(m.get("overall-tx-ccq")),
            "clients": m.get("registered-clients"),
            "status": m.get("status"),
        })
    for w in wifi_rows:
        if not w.get("name") or w.get("disabled") or w.get("master-interface"):
            continue
        m = _monitor(api, "interface", "wifi", name=w["name"]) if w.get("running") else {}
        out.append({
            "name": w["name"], "pkg": "wifi", "mac": str(w.get("mac-address") or "").upper() or None,
            "band": w.get("configuration.band")
            or w.get("channel.band"),
            "frequency": None, "channel": m.get("channel"),
            "width": w.get("channel.width"),
            "running": bool(w.get("running")),
            "noise_floor": _num(m.get("noise-floor")), "ccq": None,
            "clients": m.get("registered-peers"), "status": m.get("state"),
        })
    return out


def _capsman_radios(api) -> list[dict]:
    """Radio state as seen by the CAPsMAN controller (current channel, power, client count)."""
    out = []
    for i in _safe(api, "caps-man", "interface"):
        if not i.get("name") or i.get("disabled"):
            continue
        m = str(i.get("master-interface") or "")
        out.append({
            "name": i["name"], "master": None if m.lower() in ("", "none") else m,
            "configuration": i.get("configuration"),
            "current_channel": i.get("current-channel"),
            "state": i.get("current-state"),
            "clients": i.get("current-registered-clients"),
            "ssid": i.get("configuration.ssid"),
            "radio_mac": str(i.get("radio-mac") or "").upper() or None,
            "inactive": bool(i.get("inactive")),
        })
    return out


SLOW_INTERVAL = 300      # s — things that rarely change (version, firmware, temperature, radios, CAPsMAN map)
_slow: dict[str, dict] = {}
_slow_lock = threading.Lock()


def invalidate_slow(name: str | None = None):
    """Drop the slow-data cache (after an update, config change...) — the next poll reads everything."""
    with _slow_lock:
        if name:
            _slow.pop(name, None)
            _missing.pop(name, None)
            _mid.pop(name, None)
        else:
            _slow.clear()
            _missing.clear()
            _mid.clear()


def _collect_slow(api, dev: dict) -> dict:
    warn: list[str] = []
    out: dict = {"_warn": warn}
    ident = _safe(api, "system", "identity")
    out["identity"] = ident[0].get("name") if ident else dev["name"]

    rb = _safe(api, "system", "routerboard", warn=warn)
    if rb:
        out["routerboard"] = {
            "model": rb[0].get("model"),
            "firmware": rb[0].get("current-firmware"),
            "upgrade_firmware": rb[0].get("upgrade-firmware"),
        }

    upd = _safe(api, "system", "package", "update", warn=warn)
    if upd:
        u = upd[0]
        out["update"] = {
            "channel": u.get("channel"),
            "installed": u.get("installed-version"),
            "latest": u.get("latest-version"),
            "status": u.get("status"),
        }
    out["temp"] = _temperature(_safe(api, "system", "health"))

    # interface -> SSID mapping (legacy wireless + v7 wifi) + radio state
    ssid_map = {}
    wl_rows = _safe(api, "interface", "wireless")
    wifi_rows = _safe(api, "interface", "wifi")
    for w in wl_rows:
        if w.get("name"):
            ssid_map[w["name"]] = w.get("ssid")
    for w in wifi_rows:
        if w.get("name"):
            ssid_map[w["name"]] = (
                w.get("configuration.ssid") or w.get("ssid")
            )
    out["_ssid_map"] = ssid_map
    out["local_radios"] = _local_radios(api, wl_rows, wifi_rows)
    out["capsman_radios"] = _capsman_radios(api)

    # CAPsMAN: map cap interface -> CAP identity so that registrations
    # on the controller can be assigned to the right AP
    radio_caps, _ = _capsman_maps(api)
    out["radio_caps"] = radio_caps

    # identities of all connected CAPs (fallback via the interface name
    # prefix — v7 provisioning names them "%identity%-N" by default)
    remote_caps = set()
    for rc in _safe(api, "caps-man", "remote-cap"):
        ident = rc.get("identity") or rc.get("ident") or rc.get("name")
        if ident:
            remote_caps.add(ident)
    for rc in _safe(api, "interface", "wifi", "capsman", "remote-cap"):
        if rc.get("identity"):
            remote_caps.add(rc["identity"])
    out["remote_caps"] = sorted(remote_caps)

    # neighbours from MNDP/LLDP/CDP — the topology map is built from them
    out["neighbors"] = [
        {"interface": n.get("interface"), "identity": n.get("identity"),
         "mac": str(n.get("mac-address") or "").upper() or None, "address": n.get("address"),
         "remote_interface": n.get("interface-name"), "platform": n.get("platform"),
         "board": n.get("board"), "version": n.get("version")}
        for n in _safe(api, "ip", "neighbor") if n.get("identity") or n.get("mac-address")]

    # last configuration change (for backups: a new entry = someone changed something)
    hist = _safe(api, "system", "history")
    if hist:
        f, l = hist[0], hist[-1]
        out["history_top"] = {
            "sig": f"{len(hist)}|{f.get('.id')}|{l.get('.id')}",
            # listing order differs between versions -> both end entries
            "changes": [{"action": x.get("action"), "by": x.get("by"), "time": x.get("time")}
                        for x in (f, l) if x.get("action")]}
    return out


_IF_KEYS = ("name", "type", "mac-address", "rx-byte", "tx-byte", "running", "link-downs")


def _interfaces(api) -> list:
    try:
        return [dict(r) for r in api.path("interface").select(*[Key(k) for k in _IF_KEYS])]
    except Exception:  # noqa: BLE001
        return _safe(api, "interface")


# proplist for the fast poll — the router sends only these columns (less work for CPU and TLS)
_FAST_KEYS = {
    ("system", "resource"): ("uptime", "version", "board-name", "architecture-name", "cpu-load",
                             "free-memory", "total-memory", "free-hdd-space", "total-hdd-space"),
    ("interface", "wireless", "registration-table"): ("mac-address", "interface", "signal-strength",
                                                      "tx-rate", "rx-rate", "uptime", "bytes"),
    ("interface", "wifi", "registration-table"): ("mac-address", "interface", "ssid", "signal",
                                                  "tx-rate", "rx-rate", "uptime", "bytes"),
    ("caps-man", "registration-table"): ("mac-address", "interface", "ssid", "rx-signal",
                                         "tx-rate", "rx-rate", "uptime", "bytes"),
    ("interface", "bridge", "host"): ("mac-address", "on-interface", "vid", "local"),
    ("ip", "dhcp-server", "lease"): ("mac-address", "active-address", "address", "host-name", "comment",
                                     "server", "status", "last-seen", "expires-after", "dynamic"),
    ("ipv6", "neighbor"): ("mac-address", "address", "status"),
    ("ip", "arp"): ("address", "mac-address", "invalid"),
}
_MISSING_MARKERS = ("no such command", "no such item")
_missing: dict[str, dict] = {}          # device -> {path: ts} — command does not exist (package missing)
_STEP_KEEP = 120
_steps: dict[str, list] = {}            # device -> recent poll breakdowns [(ts, {step: ms}, slow)]


def _fast(api, dev: dict, steps: dict, label: str, *path, warn: list | None = None) -> list:
    """Read with a proplist, timing, and skipping commands the device does not have."""
    miss = _missing.setdefault(dev["name"], {})
    if path in miss and time.time() - miss[path] < SLOW_INTERVAL:
        return []
    t0 = time.perf_counter()
    try:
        keys = _FAST_KEYS.get(path)
        q = api.path(*path)
        rows = [dict(r) for r in (q.select(*[Key(k) for k in keys]) if keys else q)]
    except (TrapError, MultiTrapError) as e:
        msg = str(e)
        if any(m in msg.lower() for m in _MISSING_MARKERS):
            miss[path] = time.time()
        elif warn is not None and any(m in msg.lower() for m in _PERM_MARKERS):
            warn.append(f"/{'/'.join(path)}: {msg}")
        log.debug("path %s failed: %s", "/".join(path), msg)
        rows = []
    steps[label] = steps.get(label, 0) + round((time.perf_counter() - t0) * 1000, 1)
    return rows


# steps that don't change every poll: read once per N s, from the cache in between
_EVERY = {"bridge-host": 120, "interface-all": 60}
_mid: dict[str, dict] = {}              # device -> {step: (ts, rows)}


def _cached(dev: dict, label: str, fn):
    c = _mid.setdefault(dev["name"], {})
    hit = c.get(label)
    if hit and time.time() - hit[0] < _EVERY[label]:
        return hit[1]
    rows = fn()
    c[label] = (time.time(), rows)
    return rows


def step_summary(name: str, hours: float = 1) -> dict:
    """Average duration of the individual fast-poll queries (from memory, last ~1 h)."""
    since = time.time() - hours * 3600
    rows = [s for ts, s, _slow_run in _steps.get(name, []) if ts >= since]
    if not rows:
        return {}
    keys = {k for r in rows for k in r}
    # average only over polls where the step really ran (cached steps run only occasionally)
    out = {k: round(sum(r[k] for r in rows if k in r) / sum(1 for r in rows if k in r), 1) for k in keys}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _collect(api, dev: dict) -> dict:
    """Data from one device (runs inside with_conn).

    Fast part (every poll): CPU/RAM, interface counters, Wi-Fi registrations,
    bridge hosts, DHCP, IPv6 neighbours, ARP. Slow part (every 5 min): see _collect_slow.
    Bridge hosts every 2 min, the full interface list once a minute (otherwise uplink counters only).
    Every query is timed (poll_steps); commands the device does not have (wireless package
    on the router etc.) are skipped for 5 min after the first error.
    """
    now = time.time()
    steps: dict[str, float] = {}
    slow_ms = None
    with _slow_lock:
        sl = _slow.get(dev["name"])
    if not sl or now - sl["ts"] > SLOW_INTERVAL:
        t0 = time.perf_counter()
        sl = {"ts": now, "data": _collect_slow(api, dev)}
        slow_ms = round((time.perf_counter() - t0) * 1000, 1)
        with _slow_lock:
            _slow[dev["name"]] = sl
    slow = sl["data"]
    out: dict = {k: v for k, v in slow.items() if not k.startswith("_")}
    out["slow_age"] = round(now - sl["ts"])
    out["slow_ms"] = slow_ms
    warn: list[str] = list(slow.get("_warn") or [])
    out["warnings"] = warn
    ssid_map = slow.get("_ssid_map") or {}
    F = lambda label, *path, **kw: _fast(api, dev, steps, label, *path, **kw)  # noqa: E731

    res = F("resource", "system", "resource", warn=warn)
    if res:
        r = res[0]
        out["resource"] = {
            "uptime": r.get("uptime"),
            "version": r.get("version"),
            "board": r.get("board-name"),
            "arch": r.get("architecture-name"),
            "cpu_load": r.get("cpu-load"),
            "free_memory": r.get("free-memory"),
            "total_memory": r.get("total-memory"),
            "free_hdd": r.get("free-hdd-space"),
            "total_hdd": r.get("total-hdd-space"),
        }

    # own interface MACs — used to filter infra out of the client map
    # the full interface list (own MACs, uplink choice) only once a minute;
    # every poll reads only the uplink counters — reading stats of all ports is expensive on a CRS
    def _all():
        t0 = time.perf_counter()
        r = _interfaces(api)
        steps["interface-all"] = round((time.perf_counter() - t0) * 1000, 1)
        return r
    ifaces = _cached(dev, "interface-all", _all)
    out["own_macs"] = sorted({i["mac-address"] for i in ifaces if i.get("mac-address")})
    up = _uplink(ifaces, dev.get("uplink_interface"))
    if up and "interface-all" not in steps:
        t0 = time.perf_counter()
        try:
            cur = [dict(r) for r in api.path("interface").select(*[Key(k) for k in _IF_KEYS])
                   .where(Key("name") == up["name"])]
        except (TrapError, MultiTrapError):
            cur = []
        steps["interface-uplink"] = round((time.perf_counter() - t0) * 1000, 1)
        if cur:
            up = _uplink(cur, up["name"])
    out["uplink"] = up

    def wc(entry, signal_key, ssid=None):
        return {"mac": entry.get("mac-address"), "interface": entry.get("interface"),
                "ssid": ssid or ssid_map.get(entry.get("interface")),
                "signal": entry.get(signal_key), "tx_rate": entry.get("tx-rate"),
                "rx_rate": entry.get("rx-rate"), "uptime": entry.get("uptime"),
                "bytes": entry.get("bytes"), "type": "wifi"}

    wifi_clients = []
    # legacy wireless package (v6 + v7 wireless)
    for e in F("wireless-reg", "interface", "wireless", "registration-table"):
        wifi_clients.append(wc(e, "signal-strength"))
    # v7 wifi / wifiwave2 — on a CAPsMAN controller also the clients of all CAPs
    for e in F("wifi-reg", "interface", "wifi", "registration-table"):
        wifi_clients.append(wc(e, "signal", e.get("ssid")))
    # legacy CAPsMAN — registrations live only on the controller
    for e in F("capsman-reg", "caps-man", "registration-table"):
        wifi_clients.append(wc(e, "rx-signal", e.get("ssid")))
    out["wifi_clients"] = wifi_clients

    # bridge host table — wired MAC -> port mapping (relevant on the switch)
    out["bridge_hosts"] = [
        {"mac": h.get("mac-address"), "interface": h.get("on-interface"), "vid": h.get("vid")}
        for h in _cached(dev, "bridge-host", lambda: F("bridge-host", "interface", "bridge", "host"))
        if not h.get("local")]

    # DHCP leases (only meaningful on the DHCP server device)
    out["dhcp_leases"] = [
        {"mac": l.get("mac-address"), "ip": l.get("active-address") or l.get("address"),
         "hostname": l.get("host-name"), "comment": l.get("comment"), "server": l.get("server"),
         "status": l.get("status"), "last_seen": l.get("last-seen"),
         "expires": l.get("expires-after"), "static": not l.get("dynamic", True)}
        for l in F("dhcp-lease", "ip", "dhcp-server", "lease", warn=warn)]

    # IPv6 neighbor discovery table — mac -> client ipv6 addresses
    out["ipv6_neighbors"] = [
        {"mac": n["mac-address"], "ip": n["address"]}
        for n in F("ipv6-neighbor", "ipv6", "neighbor")
        if n.get("mac-address") and n.get("address") and n.get("status") != "failed"]

    # ARP: IP -> MAC also for devices with a static IP (no DHCP lease)
    out["arp"] = [{"ip": a["address"], "mac": str(a["mac-address"]).upper()}
                  for a in F("arp", "ip", "arp")
                  if a.get("address") and a.get("mac-address") and not a.get("invalid")]

    out["poll_steps"] = steps
    out["skipped"] = sorted("/" + "/".join(p) for p, ts in _missing.get(dev["name"], {}).items()
                            if now - ts < SLOW_INTERVAL)
    hist = _steps.setdefault(dev["name"], [])
    hist.append((now, steps, slow_ms is not None))
    del hist[:-_STEP_KEEP]
    return out


def test_connection(host: str, user: str, password: str) -> dict:
    """Try to connect and return identity/version — for the settings UI."""
    try:
        api = _connect(host, user, password)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    try:
        ident = _safe(api, "system", "identity")
        res = _safe(api, "system", "resource")
        return {
            "ok": True,
            "identity": ident[0].get("name") if ident else None,
            "version": res[0].get("version") if res else None,
            "board": res[0].get("board-name") if res else None,
        }
    finally:
        try:
            api.close()
        except Exception:  # noqa: BLE001
            pass


def poll_all(devices: list[dict], user: str, password: str) -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(devices))) as ex:
        futs = {ex.submit(poll_device, d, user, password): d for d in devices}
        for f in as_completed(futs):
            results.append(f.result())
    order = {d["name"]: i for i, d in enumerate(devices)}
    results.sort(key=lambda r: order.get(r["name"], 99))
    return results
