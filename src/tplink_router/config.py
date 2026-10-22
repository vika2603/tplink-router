"""Where to find the router, its password, and local state."""
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AuthError

KEYCHAIN_SERVICE = "tplink-router"
DEFAULT_HOST = "http://192.168.0.1"


def _config_dir() -> Path:
    return Path(os.environ.get("TPLINK_CONFIG", Path.home() / ".config/tplink-router"))


def load_settings(config_dir: Path | None = None) -> dict:
    """Persisted settings (`tpctl setup` writes them): {"host": ...}. Missing or unreadable file -> {}."""
    f = (config_dir or _config_dir()) / "config.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_settings(updates: dict, config_dir: Path | None = None) -> Path:
    """Merge `updates` into config.json (other keys are kept) and return its path."""
    d = config_dir or _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "config.json"
    f.write_text(json.dumps(load_settings(d) | updates, indent=2) + "\n")
    return f


def default_host() -> str:
    """Router URL: $TPLINK_HOST, else the one `tpctl setup` saved, else the TP-Link default."""
    return os.environ.get("TPLINK_HOST") or load_settings().get("host") or DEFAULT_HOST


@dataclass
class Config:
    host: str = field(default_factory=default_host)
    password: str | None = None
    config_dir: Path = field(default_factory=_config_dir)
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("TPLINK_STATE", Path.home() / ".local/state/tplink-router")))

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"

    def password_source(self) -> str | None:
        """Which source resolve_password() would use: arg | env | keychain | file | None."""
        if self.password:
            return "arg"
        if os.environ.get("TPLINK_PASSWORD"):
            return "env"
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True)
        if r.returncode == 0:
            return "keychain"
        return "file" if (self.config_dir / "password").exists() else None

    def store_password(self, password: str, prefer: str = "keychain") -> str:
        """Persist the password: macOS Keychain if possible, else <config_dir>/password (0600). Returns where."""
        if prefer == "keychain":
            r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", "admin",
                                "-w", password], capture_output=True, text=True)
            if r.returncode == 0:
                return "keychain"
        f = self.config_dir / "password"
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(password)
        return "file"

    def resolve_password(self) -> str:
        """$TPLINK_PASSWORD -> macOS Keychain (service tplink-router) -> <config_dir>/password."""
        if self.password:
            return self.password
        if pw := os.environ.get("TPLINK_PASSWORD"):
            return pw
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        f = self.config_dir / "password"
        if f.exists():
            return f.read_text().strip()
        raise AuthError(f"no router password: set TPLINK_PASSWORD, Keychain service '{KEYCHAIN_SERVICE}', or {f}")
