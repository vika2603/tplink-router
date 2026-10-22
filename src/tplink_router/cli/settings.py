import click

from .. import human as h
from .. import output as o
from ..services.settings import AREAS
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


class _SettingsGroup(click.Group):
    """`tpctl settings [AREA]...` reads; `tpctl settings set KEY on|off` changes a whitelisted switch."""
    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["show", *args]
        elif not args or args[0].startswith("-") and args[0] not in ("-h", "--help"):
            args = ["show", *args]
        return super().parse_args(ctx, args)


@click.group("settings", cls=_SettingsGroup, invoke_without_command=False,
             short_help="Read any settings page of the web UI (secrets ***); `set KEY on|off` for simple switches.")
def settings():
    """Every settings page of the web UI, read-only: `tpctl settings [AREA]...` (no AREA = list areas).
    Simple switches: `tpctl settings set KEY on|off [--dry-run] [--yes]` (`tpctl settings set --list`)."""


@settings.command("set")
@click.argument("key", required=False)
@click.argument("state", required=False, type=click.Choice(["on", "off"]))
@click.option("--list", "list_", is_flag=True, help="List the switches this command can change.")
@dry_run_option
@yes_option
@json_option
@pass_state
def set_cmd(st, key, state, list_, dry_run, yes):
    """Turn a whitelisted switch on/off, e.g. `tpctl settings set firewall.wan_ping off --dry-run`.
    Disruptive ones (firewall.spi, schedules.eco_mode, nat.boost) show a plan and need --yes."""
    from ..services.settings import SWITCHES, UNVERIFIED
    if list_ or not key:  # noqa: E501
        rows = [{"key": k, "endpoint": v[0], "field": v[1], "disruptive": v[3], "live_verified": k not in UNVERIFIED,
                 "what": v[4]} for k, v in SWITCHES.items()]
        return st.out(rows, [o.line("switch", **r) for r in rows],
                      lambda: h.table(["KEY", "WHAT", "CONFIRM"], [[r["key"], r["what"], "yes" if r["disruptive"] else ""] for r in rows]))
    if state is None:
        raise click.UsageError("give on or off")
    if key in SWITCHES and SWITCHES[key][3]:
        return run_disruptive(st, lambda dr: st.r.settings.set_switch(key, state == "on", dry_run=dr), yes, dry_run,
                              SWITCHES[key][4], key,
                              lambda: ["payload taken from the web UI code, not yet round-tripped on a live router: the write "
                                       "is re-read and verified (mismatch = exit 7, snapshot to compare)"] if key in UNVERIFIED else [])
    res = st.r.settings.set_switch(key, state == "on", dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@settings.command("show", hidden=True)
@click.argument("areas", nargs=-1, metavar="[AREA]...")
@click.option("--all", "all_", is_flag=True, help="Every area (one login, ~60 reads).")
@json_option
@pass_state
def show(st, areas, all_):
    """Read any settings page of the web UI, grouped like the UI (secrets shown as ***). Read-only.

    \b
    No AREA: list the areas (no login). Examples:
      tpctl settings guest            guest network
      tpctl settings schedules led    Wi-Fi/eco/reboot schedules + LEDs
      tpctl settings --all            everything
    Raw endpoints behind each area: `tpctl settings --json` (area list) / references/api.md.
    """
    if not areas and not all_:
        rows = [{"area": a, "page": p, "sections": [s[0] for s in secs]} for a, (p, secs) in AREAS.items()]
        return st.out(rows, [o.line("area", area=r["area"], page=r["page"], sections=r["sections"]) for r in rows],
                      lambda: h.table(["AREA", "WEB UI PAGE", "SECTIONS"], [[r["area"], r["page"], ", ".join(r["sections"])] for r in rows]))
    bad = [a for a in areas if a not in AREAS]
    if bad:
        from ..errors import UsageError
        raise UsageError(f"unknown settings area '{bad[0]}'", bad[0], list(AREAS), "area")
    res = [st.r.settings.get(a) for a in (list(AREAS) if all_ else areas)]

    def records():
        for d in res:
            yield o.line("area", area=d["area"], page=d["page"], unsupported=d["unsupported"] or None)
            for k, v in d["values"].items():
                yield (o.line("setting", area=d["area"], key=k, value=v) if v not in ("", None, [], {})
                       else o.line("setting", area=d["area"], key=k, empty="yes"))

    def human():
        out = []
        for d in res:
            out += [h.section(f"{d['area']}  " + h.dim(d["page"]))]
            out += [h.kvline(k, _fmt(v), 28) for k, v in d["values"].items()] or ["  " + h.dim("nothing set")]
            if d["unsupported"]:
                out.append("  " + h.dim("not on this model: " + ", ".join(d["unsupported"])))
        return out
    st.out(res, list(records()), human)


def _fmt(v) -> str:
    if isinstance(v, list):
        return f"{len(v)} entries" if v else "none"
    if v in ("", None):
        return "—"
    return str(v)


COMMANDS = [settings]
