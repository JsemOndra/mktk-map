"""Alerts: rules over poll data, deduplication, delivery via the HA webhook.

State alerts (offline, CPU, temperature...) are sent once when they fire and once
when they are resolved ("resolved"). Events (config change, new IoT domain, PPPoE
reconnect) are sent every time, but the same key at most once per cooldown.

Payload for the HA webhook:
  {"type": "alert", "alert_type": "...", "state": "fired|resolved|event",
   "severity": "info|warning|critical", "device": "...", "title": "...",
   "message": "...", "detail": {...}, "ts": ...}
"""

import ipaddress
import logging
import threading
import time

import requests

import store
from i18n import t

log = logging.getLogger("mktk")

# type -> severity; the type name lives in the translations (alert.type.<type>)
TYPES = {
    "device_offline": "critical",
    "capsman_missing": "warning",
    "radio_down": "warning",
    "pppoe_reconnect": "warning",
    "cpu_high": "warning",
    "temp_high": "warning",
    "config_changed": "info",
    "iot_new_domain": "warning",
    "update_available": "info",
    "backup_failed": "warning",
}


def type_title(typ: str) -> str:
    return t("alert.type." + typ)
DEFAULTS = {
    "enabled": {t: True for t in TYPES},
    "offline_after_min": 3,
    "cpu_threshold": 85,
    "cpu_polls": 3,
    "temp_threshold": 70,
    "radio_down_min": 10,
    "cooldown_min": 30,
    "iot_baseline_days": 3,
}


