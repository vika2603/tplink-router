"""Turn every failure into one structured, actionable record on stderr.

Record: error code=<STABLE_CODE> exit=<n> detail=... [command, usage, valid/available, did_you_mean, hint, retryable]
With --json anywhere in argv the same record is emitted as a JSON object.
"""
from __future__ import annotations

import difflib
import os
import re
import json
import sys

import click

from ..errors import AuthError, RouterError, TplinkError, UnreachableError, UnsupportedError, UsageError
from ..output import line

# exit codes (contract: only ever add)
EXIT = {"ok": 0, "issues": 1, "usage": 2, "auth": 3, "confirm": 4, "router": 5, "partial": 6, "verify": 7,
        "unsupported": 8, "internal": 10, "interrupted": 130}


def _auth_hint() -> str:
    """How to give tpctl the password without it passing through the chat: a command for the user's own terminal."""
    from ._ctx import terminal_cmd
    return (f"no or wrong router password: ask the user to run `{terminal_cmd('auth set-password')}` in a terminal "
            "(in the Claude Code CLI: `! tpctl auth set-password`); it prompts, verifies and stores it. If it was just "
            "wrong, wait ~1 min before retrying (the router throttles logins)")


def _subcommands(ctx: click.Context | None) -> list[str]:
    cmd = getattr(ctx, "command", None)
    if isinstance(cmd, click.Group):
        return sorted(cmd.list_commands(ctx))
    return []


def _cmd_path(ctx: click.Context | None) -> str | None:
    return ctx.command_path if ctx else None


def _param_choices(ctx: click.Context | None, name: str | None) -> list[str] | None:
    if not ctx or not name:
        return None
    for p in ctx.command.params:
        if p.name == name or name in getattr(p, "opts", []):
            if isinstance(p.type, click.Choice):
                return list(p.type.choices)
    return None


# common habits from other CLIs -> our flag
OPTION_ALIASES = {"--output": "-o/--format", "-f": "-o/--format", "--format=table": "-o human", "-j": "--json",
                  "-y": "--yes", "--force": "--yes", "-n": "--dry-run", "--dryrun": "--dry-run", "--dry": "--dry-run"}


def _metavar(ctx, param) -> str:
    if param is None:
        return "argument"
    if isinstance(param, click.Argument):
        return f"<{param.name.upper()}>"
    return param.opts[-1]


def usage_record(e: click.UsageError) -> dict:
    """Structured usage error. `message` is one plain sentence; `tip` is only set when it is genuinely helpful."""
    ctx = e.ctx
    path = _cmd_path(ctx) or "tpctl"
    msg = e.format_message()
    subs = _subcommands(ctx)
    rec = {"code": "USAGE", "detail": msg, "command": path,
           "usage": ctx.get_usage().replace("Usage: ", "") if ctx else None, "hint": f"{path} --help"}
    if subs and (msg.startswith("Usage:") or "Missing command" in msg):   # group invoked without a subcommand
        cmds = {n: ctx.command.get_command(ctx, n).get_short_help_str(70) for n in subs}
        return rec | {"code": "MISSING_COMMAND", "detail": f"'{path}' requires a subcommand", "available": subs,
                      "commands": cmds, "hint": f"{path} <command> --help"}
    if isinstance(e, click.NoSuchOption):
        opts = sorted({o for p in ctx.command.params for o in getattr(p, "opts", [])} - {"--help", "-h"}) if ctx else []
        bad = e.option_name
        alias = OPTION_ALIASES.get(bad)
        close = [o for o in difflib.get_close_matches(bad, opts, 2, 0.75) if o != bad]
        return rec | {"code": "UNKNOWN_OPTION", "detail": f"unexpected option '{bad}'", "option": bad,
                      "did_you_mean": [alias] if alias else close or None, "valid_options": opts}
    if isinstance(e, click.MissingParameter):
        mv = _metavar(ctx, e.param)
        return rec | {"code": "MISSING_ARGUMENT", "detail": f"missing required {mv}", "missing": e.param.name if e.param else None,
                      "valid": _param_choices(ctx, e.param.name if e.param else None)}
    if isinstance(e, click.BadParameter):
        name = e.param.name if e.param else None
        m = re.search(r"'([^']*)' is not one of", msg)
        val = m.group(1) if m else None
        valid = _param_choices(ctx, name)
        detail = f"invalid value '{val}' for {_metavar(ctx, e.param)}" if val is not None else re.sub(r"^Invalid value[^:]*:\s*", "", msg)
        return rec | {"code": "BAD_VALUE", "detail": detail, "param": name, "value": val, "valid": valid,
                      "did_you_mean": difflib.get_close_matches(val, valid, 1, 0.5) or None if val and valid else None}
    if subs and "No such command" in msg:
        bad = msg.split("'")[1] if "'" in msg else ""
        return rec | {"code": "UNKNOWN_COMMAND", "detail": f"unrecognized command '{bad}'", "available": subs,
                      "did_you_mean": [c for c in difflib.get_close_matches(bad, subs, 2, 0.7)] or None,
                      "hint": f"{path} --help · tpctl commands"}
    return rec | {"detail": re.sub(r"\s+", " ", msg).strip()}


