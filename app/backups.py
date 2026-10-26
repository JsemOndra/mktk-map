"""MikroTik configuration backups: /export terse over SSH (monitor account, read-only).

- When: every 6 h, right after a change (new entry in /system history — checked
  in the slow poll), before updates and manually from the UI.
- Only a new version is stored (by SHA-256 of the normalised text), compressed in SQLite.
- Normalisation: the header with date and software id is removed so the diff shows only changes.
- The router SSH key is stored on the first connection (TOFU) and verified afterwards.
"""

import difflib
import hashlib
import logging
import queue
import re
import threading
import time
import zlib

import paramiko

import store
from i18n import t

log = logging.getLogger("mktk")

EXPORT_EVERY = 6 * 3600
KEEP_PER_DEVICE = 300
_HEADER_RE = re.compile(r"^# (\d{4}-\d{2}-\d{2}|[a-z]{3}/\d{2}/\d{4}) .*by RouterOS (\S+)", re.I)


class HostKeyMismatch(Exception):
    pass


class _PinPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        fp = "ssh:" + hashlib.sha256(key.asbytes()).hexdigest()
        known = store.pin_get("ssh:" + hostname)
        if known is None:
            store.pin_set("ssh:" + hostname, fp)
            log.warning("SSH: first connection to %s, stored key fingerprint %s", hostname, fp)
        elif known != fp:
            raise HostKeyMismatch(t("srv.bak.key_changed", host=hostname))


def ssh_export(host: str, user: str, pw: str, timeout: float = 20) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(_PinPolicy())
    try:
        c.connect(host, username=user, password=pw, timeout=timeout, banner_timeout=timeout,
                  auth_timeout=timeout, look_for_keys=False, allow_agent=False)
        _in, out, err = c.exec_command("/export terse", timeout=180)
        data = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace").strip()
        if not data.strip():
            raise RuntimeError(t("srv.bak.empty") + (": " + e if e else ""))
        if "not enough permissions" in data or "not enough permissions" in e:
            raise RuntimeError(t("srv.bak.no_permission"))
        return data
    finally:
        c.close()


def normalize(text: str) -> tuple[str, str | None]:
    """Strip the header with date/software id; return (text, ROS version)."""
    ver = None
    lines = []
    for ln in text.replace("\r", "").split("\n"):
        m = _HEADER_RE.match(ln)
        if m:
            ver = m.group(2)
            continue
        if ln.startswith("# software id") or ln.startswith("# serial number"):
            continue
        lines.append(ln.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n", ver


def diff(a: str, b: str, name_a="before", name_b="after", context: int = 2) -> dict:
    al, bl = a.splitlines(), b.splitlines()
    ud = list(difflib.unified_diff(al, bl, name_a, name_b, lineterm="", n=context))
    added = sum(1 for x in ud if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in ud if x.startswith("-") and not x.startswith("---"))
    return {"lines": ud, "added": added, "removed": removed}


def _sections(lines: list[str]) -> list[str]:
    """Which sections (/ip firewall filter...) the diff touches — for a short summary."""
    secs = []
    for x in lines:
        if x[:1] in "+-" and not x.startswith(("+++", "---")):
            m = re.match(r"[+-](/[a-z0-9\- ]+?)(?: add| set| remove|$| \[)", x)
            if m and m.group(1).strip() not in secs:
                secs.append(m.group(1).strip())
    return secs[:8]


class BackupManager:
    def __init__(self, get_devices, get_creds, on_change=None, on_result=None):
        self.get_devices = get_devices         # () -> [dev]
        self.get_creds = get_creds             # () -> (user, pw)
        self.on_change = on_change             # (dev, info) -> None (alerts)
        self.on_result = on_result             # (dev, ok, error) -> None
        self.q: queue.Queue = queue.Queue()
        self.pending: set = set()
        self.last_try: dict[str, float] = {}
        self.status: dict[str, dict] = {}
        self.history_seen: dict[str, str] = {}
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._worker, daemon=True, name="backups").start()
        threading.Thread(target=self._scheduler, daemon=True, name="backups-sched").start()

    def request(self, name: str, reason: str, who: str | None = None):
        with self.lock:
            if name in self.pending:
                return
            self.pending.add(name)
        self.q.put((name, reason, who))

    def history_hook(self, dev: dict, top: dict | None):
        """Called from the slow poll: newest /system history entry changed -> backup."""
        if not top:
            return
        sig = top.get("sig")
        prev = self.history_seen.get(dev["name"])
        self.history_seen[dev["name"]] = sig
        if prev is not None and prev != sig:
            who = "; ".join(f"{c.get('action') or ''} ({c.get('by')})" if c.get("by") else (c.get("action") or "")
                            for c in top.get("changes") or [])
            self.request(dev["name"], "change", who or None)

    def _scheduler(self):
        time.sleep(40)
        while True:
            for d in self.get_devices():
                last = store.backup_last(d["name"])
                due = (not last) or time.time() - last["checked"] > EXPORT_EVERY
                if due and time.time() - self.last_try.get(d["name"], 0) > 600:
                    self.request(d["name"], "schedule")
            time.sleep(60)

    def _worker(self):
        while True:
            name, reason, who = self.q.get()
            with self.lock:
                self.pending.discard(name)
            dev = next((d for d in self.get_devices() if d["name"] == name), None)
            if dev:
                self.run(dev, reason, who)

    def run(self, dev: dict, reason: str, who: str | None = None) -> dict:
        self.last_try[dev["name"]] = time.time()
        user, pw = self.get_creds()
        t0 = time.time()
        try:
            raw = ssh_export(dev["host"], user, pw)
        except Exception as e:  # noqa: BLE001
            msg = str(e) or e.__class__.__name__
            if isinstance(e, paramiko.AuthenticationException):
                msg = t("srv.bak.ssh_auth")
            self.status[dev["name"]] = {"ok": False, "error": msg, "ts": time.time()}
            log.info("backup %s failed: %s", dev["name"], msg)
            if self.on_result:
                self.on_result(dev, False, msg)
            return self.status[dev["name"]]
        text, ver = normalize(raw)
        sha = hashlib.sha256(text.encode()).hexdigest()
        last = store.backup_last(dev["name"])
        info = {"ok": True, "ts": time.time(), "ms": int((time.time() - t0) * 1000),
                "changed": False, "error": None}
        if last and last["sha"] == sha:
            store.backup_touch(last["id"])
        else:
            d = diff(store.backup_text(last["id"]), text) if last else None
            summary = None
            if d:
                summary = {"added": d["added"], "removed": d["removed"],
                           "sections": _sections(d["lines"])}
            bid = store.backup_add(dev["name"], text, sha, ver, reason, who, summary)
            store.backup_prune(dev["name"], KEEP_PER_DEVICE)
            info.update(changed=bool(last), id=bid, summary=summary)
            if last and self.on_change:
                try:
                    self.on_change(dev, {"id": bid, "reason": reason, "who": who, **(summary or {})})
                except Exception:  # noqa: BLE001
                    log.exception("on_change")
        self.status[dev["name"]] = info
        if self.on_result:
            self.on_result(dev, True, None)
        return info


def pack(text: str) -> bytes:
    return zlib.compress(text.encode(), 9)


def unpack(blob: bytes) -> str:
    return zlib.decompress(blob).decode()
