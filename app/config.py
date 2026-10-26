"""Runtime-mutable config persisted back to config.yaml (comments preserved)."""

import os
import threading

from ruamel.yaml import YAML

import i18n
from i18n import t

_yaml = YAML()
_yaml.preserve_quotes = True

_lock = threading.RLock()
_path: str = ""
_data: dict = {}

VALID_ROLES = ("ap", "switch", "router")
# device names end up in HTML/JS and may come from router identity (sync-names)
_BAD_NAME_CHARS = set("<>\"'`&\\")


def load(path: str):
    global _path, _data
    with _lock:
        _path = path
        with open(path) as f:
            _data = _yaml.load(f) or {}
        _data.setdefault("devices", [])


def _save():
    tmp = _path + ".tmp"
    with open(tmp, "w") as f:
        _yaml.dump(_data, f)
    try:
        os.replace(tmp, _path)
    except OSError:
        # in Docker config.yaml is bind-mounted as a single file ->
        # renaming over the mount point fails (EBUSY) and changes never reached
        # the disk (they vanished after a container restart). Fallback: rewrite
        # in place. Not atomic, but there is no other way over a bind mount.
        with open(tmp) as src, open(_path, "w") as dst:
            dst.write(src.read())
        os.unlink(tmp)


def devices() -> list[dict]:
    with _lock:
        return [dict(d) for d in _data["devices"]]


def dhcp_source() -> str | None:
    with _lock:
        return _data.get("dhcp_source")


def webhook() -> dict:
    with _lock:
        return dict(_data.get("webhook") or {})


def ignore_ifaces() -> set:
    with _lock:
        return {
            (d["name"], i)
            for d in _data["devices"]
            for i in (d.get("ignore_interfaces") or [])
        }


