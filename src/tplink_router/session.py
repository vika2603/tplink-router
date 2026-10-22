"""Persisted router session: skip the ~2 s login on every invocation and stop parallel runs kicking each other out.

The router allows one admin session, and logging in again invalidates the previous one. So we keep one session
on disk (`<state_dir>/session.json`, mode 600) and share it across processes. That file holds the session token
and AES keys, never the password. It is bound to host + sha256(password), so a different host or password
ignores it. A session that has gone stale is detected by a probe call and replaced transparently. Logins are
serialized with a file lock so concurrent processes don't fight over the session.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import os
import time
from pathlib import Path

FIELDS = ("_stok", "_sysauth", "_aes_key", "_aes_iv", "_seq", "_nn", "_ee", "_hash", "_logged")
DEFAULT_TTL_S = int(os.environ.get("TPCTL_SESSION_TTL", 20 * 60))   # idle time after which we don't even try


def _fingerprint(host: str, password: str) -> str:
    return hashlib.sha256(f"{host}\0{password}".encode()).hexdigest()[:24]


class SessionStore:
    def __init__(self, state_dir: Path, ttl_s: int = DEFAULT_TTL_S):
        self.file = state_dir / "session.json"
        self.lock_file = state_dir / "session.lock"
        self.ttl_s = ttl_s

    @contextlib.contextmanager
    def lock(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def info(self) -> dict | None:
        try:
            d = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return None
        now = time.time()
        return {"host": d.get("host"), "created_s_ago": int(now - d.get("created", now)),
                "idle_s": int(now - d.get("last_used", now)), "ttl_s": self.ttl_s,
                "expired_by_ttl": now - d.get("last_used", 0) > self.ttl_s, "client_class": d.get("cls")}

    def load(self, host: str, password: str):
        """Rebuild a logged-in library client from disk, or None if absent/mismatched/idle too long."""
        try:
            d = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return None
        if d.get("fp") != _fingerprint(host, password) or time.time() - d.get("last_used", 0) > self.ttl_s:
            return None
        try:
            mod, name = d["cls"].rsplit(".", 1)
            cls = getattr(importlib.import_module(mod), name)
            c = cls(host, password)
            for k in FIELDS:
                if k in d["state"]:
                    setattr(c, k, d["state"][k])
            return c
        except Exception:
            return None

    def save(self, client, host: str, password: str, created: float | None = None) -> None:
        prev = {}
        with contextlib.suppress(OSError, ValueError):
            prev = json.loads(self.file.read_text())
        now = time.time()
        d = {"host": host, "fp": _fingerprint(host, password),
             "cls": f"{type(client).__module__}.{type(client).__name__}",
             "created": created or (prev.get("created") if prev.get("fp") == _fingerprint(host, password) else None) or now,
             "last_used": now, "state": {k: getattr(client, k) for k in FIELDS if hasattr(client, k)}}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, self.file)

    def touch(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            d = json.loads(self.file.read_text())
            d["last_used"] = time.time()
            tmp = self.file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(d, fh)
            os.replace(tmp, self.file)

    def drop(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.file.unlink()
