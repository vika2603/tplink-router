import sys

import click

from .. import human as h
from .. import output as o
from ..client import connect
from ..config import Config
from ..session import SessionStore
from ._ctx import json_option, pass_state


@click.group("auth", short_help='Router password and the saved login session.')
def auth():
    """Credentials and the persisted router session (shared across tpctl runs, auto re-login when stale)."""


@auth.command("status")
@click.option("--check", is_flag=True, help="Also verify against the router (one probe call).")
@json_option
@pass_state
def status(st, check):
    """Password source, persisted-session age/idle/TTL; --check proves the session works."""
    cfg = Config(host=st.host) if st.host else Config()
    info = SessionStore(cfg.state_dir).info()
    d = {"host": cfg.host, "password_source": cfg.password_source(), "session": info}
    if check:
        r = st.r
        r.call("admin/status?form=internet")
        d["check"] = {"ok": True, "session_source": r.session_source, "relogins": r.relogins}
    lines = [o.line("auth", host=d["host"], password_source=d["password_source"] or "none")]
    lines.append(o.line("session", **({k: (f"{v}s" if k.endswith("_s") or k.endswith("_ago") else v) for k, v in info.items()}
                                     if info else {"state": "none"})))
    if check:
        lines.append(o.line("check", ok="yes", via=d["check"]["session_source"], relogins=d["check"]["relogins"]))
    st.out(d, lines, lambda: h.auth_status(d))


@auth.command("login")
@json_option
@pass_state
def login(st):
    """Force a fresh login now and persist the session."""
    cfg = Config(host=st.host) if st.host else Config()
    r = connect(cfg)
    r.sessions.drop()
    r.open()
    r.close()
    st.out({"ok": True, "session_source": r.session_source}, [o.line("login", ok="yes", via=r.session_source)])


@auth.command("logout")
@json_option
@pass_state
def logout(st):
    """End the router session and delete the persisted one (frees the single admin slot for the web UI)."""
    cfg = Config(host=st.host) if st.host else Config()
    r = connect(cfg)
    had = SessionStore(cfg.state_dir).info() is not None
    if had:
        r.open()
    r.logout()
    st.out({"ok": True, "had_session": had}, [o.line("logout", ok="yes", had_session="yes" if had else "no")])


@auth.command("set-password")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the password from stdin (for agents/scripts).")
@click.option("--store", type=click.Choice(["keychain", "file"]), default="keychain", show_default=True)
@click.option("--no-verify", is_flag=True, help="Store without test-logging in.")
@json_option
@pass_state
def set_password(st, from_stdin, store, no_verify):
    """Store the router password (Keychain, falling back to ~/.config/tplink-router/password 0600), verify by login."""
    if from_stdin:
        pw = sys.stdin.read().strip()
    elif sys.stdin.isatty():
        pw = click.prompt("Router admin password", hide_input=True, confirmation_prompt=True)
    else:
        raise click.UsageError("no TTY: pass the password with --stdin")
    if not pw:
        raise click.UsageError("empty password")
    cfg = Config(host=st.host) if st.host else Config()
    if not no_verify:
        r = connect(Config(host=cfg.host, password=pw))
        r.sessions.drop()
        r.open()
        r.close()
    where = cfg.store_password(pw, store)
    st.out({"stored": where, "verified": not no_verify},
           [o.line("password", stored=where, verified="no" if no_verify else "yes",
                   note="keychain unavailable here, used 0600 file" if store == "keychain" and where == "file" else None)])


COMMANDS = [auth]