def record_for(exc: BaseException) -> tuple[dict, int]:
    if isinstance(exc, click.UsageError):
        return usage_record(exc), EXIT["usage"]
    if isinstance(exc, UsageError):
        rec = {"code": "BAD_INPUT", "detail": str(exc)}
        if getattr(exc, "candidates", None):
            q = (exc.query or "").lower()
            low = {c.lower(): c for c in exc.candidates}
            close = [low[m] for m in difflib.get_close_matches(q, list(low), 3, 0.5)]
            close += [c for c in exc.candidates if q and q[:3] in c.lower() and c not in close][:3 - len(close)]
            amb = "ambiguous" in str(exc)
            rec |= {"did_you_mean": exc.candidates[:6] if amb else close or None, "ambiguous": amb or None,
                    "candidates": exc.candidates, "what": exc.what,
                    "hint": {"device": "tpctl devices list", "node": "tpctl mesh show"}.get(exc.what, "")}
        return rec, EXIT["usage"]
    from ..errors import SpeedTestError
    if isinstance(exc, SpeedTestError):
        return {"code": "SPEEDTEST_FAILED", "detail": str(exc), "retryable": True, "retry_after_s": exc.retry_after_s,
                "hint": "third-party speed server issue, not the router; latency check: `tpctl net ping --no-mesh`"}, EXIT["router"]
    if isinstance(exc, UnreachableError):
        return {"code": "UNREACHABLE", "detail": str(exc), "retryable": True,
                "hint": "router address: `tpctl auth status` (host, host_source); save the right one with "
                        "`tpctl auth set-host <gateway-ip>`; and check this machine is on the router's LAN"}, EXIT["router"]
    if isinstance(exc, UnsupportedError):
        return {"code": "UNSUPPORTED", "detail": str(exc), "retryable": False,
                "hint": "tpctl capabilities  (what this model supports)"}, EXIT["unsupported"]
    if isinstance(exc, AuthError):
        return {"code": "AUTH_FAILED", "detail": str(exc), "retryable": True,
                "hint": _auth_hint()}, EXIT["auth"]
    if isinstance(exc, RouterError):
        return {"code": "ROUTER_REJECTED", "detail": str(exc), "endpoint": exc.endpoint, "op": exc.op,
                "fields": exc.fields, "retryable": False,
                "hint": "verify endpoint/op in references/api.md; try `tpctl api get <endpoint> --op load`"}, EXIT["router"]
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return {"code": "UNREACHABLE", "detail": str(exc)[-300:], "retryable": True,
                "hint": "router reachable? `tpctl auth status` shows the address in use; `tpctl auth set-host <ip>` "
                        "saves another"}, EXIT["router"]
    if isinstance(exc, TplinkError):
        return {"code": "TPLINK_ERROR", "detail": str(exc)}, EXIT["router"]
    return {"code": "INTERNAL", "detail": f"{type(exc).__name__}: {str(exc)[-300:]}",
            "hint": ("bug in tpctl: the traceback above shows where" if __import__("os").environ.get("TPCTL_DEBUG")
                     else "bug in tpctl; rerun with TPCTL_DEBUG=1 for a traceback")}, EXIT["internal"]


