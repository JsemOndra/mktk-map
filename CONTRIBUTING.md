# Contributing

Thanks for your interest in mktk-map! Issues and pull requests are welcome.

## Development setup

```sh
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python tools/demo.py --lang en                    # http://127.0.0.1:8080, demo / demo
```

The demo replaces the MikroTik polling with made-up data, so you can work on the UI without any devices.
To run against real devices, use Docker Compose as described in the [README](README.md).

## Guidelines

- Keep changes focused; one topic per pull request.
- Security problems: do not open a public issue, see [SECURITY.md](SECURITY.md).
- Code and comments are in English. Match the style of the surrounding code (plain FastAPI + vanilla JS, no build step).
- Never commit secrets, real IP addresses, MAC addresses or hostnames — use `.env` / `config.yaml`
  (both ignored by git) and placeholder values such as `192.0.2.x` in examples.
- The monitoring account must stay read-only; anything that changes a device must go through the admin
  credentials entered in the UI and be written to the audit log.

## Translations

All user-visible text lives in `app/locales/<language>.json` (a flat `"key": "text"` map, placeholders as `{name}`).
`en.json` is the reference and the fallback.

- **New text in the UI or backend:** add the key to `en.json` **and** `cs.json`; in HTML use `data-i18n="key"`
  (or `data-i18n-placeholder`, `data-i18n-title`, `data-i18n-html`), in JS `t("key", {param})`, in Python `t("key", param=...)`.
- **Plurals:** use `tn("key", n)` with keys `key.one`, `key.few`, `key.other` (forms follow `Intl.PluralRules`).
- **New language:** copy `en.json` to e.g. `de.json`, set `_meta.name` (shown in the language picker) and
  `_meta.locale` (used for dates and numbers, e.g. `de-DE`), translate the values and restart. It appears in
  ⚙ settings automatically.

Before opening a pull request, run:

```sh
python tools/check_i18n.py
```

The same check runs automatically on every push and pull request (GitHub Actions).
It fails when a key used in the code is missing in a language, when languages have different keys, or when
untranslated Czech text is left in the UI or backend messages.