class Alerts:
    def __init__(self, get_cfg, get_webhook_url):
        self.get_cfg = get_cfg                 # () -> dict (DEFAULTS + config.yaml alerts)
        self.get_url = get_webhook_url         # () -> str | None
        self.active: dict[str, dict] = {}      # key -> alert (state alerts)
        self.last_event: dict[str, float] = {}
        self.lock = threading.Lock()
        self.cpu_hits: dict[str, int] = {}
        self.cpu_ok: dict[str, int] = {}
        self.radio_bad_since: dict[str, float] = {}
        self.link_downs: dict[str, int] = {}
        self.notified_updates: set = set()
        self.iot_known: dict[str, set] = {}
        self.iot_first: dict[str, float] = {}
        self.iot_loaded = False

    # -- delivery
    def _send(self, a: dict) -> bool:
        url = self.get_url()
        if not url:
            return False
        payload = {"type": "alert", "alert_type": a["type"], "state": a["state"],
                   "severity": a["severity"], "device": a.get("device"), "title": a["title"],
                   "message": a.get("message"), "detail": a.get("detail") or {}, "ts": a["ts"]}
        try:
            r = requests.post(url, json=payload, timeout=5)
            return r.ok
        except requests.RequestException as e:
            log.warning("webhook (alert) failed: %s", e)
            return False

    def _emit(self, a: dict):
        cfg = self.get_cfg()
        a["sent"] = bool(cfg["enabled"].get(a["type"], True)) and self._send(a)
        store.alert_add(a)
        log.info("alert %s/%s: %s - %s", a["type"], a["state"], a["title"], a.get("message"))

    def fire(self, key, typ, device, message, detail=None, severity=None):
        title = f"{type_title(typ)}: {device}" if device else type_title(typ)
        with self.lock:
            if key in self.active:
                return
            a = {"ts": time.time(), "key": key, "type": typ, "severity": severity or TYPES[typ],
                 "device": device, "title": title, "message": message, "state": "fired",
                 "detail": detail}
            self.active[key] = a
        self._emit(dict(a))

    def resolve(self, key, message=None):
        with self.lock:
            a = self.active.pop(key, None)
        if not a:
            return
        dur = time.time() - a["ts"]
        self._emit({**a, "ts": time.time(), "state": "resolved", "severity": "info",
                    "title": t("alert.resolved_title", title=a["title"]),
                    "message": t("alert.resolved_msg", message=message or "", minutes=int(dur // 60))})

    def event(self, key, typ, device, message, detail=None, severity=None):
        cd = self.get_cfg()["cooldown_min"] * 60
        now = time.time()
        with self.lock:
            if now - self.last_event.get(key, 0) < cd:
                return
            self.last_event[key] = now
        title = f"{type_title(typ)}: {device}" if device else type_title(typ)
        self._emit({"ts": now, "key": key, "type": typ, "severity": severity or TYPES[typ],
                    "device": device, "title": title, "message": message, "state": "event",
                    "detail": detail})

    def active_list(self) -> list[dict]:
        with self.lock:
            return sorted(self.active.values(), key=lambda a: -a["ts"])

    # -- rules over poll data
    def evaluate(self, fleet: list[dict], offline_since: dict):
        cfg = self.get_cfg()
        now = time.time()
        controllers_caps = set()
        has_controller = False
        for d in fleet:
            if d.get("capsman_radios"):
                has_controller = True
                controllers_caps |= {x.lower() for x in d.get("remote_caps") or []}
        for d in fleet:
            name = d["name"]
            ident = d.get("identity") or name
            # offline
            since = offline_since.get(name)
            k = f"offline:{name}"
            if since and now - since >= cfg["offline_after_min"] * 60:
                self.fire(k, "device_offline", ident,
                          t("alert.msg.offline", since=time.strftime('%H:%M', time.localtime(since)),
                            error=d.get('error') or '?'),
                          {"host": d.get("host")})
            elif d.get("reachable"):
                self.resolve(k, t("alert.msg.online_again"))
            if not d.get("reachable"):
                continue
            # CAPsMAN
            if d.get("role") == "ap" and has_controller and d.get("slow_age") is not None:
                k = f"capsman:{name}"
                if ident.lower() not in controllers_caps:
                    self.fire(k, "capsman_missing", ident, t("alert.msg.capsman_missing"))
                else:
                    self.resolve(k, t("alert.msg.capsman_back"))
            # CPU
            try:
                cpu = float((d.get("resource") or {}).get("cpu_load"))
            except (TypeError, ValueError):
                cpu = None
            if cpu is not None:
                k = f"cpu:{name}"
                if cpu >= cfg["cpu_threshold"]:
                    self.cpu_hits[name] = self.cpu_hits.get(name, 0) + 1
                    self.cpu_ok[name] = 0
                    if self.cpu_hits[name] >= cfg["cpu_polls"]:
                        self.fire(k, "cpu_high", ident, t("alert.msg.cpu", cpu=f"{cpu:.0f}", threshold=cfg['cpu_threshold']),
                                  {"cpu": cpu})
                else:
                    self.cpu_hits[name] = 0
                    if cpu < cfg["cpu_threshold"] - 10:
                        self.cpu_ok[name] = self.cpu_ok.get(name, 0) + 1
                        if self.cpu_ok[name] >= cfg["cpu_polls"]:
                            self.resolve(k, f"CPU {cpu:.0f} %")
            # temperature
            temp = d.get("temp")
            if temp is not None:
                k = f"temp:{name}"
                if temp >= cfg["temp_threshold"]:
                    self.fire(k, "temp_high", ident, t("alert.msg.temp", temp=f"{temp:.0f}",
                              threshold=cfg['temp_threshold']), {"temp": temp})
                elif temp < cfg["temp_threshold"] - 5:
                    self.resolve(k, f"{temp:.0f} °C")
            # radios on the controller
            for r in d.get("capsman_radios") or []:
                m = str(r.get("master") or "").lower()
                if m not in ("", "none"):
                    continue
                rk = f"{name}:{r['name']}"
                state = str(r.get("state") or "")
                bad = r.get("inactive") or (state and "running" not in state.lower())
                k = f"radio:{rk}"
                if bad:
                    s0 = self.radio_bad_since.setdefault(rk, now)
                    if now - s0 >= cfg["radio_down_min"] * 60:
                        cap = (d.get("radio_caps") or {}).get(r["name"]) or r["name"]
                        self.fire(k, "radio_down", cap, t("alert.msg.radio", radio=r['name'], channel=r.get('current_channel') or '?',
                                                  state=state or t("alert.msg.inactive")),
                                  {"interface": r["name"], "state": state})
                else:
                    self.radio_bad_since.pop(rk, None)
                    self.resolve(k, t("alert.msg.radio_ok"))
            # PPPoE reconnect
            up = d.get("uplink") or {}
            if up.get("type") == "pppoe-out" and up.get("link_downs") is not None:
                try:
                    ld = int(up["link_downs"])
                except (TypeError, ValueError):
                    ld = None
                prev = self.link_downs.get(name)
                if ld is not None:
                    if prev is not None and ld > prev:
                        self.event(f"pppoe:{name}:{ld}", "pppoe_reconnect", ident,
                                   t("alert.msg.pppoe", iface=up.get('name'), count=ld - prev),
                                   {"link_downs": ld})
                    self.link_downs[name] = ld
            # updates
            if d.get("update_available"):
                lv = (d.get("latest") or {}).get("version")
                k = f"update:{name}:{lv}"
                if lv and k not in self.notified_updates:
                    self.notified_updates.add(k)
                    self.event(k, "update_available", ident,
                               t("alert.msg.update", latest=lv, running=(d.get('resource') or {}).get('version')))

    # -- IoT: new domain
    def _load_iot(self, subnet: str):
        net = ipaddress.ip_network(subnet)
        known: dict[str, set] = {}
        first: dict[str, float] = {}
        for ip, name, f in store.dns_known_pairs():
            try:
                if ipaddress.ip_address(ip) not in net:
                    continue
            except ValueError:
                continue
            known.setdefault(ip, set()).add(name)
            first[ip] = min(first.get(ip, f), f)
        self.iot_known, self.iot_first, self.iot_loaded = known, first, True

    def dns_query(self, ip: str, name: str, client: str | None, subnet: str):
        """Called for every DNS query. Alerts when an IoT device with a learned
        baseline (>= N days of history) asks for a domain it has never asked for."""
        try:
            if ipaddress.ip_address(ip) not in ipaddress.ip_network(subnet):
                return
        except ValueError:
            return
        if not self.iot_loaded:
            self._load_iot(subnet)
        s = self.iot_known.setdefault(ip, set())
        if name in s:
            return
        s.add(name)
        first = self.iot_first.setdefault(ip, time.time())
        if time.time() - first < self.get_cfg()["iot_baseline_days"] * 86400:
            return                       # the device is still learning
        self.event(f"iotdom:{ip}:{name}", "iot_new_domain", client or ip,
                   t("alert.msg.iot_domain", domain=name), {"ip": ip, "domain": name})
