# První spuštění

[English](../en/getting-started.md) · **Čeština** · [← README](../../README.cs.md)

Tento návod vás provede od nuly k běžícímu přehledu. Počítejte zhruba s 20 minutami.

## 1. Co budete potřebovat

- Server v lokální síti s Dockerem a Docker Compose v2 (stačí Raspberry Pi 4, NAS nebo jakýkoli Linux).
- Jedno nebo více zařízení MikroTik s RouterOS 7, dostupných ze serveru.
- Admin přístup k zařízením pro jednorázovou přípravu níže.

## 2. Příprava zařízení MikroTik

Na **každém** zařízení spusťte v terminálu (WinBox → New Terminal, nebo SSH) následující příkazy.
`192.168.88.10` nahraďte IP adresou serveru a zvolte silné heslo.

```routeros
# skupina a účet jen pro čtení (ssh je potřeba jen pro zálohy konfigurace)
/user group add name=monitor policy=read,api,test,ssh
/user add name=monitor group=monitor address=192.168.88.10/32 password="ZMEN-MNE"

# API přes TLS se self-signed certifikátem, nešifrované API vypnout
/certificate add name=api-ssl common-name=api-ssl days-valid=3650 key-usage=digital-signature,key-encipherment,tls-server
/certificate sign api-ssl
/ip service set api-ssl certificate=api-ssl address=192.168.88.10/32 disabled=no
/ip service set api disabled=yes

# SSH dostupné ze serveru (kvůli zálohám)
/ip service set ssh address=192.168.88.10/32

# neighbor discovery pro mapu topologie
/ip neighbor discovery-settings set discover-interface-list=LAN
```

Poznámky:

- Podepsání certifikátu může na pomalejších zařízeních trvat několik sekund.
- Když použijete stejné heslo na všech zařízeních, stačí v `.env` jedno `MIKROTIK_PASS`.
- Účet na zařízení nic nezmění. Akce, které něco mění (aktualizace, IoT pravidla, zapnutí DNS logu),
  si pokaždé v UI vyžádají admin údaje.

## 3. Instalace mktk-map

```sh
git clone https://github.com/JsemOndra/mktk-map.git
cd mktk-map
cp .env.example .env
cp config.example.yaml config.yaml
```

Upravte `.env`:

| Proměnná | Hodnota |
|---|---|
| `MIKROTIK_USER` / `MIKROTIK_PASS` | monitorovací účet z kroku 2 |
| `APP_USER` | vaše přihlašovací jméno do webu |
| `APP_PASS_HASH` | viz níže |
| `PROXY_SECRET` | náhodný řetězec, např. `openssl rand -hex 20` |
| `MKTK_HOST` | IP serveru (UI poběží na `https://<MKTK_HOST>:8443`) |

Vygenerujte hash hesla a vypsaný řádek vložte do `.env`:

```sh
docker compose run --rm --no-deps mktk-map python auth.py
```

V `config.yaml` vyplňte svá zařízení. `role` je `router`, `switch` nebo `ap`; `dhcp_source` je zařízení,
na kterém běží DHCP server (obvykle router). Seznam můžete nechat i prázdný a zařízení přidat v UI.

## 4. První spuštění

```sh
docker compose up -d --build
docker compose logs -f mktk-map     # sledování logu ukončíte Ctrl+C
```

Otevřete `https://<MKTK_HOST>:8443`. Prohlížeč upozorní na certifikát — vydal ho Caddy z vlastní lokální CA.
Varování buď potvrďte, nebo jednou naimportujte CA:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./mktk-root.crt
```

a přidejte `mktk-root.crt` mezi důvěryhodné kořenové certifikáty systému/prohlížeče.

Přihlaste se jménem `APP_USER` a svým heslem. Zhruba do 30 sekund karty zařízení zezelenají a objeví se klienti.

## 5. První kroky v UI

1. **⚙ nastavení → Jazyk** — přepnutí mezi angličtinou a češtinou.
2. **⚙ nastavení → Zařízení** — přidání zařízení, před uložením použijte *otestovat spojení*. Pokud se jména liší od
   identity zařízení, *převzít názvy z identity* je přejmenuje (historie zůstane).
3. **Topologie** — zkontrolujte, že mapa odpovídá kabeláži. Pokud nevidíte žádná propojení, viz
   [Řešení problémů](reseni-problemu.md#topologie-neukazuje-žádná-propojení).
4. **Zálohy** — první záloha proběhne do minuty. Pokud selže s chybou SSH, použijte *povolit SSH pro monitor*.
5. **⚙ nastavení → Webhook / Upozornění** — napojení Home Assistanta (viz [Funkce](funkce.md#home-assistant)).

## Aktualizace mktk-map

```sh
git pull
docker compose up -d --build
```

Data (SQLite databáze) jsou v Docker volume `mktk-data` a aktualizaci přežijí.
Verze (čas sestavení) je vidět v pravém dolním rohu přehledu.
