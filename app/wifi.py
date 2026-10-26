"""Wi-Fi overview: radios (CAPsMAN + local), channels, noise, weak clients,
overlap of 2.4 GHz Wi-Fi with Thread/Zigbee (IEEE 802.15.4)."""

import re

from i18n import t

WEAK_DBM = -75
BAND_LO, BAND_HI = 2400, 2484

_CH_RE = re.compile(r"^(\d+)/(\d+)(?:-([CceE]+))?/([^/(]+)(?:/([A-Za-z]+))?(?:\((\d+)dBm\))?")


def parse_current_channel(s: str | None) -> dict:
    """'2437/20-Ce/gn(20dBm)' -> freq, width, span, proto, txpower."""
    if not s:
        return {}
    m = _CH_RE.match(str(s))
    if not m:
        return {"raw": s}
    freq, base, ext, proto, flags, pw = m.groups()
    freq, base = int(freq), int(base)
    letters = ext or "C"
    width = base * len(letters)
    ci = letters.index("C") if "C" in letters else 0
    lo = freq - base / 2 - base * ci
    return {"raw": s, "freq": freq, "width": width, "span": [lo, lo + width],
            "proto": proto, "flags": flags, "txpower": int(pw) if pw else None,
            "band": "2.4" if freq < 3000 else ("5" if freq < 5950 else "6")}


def wifi_channel(freq: int | None) -> int | None:
    if not freq:
        return None
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    return None


def zb_center(ch: int) -> int:
    return 2405 + 5 * (ch - 11)


def _sig(v) -> int | None:
    try:
        return int(str(v).split("@")[0].split(",")[0])
    except (TypeError, ValueError):
        return None


def _band(v) -> str | None:
    v = str(v or "").lower()
    if v.startswith("2"):
        return "2.4"
    if v.startswith("5"):
        return "5"
    if v.startswith("6"):
        return "6"
    return None


def _local_info(lr: dict) -> dict:
    """Actual channel from /interface wireless monitor ('2437/20/gn(17dBm)'), else the configured one."""
    ch = parse_current_channel(lr.get("channel")) if lr.get("channel") else {}
    if not ch.get("freq"):
        try:
            f = int(str(lr.get("frequency") or "").split()[0])
            ch = {"freq": f, "width": 20, "span": [f - 10, f + 10],
                  "band": "2.4" if f < 3000 else "5", "raw": lr.get("channel")}
        except ValueError:
            ch = {"raw": lr.get("channel")}
    ch["band"] = ch.get("band") or _band(lr.get("band"))
    return ch


