"""`tpctl` command. Noun-verb groups; bare `tpctl` prints help (no router login).

Exit codes: 0 ok · 1 check/wait found problems · 2 usage · 3 auth · 4 confirmation required · 5 router rejected/unreachable
· 6 partial data (some reads failed) · 7 write verify mismatch · 8 unsupported on this model · 10 internal bug · 130 interrupted.
Errors: one `error code=... exit=... detail=... hint=...` record on stderr (JSON object with --json).
"""
import os
import sys
import traceback

import click

from ._errors import EXIT, emit_error, record_for
from . import api, auth, caps, devices, dhcp, events, lan, mesh, meta, nat, net, overview, parental, py, qos, settings, snapshot, system, wifi
from . import _ctx
from ._ctx import State, json_option

EPILOG = """\b
Quick reference (add --json anywhere for JSON, --format human for tables):
  tpctl overview                       everything at a glance
  tpctl devices identify               what is every device on the network?
  tpctl devices list --weak            devices with weak Wi-Fi
  tpctl mesh show --sort path          satellites, worst connection first
  tpctl net speed [--engine apple]     internet speed + latency under load
  tpctl wifi guest on --dry-run        plan a change (nothing is written without --yes)
  tpctl settings                       any setting from the router's web UI
"""


SECTIONS = [("Look", ["overview", "check", "status", "devices", "mesh", "wifi"]),
            ("Network settings", ["dhcp", "nat", "lan", "qos", "parental", "settings", "system"]),
            ("Diagnose & history", ["net", "events", "snapshot", "observe"]),
            ("Tools", ["api", "py", "auth", "capabilities", "commands", "schema"])]


class SectionedGroup(click.Group):
    """Help lists commands by task instead of alphabetically, with untruncated one-line descriptions."""

    def format_commands(self, ctx, formatter):
        cmds = {n: self.get_command(ctx, n) for n in self.list_commands(ctx)}
        cmds = {n: c for n, c in cmds.items() if c is not None and not c.hidden}
        width = max(formatter.width or 100, 80) - 20
        listed = set()
        for title, names in SECTIONS:
            rows = [(n, cmds[n].get_short_help_str(limit=width)) for n in names if n in cmds]
            listed |= {n for n, _ in rows}
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
        rest = [(n, c.get_short_help_str(limit=width)) for n, c in cmds.items() if n not in listed]
        if rest:
            with formatter.section("Other"):
                formatter.write_dl(rest)


@click.group(cls=SectionedGroup, invoke_without_command=True, epilog=EPILOG,
             context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110})
@click.version_option(__import__("tplink_router").__version__, "--version", message="tpctl %(version)s")
@click.option("--host", envvar="TPLINK_HOST", help="Router URL (default http://192.168.0.1).")
@json_option
@click.pass_context
def cli(ctx, host):
    """TP-Link router + EasyMesh: inspect and change it (agent- and human-friendly)."""
    st = ctx.ensure_object(State)
    st.host = host
    _ctx.CURRENT = st
    ctx.call_on_close(st.close)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


for mod in (overview, events, wifi, mesh, devices, nat, dhcp, lan, qos, parental, settings, snapshot, net, api, system, auth, caps, py, meta):
    for c in mod.COMMANDS:
        cli.add_command(c)

# Aliases: everyday words for the same groups (canonical names stay the documented ones).
ALIASES = {"clients": "devices"}   # old name kept working, hidden from help/docs


class _Alias(click.Group):
    """Same subcommands as the target group; shown in help as an alias, not duplicated in `tpctl commands`."""


for alias, target in ALIASES.items():
    grp = cli.commands[target]
    cli.add_command(click.Group(alias, commands=grp.commands, help=f"Alias of `{target}`.", hidden=True,
                                short_help=f"Alias of `{target}` (same subcommands)."))


def _partial(code: int) -> int:
    cur = _ctx.CURRENT
    if code == 0 and cur is not None and cur.exit_override:
        return cur.exit_override                          # 4 confirmation required / 7 verify mismatch
    return EXIT["partial"] if code == 0 and cur is not None and cur.partial else code


def main():
    try:
        rv = cli.main(standalone_mode=False)
        sys.exit(_partial(rv if isinstance(rv, int) else 0))
    except click.exceptions.Exit as e:
        sys.exit(_partial(e.exit_code))
    except (click.exceptions.Abort, KeyboardInterrupt):
        sys.exit(EXIT["interrupted"])
    except Exception as e:  # noqa: BLE001 - every failure becomes one structured record
        if os.environ.get("TPCTL_DEBUG"):
            traceback.print_exc()
        rec, code = record_for(e)
        emit_error(rec, code)
        sys.exit(code)
