"""Demo mode: runs mktk-map with made-up devices and clients — no MikroTik needed.

Useful for trying the UI, taking screenshots and checking translations.
All names, IPs and MAC addresses are fictional (device IPs from the 192.0.2.0/24
documentation range).

    pip install -r requirements.txt
    python tools/demo.py [--port 8080] [--lang en|cs]

Then open http://127.0.0.1:8080 and sign in as demo / demo.
"""

import argparse
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")

CONFIG = """\
dhcp_source: router
language: {lang}
devices:
- name: router
  host: 192.0.2.1
  role: router
  ignore_interfaces: []
- name: core-switch
  host: 192.0.2.2
  role: switch
  ignore_interfaces: [sfp-sfpplus1]
- name: ap-living-room
  host: 192.0.2.3
  role: ap
  ignore_interfaces: []
- name: ap-office
  host: 192.0.2.4
  role: ap
  ignore_interfaces: []
- name: ap-garden
  host: 192.0.2.5
  role: ap
  ignore_interfaces: []
ieee802154:
  thread: [25]
  zigbee: [15]
webhook:
  url: ''
  notify_new: true
  notify_gone: false
  notify_back: false
  notify_roaming: false
  gone_after_min: 10
"""

# (hostname, OUI prefix, AP or switch port, ssid/vlan, signal)
CLIENTS = [
    ("laptop-anna", "3C:22:FB", "ap-office", "HomeNet", -52),
    ("phone-anna", "F0:18:98", "ap-living-room", "HomeNet", -61),
    ("phone-tom", "DC:A6:32", "ap-living-room", "HomeNet", -77),
    ("tablet", "AC:BC:32", "ap-living-room", "HomeNet", -58),
    ("tv-living-room", "70:2A:D5", "ap-living-room", "HomeNet", -66),
    ("printer", "00:1B:A9", "ap-office", "HomeNet", -70),
    ("weather-station", "24:0A:C4", "ap-garden", "IoT", -79),
    ("garden-lights", "84:CC:A8", "ap-garden", "IoT", -73),
    ("thermostat", "18:B4:30", "ap-office", "IoT", -64),
    ("nas", "00:11:32", "ether5", 10, None),
    ("desktop", "B4:2E:99", "ether6", 10, None),
    ("home-assistant", "B8:27:EB", "ether7", 10, None),
    ("camera-gate", "BC:32:5F", "ether8", 40, None),
]
APS = {"ap-living-room": ("cap1", "cap2", "ether2"), "ap-office": ("cap3", "cap4", "ether3"),
       "ap-garden": ("cap5", None, "ether4")}


def mac(prefix: str, n: int) -> str:
    return f"{prefix}:{n >> 16 & 255:02X}:{n >> 8 & 255:02X}:{n & 255:02X}"


def own(n: int) -> str:
    return mac("02:00:5E", n)          # locally administered


START = time.time()


