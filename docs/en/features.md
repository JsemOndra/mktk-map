# Features

**English** · [Čeština](../cs/funkce.md) · [← README](../../README.md)

- [Overview](#overview)
- [Topology](#topology)
- [Updates](#updates)
- [Configuration backups](#configuration-backups)
- [Wi-Fi](#wi-fi)
- [Connections and DNS](#connections-and-dns)
- [IoT whitelist](#iot-whitelist)
- [Alerts](#alerts)
- [Home Assistant](#home-assistant)
- [API](#api)
- [config.yaml reference](#configyaml-reference)

## Overview

The main page (`/`) shows:

- **Devices** — one card per device with uptime, RouterOS version, CPU/RAM/disk, client count and the age of the
  API connection. A badge shows when a newer RouterOS or firmware is available. *history* opens charts for
  24 h / 7 days: CPU, RAM, temperature, uplink traffic, clients and poll duration; outages are shaded.
  The uplink is PPPoE on a router, otherwise `ether1`; you can override it per device (*uplink iface*).
- **Client map** — clients grouped by device or by VLAN. Wi-Fi clients are assigned to the AP they are
  registered on (including CAPsMAN), wired clients come from the bridge host table of the switches.
  IP and hostname are taken from the DHCP leases of the `dhcp_source` device; clients with a static IP get
  their address from ARP. Click a client for details, 24 h signal and traffic charts and roaming history.
  Red IP = dynamic lease, blue = static lease. Leases without a live client are in *DHCP lease only*.
- **Filter** — full-text search over hostname, IP, MAC, vendor, comment and SSID.
- **Alerts** and **Events** (new client, gone, back, roaming) for the last 24 h.
- **⚙ settings** — language, devices, DHCP lease source, API tokens, Thread/Zigbee channels, alerts, webhook.

Vendors are looked up offline from the IEEE OUI database.

## Topology

`/topology` draws how the devices are connected. It is built from the neighbour tables
(`/ip neighbor` — MNDP, LLDP, CDP) that every device reports in the slow poll (every 5 minutes):

- Each line is a link with the port names on both ends. Solid = cable, dashed = wireless,
  dotted = the AP is known only from CAPsMAN (its physical link was not seen), orange dotted = the device was seen
  only over a VLAN interface, so its physical port is unknown.
- MNDP passes through bridges, so a device also sees neighbours several hops away. These indirect links are
  filtered out: a link A–C is dropped when A sees B on the same port and B sees C on a different port.
- Devices that announce themselves but are not in your configuration (other switches, phones with LLDP…)
  are shown with a dashed frame.
- A device that announces itself to nobody (e.g. behind an unmanaged switch) is placed from the bridge MAC tables:
  the port where another device learned its MAC (dashed line). If other wired clients share that port, an
  **unmanaged switch** is drawn in between and those clients are moved under it. It is named after a client whose
  DHCP host name looks like a switch (e.g. `TL-SG108PE-…`), otherwise just *unmanaged switch*.
- The tree is laid out from the router (role `router`, otherwise the first switch).
- **Show clients** (top right, remembered in the browser) adds the clients under the device they are connected to:
  one *cable* box per device with the port of each client, and one box per wireless interface with its SSID and
  the clients sorted by signal. At most 10 clients are listed per box, hover a client for its IP, MAC and vendor.

Requirement: neighbour discovery enabled on the device interfaces
(`/ip neighbor discovery-settings set discover-interface-list=LAN`).

## Updates

`/updates` (needs a signed-in session, not an API token):

1. Enter the admin username and password of the devices. They are verified by logging in to all devices and
   kept **only in server memory for 30 minutes** (bound to your session), then discarded.
2. *check* runs `check-for-updates`; *install* installs a new RouterOS; *firmware* upgrades the RouterBOARD firmware.
3. Only one job runs at a time. An install does: connect → check → backup on the device
   (`pre-<version>-<date>.backup` + `.rsc`) and in mktk-map → download → reboot → wait for the device (max 15 min)
   → verify the version → for an AP wait until it is back in CAPsMAN.
4. Every step is logged in the history. A job interrupted by an app restart is marked as *interrupted*.

The latest available version is checked on `upgrade.mikrotik.com` every 6 hours for the channel of each device.

Recommended order: access points one by one, the router last (its reboot takes down the whole network).

## Configuration backups

`/backups`:

- `/export terse` over SSH with the monitoring account (the export contains no passwords).
- When: every 6 h, within 5 min after a change (a new entry in `/system history`), before an update, and manually.
- Only a changed configuration is stored (compared without the header with date and serial number), compressed,
  up to 300 versions per device.
- Each version shows a summary (+/− lines, affected sections), a diff against the previous one and a `.rsc` download.
- The monitoring group needs the `ssh` policy — the button *allow SSH for monitor* adds it on all devices
  (admin credentials needed); `write` stays off.
- The SSH host key is pinned on the first connection; if it changes, the backup is refused until you confirm it.

## Wi-Fi

`/wifi`:

- **Radios** — band, channel, width, power and SSIDs reported by the CAPsMAN controller, noise floor from the AP,
  average/minimum client signal and SNR.
- **2.4 GHz vs. Thread/Zigbee** — the 802.15.4 channels (11–26) drawn over the 2.4 GHz Wi-Fi channels.
  Set your Thread and Zigbee channels in ⚙ settings to see whether they overlap.
- **Weak clients** — clients at −75 dBm or worse (often “sticky” clients holding on to a distant AP).

## Connections and DNS

`/connections`:

- Reads `/ip/firewall/connection` (+ IPv6) from the router every 10 s while the page is open, otherwise every 30 s.
- For each connection: client, destination with DNS name (from the router's DNS cache; the name the client itself
  asked for wins), port and guessed service, state, duration, transferred data and rate.
- Closed connections stay visible for 5 minutes; hourly statistics (top destinations, clients by data) are kept for 7 days.
- Limitations: connections shorter than the poll interval may be missed; RouterOS may not count bytes for
  FastTrack connections (marked *FT*).

**DNS queries:** the router can send its `dns` log topic over syslog (UDP 5514) to mktk-map. The button on
`/connections` (admin credentials) creates `/system logging action mktkdns` and a rule `topics=dns`. Only packets from
configured devices are accepted. Clients using DoH/DoT or their own DNS server do not appear.
`SYSLOG_TARGET` in `docker-compose.yml` must be the server IP as seen by the router.

`/dns` shows all queries for a period (1 h – 90 days): domains by number of queries, which clients asked,
record types, last time, and a *new* tag for domains first seen in the last 24 h. Click a client to filter.

## IoT whitelist

`/iot` manages a firewall whitelist for an IoT network that is blocked from the internet by default.
It expects these objects on the router (create them once yourself; names are configurable in `config.yaml`, section `iot`):

| Object | Default name |
|---|---|
| IoT subnet | `192.168.40.0/24` (`subnet`) |
| address list with allowed destinations | `iot_wan_allow` (`allow_list`) |
| `/ip firewall filter` accept rule IoT → internet for `allow_list` | comment `IoT -> cloud whitelist` |
| final drop rule with logging, `log-prefix=IOT-DROP` | comment `IoT drop all other (log)` (`rule_drop`) |

What you can do:

- See **dropped attempts** (from the router log) with the DNS names of the destinations and allow a domain, IP or port in one click.
- Manage **domains** (`/ip dns static` entries of type FWD that fill the address list when the device resolves them),
  **static IPs** and **port rules** (`IoT -> cloud <tag> tcp|udp`).
- **Learn**: a device gets full internet access for 10–60 minutes (address list with a timeout on the router as a safety
  net) and DNS logging is enabled; the page shows its queries and connections and suggests domains to allow.

Reading works with the monitoring account; changes need admin credentials (same as updates) and every change is
logged in the *Changes* table. Router comments are ASCII only (diacritics are removed).

## Alerts

Rules evaluated after every poll. State alerts are sent when they fire and when they are resolved; events at most
once per cooldown (30 min).

| Type | When |
|---|---|
| `device_offline` | device unreachable for ≥ 3 min |
| `capsman_missing` | an AP is not connected to CAPsMAN |
| `radio_down` | a radio on the controller is not running for ≥ 10 min |
| `pppoe_reconnect` | PPPoE went down and reconnected |
| `cpu_high` | CPU ≥ 85 % in 3 polls in a row |
| `temp_high` | temperature ≥ 70 °C |
| `config_changed` | a new backup version (with a change summary) |
| `iot_new_domain` | an IoT device with ≥ 3 days of history asks for a new domain |
| `update_available` | a new RouterOS version |
| `backup_failed` | a backup failed |

Thresholds and enabled types: ⚙ settings → Alerts (`config.yaml`, section `alerts`). History is kept for 90 days.
Alert titles and messages use the language selected in settings.

## Home Assistant

### Webhook (alerts and client events)

Set the webhook URL in ⚙ settings, e.g. `http://homeassistant.local:8123/api/webhook/mktk-map`.
Client events: `{"ts", "type", "mac", "detail": {...}}` with `type` = `new_client`, `client_gone`, `client_back`, `roaming`
(each can be switched on/off). Alerts are sent to the same URL:

```json
{"type":"alert","alert_type":"device_offline","state":"fired","severity":"critical",
 "device":"router","title":"Device unreachable: router","message":"...","detail":{},"ts":1760474000}
```

Example automation:

```yaml
alias: mktk-map alerts
trigger:
  - platform: webhook
    webhook_id: mktk-map
    local_only: true
condition:
  - "{{ trigger.json.type == 'alert' }}"
action:
  - service: notify.mobile_app_my_phone
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data:
        tag: "mktk-{{ trigger.json.alert_type }}-{{ trigger.json.device }}"
```

### Presence with an API token

Create a token in ⚙ settings → API tokens (it is shown only once; the database keeps only its SHA-256 hash).
Tokens are read-only (GET only) and cannot manage tokens. Revoking a token blocks everything that uses it immediately.

By default a token can read only the endpoints Home Assistant needs: `/api/snapshot`, `/api/presence/…`,
`/api/alerts` and `/api/events`; anything else returns 403. To allow more, list them in `config.yaml`
(a trailing `/` means a prefix; `/api/tokens` is never allowed):

```yaml
api_token_paths:
  - /api/snapshot
  - /api/presence/
  - /api/alerts
  - /api/events
  - /api/topology
```

```yaml
binary_sensor:
  - platform: rest
    name: phone_home
    resource: http://<server>:8080/api/presence/AA:BB:CC:DD:EE:FF
    headers:
      Authorization: "Bearer mktk_..."
    value_template: "{{ value_json.online }}"
    device_class: presence
    scan_interval: 60
```

The header `X-API-Token: mktk_...` works as well.

## API

All endpoints need a session or an API token (GET only, and only the endpoints in `api_token_paths`).

| Endpoint | Description |
|---|---|
| `GET /api/snapshot` | full state: devices, clients, events |
| `GET /api/presence/{mac}` | presence (`online` = seen in the last 3 poll intervals) |
| `GET /api/clients/{mac}/detail?hours=24` | signal/traffic history and roaming |
| `GET /api/events?hours=24` | events |
| `GET /api/topology` | topology nodes and links |
| `GET /api/alerts` | active and recent alerts |
| `GET /api/devices/{name}/history?hours=24` | device charts data |
| `GET /api/wifi` | Wi-Fi overview |
| `GET /api/connections`, `GET /api/dns` | connections and DNS analysis |
| `GET /api/backups`, `GET /api/backups/{id}/diff`, `GET /api/backups/{id}/download` | backups |
| `GET /api/i18n` | UI texts for the selected language (public) |

## config.yaml reference

```yaml
language: en                 # UI and alert language (en, cs, or any file in app/locales)
dhcp_source: router          # device whose DHCP leases give IPs and hostnames

devices:
- name: router               # name shown in the UI
  host: 192.168.88.1         # IP or hostname (API-SSL port 8729)
  role: router               # router | switch | ap
  ignore_interfaces: []      # trunk/uplink ports that are not client ports
  uplink_interface: ether1   # optional: interface for the traffic chart

webhook:
  url: http://homeassistant.local:8123/api/webhook/mktk-map
  notify_new: true           # client events to send
  notify_gone: false
  notify_back: false
  notify_roaming: false
  gone_after_min: 10         # a client is "gone" after this many minutes

alerts:                      # all optional, defaults shown
  offline_after_min: 3
  cpu_threshold: 85
  cpu_polls: 3
  temp_threshold: 70
  radio_down_min: 10
  cooldown_min: 30
  iot_baseline_days: 3
  enabled: {device_offline: true, cpu_high: true}   # per type

ieee802154:
  thread: [25]
  zigbee: [15]

iot:                         # all optional, defaults shown
  router: null               # device with the firewall (default: dhcp_source)
  subnet: 192.168.40.0/24
  dhcp_server: dhcp_iot
  allow_list: iot_wan_allow
  temp_list: iot_temp_wan
  drop_prefix: IOT-DROP
  dns_forward: 1.1.1.1
  rule_drop: IoT drop all other (log)
  rule_temp: IoT temporary full internet

retention:
  stats_hourly_days: 7       # hourly connection and DNS details
  dns_daily_days: 90         # daily DNS summary (domain × client × count)
```

Other retention: client samples 7 days, events 30 days, alerts 90 days, device charts 8 days.
Environment variables in `docker-compose.yml`: `POLL_INTERVAL` (30 s), `DNS_SYSLOG_PORT` (5514), `SYSLOG_TARGET`, `PUBLIC_URL`.
