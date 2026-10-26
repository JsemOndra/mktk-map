"""RouterOS and firmware updates.

- Admin credentials are kept only in memory, bound to the web session,
  for 30 minutes after entry. A running job keeps its own copy until it ends
  (at most the job time limit), then it is discarded.
- Only one job at a time (one device).
- Every job step is written to SQLite (update_jobs / update_job_log),
  so it is visible what ran when and how it ended — even after an app restart.
- The available RouterOS version comes from upgrade.mikrotik.com (no rights on
  the routers needed), firmware from /system/routerboard (read).
"""

import hashlib
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from librouteros.exceptions import MultiTrapError, TrapError

import store
from i18n import t
from mikrotik import API_PORT, close, connect_once, drop_connection

log = logging.getLogger("mktk")

CRED_TTL = 30 * 60
LATEST_REFRESH = 6 * 3600
LATEST_URL = os.environ.get("LATEST_URL",
                            "https://upgrade.mikrotik.com/routeros/NEWESTa7.{channel}")

DOWN_TIMEOUT = 120      # how long to wait for the device to go down after reboot
UP_TIMEOUT = 15 * 60    # how long to wait for it to come back (package install is slow)
CAPSMAN_TIMEOUT = 180   # how long to wait for the AP to return to CAPsMAN


# --- versions -------------------------------------------------------------

def vkey(v: str | None) -> tuple:
    """'7.24.5' / '7.25rc1' / '7.24.5 (stable)' -> comparable tuple."""
    m = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?([a-z]+\d*)?", v or "")
    if not m:
        return ()
    a, b, c, pre = m.groups()
    return (int(a), int(b), int(c or 0), 0 if pre else 1, pre or "")


def short_version(v: str | None) -> str | None:
    return v.split()[0] if v else v


# --- in-memory credentials -------------------------------------------------

class _Vault:
    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict[str, dict] = {}

    @staticmethod
    def _k(session: str) -> str:
        return hashlib.sha256(session.encode()).hexdigest()

    def put(self, session: str, user: str, pw: str):
        with self._lock:
            self._d[self._k(session)] = {
                "user": user, "pw": pw, "set_at": time.time(),
                "expires": time.time() + CRED_TTL}

    def get(self, session: str) -> tuple[str, str] | None:
        self.purge()
        with self._lock:
            e = self._d.get(self._k(session))
            return (e["user"], e["pw"]) if e else None

    def info(self, session: str) -> dict:
        self.purge()
        with self._lock:
            e = self._d.get(self._k(session))
            if not e:
                return {"active": False}
            return {"active": True, "user": e["user"], "set_at": e["set_at"],
                    "expires_at": e["expires"]}

    def clear(self, session: str):
        with self._lock:
            self._d.pop(self._k(session), None)

    def purge(self):
        now = time.time()
        with self._lock:
            for k in [k for k, e in self._d.items() if e["expires"] <= now]:
                del self._d[k]
                log.info("updates: credentials expired and were discarded")


vault = _Vault()


def _vault_janitor():
    while True:
        time.sleep(30)
        vault.purge()


# --- latest version from the internet ------------------------------------

_latest: dict[str, dict] = {}
_latest_lock = threading.Lock()


def refresh_latest(channels: set[str]):
    for ch in sorted(channels or {"stable"}):
        try:
            r = requests.get(LATEST_URL.format(channel=ch), timeout=10)
            r.raise_for_status()
            ver = r.text.split()[0]
            if not vkey(ver):
                raise ValueError(t("srv.upd.unexpected", text=repr(r.text[:40])))
            info = {"version": ver, "fetched_at": time.time(), "error": None}
        except Exception as e:  # noqa: BLE001
            with _latest_lock:
                prev = _latest.get(ch, {})
            info = {**prev, "error": str(e), "fetched_at": time.time()}
            log.warning("version check %s failed: %s", ch, e)
        with _latest_lock:
            _latest[ch] = info


def latest_for(channel: str | None) -> dict:
    with _latest_lock:
        return dict(_latest.get(channel or "stable") or {})


def latest_all() -> dict:
    with _latest_lock:
        return {k: dict(v) for k, v in _latest.items()}


def _latest_loop(get_channels):
    time.sleep(5)
    while True:
        try:
            refresh_latest(get_channels())
        except Exception:  # noqa: BLE001
            log.exception("latest loop")
        time.sleep(LATEST_REFRESH)


