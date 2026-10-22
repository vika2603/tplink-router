"""Shared CLI state: lazy router session, --json flag usable at any level, output helpers."""
import json
import os
import sys

import click

from ..client import Router, connect
from ..models import to_dict
from ..output import emit, warning_lines

CURRENT: "State | None" = None   # set by the root group; main() reads it for the partial-data exit code


def default_format() -> str:
    f = os.environ.get("TPCTL_FORMAT")
    if f in ("human", "records", "json"):
        return f
    return "human" if sys.stdout.isatty() else "records"


class State:
    no_shared_reads = False          # `tpctl py` turns this on (scripts may poll)
    def __init__(self):
        self.format = default_format()
        self.host: str | None = None
        self.partial = False
        self.exit_override: int | None = None   # 4 confirmation required, 7 verify mismatch
        self._router: Router | None = None

    @property
    def r(self) -> Router:
        if self._router is None:
            self._router = (connect(host=self.host) if self.host else connect()).open()
            # one CLI command = one consistent read: its plan/effect/caveats count the same devices
            self._router.share_reads = not self.no_shared_reads
        return self._router

    def close(self):
        if self._router is not None:
            self._router.close()

    @property
    def json(self) -> bool:
        return self.format == "json"

    def out(self, data, text_lines, human=None):
        """Print result as human (TTY default) / records (agent default) / json. Failed reads are always surfaced.

        `human` is a zero-arg callable returning lines; commands without one fall back to records.
        """
        warns = list(self._router.warnings) if self._router else []
        self.partial = bool(warns)
        from ..models import WriteResult
        if isinstance(data, WriteResult) and data.mismatches:
            self.exit_override = 7
        if self.format == "human":
            from .. import human as h
            body = human() if human is not None else h.from_records(text_lines)   # never raw records for humans
            for ln in [*body, *h.warnings(warns)]:
                print(ln)
            return
        if self.json:
            from ..output import SCHEMA_VERSION
            ctx = click.get_current_context(silent=True)
            env = {"schema_version": SCHEMA_VERSION, "command": ctx.command_path if ctx else None,
                   "ok": not warns and self.exit_override is None, "data": data}
            if warns:
                env["partial_reads"] = warns
            if self.exit_override == 4:
                env["confirmation_required"] = True
            print(json.dumps(to_dict(env), ensure_ascii=False, indent=1))
        else:
            emit(data, text_lines, False)
            for ln in warning_lines(warns):
                print(ln)


pass_state = click.make_pass_decorator(State, ensure=True)


def json_option(f):
    """--json / --format accepted on every command (and on the root group)."""
    def cb_json(ctx, _param, value):
        if value:
            ctx.ensure_object(State).format = "json"

    def cb_fmt(ctx, _param, value):
        if value:
            ctx.ensure_object(State).format = value
    def cb_detail(ctx, _param, value):
        if value:
            from .. import output
            output.DETAIL = True
    def cb_fields(ctx, _param, value):
        if value:
            from .. import output
            from ..errors import UsageError
            wanted = [x.strip() for x in value.split(",") if x.strip()]
            valid = sorted(set(output.DEVICE_KEYS) | set(output.NODE_KEYS))
            bad = [w for w in wanted if w not in valid]
            if bad:
                raise UsageError(f"unknown field(s) {', '.join(bad)} for --fields", bad[0], valid, "field")
            output.FIELDS = wanted
    def cb_notrack(ctx, _param, value):
        if value:
            os.environ["TPCTL_NO_TRACK"] = "1"
    f = click.option("--no-track", "no_track_global", is_flag=True, expose_value=False, is_eager=True,
                     callback=cb_notrack, help="Don't write the local event history (any command).")(f)
    f = click.option("--fields", expose_value=False, is_eager=True, callback=cb_fields, metavar="K1,K2",
                     help="Records: keep only these keys on device/node lines (label/name always kept).")(f)
    f = click.option("--detail", "-v", is_flag=True, expose_value=False, is_eager=True, callback=cb_detail,
                     help="Records: include every field (MACs, models, modes...). Default is the concise set.")(f)
    f = click.option("--json", is_flag=True, expose_value=False, is_eager=True, callback=cb_json,
                     help="Same as --format json.")(f)
    return click.option("-o", "--format", "fmt", type=click.Choice(["human", "records", "json"]), expose_value=False,
                        is_eager=True, callback=cb_fmt,
                        help="Output: human (default on a terminal), records (key=value lines, default when piped), json.")(f)


