"""Router session: login, raw calls, read-merge-write, snapshots. Domain logic lives in services/."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from tplinkrouterc6u import TplinkRouterProvider

from .config import Config
from .errors import AuthError, RouterError, UnreachableError
from .models import Change, WriteResult
from .session import SessionStore

SECRET_KEY = re.compile(r"psk|passw|key|secret|pwd|token", re.I)

# Read-only endpoints captured by snapshots. Action/credential endpoints are deliberately absent.
SNAPSHOT_ENDPOINTS = [
    ("admin/network?form=status_ipv4", "read"), ("admin/network?form=lan_ipv4", "read"),
    ("admin/network?form=lan_ipv6", "read"), ("admin/network?form=wan_ipv4_pppoe", "read"),
    ("admin/network?form=wan_dohdot", "read"), ("admin/network?form=lan_jumbo_frame", "read"),
    ("admin/network?form=routes_static", "load"), ("admin/dhcps?form=setting", "read"),
    ("admin/dhcps?form=reservation", "load"), ("admin/security_settings?form=new_enable", "read"),
    ("admin/nat?form=setting", "read"), ("admin/nat?form=alg", "read"), ("admin/nat?form=dmz", "read"),
    ("admin/nat?form=vs", "load"), ("admin/access_control?form=black_list", "load"), ("admin/nat?form=fr6", "read"), ("admin/upnp?form=enable", "read"),
    ("admin/smart_network?form=qos", "read"), ("admin/easymesh?form=easymesh_enable", "read"),
    ("admin/wireless?form=smart_connect", "read"), ("admin/wireless?form=ofdma_mimo", "read"),
    ("admin/wireless?form=region", "read"), ("admin/wireless?form=guest", "read"),
    ("admin/wireless?form=wireless_2g", "read_spf"), ("admin/wireless?form=wireless_5g", "read_spf"),
    ("admin/wireless?form=wireless_6g", "read_spf"), ("admin/wireless?form=wireless_addition_setting", "read"),
    ("admin/time?form=settings", "read"), ("admin/iot_security?form=enable", "read"),
    ("admin/administration?form=remote", "read"), ("admin/ledpm?form=setting", "read"),
    ("admin/eco_mode_v2?form=settings", "read"), ("admin/wireless_schedule_v2?form=settings", "read"),
    ("admin/firmware?form=auto_upgrade", "read"), ("admin/time?form=dst", "read"),
    ("admin/folder_sharing?form=media", "read"), ("admin/imb?form=bind_list", "load"), ("admin/nat?form=pt", "load"),
    ("admin/reboot?form=set", "read"), ("admin/ledgeneral?form=setting", "read"),
]


def redact(obj, show: bool = False):
    if show:
        return obj
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEY.search(k) and isinstance(v, str) and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


class Router:
    """One authenticated session. Use as a context manager, or call open()/close().

    Services (lazy): status, wifi, mesh, clients, nat, dhcp, snapshots, health, system.
    """

    def __init__(self, config: Config | None = None, persist_session: bool | None = None):
        self.config = config or Config()
        self.persist = (not os.environ.get("TPCTL_NO_SESSION_CACHE")) if persist_session is None else persist_session
        self.sessions = SessionStore(self.config.state_dir)
        self._c = None
        self._pw: str | None = None
        self.session_source: str | None = None   # "cache" | "login"
        self._verified = False                   # cached session proven alive in this process
        self.warnings: list[dict] = []   # reads that failed inside try_call(); surfaced by the CLI as partial data
        self.relogins = 0
        self.transient_retries = 0          # reads that succeeded on a second attempt
        self._rejected: set[str] = set()    # endpoints that failed twice: don't keep retrying them
        from .services import attach
        attach(self)

    # -- session --
    def open(self) -> "Router":
        """Reuse the persisted session if possible, else log in (serialized across processes)."""
        if self._c is not None:
            return self
        self._pw = self._pw or self.config.resolve_password()
        if self.persist and (c := self.sessions.load(self.config.host, self._pw)):
            self._c, self.session_source, self._verified = c, "cache", False
            return self
        if not self.persist:
            self._login()
            return self
        with self.sessions.lock():
            # another process may have logged in while we waited for the lock
            if c := self.sessions.load(self.config.host, self._pw):
                self._c, self.session_source, self._verified = c, "cache", False
                return self
            self._login()
            self.sessions.save(self._c, self.config.host, self._pw)
        return self

    def _check_reachable(self, timeout: float = 3.0) -> None:
        """Fail in seconds instead of letting the library probe every router type with long timeouts."""
        import socket
        from urllib.parse import urlparse
        u = urlparse(self.config.host if "://" in self.config.host else f"http://{self.config.host}")
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            socket.create_connection((u.hostname, port), timeout=timeout).close()
        except OSError as e:
            raise UnreachableError(f"router {u.hostname}:{port} unreachable ({e.__class__.__name__}: {e}). "
                                   "Wrong TPLINK_HOST, not on the router's network, or router down") from e

    def _login(self) -> None:
        self._check_reachable()
        last = None
        # the router briefly refuses logins right after a failed/expired one, so back off and retry
        for delay in (0, 3, 8):
            time.sleep(delay)
            c = TplinkRouterProvider.get_client(self.config.host, self._pw)
            try:
                c.authorize()
                self._c, self.session_source, self._verified = c, "login", True
                return
            except Exception as e:
                last = e
        raise AuthError(f"login to {self.config.host} failed after 3 tries ({str(last)[-160:]}). Hint: wrong "
                        "password, router unreachable, or lockout after repeated failed logins (wait ~1min)") from last

    def _relogin(self) -> None:
        """Current session is dead: drop it everywhere and log in again."""
        self._c = None
        self.relogins += 1
        if self.persist:
            with self.sessions.lock():
                cached = self.sessions.load(self.config.host, self._pw)
                # someone else may already have replaced it with a fresh one
                if cached and cached._stok != getattr(self, "_dead_stok", None):
                    self._c, self.session_source = cached, "cache"
                    if self._session_alive():
                        self._verified = True
                        return
                self.sessions.drop()
                self._login()
                self.sessions.save(self._c, self.config.host, self._pw)
        else:
            self._login()

    def close(self) -> None:
        """Persisted sessions stay valid for the next process (only last_used is touched); otherwise log out."""
        if self._c is None:
            return
        if self.persist:
            self.sessions.touch()
        else:
            with contextlib.suppress(Exception):
                self._c.logout()
        self._c = None

    def logout(self) -> None:
        """End the router session for real and forget the persisted one."""
        if self._c is not None:
            with contextlib.suppress(Exception):
                self._c.logout()
        self._c = None
        self.sessions.drop()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    @property
    def lib(self):
        """Underlying tplinkrouterc6u client (get_firmware, get_ipv6_status, ...)."""
        return self.open()._c

    def lib_call(self, method: str, *args, **kw):
        """Call a tplinkrouterc6u helper (get_firmware, get_status, ...) with the same stale-session recovery as call().
        These helpers are reads, so one retry after re-login is safe."""
        self.lib  # ensure open
        try:
            return getattr(self._c, method)(*args, **kw)
        except Exception as e:
            if not self._session_alive():
                self._dead_stok = getattr(self._c, "_stok", None)
                self._relogin()
                try:
                    return getattr(self._c, method)(*args, **kw)
                except Exception as e2:
                    raise RouterError(f"lib.{method}", "read", [], str(e2)[-200:]) from e2
            raise RouterError(f"lib.{method}", "read", [], str(e)[-200:]) from e

    # -- raw API --
    _PROBE = "admin/status?form=internet"
    _READ_OPS = ("read", "load", "read_spf", "loadSpeed", "loadDevice", "read_max")

    def _session_alive(self) -> bool:
        try:
            self._c.request(self._PROBE, "operation=read")
            return True
        except Exception:
            return False

    def call(self, endpoint: str, op: str = "read", _retry_transient: bool = True, **fields):
        """POST operation=<op>&fields to admin/<module>?form=<form>; returns decrypted data or raises RouterError.

        A failed *read* is retried once after re-login, but only if a known-good probe shows the session died
        (router drops sessions on concurrent logins/timeouts). Writes are never retried.
        """
        body = urlencode({"operation": op, **fields})
        is_read = op in self._READ_OPS
        if not is_read:
            self._inv_cache = None                 # the router changed: the next inventory() must re-read
        self.lib  # ensure open
        if not is_read and not self._verified:
            # writes are never retried, so prove a cached session is alive before the first write
            if not self._session_alive():
                self._dead_stok = getattr(self._c, "_stok", None)
                self._relogin()
            self._verified = True
        try:
            res = self._c.request(endpoint, body)
            self._verified = True
            return res
        except Exception as e:
            if is_read and not self._session_alive():
                self._dead_stok = getattr(self._c, "_stok", None)
                self._relogin()
                try:
                    return self._c.request(endpoint, body)
                except Exception as e2:
                    raise RouterError(endpoint, op, sorted(fields), str(e2)[-200:]) from e2
            if is_read and _retry_transient and endpoint not in self._rejected:
                # session alive but the read failed: routers transiently reject requests when busy; reads are
                # idempotent, so retry once after a short pause (endpoints that never work are remembered, no retry)
                time.sleep(0.6)
                try:
                    res = self._c.request(endpoint, body)
                    self.transient_retries += 1
                    return res
                except Exception:
                    self._rejected.add(endpoint)
            raise RouterError(endpoint, op, sorted(fields), str(e)[-200:]) from e

    def call_json(self, endpoint: str, op: str, **fields):
        """Like call(), but the way the web UI sends its `/v2/...` endpoints: the plaintext is JSON (lists stay
        lists), the outer body is JSON {"sign","data"} with Content-Type application/json, same URL (no /v2).
        Needed by forms whose list edits reject urlencoded bodies (e.g. DHCP reservations on BE805 fw 1.5)."""
        from hashlib import sha256

        import requests
        c = self.lib
        if not self._verified and not self._session_alive():
            self._relogin()
        self._verified = True
        self._inv_cache = None
        plain = json.dumps({"operation": op, **fields}, ensure_ascii=False, separators=(",", ":"))
        enc = c._aes_encrypt(plain)
        c._hash = sha256(enc.encode()).hexdigest()
        sign = c._build_request_signature(len(enc))
        url = f"{c.host}/cgi-bin/luci/;stok={c._stok}/{endpoint.lstrip('/').removeprefix('v2/')}"
        hdrs = dict(c._headers_request, **{"Content-Type": "application/json"})
        try:
            resp = requests.post(url, json={"sign": sign, "data": enc}, headers=hdrs, cookies={"sysauth": c._sysauth},
                                 timeout=c.timeout, verify=c._verify_ssl)
            out = json.loads(c._aes_decrypt(resp.json()["data"]))
        except Exception as e:                                   # noqa: BLE001 - surfaced as RouterError
            raise RouterError(endpoint, op, sorted(fields), str(e)[-200:]) from e
        if not out.get("success"):
            raise RouterError(endpoint, op, sorted(fields), f"errorcode={out.get('errorcode')}")
        return out.get("data")

    def download_backup(self) -> tuple[str, bytes]:
        """The web UI's System > Backup: multipart POST operation=backup to firmware?form=config_multipart (not
        encrypted by the session; the file itself is the router's encrypted .bin). Returns (file name, bytes)."""
        import re as _re

        import requests
        c = self.lib
        if not self._verified and not self._session_alive():
            self._relogin()
        self._verified = True
        url = f"{c.host}/cgi-bin/luci/;stok={c._stok}/admin/firmware?form=config_multipart"
        hdrs = {k: v for k, v in c._headers_request.items() if k.lower() != "content-type"}
        try:
            resp = requests.post(url, files={"operation": (None, "backup")}, headers=hdrs, cookies={"sysauth": c._sysauth},
                                 timeout=max(c.timeout, 60), verify=c._verify_ssl)
        except Exception as e:                                   # noqa: BLE001 - surfaced as RouterError
            raise RouterError("admin/firmware?form=config_multipart", "backup", [], str(e)[-200:]) from e
        if resp.status_code != 200 or "octet-stream" not in resp.headers.get("Content-Type", ""):
            raise RouterError("admin/firmware?form=config_multipart", "backup", [],
                              f"HTTP {resp.status_code} {resp.headers.get('Content-Type')}: {resp.text[:120]}")
        m = _re.search(r'filename="([^"]+)"', resp.headers.get("Content-Disposition", ""))
        return (m.group(1) if m else "router-backup.bin"), resp.content

    def try_call(self, endpoint: str, op: str = "read", default=None, quiet: bool = False, **fields):
        """Like call() but returns `default` (or {}) on RouterError and records a warning (unless quiet=True,
        for probes where rejection is expected)."""
        try:
            # quiet probes are *expected* to fail on some models: no transient-retry pause for them
            return self.call(endpoint, op, _retry_transient=not quiet, **fields)
        except RouterError as e:
            if not quiet:
                self.warnings.append({"endpoint": endpoint, "op": op, "fields": sorted(fields), "detail": e.cause[-120:]})
            return {} if default is None else default

    def write_merged(self, endpoint: str, changes: dict, read_op: str = "read", write_op: str = "write") -> list[Change]:
        """Read form, overlay changes, write the whole form back (as the web UI does), re-read to verify."""
        cur = self.call(endpoint, read_op)
        body = {k: v for k, v in cur.items() if isinstance(v, (str, int, float, bool))} | changes
        self.call(endpoint, write_op, **body)
        after = self.call(endpoint, read_op)
        return [Change(k, cur.get(k), after.get(k)) for k in changes]

    def apply_form(self, endpoint: str, changes: dict, *, tag: str, read_op: str = "read", write_op: str = "write",
                   partial: bool = False, dry_run: bool = False, side_effect: str | None = None,
                   next_cmd: str | None = None, key_prefix: str = "") -> WriteResult:
        """Standard write: plan -> (dry-run | no-op skip) -> snapshot -> write -> re-read verify."""
        cur = self.call(endpoint, read_op)
        plan = [Change(key_prefix + k, cur.get(k), v, v) for k, v in changes.items()]
        if dry_run:
            return WriteResult(plan, None, side_effect, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.snapshot(tag)
        if partial:
            self.call(endpoint, write_op, **changes)
        else:
            body = {k: v for k, v in cur.items() if isinstance(v, (str, int, float, bool))} | changes
            self.call(endpoint, write_op, **body)
        after = self.call(endpoint, read_op)
        return WriteResult([Change(key_prefix + k, cur.get(k), after.get(k), v) for k, v in changes.items()], str(snap),
                           side_effect, next_cmd)

    def list_rows(self, endpoint: str) -> list[dict]:
        """Rows of a list form (`load`): the router answers a list, {"list": [...]}, or {} when empty."""
        res = self.call(endpoint, "load")
        rows = res.get("list") if isinstance(res, dict) else res
        return [x for x in (rows or []) if isinstance(x, dict)]

    def list_change(self, endpoint: str, *, tag: str, key_field: str, label: str, add: dict | None = None,
                    remove: str | None = None, dry_run: bool = False, side_effect: str | None = None,
                    next_cmd: str | None = None, json_body: bool = False, json_list: bool = True) -> WriteResult:
        """Add one row (insert new=<JSON> index=0) or remove the row whose `key_field` equals `remove`
        (remove key=<row key> index=<i>), as the web UI does; then re-read and verify."""
        rows = self.list_rows(endpoint)
        ident = (add or {}).get(key_field) if add else remove
        hit = [(i, x) for i, x in enumerate(rows) if str(x.get(key_field, "")).upper() == str(ident).upper()]
        if add is not None:
            plan = [Change(f"{label}[{ident}]", "absent" if not hit else "present", "present", "present")]
        else:
            plan = [Change(f"{label}[{ident}]", "present" if hit else "absent", "absent", "absent")]
        if dry_run:
            return WriteResult(plan, None, side_effect, None, dry_run=True, applied=False)
        if not plan[0].changed:
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.snapshot(tag)
        if add is not None and json_body:
            self.call_json(endpoint, "insert", new=[add] if json_list else add, index=0)   # the UI wraps some forms' rows in a list
        elif add is not None:
            self.call(endpoint, "insert", new=json.dumps(add, ensure_ascii=False), index="0")
        else:
            i, row = hit[0]
            self.call(endpoint, "remove", key=str(row.get("key") or row.get(key_field)), index=str(i))
        now = any(str(x.get(key_field, "")).upper() == str(ident).upper() for x in self.list_rows(endpoint))
        return WriteResult([Change(plan[0].key, plan[0].old, "present" if now else "absent", plan[0].target)],
                           str(snap), side_effect, next_cmd)

    def list_edit(self, endpoint: str, *, tag: str, key_field: str, ident: str, changes: dict, label: str,
                  dry_run: bool = False, side_effect: str | None = None, next_cmd: str | None = None) -> WriteResult:
        """Edit fields of the row whose `key_field` equals `ident`, the way the web UI does on `/v2` list forms:
        JSON update with key=<any uuid> (the UI's local row id), old=<row>, new=<row + changes> (objects, not lists)."""
        import uuid
        rows = self.list_rows(endpoint)
        row = next((x for x in rows if str(x.get(key_field, "")).upper() == str(ident).upper()), None)
        if row is None:
            from .errors import UsageError
            raise UsageError(f"no {label} for {ident}", ident, [x.get(key_field) for x in rows], key_field)
        plan = [Change(f"{label}[{ident}].{k}", row.get(k), v, v) for k, v in changes.items()]
        if dry_run:
            return WriteResult(plan, None, side_effect, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.snapshot(tag)
        k = str(uuid.uuid4())
        self.call_json(endpoint, "update", key=k, old={"key": k, **row}, new={"key": k, **row, **changes})
        after = next((x for x in self.list_rows(endpoint) if str(x.get(key_field, "")).upper() == str(ident).upper()), {})
        return WriteResult([Change(c.key, c.old, after.get(f), c.target) for c, f in zip(plan, changes)], str(snap),
                           side_effect, next_cmd)

    def write_wifi(self, bands, **fields) -> None:
        """Radio write via write_spf on combined forms with wireless_<band>_<field> keys."""
        endpoint = "admin/wireless?" + "&".join(f"form=wireless_{b}" for b in bands)
        self.call(endpoint, "write_spf", **{f"wireless_{b}_{k}": v for b in bands for k, v in fields.items()})

    # -- snapshots (used by every write) --
    def capture(self) -> dict:
        """Redacted config of all snapshot endpoints, in memory (no file)."""
        return redact({ep: self.try_call(ep, op, default={"_error": "rejected"}, quiet=True) for ep, op in SNAPSHOT_ENDPOINTS})

    def snapshot(self, tag: str = "manual") -> Path:
        data = self.capture()
        d = self.config.snapshot_dir
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{tag}.json"
        f.write_text(json.dumps(redact(data), ensure_ascii=False, indent=1, default=str))
        return f


def connect(config: Config | None = None, persist_session: bool | None = None, **kw) -> Router:
    """`with connect() as r: ...` — kw are Config fields (host=, password=, state_dir=...).

    Sessions persist across processes by default (fast, no mutual kick-outs); persist_session=False logs out on close.
    """
    return Router(config or Config(**kw), persist_session=persist_session)