def build(fleet: list[dict], clients: list[dict], ieee: dict) -> dict:
    by_ident = {}
    for d in fleet:
        by_ident[(d.get("identity") or d["name"]).lower()] = d["name"]
        by_ident[d["name"].lower()] = d["name"]

    def dev_of(ident):
        return by_ident.get((ident or "").lower(), ident)

    local = {d["name"]: d.get("local_radios") or [] for d in fleet}
    local_by_mac = {lr["mac"]: (dname, lr) for dname, lrs in local.items()
                    for lr in lrs if lr.get("mac")}
    wifi_cl = [c for c in clients if c.get("conn") == "wifi"]
    radios = []
    used_local = set()
    for ctl in fleet:
        caps = [dict(x) for x in (ctl.get("capsman_radios") or [])]
        if not caps:
            continue
        for x in caps:   # the API returns master-interface="none" for main radios
            if str(x.get("master") or "").lower() in ("", "none"):
                x["master"] = None
            x["radio_mac"] = str(x.get("radio_mac") or "").upper() or None
        rc = ctl.get("radio_caps") or {}
        slaves: dict[str, list] = {}
        for r in caps:
            if r.get("master"):
                slaves.setdefault(r["master"], []).append(r)
        for r in caps:
            if r.get("master"):
                continue
            ap = dev_of(rc.get(r["name"])) if rc.get(r["name"]) else None
            ch = parse_current_channel(r.get("current_channel")) if r.get("current_channel") else {}
            lr = None
            if r.get("radio_mac") in local_by_mac:
                dname, lr = local_by_mac[r["radio_mac"]]
                ap = ap or dname
            elif ap:
                # fallback matching: the CAP's N-th radio (by MAC) = the N-th local radio (by MAC)
                mine = sorted((x for x in caps if not x.get("master")
                               and dev_of(rc.get(x["name"])) == ap),
                              key=lambda x: x.get("radio_mac") or x["name"])
                lrs = sorted(local.get(ap) or [], key=lambda x: x.get("mac") or x["name"])
                idx = next((i for i, x in enumerate(mine) if x["name"] == r["name"]), None)
                if idx is not None and idx < len(lrs) and len(lrs) == len(mine):
                    lr = lrs[idx]
            if lr:
                used_local.add((ap, lr["name"]))
            if not ch.get("freq") and lr:
                ch = _local_info(lr)
            ifaces = {r["name"]} | {x["name"] for x in slaves.get(r["name"], [])}
            ssids = sorted({c.get("ssid") for c in wifi_cl
                            if c.get("interface") in ifaces and c.get("ssid")})
            radios.append({
                "device": ap, "controller": ctl["name"], "interface": r["name"],
                "local_interface": lr["name"] if lr else None,
                "ifaces": sorted(ifaces), "ssids": ssids, "vaps": len(ifaces),
                "state": r.get("state") or (lr or {}).get("status"),
                "inactive": r.get("inactive"),
                "noise_floor": (lr or {}).get("noise_floor"), "ccq": (lr or {}).get("ccq"),
                **ch,
            })
    for d in fleet:
        for lr in local.get(d["name"]) or []:
            if (d["name"], lr["name"]) in used_local:
                continue
            if "managed by caps" in str(lr.get("status") or "").lower():
                continue
            ch = _local_info(lr)
            radios.append({"device": d["name"], "controller": None, "interface": lr["name"],
                           "local_interface": lr["name"], "ifaces": [lr["name"]], "ssids": [],
                           "vaps": 1, "state": lr.get("status"),
                           "noise_floor": lr.get("noise_floor"), "ccq": lr.get("ccq"), **ch})

    for r in radios:
        r["wifi_channel"] = wifi_channel(r.get("freq"))
        cl = [c for c in wifi_cl if c.get("device") == r.get("device")
              and c.get("interface") in set(r["ifaces"])]
        sigs = [s for s in (_sig(c.get("signal")) for c in cl) if s is not None]
        r["clients"] = len(cl)
        r["signal_avg"] = round(sum(sigs) / len(sigs)) if sigs else None
        r["signal_min"] = min(sigs) if sigs else None
        r["weak"] = sum(1 for s in sigs if s <= WEAK_DBM)
        if r.get("noise_floor") is not None and r["signal_avg"] is not None:
            r["snr_avg"] = r["signal_avg"] - r["noise_floor"]

    radios.sort(key=lambda r: (r.get("band") or "", r.get("device") or "", r.get("interface")))

    weak = []
    for c in clients:
        if c.get("conn") != "wifi":
            continue
        s = _sig(c.get("signal"))
        if s is not None and s <= WEAK_DBM:
            weak.append({"mac": c["mac"], "hostname": c.get("hostname") or c.get("comment"),
                         "ip": c.get("ip"), "vendor": c.get("vendor"), "device": c.get("device"),
                         "ssid": c.get("ssid"), "signal": s, "tx_rate": c.get("tx_rate"),
                         "rx_rate": c.get("rx_rate")})
    weak.sort(key=lambda x: x["signal"])

    # 802.15.4 vs 2,4 GHz Wi-Fi
    spans = [(r["span"], r.get("device"), r.get("wifi_channel")) for r in radios
             if r.get("band") == "2.4" and r.get("span")]
    zb = []
    for ch in range(11, 27):
        c = zb_center(ch)
        hits, near = [], []
        for (lo, hi), dev, wch in spans:
            if lo - 1 <= c <= hi + 1:
                hits.append(t("srv.wifi.dev_channel", device=dev, channel=wch))
            elif lo - 5 <= c <= hi + 5:
                near.append(t("srv.wifi.dev_channel", device=dev, channel=wch))
        use = [k for k in ("thread", "zigbee") if ch in (ieee.get(k) or [])]
        zb.append({"channel": ch, "center": c, "overlap": hits, "near": near, "used_by": use,
                   "status": "overlap" if hits else ("near" if near else "free")})
    conflicts = [z for z in zb if z["used_by"] and z["status"] != "free"]
    free = [z["channel"] for z in zb if z["status"] == "free"]
    return {"radios": radios, "weak_clients": weak, "weak_dbm": WEAK_DBM,
            "ieee802154": ieee, "zb_channels": zb, "zb_conflicts": conflicts,
            "zb_free": free, "band_24": [BAND_LO, BAND_HI]}
