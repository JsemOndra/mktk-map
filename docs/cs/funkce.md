# Funkce

[English](../en/features.md) · **Čeština** · [← README](../../README.cs.md)

- [Přehled](#přehled)
- [Topologie](#topologie)
- [Aktualizace](#aktualizace)
- [Zálohy konfigurace](#zálohy-konfigurace)
- [Wi-Fi](#wi-fi)
- [Spojení a DNS](#spojení-a-dns)
- [IoT whitelist](#iot-whitelist)
- [Upozornění](#upozornění)
- [Home Assistant](#home-assistant)
- [API](#api)
- [Přehled config.yaml](#přehled-configyaml)

## Přehled

Hlavní stránka (`/`) ukazuje:

- **Zařízení** — karta pro každé zařízení s uptime, verzí RouterOS, CPU/RAM/diskem, počtem klientů a stářím
  API spojení. Štítek upozorní na novější RouterOS nebo firmware. *historie* otevře grafy za 24 h / 7 dní:
  CPU, RAM, teplota, provoz na uplinku, klienti a doba dotazování; výpadky jsou podbarvené.
  Uplink je PPPoE na routeru, jinak `ether1`; u každého zařízení ho lze přepsat (*uplink iface*).
- **Mapa klientů** — klienti podle zařízení nebo podle VLAN. Wi-Fi klienti se přiřadí k AP, na kterém jsou
  registrovaní (i přes CAPsMAN), klienti na kabelu se berou z bridge host tabulky switchů.
  IP a jméno pochází z DHCP leases zařízení `dhcp_source`; klienti s pevnou IP dostanou adresu z ARP.
  Kliknutím na klienta se zobrazí detail, grafy signálu a provozu za 24 h a historie roamingu.
  Červená IP = dynamický lease, modrá = statický. Leasy bez živého klienta jsou ve sloupci *jen DHCP lease*.
- **Filtr** — fulltext přes hostname, IP, MAC, výrobce, komentář a SSID.
- **Upozornění** a **Události** (nový klient, zmizel, vrátil se, roaming) za posledních 24 h.
- **⚙ nastavení** — jazyk, zařízení, zdroj DHCP leases, API tokeny, kanály Thread/Zigbee, upozornění, webhook.

Výrobce se dohledává offline z databáze IEEE OUI.

## Topologie

Stránka `/topology` kreslí, jak jsou zařízení propojená. Skládá se z tabulek sousedů
(`/ip neighbor` — MNDP, LLDP, CDP), které každé zařízení hlásí v pomalém pollu (každých 5 minut):

- Každá čára je propojení s názvy portů na obou koncích. Plná = kabel, čárkovaná = bezdrátově,
  tečkovaná = AP je známé jen z CAPsMANu (jeho fyzické propojení nebylo vidět), oranžová tečkovaná = zařízení
  bylo vidět jen přes VLAN rozhraní, takže jeho fyzický port není známý.
- MNDP prochází přes bridge, takže zařízení vidí i sousedy o několik skoků dál. Takové nepřímé vazby se
  odfiltrují: vazba A–C se zahodí, když A vidí B na stejném portu a B vidí C na jiném portu.
- Zařízení, která se ohlašují, ale nejsou v konfiguraci (jiné switche, telefony s LLDP…), mají čárkovaný rámeček.
- Zařízení, které se nikomu neohlašuje (např. za neřízeným switchem), se umístí podle tabulek MAC adres bridge:
  na port, kde jiné zařízení vidí jeho MAC (čárkovaná čára). Pokud jsou na stejném portu i další klienti na kabelu,
  nakreslí se mezi nimi **neřízený switch** a tito klienti se přesunou pod něj. Pojmenuje se podle klienta, jehož
  jméno z DHCP vypadá jako switch (např. `TL-SG108PE-…`), jinak jen *neřízený switch*.
- Strom se kreslí od routeru (role `router`, jinak první switch).
- **Zobrazit klienty** (vpravo nahoře, prohlížeč si volbu pamatuje) přidá klienty pod zařízení, ke kterému jsou
  připojení: u každého zařízení jeden box *kabel* s portem každého klienta a jeden box na každé bezdrátové rozhraní
  s SSID a klienty seřazenými podle signálu. V boxu je nejvýš 10 klientů, po najetí myší je vidět IP, MAC a výrobce.

Předpoklad: zapnuté neighbor discovery na rozhraních zařízení
(`/ip neighbor discovery-settings set discover-interface-list=LAN`).

## Aktualizace

`/updates` (jen s přihlášením do webu, ne s API tokenem):

1. Zadejte admin jméno a heslo k zařízením. Ověří se přihlášením na všechna zařízení a drží se
   **jen v paměti serveru 30 minut** (svázané s vaší session), pak se zahodí.
2. *zkontrolovat* spustí `check-for-updates`; *instalovat* nainstaluje nový RouterOS; *firmware* povýší firmware RouterBOARDu.
3. Vždy běží jen jedna úloha. Instalace: připojení → kontrola → záloha na zařízení
   (`pre-<verze>-<datum>.backup` + `.rsc`) i v mktk-map → stažení → restart → čekání na návrat (max 15 min)
   → ověření verze → u AP čekání na návrat do CAPsMANu.
4. Každý krok se zapisuje do historie. Úloha přerušená restartem aplikace se označí jako *přerušeno*.

Nejnovější dostupná verze se ověřuje na `upgrade.mikrotik.com` každých 6 hodin podle kanálu zařízení.

Doporučené pořadí: AP po jednom, router nakonec (jeho restart shodí celou síť).

## Zálohy konfigurace

`/backups`:

- `/export terse` přes SSH monitorovacím účtem (export neobsahuje hesla).
- Kdy: každých 6 h, do 5 min po změně (nová položka v `/system history`), před aktualizací a ručně.
- Ukládá se jen změněná konfigurace (porovnává se bez hlavičky s datem a sériovým číslem), komprimovaně,
  max. 300 verzí na zařízení.
- U každé verze je souhrn (+/− řádků, dotčené sekce), rozdíl proti předchozí a stažení `.rsc`.
- Skupina monitor potřebuje policy `ssh` — tlačítko *povolit SSH pro monitor* ji přidá na všech zařízeních
  (potřeba admin údajů); `write` zůstává vypnuté.
- SSH klíč zařízení se při prvním spojení zapamatuje; při změně se záloha odmítne, dokud změnu nepotvrdíte.

## Wi-Fi

`/wifi`:

- **Rádia** — pásmo, kanál, šířka, výkon a SSID z CAPsMAN kontroleru, šum z AP, průměrný/minimální signál klientů a SNR.
- **2,4 GHz vs. Thread/Zigbee** — kanály 802.15.4 (11–26) vykreslené přes kanály 2,4 GHz Wi-Fi.
  Kanály Threadu a Zigbee nastavte v ⚙ nastavení a uvidíte, zda se překrývají.
- **Slabí klienti** — klienti se signálem −75 dBm a horším (často „přilepení“ ke vzdálenému AP).

## Spojení a DNS

`/connections`:

- Čte `/ip/firewall/connection` (+ IPv6) z routeru každých 10 s, když je stránka otevřená, jinak každých 30 s.
- U každého spojení: klient, cíl s DNS jménem (z DNS cache routeru; přednost má jméno, na které se klient sám ptal),
  port a odhad služby, stav, trvání, přenesená data a rychlost.
- Ukončená spojení zůstávají vidět 5 minut; hodinové statistiky (nejčastější cíle, klienti podle dat) se drží 7 dní.
- Omezení: spojení kratší než interval pollu se nemusí zachytit; u FastTrack spojení (štítek *FT*) RouterOS nemusí počítat bajty.

**DNS dotazy:** router může posílat téma logu `dns` přes syslog (UDP 5514) do mktk-map. Tlačítko na `/connections`
(admin údaje) vytvoří `/system logging action mktkdns` a pravidlo `topics=dns`. Přijímají se jen pakety od zařízení
z konfigurace. Klienti s DoH/DoT nebo vlastním DNS serverem se neobjeví.
`SYSLOG_TARGET` v `docker-compose.yml` musí být IP serveru, jak ji vidí router.

`/dns` ukazuje všechny dotazy za zvolené období (1 h – 90 dní): domény podle počtu dotazů, kteří klienti se ptali,
typy záznamů, kdy naposledy a štítek *nová* u domén poprvé viděných za posledních 24 h. Kliknutím na klienta se filtruje.

## IoT whitelist

`/iot` spravuje whitelist ve firewallu pro IoT síť, která má internet ve výchozím stavu zakázaný.
Na routeru očekává tyto objekty (založte je jednou sami; názvy jdou změnit v `config.yaml`, sekce `iot`):

| Objekt | Výchozí název |
|---|---|
| IoT podsíť | `192.168.40.0/24` (`subnet`) |
| address-list povolených cílů | `iot_wan_allow` (`allow_list`) |
| `/ip firewall filter` accept pravidlo IoT → internet pro `allow_list` | komentář `IoT -> cloud whitelist` |
| závěrečné drop pravidlo s logováním, `log-prefix=IOT-DROP` | komentář `IoT drop all other (log)` (`rule_drop`) |

Co lze dělat:

- Zobrazit **zahozené pokusy** (z logu routeru) s DNS jmény cílů a jedním kliknutím povolit doménu, IP nebo port.
- Spravovat **domény** (záznamy `/ip dns static` typu FWD, které plní address-list, když se na ně zařízení zeptá),
  **pevné IP** a **pravidla portů** (`IoT -> cloud <značka> tcp|udp`).
- **Učit**: zařízení dostane na 10–60 minut plný internet (address-list s timeoutem na routeru jako pojistka) a zapne
  se logování DNS; stránka ukazuje jeho dotazy a spojení a navrhne domény k povolení.

Čtení jde přes monitorovací účet; změny vyžadují admin údaje (stejně jako aktualizace) a každá změna se zapíše
do tabulky *Změny*. Komentáře na routeru jsou jen ASCII (diakritika se odstraní).

## Upozornění

Pravidla se vyhodnocují po každém pollu. Stavová upozornění se pošlou při vzniku a při vyřešení; události nejvýš
jednou za cooldown (30 min).

| Typ | Kdy |
|---|---|
| `device_offline` | zařízení nedostupné ≥ 3 min |
| `capsman_missing` | AP není připojené ke CAPsMANu |
| `radio_down` | rádio na kontroleru neběží ≥ 10 min |
| `pppoe_reconnect` | PPPoE spadlo a znovu se připojilo |
| `cpu_high` | CPU ≥ 85 % ve 3 pollech po sobě |
| `temp_high` | teplota ≥ 70 °C |
| `config_changed` | nová verze zálohy (se souhrnem změn) |
| `iot_new_domain` | IoT zařízení s ≥ 3 dny historie se ptá na novou doménu |
| `update_available` | nová verze RouterOS |
| `backup_failed` | záloha selhala |

Prahy a zapnuté typy: ⚙ nastavení → Upozornění (`config.yaml`, sekce `alerts`). Historie se drží 90 dní.
Nadpisy a texty upozornění jsou v jazyce zvoleném v nastavení.

## Home Assistant

### Webhook (upozornění a události klientů)

URL webhooku nastavte v ⚙ nastavení, např. `http://homeassistant.local:8123/api/webhook/mktk-map`.
Události klientů: `{"ts", "type", "mac", "detail": {...}}`, kde `type` = `new_client`, `client_gone`, `client_back`,
`roaming` (každý typ lze zapnout/vypnout). Upozornění chodí na stejnou URL:

```json
{"type":"alert","alert_type":"device_offline","state":"fired","severity":"critical",
 "device":"router","title":"Zařízení nedostupné: router","message":"...","detail":{},"ts":1760474000}
```

Příklad automatizace:

```yaml
alias: mktk-map upozornění
trigger:
  - platform: webhook
    webhook_id: mktk-map
    local_only: true
condition:
  - "{{ trigger.json.type == 'alert' }}"
action:
  - service: notify.mobile_app_muj_telefon
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data:
        tag: "mktk-{{ trigger.json.alert_type }}-{{ trigger.json.device }}"
```

### Přítomnost s API tokenem

Token vytvoříte v ⚙ nastavení → API tokeny (zobrazí se jen jednou; v databázi je jen jeho SHA-256 hash).
Tokeny jsou jen pro čtení (pouze GET) a nemohou spravovat tokeny. Zrušení tokenu okamžitě zablokuje vše, co ho používá.

Ve výchozím stavu token čte jen endpointy, které potřebuje Home Assistant: `/api/snapshot`, `/api/presence/…`,
`/api/alerts` a `/api/events`; ostatní vrátí 403. Další povolíte v `config.yaml`
(`/` na konci znamená prefix; `/api/tokens` nejde povolit nikdy):

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
    name: telefon_doma
    resource: http://<server>:8080/api/presence/AA:BB:CC:DD:EE:FF
    headers:
      Authorization: "Bearer mktk_..."
    value_template: "{{ value_json.online }}"
    device_class: presence
    scan_interval: 60
```

Funguje i hlavička `X-API-Token: mktk_...`.

## API

Všechny endpointy vyžadují session nebo API token (jen GET a jen endpointy z `api_token_paths`).

| Endpoint | Popis |
|---|---|
| `GET /api/snapshot` | celý stav: zařízení, klienti, události |
| `GET /api/presence/{mac}` | přítomnost (`online` = viděn v posledních 3 intervalech pollu) |
| `GET /api/clients/{mac}/detail?hours=24` | historie signálu/provozu a roaming |
| `GET /api/events?hours=24` | události |
| `GET /api/topology` | uzly a propojení topologie |
| `GET /api/alerts` | aktivní a nedávná upozornění |
| `GET /api/devices/{name}/history?hours=24` | data pro grafy zařízení |
| `GET /api/wifi` | přehled Wi-Fi |
| `GET /api/connections`, `GET /api/dns` | spojení a analýza DNS |
| `GET /api/backups`, `GET /api/backups/{id}/diff`, `GET /api/backups/{id}/download` | zálohy |
| `GET /api/i18n` | texty UI ve zvoleném jazyce (veřejné) |

## Přehled config.yaml

Kompletní ukázka všech sekcí s výchozími hodnotami je v [anglické verzi](../en/features.md#configyaml-reference)
(komentáře v konfiguraci jsou anglicky). Nejdůležitější položky:

| Klíč | Význam |
|---|---|
| `language` | jazyk UI a upozornění (`en`, `cs` nebo jiný soubor v `app/locales`) |
| `dhcp_source` | zařízení, jehož DHCP leases dávají IP a jména klientů |
| `devices[]` | `name`, `host`, `role` (`router`/`switch`/`ap`), `ignore_interfaces`, volitelně `uplink_interface` |
| `webhook` | `url`, `notify_new/gone/back/roaming`, `gone_after_min` |
| `alerts` | prahy (`offline_after_min`, `cpu_threshold`, `temp_threshold`, …) a `enabled` po typech |
| `ieee802154` | kanály `thread` a `zigbee` |
| `iot` | `router`, `subnet`, `allow_list`, `temp_list`, `drop_prefix`, `dns_forward`, `rule_drop`, `rule_temp` |
| `retention` | `stats_hourly_days` (7), `dns_daily_days` (90) |

Další uchování dat: vzorky klientů 7 dní, události 30 dní, upozornění 90 dní, grafy zařízení 8 dní.
Proměnné prostředí v `docker-compose.yml`: `POLL_INTERVAL` (30 s), `DNS_SYSLOG_PORT` (5514), `SYSLOG_TARGET`, `PUBLIC_URL`.