def start_background(get_channels):
    threading.Thread(target=_vault_janitor, daemon=True).start()
    threading.Thread(target=_latest_loop, args=(get_channels,), daemon=True).start()


# --- credential verification -----------------------------------------------

def verify_credentials(devices: list[dict], user: str, pw: str) -> list[dict]:
    def one(dev):
        try:
            api = connect_once(dev["host"], user, pw, timeout=8)
        except Exception as e:  # noqa: BLE001
            return {"device": dev["name"], "ok": False, "error": str(e) or e.__class__.__name__}
        try:
            ident = list(api.path("system", "identity"))
            return {"device": dev["name"], "ok": True,
                    "identity": ident[0].get("name") if ident else None}
        except Exception as e:  # noqa: BLE001
            return {"device": dev["name"], "ok": False, "error": str(e)}
        finally:
            close(api)
    if not devices:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(devices))) as ex:
        return list(ex.map(one, devices))


# --- jobs --------------------------------------------------------------------

class JobBusy(Exception):
    pass


ACTIONS = ("check", "install", "firmware")   # names in the translations job.action.<action>

_job_lock = threading.Lock()
_current: dict | None = None
_hooks = {"wake": lambda: None, "capsman_has": lambda ident: None,
          "pre_change": lambda dev, reason: None}


def set_hooks(wake=None, capsman_has=None, pre_change=None):
    if wake:
        _hooks["wake"] = wake
    if capsman_has:
        _hooks["capsman_has"] = capsman_has
    if pre_change:
        _hooks["pre_change"] = pre_change


def current_job() -> dict | None:
    with _job_lock:
        return dict(_current) if _current else None


def start_job(dev: dict, action: str, creds: tuple[str, str], started_by: str) -> int:
    global _current
    if action not in ACTIONS:
        raise ValueError(t("srv.upd.unknown_action"))
    with _job_lock:
        if _current is not None:
            raise JobBusy(t("srv.upd.busy", id=_current['id'], device=_current['device']))
        jid = store.job_create(dev["name"], action, started_by)
        _current = {"id": jid, "device": dev["name"], "action": action}
    store.job_log(jid, "queued", t("srv.upd.queued", action=t("job.action." + action), by=started_by))
    threading.Thread(target=_run, args=(jid, dev, action, creds), daemon=True,
                     name=f"job-{jid}").start()
    return jid


class _StepError(Exception):
    pass


def _run(jid: int, dev: dict, action: str, creds: tuple[str, str]):
    global _current
    user, pw = creds
    job = _Job(jid, dev, user, pw)
    try:
        if action == "check":
            job.do_check()
        elif action == "install":
            job.do_install()
        elif action == "firmware":
            job.do_firmware()
        status = "warning" if job.warnings else "ok"
        store.job_set(jid, status=status, finished=time.time())
        job.log("done" if status == "ok" else "done_warning",
                "; ".join(job.warnings))
    except Exception as e:  # noqa: BLE001
        msg = str(e) or e.__class__.__name__
        log.warning("job #%s (%s) failed: %s", jid, dev["name"], msg)
        store.job_set(jid, status="failed", finished=time.time(), error=msg)
        store.job_log(jid, "error", msg)
    finally:
        job.close()
        del user, pw, creds, job
        with _job_lock:
            _current = None
        drop_connection(dev["name"])   # monitoring reconnects right away (no backoff)
        _hooks["wake"]()


