# Getting started

**English** · [Čeština](../cs/prvni-spusteni.md) · [← README](../../README.md)

This guide takes you from nothing to a running dashboard. Allow about 20 minutes.

## 1. What you need

- A server on your LAN with Docker and Docker Compose v2 (a Raspberry Pi 4, a NAS or any Linux box is enough).
- One or more MikroTik devices with RouterOS 7, reachable from the server.
- Admin access to the devices for the one-time preparation below.

## 2. Prepare the MikroTik devices

Run the following in the terminal of **every** device (WinBox → New Terminal, or SSH). Replace
`192.168.88.10` with the IP of your server and choose a strong password.

```routeros
# read-only group and account for monitoring (ssh is needed only for configuration backups)
/user group add name=monitor policy=read,api,test,ssh
/user add name=monitor group=monitor address=192.168.88.10/32 password="CHANGE-ME"

# API over TLS with a self-signed certificate, plain API off
/certificate add name=api-ssl common-name=api-ssl days-valid=3650 key-usage=digital-signature,key-encipherment,tls-server
/certificate sign api-ssl
/ip service set api-ssl certificate=api-ssl address=192.168.88.10/32 disabled=no
/ip service set api disabled=yes

# SSH reachable from the server (for backups)
/ip service set ssh address=192.168.88.10/32

# neighbour discovery for the topology map
/ip neighbor discovery-settings set discover-interface-list=LAN
```

Notes:

- Signing a certificate can take a few seconds on slower devices.
- If you use the same password on all devices, you need only one `MIKROTIK_PASS` in `.env`.
- The account cannot change anything on the device. Actions that do change something (updates, IoT rules,
  enabling the DNS log) ask for admin credentials in the UI each time.

## 3. Install mktk-map

```sh
git clone https://github.com/JsemOndra/mktk-map.git
cd mktk-map
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env`:

| Variable | Value |
|---|---|
| `MIKROTIK_USER` / `MIKROTIK_PASS` | the monitoring account from step 2 |
| `APP_USER` | your login name for the web UI |
| `APP_PASS_HASH` | see below |
| `PROXY_SECRET` | a random string, e.g. `openssl rand -hex 20` |
| `MKTK_HOST` | the IP of the server (the UI will run on `https://<MKTK_HOST>:8443`) |

Generate the password hash and paste the printed line into `.env`:

```sh
docker compose run --rm --no-deps mktk-map python auth.py
```

Edit `config.yaml` and list your devices. `role` is `router`, `switch` or `ap`; `dhcp_source` is the device
that runs the DHCP server (usually the router). You can also leave the list empty and add devices in the UI.

## 4. First run

```sh
docker compose up -d --build
docker compose logs -f mktk-map     # Ctrl+C to stop following the log
```

Open `https://<MKTK_HOST>:8443`. The browser warns about the certificate — Caddy issued it from its own local
CA. Either accept the warning, or import the CA once:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./mktk-root.crt
```

and add `mktk-root.crt` to the trusted root certificates of your OS/browser.

Sign in with `APP_USER` and your password. Within ~30 seconds the device cards turn green and clients appear.

## 5. First steps in the UI

1. **⚙ settings → Language** — switch between English and Czech.
2. **⚙ settings → Devices** — add devices, use *test connection* before saving. If the device names differ from
   the device identities, *use names from identity* renames them (history is kept).
3. **Topology** — check that the map matches your cabling. If you see no links, see
   [Troubleshooting](troubleshooting.md#the-topology-shows-no-links).
4. **Backups** — the first backup runs within a minute. If it fails with an SSH error, use *allow SSH for monitor*.
5. **⚙ settings → Webhook / Alerts** — connect Home Assistant (see [Features](features.md#home-assistant)).

## Updating mktk-map

```sh
git pull
docker compose up -d --build
```

Your data (SQLite database) lives in the Docker volume `mktk-data` and survives updates.
The version (build timestamp) is shown in the bottom right corner of the overview.
