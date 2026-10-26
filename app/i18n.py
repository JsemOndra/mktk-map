"""Translations shared by the backend and the web UI.

Every language is one JSON file in app/locales/<code>.json (flat "key": "text"
map, placeholders as {name}). English (en.json) is the reference and the
fallback for missing keys. Adding a language = adding a file; nothing else.
"""

import json
import logging
import os
import threading

log = logging.getLogger("mktk")

DEFAULT = "en"
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
_lock = threading.Lock()
_cache: dict[str, dict] = {}
_get_lang = lambda: DEFAULT  # noqa: E731 - replaced by set_language_source()


def set_language_source(fn):
    """fn() -> current language code (from config.yaml)."""
    global _get_lang
    _get_lang = fn


def _load(code: str) -> dict:
    with _lock:
        if code not in _cache:
            try:
                with open(os.path.join(_DIR, code + ".json"), encoding="utf-8") as f:
                    _cache[code] = json.load(f)
            except (OSError, ValueError) as e:
                log.error("locale %s cannot be loaded: %s", code, e)
                _cache[code] = {}
        return _cache[code]


def languages() -> list[dict]:
    """[{code, name}] for every locales/*.json file."""
    out = []
    for fn in sorted(os.listdir(_DIR)):
        if fn.endswith(".json"):
            code = fn[:-5]
            out.append({"code": code, "name": _load(code).get("_meta.name", code)})
    return out


def is_valid(code: str) -> bool:
    return any(lang["code"] == code for lang in languages())


def current() -> str:
    code = _get_lang() or DEFAULT
    return code if is_valid(code) else DEFAULT


def messages(code: str | None = None) -> dict:
    """All texts for a language, English filled in for missing keys."""
    code = code or current()
    return {**_load(DEFAULT), **_load(code)}


def t(key: str, **params) -> str:
    code = current()
    text = _load(code).get(key) or _load(DEFAULT).get(key)
    if text is None:
        log.warning("missing translation: %s", key)
        return key
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text