class _Job:
    def __init__(self, jid, dev, user, pw):
        self.jid, self.dev, self.user, self.pw = jid, dev, user, pw
        self.api = None
        self.warnings: list[str] = []
        self.identity = dev["name"]

    # -- helpers
    def log(self, state, msg=""):
        store.job_log(self.jid, state, msg)

    def connect(self, timeout=60):
        self.close()
        self.api = connect_once(self.dev["host"], self.user, self.pw, timeout=timeout)
        return self.api

    def close(self):
        if self.api is not None:
            close(self.api)
            self.api = None

    def read(self, *path) -> dict:
        rows = list(self.api.path(*path))
        return rows[0] if rows else {}

    def info(self) -> dict:
        res = self.read("system", "resource")
        rb = self.read("system", "routerboard")
        upd = self.read("system", "package", "update")
        ident = self.read("system", "identity")
        return {"version": short_version(res.get("version")),
                "uptime": res.get("uptime"),
                "free_hdd": res.get("free-hdd-space"),
                "channel": upd.get("channel"),
                "fw": rb.get("current-firmware"),
                "fw_upgrade": rb.get("upgrade-firmware"),
                "identity": ident.get("name")}

    def tcp_ok(self, timeout=3) -> bool:
        try:
            with socket.create_connection((self.dev["host"], API_PORT), timeout=timeout):
                return True
        except OSError:
            return False

    def reboot(self):
        self.log("reboot", t("srv.upd.reboot_send"))
        try:
            tuple(self.api("/system/reboot"))
        except Exception:  # noqa: BLE001 - the connection normally drops on reboot
            pass
        self.close()
        t0 = time.time()
        while time.time() - t0 < DOWN_TIMEOUT:
            if not self.tcp_ok(timeout=2):
                self.log("down", t("srv.upd.went_down", s=int(time.time() - t0)))
                return
            time.sleep(1)
        raise _StepError(t("srv.upd.not_down", s=DOWN_TIMEOUT))

    def wait_up(self) -> dict:
        self.log("waiting", t("srv.upd.wait_limit", min=UP_TIMEOUT // 60))
        t0 = time.time()
        last_note = t0
        last_err = None
        while time.time() - t0 < UP_TIMEOUT:
            if self.tcp_ok():
                try:
                    self.connect()
                    inf = self.info()
                    self.log("online", t("srv.upd.back", s=int(time.time() - t0),
                                         version=inf['version'], uptime=inf['uptime']))
                    return inf
                except Exception as e:  # noqa: BLE001
                    last_err = str(e)
                    self.close()
            if time.time() - last_note > 60:
                self.log("waiting", f"{int(time.time() - t0)} s"
                         + (" " + t("srv.upd.last_error", error=last_err) if last_err else ""))
                last_note = time.time()
            time.sleep(5)
        raise _StepError(t("srv.upd.not_back", min=UP_TIMEOUT // 60)
                         + (" " + t("srv.upd.last_error", error=last_err) if last_err else ""))

    def wait_capsman(self):
        if self.dev.get("role") != "ap":
            return
        self.log("capsman", t("srv.upd.capsman_wait", identity=self.identity))
        t0 = time.time()
        while time.time() - t0 < CAPSMAN_TIMEOUT:
            has = _hooks["capsman_has"](self.identity)
            if has is None:
                self.warnings.append(t("srv.upd.capsman_unknown"))
                self.log("capsman", t("srv.upd.capsman_cannot"))
                return
            if has:
                self.log("capsman", t("srv.upd.capsman_ok", s=int(time.time() - t0)))
                return
            time.sleep(10)
        self.warnings.append(t("srv.upd.capsman_not_back"))
        self.log("capsman", t("srv.upd.capsman_timeout", s=CAPSMAN_TIMEOUT))

    def check_for_updates(self) -> dict:
        upd = self.api.path("system", "package", "update")
        tuple(upd("check-for-updates"))
        time.sleep(1)
        u = self.read("system", "package", "update")
        return {"channel": u.get("channel"), "installed": u.get("installed-version"),
                "latest": u.get("latest-version"), "status": u.get("status")}

    # -- actions
    def prepare(self) -> dict:
        self.log("connecting", t("srv.upd.connecting", host=self.dev['host'], user=self.user))
        self.connect()
        inf = self.info()
        self.identity = inf.get("identity") or self.dev["name"]
        store.job_set(self.jid, from_version=inf["version"])
        self.log("before", t("srv.upd.before", identity=self.identity, version=inf['version'],
                             channel=inf['channel'], fw=inf['fw'], uptime=inf['uptime']))
        return inf

    def do_check(self):
        self.prepare()
        self.log("check", "check-for-updates")
        u = self.check_for_updates()
        self.log("check", t("srv.upd.check_result", installed=u['installed'], latest=u['latest'],
                            channel=u['channel'], status=u['status']))
        store.job_set(self.jid, to_version=u["latest"])

    def backup(self, version):
        stamp = time.strftime("%Y%m%d-%H%M")
        name = f"pre-{version}-{stamp}"
        self.log("backup", f"/system/backup/save name={name}")
        try:
            tuple(self.api("/system/backup/save", name=name, **{"dont-encrypt": "yes"}))
        except (TrapError, MultiTrapError) as e:
            raise _StepError(t("srv.upd.backup_failed", error=e)) from e
        try:
            tuple(self.api("/export", file=name))
            self.log("backup", t("srv.upd.backup_saved", name=name))
        except Exception as e:  # noqa: BLE001
            self.warnings.append(t("srv.upd.rsc_failed"))
            self.log("backup", t("srv.upd.backup_saved_no_rsc", name=name, error=e))
            self.connect()   # a fresh connection to be safe

    def app_backup(self):
        """Copy of /export into mktk-map too (version history), besides the backup on the device."""
        try:
            info = _hooks["pre_change"](self.dev, "pre_update") or {}
            if info.get("ok"):
                self.log("backup", t("srv.upd.app_backup_ok"))
            else:
                self.warnings.append(t("srv.upd.app_backup_warn"))
                self.log("backup", t("srv.upd.app_backup_failed", error=info.get('error')))
        except Exception as e:  # noqa: BLE001
            self.log("backup", t("srv.upd.app_backup_failed", error=e))

    def do_install(self):
        before = self.prepare()
        self.log("check", "check-for-updates")
        u = self.check_for_updates()
        latest = u.get("latest")
        self.log("check", t("srv.upd.latest", latest=latest, channel=u['channel'], status=u['status']))
        if not latest or not vkey(latest):
            raise _StepError(t("srv.upd.no_new", status=u['status']))
        if vkey(latest) <= vkey(before["version"]):
            self.log("current", t("srv.upd.is_latest", version=before['version']))
            store.job_set(self.jid, to_version=before["version"])
            return
        store.job_set(self.jid, to_version=latest)
        self.backup(before["version"])
        self.app_backup()

        self.log("download", f"RouterOS {latest}")
        # librouteros returns replies only after the command finishes -> the download
        # runs in a thread and its duration is logged as it goes
        upd = self.api.path("system", "package", "update")
        res: dict = {}

        def dl():
            try:
                res["replies"] = list(upd("download"))
            except Exception as e:  # noqa: BLE001
                res["error"] = e
        th = threading.Thread(target=dl, daemon=True)
        t0 = time.time()
        th.start()
        while th.is_alive():
            th.join(15)
            if th.is_alive():
                self.log("download", t("srv.upd.downloading", s=int(time.time() - t0)))
        if "error" in res:
            raise _StepError(t("srv.upd.download_failed", error=res['error']))
        st = self.read("system", "package", "update").get("status") or ""
        self.log("downloaded", st)
        if "download" not in st.lower() and "reboot" not in st.lower():
            raise _StepError(t("srv.upd.download_unconfirmed", status=repr(st)))

        self.reboot()
        after = self.wait_up()
        if vkey(after["version"]) != vkey(latest):
            raise _StepError(t("srv.upd.wrong_version", running=after['version'], expected=latest))
        self.log("verified", f"RouterOS {before['version']} → {after['version']}")
        self.wait_capsman()
        if after.get("fw_upgrade") and after["fw_upgrade"] != after.get("fw"):
            self.log("fw_available",
                     t("srv.upd.fw_available", cur=after['fw'], new=after['fw_upgrade']))

    def do_firmware(self):
        before = self.prepare()
        cur, new = before.get("fw"), before.get("fw_upgrade")
        if not new or cur == new:
            self.log("current", t("srv.upd.fw_current", fw=cur))
            return
        store.job_set(self.jid, from_version=f"fw {cur}", to_version=f"fw {new}")
        self.app_backup()
        self.log("firmware", f"/system/routerboard/upgrade {cur} → {new}")
        try:
            tuple(self.api.path("system", "routerboard")("upgrade"))
        except (TrapError, MultiTrapError) as e:
            raise _StepError(t("srv.upd.fw_failed", error=e)) from e
        time.sleep(2)
        rb = self.read("system", "routerboard")
        self.log("firmware", t("srv.upd.fw_ready", fw=rb.get('upgrade-firmware')))
        self.reboot()
        after = self.wait_up()
        if after.get("fw") != new:
            raise _StepError(t("srv.upd.fw_wrong", running=after.get('fw'), expected=new))
        self.log("verified", f"firmware {cur} → {after['fw']}")
        self.wait_capsman()
