# Řešení problémů

[English](../en/troubleshooting.md) · **Čeština** · [← README](../../README.cs.md)

Začněte logem: `docker compose logs --tail 100 mktk-map`.

## Kontejnery se nespustí

- V logu je `... not set - refusing to start`: chybí `PROXY_SECRET`, `APP_USER` nebo `APP_PASS_HASH`.
  `MKTK_DEV=1` tuto kontrolu vypne, jen pro vývoj.
- `Permission denied` u `config.yaml` nebo `/data`: aplikace běží jako uid 10001 a entrypoint při startu změní
  vlastníka `config.yaml` a datového volume. Na hostiteli pak `config.yaml` patří uid 10001; upravujte ho přes
  `sudo`, nebo mu nechte práva pro čtení i zápis.
- `required variable ... is missing a value` / `set in .env` — v `.env` je prázdná proměnná. Povinné jsou
  `MIKROTIK_USER`, `MIKROTIK_PASS`, `APP_USER`, `APP_PASS_HASH`, `PROXY_SECRET` a `MKTK_HOST`.
- `config.yaml` je adresář / `Is a directory` — Compose se spustil dřív, než soubor existoval, a Docker místo něj
  vytvořil adresář. Smažte ho a spusťte `cp config.example.yaml config.yaml`.
- Port 8443, 8080 nebo 5514 je obsazený — změňte levou stranu mapování portů v `docker-compose.yml`.

## UI nejde otevřít / prohlížeč hlásí chybu SSL

- Používejte `https://<MKTK_HOST>:8443`, ne port 8080 (8080 přesměruje na HTTPS nebo odpovídá jen na API tokeny).
- `ERR_SSL_PROTOCOL_ERROR`: `MKTK_HOST` v `.env` musí být přesně ta IP/jméno, které píšete do prohlížeče.
  Po změně spusťte `docker compose up -d`.
- Varování o certifikátu je normální, dokud nenaimportujete kořenovou CA Caddy (viz [První spuštění](prvni-spusteni.md#4-první-spuštění)).

## Nejde se přihlásit

- „příliš mnoho neúspěšných přihlášení“: po 10 chybných pokusech z vaší adresy je přihlášení z ní na 15 minut
  zablokované; počkejte, nebo restartujte kontejner (`docker compose restart mktk-map`).
- „špatné jméno nebo heslo“: zkontrolujte `APP_USER` a znovu vygenerujte `APP_PASS_HASH` příkazem
  `docker compose run --rm --no-deps mktk-map python auth.py`, pak `docker compose up -d`.
- V logu je `APP_USER / APP_PASS_HASH are not set` — `.env` se nenačítá; spouštějte Compose ze složky projektu.

## Zařízení je červené („nedostupné“)

- `timed out` / `Connection refused` — služba `api-ssl` je vypnutá, ze serveru nedostupná nebo omezená
  parametrem `address=` na jinou IP. Zkontrolujte `/ip service print` na zařízení a firewall.
- `invalid user name or password` — zkontrolujte `MIKROTIK_USER` / `MIKROTIK_PASS` a `address=` u uživatele.
- `zařízení neposlalo certifikát` — `api-ssl` nemá nastavený certifikát (krok 2 v návodu První spuštění).
- `TLS certifikát ... se změnil` — certifikát zařízení je jiný než při prvním spojení (reset, nový certifikát).
  Pokud víte proč, klikněte na kartě zařízení na *důvěřovat novému certifikátu*.
- `další pokus za N s` — po neúspěšných spojeních mktk-map čeká (až 5 min), aby zařízení nezahltil přihlašováním.
- *chybí oprávnění: ...* na kartě — monitorovací skupině chybí `read`/`api`; uvedené cesty nejdou číst.

## Klienti nemají IP ani jméno

- V ⚙ nastavení zvolte správný **zdroj DHCP leases** (zařízení, na kterém běží DHCP server).
- Zařízení s pevnou IP dostanou IP z ARP; jméno je pak prázdné — doplňte komentář k DHCP leasu nebo ARP záznamu.

## Klienti na kabelu jsou na špatném portu / všichni na jednom portu

Trunk/uplink porty switche přidejte do *ignore interfaces* daného zařízení. Jinak se mu přiřadí každá MAC,
která trunkem prochází.

## Topologie neukazuje žádná propojení

- Musí být zapnuté neighbor discovery: `/ip neighbor discovery-settings set discover-interface-list=LAN`
  (seznam rozhraní musí obsahovat porty mezi zařízeními).
- Kontrola na zařízení: `/ip neighbor print` musí vypsat ostatní zařízení.
- Počítá se jen fyzický port: soused viditelný jen na VLAN nebo bridge rozhraní (`vlan_internal`) se jako kabel
  nenakreslí. Fyzické porty musí být v seznamu rozhraní pro discovery na **obou** koncích.
- Data se obnovují každých 5 minut (pomalý poll). Restart kontejneru je načte hned.
- Propojení přes neřízené switche, které neumí LLDP/MNDP, se zobrazí jako přímé propojení řízených zařízení.

## Zálohy selhávají

- `SSH přihlášení selhalo` / `účet nemá oprávnění pro export` — monitorovací skupina potřebuje policy `ssh`:
  na `/backups` použijte *povolit SSH pro monitor* a povolte SSH ze serveru (`/ip service set ssh address=...`).
- `SSH klíč ... se změnil` — zařízení bylo resetováno nebo přeinstalováno; potvrďte *důvěřovat novému klíči*.

## Na /connections nejsou DNS dotazy

- Klikněte na *zapnout DNS log na routeru* (admin údaje). Router pak posílá syslog na `SYSLOG_TARGET:5514`.
- `SYSLOG_TARGET` musí být IP serveru, jak ji vidí router; port `5514/udp` musí být publikovaný v `docker-compose.yml`
  a nic ho nesmí blokovat.
- *Pakety přišly z nečekané adresy* — router posílá z IP, která není v seznamu zařízení (např. z jiného rozhraní).
  Jako `host` zařízení použijte IP, kterou router používá směrem k serveru.
- Klienti s DNS over HTTPS/TLS nebo vlastním DNS serverem se neobjeví.

## IoT stránka hlásí „na routeru chybí pravidla“

IoT whitelist pracuje s pravidly firewallu, která na routeru jednou založíte sami. Jejich komentáře musí odpovídat
`rule_drop` a `IoT -> cloud whitelist` (viz [Funkce → IoT whitelist](funkce.md#iot-whitelist)).
Pokud mají vaše pravidla jiné komentáře, nastavte `iot.rule_drop` / `iot.rule_temp` v `config.yaml`.

## Upozornění nechodí do Home Assistanta

- Nastavte URL webhooku a použijte *poslat test* v ⚙ nastavení → Webhook.
- V seznamu upozornění znamená *neodesláno*, že je typ vypnutý nebo není nastavená URL webhooku.
- Automatizace musí používat stejné webhook id jako URL (v příkladech `mktk-map`).

## Home Assistant dostává s API tokenem 403

Endpoint není v `api_token_paths` (viz [Funkce → Přítomnost s API tokenem](funkce.md#přítomnost-s-api-tokenem)).

## Text je ve špatném jazyce

Jazyk je nastavení serveru (⚙ nastavení → Jazyk, `language` v `config.yaml`). Dříve vzniklé záznamy
(upozornění, průběh aktualizací, změny) zůstávají v jazyce, ve kterém byly zapsané.
