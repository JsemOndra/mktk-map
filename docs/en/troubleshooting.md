# Troubleshooting

**English** · [Čeština](../cs/reseni-problemu.md) · [← README](../../README.md)

Start with the log: `docker compose logs --tail 100 mktk-map`.

## The containers do not start

- The log says `... not set - refusing to start`: `PROXY_SECRET`, `APP_USER` or `APP_PASS_HASH` is empty.
  `MKTK_DEV=1` skips this check, for development only.
- `Permission denied` on `config.yaml` or `/data`: the app runs as uid 10001 and the entrypoint changes the owner
  of `config.yaml` and the data volume at start. On the host `config.yaml` is then owned by uid 10001; edit it
  with `sudo` or keep it readable/writable for that uid.
- `required variable ... is missing a value` / `set in .env` — a variable in `.env` is empty. All of
  `MIKROTIK_USER`, `MIKROTIK_PASS`, `APP_USER`, `APP_PASS_HASH`, `PROXY_SECRET` and `MKTK_HOST` are required.
- `config.yaml` is a directory / `Is a directory` — you started Compose before creating the file, so Docker created a
  directory instead. Remove it and run `cp config.example.yaml config.yaml`.
- Port 8443, 8080 or 5514 already in use — change the left side of the port mapping in `docker-compose.yml`.

## I cannot open the UI / the browser shows an SSL error

- Use `https://<MKTK_HOST>:8443`, not port 8080 (8080 redirects to HTTPS or answers only API tokens).
- `ERR_SSL_PROTOCOL_ERROR`: `MKTK_HOST` in `.env` must be exactly the IP/hostname you type in the browser.
  After changing it run `docker compose up -d`.
- The certificate warning is expected until you import Caddy's root CA (see [Getting started](getting-started.md#4-first-run)).

## I cannot sign in

- "too many failed logins": 10 failed attempts from your address lock it for 15 minutes; wait, or restart the
  container (`docker compose restart mktk-map`).
- "wrong username or password": check `APP_USER` and regenerate `APP_PASS_HASH` with
  `docker compose run --rm --no-deps mktk-map python auth.py`, then `docker compose up -d`.
- The log says `APP_USER / APP_PASS_HASH are not set` — `.env` is not being read; run Compose from the project folder.

## A device is red ("unreachable")

- `timed out` / `Connection refused` — the `api-ssl` service is off, not reachable from the server, or limited by
  `address=` to another IP. Check `/ip service print` on the device and the firewall.
- `invalid user name or password` — check `MIKROTIK_USER` / `MIKROTIK_PASS` and the `address=` of the user.
- `the device did not send a certificate` — `api-ssl` has no certificate set (step 2 of the getting-started guide).
- `the TLS certificate ... has changed` — the device certificate is different from the first connection
  (reset, new certificate). If you know why, click *trust the new certificate* on the device card.
- `next attempt in N s` — after failed connections mktk-map waits (up to 5 min) to avoid flooding the device with logins.
- *missing permissions: ...* on a card — the monitoring group lacks `read`/`api`; the listed paths cannot be read.

## Clients have no IP or hostname

- Set the correct **DHCP lease source** in ⚙ settings (the device running the DHCP server).
- Devices with a static IP get their IP from ARP; the hostname is then empty — add a comment to the DHCP lease or ARP entry.

## Wired clients appear on the wrong port / everything appears on one port

Add the trunk/uplink ports of the switch to *ignore interfaces* of that device. Otherwise every MAC that
passes through the trunk is attributed to it.

## The topology shows no links

- Neighbour discovery must be enabled: `/ip neighbor discovery-settings set discover-interface-list=LAN`
  (the interface list must contain the ports between the devices).
- Check on the device: `/ip neighbor print` must list the other devices.
- Only the physical port counts: a neighbour seen only on a VLAN or bridge interface (`vlan_internal`) is not drawn
  as a cable. Include the physical ports in the discovery interface list on **both** ends.
- The data is refreshed every 5 minutes (slow poll). Restarting the container reloads it immediately.
- Links through unmanaged switches that do not speak LLDP/MNDP appear as a direct link between the managed devices.

## Backups fail

- `SSH login failed` / `no permission to export` — the monitoring group needs the `ssh` policy:
  use *allow SSH for monitor* on `/backups`, and allow SSH from the server (`/ip service set ssh address=...`).
- `the SSH key ... has changed` — the device was reset or reinstalled; confirm with *trust the new key*.

## No DNS queries on /connections

- Click *enable DNS log on the router* (admin credentials). The router then sends syslog to `SYSLOG_TARGET:5514`.
- `SYSLOG_TARGET` must be the server IP as the router sees it; port `5514/udp` must be published in `docker-compose.yml`
  and not blocked.
- *Packets came from an unexpected address* — the router sends from an IP that is not in your device list
  (e.g. another interface). Use the IP the router uses towards the server as the device `host`.
- Clients using DNS over HTTPS/TLS or their own DNS server do not show up.

## The IoT page shows "the router is missing the rules"

The IoT whitelist works with firewall rules you create once on the router. Their comments must match
`rule_drop` and `IoT -> cloud whitelist` (see [Features → IoT whitelist](features.md#iot-whitelist)).
If your rules have different comments, set `iot.rule_drop` / `iot.rule_temp` in `config.yaml`.

## Alerts do not arrive in Home Assistant

- Set the webhook URL and use *send test* in ⚙ settings → Webhook.
- In the alert list, *not sent* means the alert type is disabled or no webhook URL is set.
- The automation must use the same webhook id as the URL (`mktk-map` in the examples).

## Home Assistant gets 403 with an API token

The endpoint is not in `api_token_paths` (see [Features → Presence with an API token](features.md#presence-with-an-api-token)).

## Text is in the wrong language

The language is a server setting (⚙ settings → Language, `language` in `config.yaml`). Entries created earlier
(alerts, update logs, audit) keep the language they were written in.