def _resolved_format() -> str:
    """Format the CLI resolved (-o/--format/--json/env/TTY); usage errors can happen before that, so re-derive."""
    from . import _ctx
    if _ctx.CURRENT is not None:
        return _ctx.CURRENT.format
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--json":
            return "json"
        if a in ("-o", "--format") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(("--format=", "-o=")):
            return a.split("=", 1)[1]
    return _ctx.default_format() if sys.stderr.isatty() or os.environ.get("TPCTL_FORMAT") else "records"


def emit_error(rec: dict, exit_code: int) -> None:
    rec = {"code": rec.pop("code"), "exit": exit_code, **rec}
    fmt = _resolved_format()
    if fmt == "json":
        from ..output import SCHEMA_VERSION
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False,
                          "error": {k: v for k, v in rec.items() if v not in (None, [], "")}}, ensure_ascii=False), file=sys.stderr)
    elif fmt == "human":
        print(render_human(rec), file=sys.stderr)
    else:
        if len(rec.get("candidates") or []) > 12:
            rec["candidates"] = rec["candidates"][:12] + [f"...(+{len(rec['candidates']) - 12})"]
        print(line("error", **rec), file=sys.stderr)


def human_mode() -> bool:
    """Pretty output only for a person at a terminal; agents/pipes get records. TPCTL_FORMAT=records|human overrides."""
    argv = " ".join(sys.argv[1:])
    for f in ("human", "records"):
        if f"--format {f}" in argv or f"--format={f}" in argv:
            return f == "human"
    f = os.environ.get("TPCTL_FORMAT")
    if f in ("records", "human"):
        return f == "human"
    return sys.stderr.isatty() and sys.stdout.isatty()


def render_human(rec: dict) -> str:
    """clap-style: `error: <sentence>` / tip / possible values / usage / help pointer."""
    color = not os.environ.get("NO_COLOR")
    R, G, Y, B, D, Z = ("\033[1;31m", "\033[32m", "\033[33m", "\033[1m", "\033[2m", "\033[0m") if color else ("",) * 6
    out = [f"{R}error:{Z} {rec.get('detail', '')}"]
    code = rec["code"]
    dym = rec.get("did_you_mean")
    if dym:
        what = {"UNKNOWN_COMMAND": "a similar command exists", "UNKNOWN_OPTION": "a similar option exists",
                "BAD_VALUE": "a similar value exists",
                "BAD_INPUT": "it matches (be more specific)" if rec.get("ambiguous") else f"similar {rec.get('what', 'value')}s"}.get(code, "did you mean")
        out += ["", f"  {G}tip:{Z} {what}: " + ", ".join(f"{G}'{x}'{Z}" for x in dym)]
    if rec.get("valid") and code in ("BAD_VALUE", "MISSING_ARGUMENT"):
        out += ["", f"  {D}[possible values: {', '.join(map(str, rec['valid']))}]{Z}"]
    if code == "MISSING_COMMAND" and rec.get("commands"):
        w = max(len(n) for n in rec["commands"])
        out += ["", f"{B}Commands:{Z}"] + [f"  {G}{n.ljust(w)}{Z}  {h}" for n, h in rec["commands"].items()]
    elif code == "UNKNOWN_COMMAND" and not dym and rec.get("available"):
        out += ["", f"  {D}available: {', '.join(rec['available'])}{Z}"]
    if code == "BAD_INPUT" and rec.get("candidates") and len(rec["candidates"]) <= 12:
        out += ["", f"  {D}[possible {rec.get('what', 'value')}s: {', '.join(rec['candidates'])}]{Z}"]
    elif code == "BAD_INPUT" and rec.get("candidates") and not dym:
        c = rec["candidates"]
        out += ["", f"  {D}{rec.get('what', 'value')}s: {', '.join(c[:12])}{' …' if len(c) > 12 else ''}{Z}"]
    if code in ("AUTH_FAILED", "ROUTER_REJECTED", "UNREACHABLE", "UNSUPPORTED", "BAD_INPUT", "INTERNAL", "TPLINK_ERROR"):
        if rec.get("hint"):
            out += ["", f"  {Y}hint:{Z} {rec['hint']}"]
        return "\n".join(out)
    if rec.get("usage"):
        out += ["", f"{B}Usage:{Z} {rec['usage']}"]
    help_cmd = rec.get("command") or "tpctl"
    out += ["", f"For more information, try '{B}{help_cmd} --help{Z}'."]
    return "\n".join(out)