def fake_poll(devices, user, password):
    rnd = random.Random(int(time.time() // 30))
    t = time.time() - START
    leases, wifi, hosts = [], [], []
    for i, (host, oui, where, net, sig) in enumerate(CLIENTS):
        m = mac(oui, 0x10203 + i * 4099)
        ip = f"192.168.{40 if net == 'IoT' or net == 40 else 88}.{20 + i}"
        leases.append({"mac": m, "ip": ip, "hostname": host, "comment": None, "server": "dhcp-lan",
                       "status": "bound", "last_seen": f"{rnd.randint(5, 50)}s",
                       "expires": "9m12s", "static": i % 3 == 0})
        if sig is not None:
            cap = APS[where][0] if i % 2 == 0 or not APS[where][1] else APS[where][1]
            wifi.append({"mac": m, "interface": cap, "ssid": net, "signal": sig + rnd.randint(-3, 3),
                         "tx_rate": "144.4Mbps", "rx_rate": "130Mbps", "uptime": "2h13m",
                         "bytes": f"{int(5e7 + t * 9e4 * (i + 1))},{int(9e8 + t * 3e5 * (i + 1))}",
                         "type": "wifi"})
        else:
            hosts.append({"mac": m, "interface": where, "vid": net})

    def base(dev, idx, model, board, cpu):
        return {
            "name": dev["name"], "host": dev["host"], "role": dev.get("role"), "reachable": True,
            "error": None, "polled_at": time.time(), "identity": dev["name"],
            "resource": {"uptime": "3w2d04:11:52", "version": "7.16.1 (stable)", "board": board,
                         "arch": "arm64", "cpu_load": cpu + rnd.randint(0, 6),
                         "free_memory": 512e6 * (0.55 + rnd.random() * 0.05), "total_memory": 1024e6,
                         "free_hdd": 90e6, "total_hdd": 128e6},
            "routerboard": {"model": model, "firmware": "7.16.1", "upgrade_firmware": "7.16.1"},
            "update": {"channel": "stable", "installed": "7.16.1"},
            "temp": 42 + idx * 2 + rnd.randint(0, 2),
            "uplink": {"name": "ether1", "rx": int(t * 4e6 * (6 - idx)), "tx": int(t * 1e6 * (6 - idx)),
                       "type": "ether", "running": True, "link_downs": 0},
            "own_macs": [own(idx * 16 + k) for k in range(4)], "wifi_clients": [], "bridge_hosts": [],
            "dhcp_leases": [], "ipv6_neighbors": [], "arp": [], "warnings": [], "neighbors": [],
            "local_radios": [], "capsman_radios": [], "radio_caps": {}, "remote_caps": [],
            "conn": {"connected_since": START}, "poll_ms": 80 + rnd.randint(0, 40), "slow_ms": None,
            "slow_age": 30, "poll_steps": {}}

    def nb(identity, iface, remote, idx):
        return {"interface": iface, "identity": identity, "mac": own(idx * 16), "address": None,
                "remote_interface": remote, "platform": "MikroTik", "board": None, "version": "7.16.1"}

    out = []
    for dev in devices:
        n = dev["name"]
        if n == "router":
            d = base(dev, 0, "RB5009UG+S+", "RB5009UG+S+", 9)
            d["dhcp_leases"] = leases
            d["wifi_clients"] = wifi
            d["uplink"]["type"], d["uplink"]["name"] = "pppoe-out", "pppoe-out1"
            caps = []
            for k, (ap, (c24, c5, _p)) in enumerate(APS.items()):
                d["radio_caps"][c24] = ap
                caps.append({"name": c24, "master": None, "current_channel": f"{2412 + 25 * k}/20-Ce/gn(17dBm)",
                             "state": "running-ap", "clients": None, "ssid": "HomeNet",
                             "radio_mac": own(200 + k * 2), "inactive": False})
                if c5:
                    d["radio_caps"][c5] = ap
                    caps.append({"name": c5, "master": None, "current_channel": f"{5180 + 80 * k}/20-Ceee/ac(20dBm)",
                                 "state": "running-ap", "clients": None, "ssid": "HomeNet",
                                 "radio_mac": own(201 + k * 2), "inactive": False})
            d["capsman_radios"] = caps
            d["remote_caps"] = list(APS)
            d["neighbors"] = [nb("core-switch", "ether2", "sfp-sfpplus1", 1)] + \
                [nb(ap, "ether2", "ether1", 2 + k) for k, ap in enumerate(APS)]
        elif n == "core-switch":
            d = base(dev, 1, "CRS326-24G-2S+", "CRS326-24G-2S+", 4)
            d["bridge_hosts"] = hosts
            d["neighbors"] = [nb("router", "sfp-sfpplus1", "ether2", 0)] + \
                [nb(ap, port, "ether1", 2 + k) for k, (ap, (_a, _b, port)) in enumerate(APS.items())] + \
                [{"interface": "ether24", "identity": "office-switch", "mac": "02:00:5E:AA:BB:CC",
                  "address": None, "remote_interface": "port1", "platform": "LLDP", "board": "unmanaged-8p",
                  "version": None}]
        elif n in APS:
            k = list(APS).index(n)
            d = base(dev, 2 + k, "cAP ax", "cAPGi-5HaxD2HaxD", 2)
            d["local_radios"] = [{"name": "wifi1", "pkg": "wifi", "mac": own(200 + k * 2), "band": "2ghz-ax",
                                  "frequency": None, "channel": None, "width": "20mhz", "running": True,
                                  "noise_floor": -104 + k, "ccq": None, "clients": None, "status": "running"}]
            d["neighbors"] = [nb("core-switch", "ether1", APS[n][2], 1), nb("router", "ether1", "ether2", 0)]
            d["uplink"]["rx"] //= 4
        else:
            d = {"name": n, "host": dev["host"], "role": dev.get("role"), "reachable": False,
                 "error": "timed out", "error_kind": "conn", "polled_at": time.time(), "conn": {}}
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lang", default="en")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="mktk-demo-")
    with open(os.path.join(tmp, "config.yaml"), "w") as f:
        f.write(CONFIG.format(lang=a.lang))
    sys.path.insert(0, APP)
    import auth
    os.environ.update({"CONFIG_PATH": os.path.join(tmp, "config.yaml"), "DB_PATH": os.path.join(tmp, "mktk.db"),
                       "APP_USER": "demo", "APP_PASS_HASH": auth.hash_password("demo"),
                       "MIKROTIK_USER": "demo", "MIKROTIK_PASS": "demo", "PROXY_SECRET": "", "MKTK_DEV": "1",
                       "POLL_INTERVAL": "10"})
    import main as mk
    mk.poll_all = fake_poll
    mk.backups.start = lambda: None       # no SSH exports in demo mode
    mk.tracker.start = lambda: None       # no connection tracking
    mk.dns.start = lambda: None           # no syslog listener
    import uvicorn
    print(f"mktk-map demo on http://127.0.0.1:{a.port}  (user demo / password demo), data in {tmp}")
    uvicorn.run(mk.app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