def terminal_cmd(args: str) -> str:
    """A tpctl command the user can paste into ANY terminal on this machine (system Terminal, not only Claude's).

    Inside the Claude Code plugin, `tpctl` is on Claude's PATH only, so a plain `tpctl ...` fails in the user's own
    shell: give the plugin's launcher by absolute path then. Installed with `uv tool install`/pipx it is on PATH.
    """
    import shlex
    import shutil
    launcher = os.environ.get("TPCTL_LAUNCHER")      # set by the plugin's bin/tpctl
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if launcher and os.path.exists(launcher):
        exe = launcher
    elif root and os.path.exists(os.path.join(root, "bin", "tpctl")):
        exe = os.path.join(root, "bin", "tpctl")
    else:
        exe = os.path.realpath(sys.argv[0]) if sys.argv and os.path.isabs(sys.argv[0]) else "tpctl"
        on_path = shutil.which("tpctl")
        if on_path and os.path.realpath(on_path) == exe:
            exe = "tpctl"                     # what the user's shell finds anyway: keep it short
    return f"{shlex.quote(exe)} {args}"


def parse_kv(pairs: tuple[str, ...]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise click.BadParameter(f"expected key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def dry_run_option(f):
    return click.option("--dry-run", is_flag=True, help="Show planned old -> new without writing (exit 0).")(f)


def yes_option(f):
    return click.option("--yes", "-y", is_flag=True, help="Confirm a disruptive change (without it: plan only, exit 4).")(f)


def confirm_command() -> str:
    """The exact command to apply after the user agrees: same args, minus --dry-run/output-format flags, plus --yes."""
    import shlex
    out, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a in ("-o", "--format", "--fields"):
            skip = True
            continue
        if a in ("--dry-run", "--json", "-v", "--detail") or a.startswith(("--format=", "-o=", "--fields=")):
            continue
        out.append(a)
    return "tpctl " + shlex.join(out + ["--yes"])


def _this_machine() -> str:
    from .net import _wired
    return {True: "wired", False: "wifi", None: "unknown"}[_wired()]


def run_disruptive(st: "State", compute, yes: bool, dry_run: bool, effect: str, scope: str | None = None, caveats=None,
                   effect_fields: dict | None = None):
    """compute(dry_run: bool) -> WriteResult. Without --yes: show the plan (+ scope, caveats) and exit 4; nothing written.
    caveats: zero-arg callable returning risk strings for this change in the current topology."""
    from .. import human as h
    from .. import output as o
    if yes and not dry_run:
        res = compute(False)
        st.out(res, o.write_lines(res), lambda: h.write(res))
        return
    plan = compute(True)
    if not plan.any_changed:                          # nothing would change: no confirmation needed
        plan.dry_run, plan.applied, plan.skipped_reason = False, False, "already at target; nothing written"
        st.out(plan, o.write_lines(plan), lambda: h.write(plan))
        return
    allc = list(dict.fromkeys(caveats() if caveats else []))      # dedupe, keep order
    if _this_machine() == "wifi" and any(w in (effect or "") for w in ("Wi-Fi", "radios", "re-onboard")):
        allc.append("this machine is on Wi-Fi: tpctl's own connection drops during the change; re-run "
                    "`tpctl mesh wait` once it is back")
    notes = [c[4:] for c in allc if c.startswith("ok: ")]     # reassurances, not risks
    risks = [c for c in allc if not c.startswith("ok: ")]
    if dry_run:                                       # explicit --dry-run: plan only, exit 0
        cmd = confirm_command()
        st.out({"plan": plan, "effect": effect, "scope": scope, "caveats": risks, "notes": notes, "confirm_command": cmd},
               [*o.write_lines(plan, status="dry_run", with_effect=False),
                o.line("effect", **(effect_fields or {}), detail=effect, scope=scope,
                       read_at=__import__("datetime").datetime.now().strftime("%H:%M:%S"),
                       this_machine=_this_machine(),
                       note="device counts are a live read at read_at; an earlier overview may differ by a few (roaming)"
                       if "device" in (effect or "") else None),
                *(o.line("caveat", detail=c) for c in risks), *(o.line("note", detail=n) for n in notes),
                o.line("confirm", required="yes", risk="disruptive", caveats=len(risks) or None, run=cmd,
                       note="dry run: nothing written; run this after the user agrees")],
               lambda: [*h.write(plan, effect=False), h.yellow("  effect: " + effect), *(h.red("  caveat: " + c) for c in risks),
                        *(h.green("  ok:     " + n) for n in notes), "", f"  to apply:  {h.bold(cmd)}"])
        return
    cmd = confirm_command()
    st.exit_override = 4
    st.out({"confirmation_required": True, "plan": plan, "effect": effect, "scope": scope, "caveats": risks,
            "notes": notes, "confirm_command": cmd},
           [*o.write_lines(plan, status="needs_confirmation", with_effect=False),
            *(o.line("caveat", detail=c) for c in risks), *(o.line("note", detail=n) for n in notes),
            o.line("confirm", required="yes", risk="disruptive", effect=effect, scope=scope, caveats=len(risks) or None, run=cmd)],
           lambda: [*h.write(plan, effect=False, title="PLAN — needs your confirmation, nothing written"),
                    h.yellow("  effect: " + effect), *([h.dim("  scope:  " + scope)] if scope else []),
                    *(h.red("  caveat: " + c) for c in risks), *(h.green("  ok:     " + n) for n in notes), "",
                    f"  to apply:  {h.bold(cmd)}"])
