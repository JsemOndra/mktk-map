# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-10-26

First public release.

### Added
- Device overview with resources, versions, available updates and per-device history charts.
- Client map (Wi-Fi and wired) by device or VLAN, with DHCP/ARP/IPv6 data, signal and roaming history.
- **Topology** page — automatic map of device interconnections:
  - built from neighbour discovery (MNDP/LLDP/CDP) and CAPsMAN, using the physical port names; neighbours seen
    only over a VLAN/bridge interface are used only as a fallback;
  - devices that announce themselves to nobody are placed from the bridge MAC tables, with an inferred unmanaged
    switch when other wired clients share the port;
  - optionally shows the clients under their device (wired per port, wireless per interface/SSID with signal).
- RouterOS and firmware updates, one device at a time, with backups and a step-by-step log.
- Configuration backups (`/export` over SSH) with version history and diffs.
- Wi-Fi overview with channel/noise/SNR and 2.4 GHz overlap check against Thread/Zigbee.
- Connection tracking and DNS query analysis per client.
- IoT firewall whitelist management with a timed learning mode.
- Alerts and client events sent to a Home Assistant webhook; read-only API tokens.
- **English and Czech UI** with a language setting stored in `config.yaml`; new languages are added as JSON files.
- Demo mode with made-up data (`tools/demo.py`) and a translation check (`tools/check_i18n.py`).

### Security
- API tokens can read only the endpoints Home Assistant needs (`/api/snapshot`, `/api/presence/…`, `/api/alerts`,
  `/api/events`); configurable with `api_token_paths` in `config.yaml`.
- Login is locked for 15 minutes after 10 failed attempts from one address.
- The app refuses to start without `PROXY_SECRET`, `APP_USER` and `APP_PASS_HASH` unless `MKTK_DEV=1`.
- `API_BIND` in `.env` limits the published port 8080 to one address.
- The container runs as an unprivileged user (uid 10001); the entrypoint hands `/data` and `config.yaml` over to it.

### Changed
- Comment names of the IoT drop and learning firewall rules are configurable (`iot.rule_drop`, `iot.rule_temp`).
- New logo.