def set_webhook(cfg: dict):
    clean = {}
    url = (cfg.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError(t("srv.cfg.url_scheme"))
    clean["url"] = url
    for k in ("notify_new", "notify_gone", "notify_back", "notify_roaming"):
        clean[k] = bool(cfg.get(k))
    try:
        clean["gone_after_min"] = max(1, int(cfg.get("gone_after_min", 10)))
    except (TypeError, ValueError):
        raise ValueError(t("srv.cfg.not_number", field="gone_after_min")) from None
    with _lock:
        _data["webhook"] = clean
        _save()


def set_dhcp_source(name: str | None):
    with _lock:
        if name and not any(d["name"] == name for d in _data["devices"]):
            raise ValueError(t("srv.cfg.unknown_device"))
        _data["dhcp_source"] = name or None
        _save()


def _validate_device(dev: dict) -> dict:
    name = (dev.get("name") or "").strip()
    host = (dev.get("host") or "").strip()
    if not name or not host:
        raise ValueError(t("srv.cfg.name_host_required"))
    if len(name) > 64 or any(c in _BAD_NAME_CHARS or ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError(t("srv.cfg.bad_name"))
    role = dev.get("role") or "ap"
    if role not in VALID_ROLES:
        raise ValueError(t("srv.cfg.bad_role", roles=", ".join(VALID_ROLES)))
    ign = dev.get("ignore_interfaces") or []
    if isinstance(ign, str):
        ign = [s.strip() for s in ign.split(",") if s.strip()]
    out = {"name": name, "host": host, "role": role, "ignore_interfaces": list(ign)}
    upl = (dev.get("uplink_interface") or "").strip()
    if upl:
        out["uplink_interface"] = upl
    return out


def add_device(dev: dict) -> dict:
    clean = _validate_device(dev)
    with _lock:
        if any(d["name"] == clean["name"] for d in _data["devices"]):
            raise ValueError(t("srv.cfg.name_exists"))
        _data["devices"].append(clean)
        _save()
    return clean


def update_device(name: str, dev: dict) -> dict:
    clean = _validate_device(dev)
    with _lock:
        idx = next((i for i, d in enumerate(_data["devices"]) if d["name"] == name), None)
        if idx is None:
            raise ValueError(t("srv.cfg.unknown_device"))
        if clean["name"] != name and any(d["name"] == clean["name"] for d in _data["devices"]):
            raise ValueError(t("srv.cfg.name_exists"))
        _data["devices"][idx] = clean
        if _data.get("dhcp_source") == name:
            _data["dhcp_source"] = clean["name"]
        _save()
    return clean


def remove_device(name: str):
    with _lock:
        before = len(_data["devices"])
        _data["devices"] = [d for d in _data["devices"] if d["name"] != name]
        if len(_data["devices"]) == before:
            raise ValueError(t("srv.cfg.unknown_device"))
        if _data.get("dhcp_source") == name:
            _data["dhcp_source"] = None
        _save()


IEEE802154_DEFAULT = {"thread": [], "zigbee": []}
IOT_DEFAULT = {
    "router": None,                  # name of the device with the firewall (None = dhcp_source)
    "subnet": "192.168.40.0/24",
    "dhcp_server": "dhcp_iot",
    "allow_list": "iot_wan_allow",
    "temp_list": "iot_temp_wan",
    "drop_prefix": "IOT-DROP",
    "rule_drop": "IoT drop all other (log)",     # comment of the final drop rule
    "rule_temp": "IoT temporary full internet",  # comment of the learning (temporary access) rule
    "dns_forward": "1.1.1.1",
}


def ieee802154() -> dict:
    with _lock:
        d = dict(IEEE802154_DEFAULT)
        d.update(_data.get("ieee802154") or {})
        return {k: [int(x) for x in (d.get(k) or [])] for k in ("thread", "zigbee")}


def set_ieee802154(cfgd: dict):
    clean = {}
    for k in ("thread", "zigbee"):
        v = cfgd.get(k) or []
        if isinstance(v, str):
            v = [x for x in v.replace(";", ",").split(",") if x.strip()]
        try:
            chs = sorted({int(x) for x in v})
        except (TypeError, ValueError):
            raise ValueError(t("srv.cfg.zb_numbers", kind=k)) from None
        if any(c < 11 or c > 26 for c in chs):
            raise ValueError(t("srv.cfg.zb_range", kind=k))
        clean[k] = chs
    with _lock:
        _data["ieee802154"] = clean
        _save()


def iot() -> dict:
    with _lock:
        d = dict(IOT_DEFAULT)
        d.update(_data.get("iot") or {})
        if not d.get("router"):
            d["router"] = _data.get("dhcp_source")
        return d


def alerts() -> dict:
    import alerts as al
    with _lock:
        raw = dict(_data.get("alerts") or {})
    out = {k: v for k, v in al.DEFAULTS.items() if k != "enabled"}
    out.update({k: v for k, v in raw.items() if k in out})
    en = dict(al.DEFAULTS["enabled"])
    en.update({k: bool(v) for k, v in (raw.get("enabled") or {}).items() if k in en})
    out["enabled"] = en
    return out


def set_alerts(body: dict):
    import alerts as al
    cur = alerts()
    clean = {"enabled": {k: bool((body.get("enabled") or {}).get(k, cur["enabled"][k]))
                         for k in al.DEFAULTS["enabled"]}}
    for k, v in al.DEFAULTS.items():
        if k == "enabled":
            continue
        try:
            clean[k] = max(1, int(body.get(k, cur[k])))
        except (TypeError, ValueError):
            raise ValueError(t("srv.cfg.not_number", field=k)) from None
    with _lock:
        _data["alerts"] = clean
        _save()


def language() -> str:
    with _lock:
        return str(_data.get("language") or i18n.DEFAULT)


def set_language(code: str):
    if not i18n.is_valid(code):
        raise ValueError(t("srv.cfg.bad_language", code=code))
    with _lock:
        _data["language"] = code
        _save()


RETENTION_DEFAULT = {"stats_hourly_days": 7, "dns_daily_days": 90}


# GET endpoints an API token may read (Home Assistant); a trailing "/" means a prefix.
# Override with `api_token_paths:` in config.yaml. Token management is never allowed.
TOKEN_PATHS_DEFAULT = ["/api/snapshot", "/api/presence/", "/api/alerts", "/api/events"]


def token_paths() -> list[str]:
    with _lock:
        v = _data.get("api_token_paths")
    return [str(p) for p in v] if isinstance(v, list) else list(TOKEN_PATHS_DEFAULT)


def token_allowed(path: str) -> bool:
    if path.startswith("/api/tokens"):
        return False
    return any(path.startswith(p) if p.endswith("/") else path == p for p in token_paths())


def retention() -> dict:
    with _lock:
        d = dict(RETENTION_DEFAULT)
        d.update(_data.get("retention") or {})
    return {k: max(1, int(v)) for k, v in d.items()}


def snapshot() -> dict:
    with _lock:
        return {
            "devices": devices(),
            "dhcp_source": _data.get("dhcp_source"),
            "webhook": webhook(),
            "ieee802154": ieee802154(),
            "alerts": alerts(),
            "iot": iot(),
            "language": language(),
        }
