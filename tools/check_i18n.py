"""Translation check: every key used in the code exists in every language,
languages have the same keys, and no Czech text is left in the UI (HTML/JS)
or in backend messages outside the translation files.

Run:  python tools/check_i18n.py   (exit code 1 = something is missing)
"""

import glob
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
LOCALES = os.path.join(ROOT, "locales")

# keys built at runtime (prefix + value from the backend)
DYNAMIC = {
    "idx.ev.": ["new_client", "client_gone", "client_back", "roaming"],
    "idx.al.sev.": ["critical", "warning", "info"],
    "alert.type.": ["device_offline", "capsman_missing", "radio_down", "pppoe_reconnect", "cpu_high",
                    "temp_high", "config_changed", "iot_new_domain", "update_available", "backup_failed"],
    "job.action.": ["check", "install", "firmware"],
    "job.status.": ["running", "ok", "failed", "warning", "interrupted"],
    "job.state.": ["queued", "connecting", "before", "check", "current", "backup", "download",
                   "downloaded", "reboot", "down", "waiting", "online", "verified", "capsman",
                   "fw_available", "firmware", "done", "done_warning", "error", "interrupted"],
    "bak.reason.": ["schedule", "change", "manual", "pre_update", "ssh_enabled"],
    "topo.role.": ["router", "switch", "ap", "external"],
    "topo.kind.": ["wired", "wireless", "capsman", "l2", "fdb"],
    "title.": ["index", "login", "updates", "connections", "dns", "iot", "backups", "wifi", "topology"],
}
PLURAL_FORMS = {"en": ["one", "other"], "cs": ["one", "few", "other"]}
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]")


def used_keys():
    keys, plurals = set(), set()
    for f in glob.glob(os.path.join(ROOT, "**", "*.*"), recursive=True):
        if not f.endswith((".py", ".html", ".js")):
            continue
        s = open(f, encoding="utf-8").read()
        keys |= set(re.findall(r"""\bt\(\s*["']([A-Za-z0-9_.]+)["']""", s))
        keys |= set(re.findall(r"""data-i18n(?:-html|-placeholder|-title)?="([A-Za-z0-9_.]+)\"""", s))
        plurals |= set(re.findall(r"""\btn\(\s*["']([A-Za-z0-9_.]+)["']""", s))
    keys = {k for k in keys if not k.endswith(".")}      # t("prefix." + x) = prefix only
    for prefix, vals in DYNAMIC.items():
        keys |= {prefix + v for v in vals}
    return keys, plurals


def czech_leftovers():
    out = []
    for f in glob.glob(os.path.join(ROOT, "static", "*.*")):
        if not f.endswith((".html", ".js")):
            continue
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            code = re.sub(r"/\*.*?\*/|//.*$", "", line)
            # allowed: recognising older Czech DB records (regexes and CSS classes)
            code = re.sub(r"/[^/\n]*(oprávn|klíč)[^/\n]*/", "", code)
            code = code.replace(".log .s.ověřeno", "")
            if CZECH.search(code):
                out.append(f"{os.path.relpath(f, ROOT)}:{i}: {line.strip()[:100]}")
    for f in glob.glob(os.path.join(ROOT, "*.py")):
        src = open(f, encoding="utf-8").read()
        for m in re.finditer(r"""(?<!")("|')(?:(?!\1).)*?\1""", src):
            s = m.group(0)
            if CZECH.search(s):
                line = src.count("\n", 0, m.start()) + 1
                out.append(f"{os.path.relpath(f, ROOT)}:{line}: {s[:100]}")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    langs = {os.path.basename(f)[:-5]: json.load(open(f, encoding="utf-8"))
             for f in sorted(glob.glob(os.path.join(LOCALES, "*.json")))}
    keys, plurals = used_keys()
    errors = []
    for code, msgs in langs.items():
        forms = PLURAL_FORMS.get(code, ["one", "other"])
        need = keys | {f"{p}.{f}" for p in plurals for f in forms} | {"_meta.name", "_meta.locale"}
        for k in sorted(need - set(msgs)):
            errors.append(f"[{code}] missing: {k}")
    base = set(langs.get("en", {}))
    for code, msgs in langs.items():
        if code == "en":
            continue
        extra = {k for k in set(msgs) - base if not re.search(r"\.(few|many|zero|two)$", k)}
        for k in sorted(extra):
            errors.append(f"[{code}] key not in en.json: {k}")
    unused = base - keys - {f"{p}.{f}" for p in plurals for f in ("one", "other")} - {"_meta.name", "_meta.locale"}
    for k in sorted(unused):
        errors.append(f"[en] unused key: {k}")
    for c in czech_leftovers():
        errors.append("untranslated Czech text: " + c)
    print("\n".join(errors) or f"OK - {len(keys)} keys, {len(plurals)} plural keys, languages: {', '.join(langs)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
