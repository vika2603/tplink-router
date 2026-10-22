import click

from .. import human as h
from .. import output as o
from ..services.parental import UNVERIFIED_NOTE
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


@click.group("parental", invoke_without_command=True, short_help="Parental controls: profiles, pause internet, bedtime.")
@json_option
@pass_state
@click.pass_context
def parental(ctx, st):
    """Parental controls (HomeCare): profiles of devices, pause their internet now, a daily bedtime.

    \b
      tpctl parental                                   # list profiles
      tpctl parental add Kids --device "Kid's iPad" --bedtime 21:30-07:00 --dry-run
      tpctl parental pause Kids on --dry-run           # internet off now
      tpctl parental rm Kids --dry-run
    Writes follow the web UI's payloads but are not yet live-tested: planned first, applied with --yes.
    """
    if ctx.invoked_subcommand:
        return
    ps, lim = st.r.parental.profiles(), st.r.parental.limits()
    st.out({"profiles": ps, "limits": lim},
           [*(o.line("profile", name=p["name"], devices=p["devices"] or None, internet_blocked=p["internet_blocked"],
                     bedtime=p["bedtime"], filters=p["filters"] or None) for p in ps),
            o.line("profiles", count=len(ps), max=lim.get("totalOwnerMax"), devices_per_profile=lim.get("clientPerOwnerMax"))],
           lambda: h.table(["PROFILE", "DEVICES", "PAUSED", "BEDTIME"],
                           [[p["name"], ", ".join(p["devices"]), "yes" if p["internet_blocked"] else "", p["bedtime"] or ""]
                            for p in ps]) or [h.dim("no parental-control profiles")])


def _plan(st, compute, yes, dry_run, effect, scope):
    run_disruptive(st, compute, yes, dry_run, effect, scope, lambda: [UNVERIFIED_NOTE + "; check `tpctl parental` after"])


@parental.command("add")
@click.argument("name")
@click.option("--device", "devices", multiple=True, metavar="Q", help="Device (label / IP / MAC); repeat for more.")
@click.option("--bedtime", metavar="HH:MM-HH:MM", help="Internet off in this window every day.")
@dry_run_option
@yes_option
@json_option
@pass_state
def add(st, name, devices, bedtime, dry_run, yes):
    """Create a profile for some devices, optionally with a daily bedtime."""
    st.r.parental.add(name, devices, bedtime, dry_run=True)       # validate first
    _plan(st, lambda dr: st.r.parental.add(name, devices, bedtime, dry_run=dr), yes, dry_run,
          f"bedtime {bedtime}: internet off for these devices every day" if bedtime else "new profile, no schedule",
          f"{len(devices)} device(s)")


@parental.command("pause")
@click.argument("name")
@click.argument("state", type=click.Choice(["on", "off"]))
@dry_run_option
@yes_option
@json_option
@pass_state
def pause(st, name, state, dry_run, yes):
    """Internet off (on) or back (off) for every device of a profile, right now."""
    _plan(st, lambda dr: st.r.parental.pause(name, state == "on", dry_run=dr), yes, dry_run,
          "the profile's devices lose internet now (they stay on Wi-Fi/LAN)" if state == "on" else "internet back",
          "the profile's devices")


@parental.command("rm")
@click.argument("name")
@dry_run_option
@yes_option
@json_option
@pass_state
def rm(st, name, dry_run, yes):
    """Delete a profile (its devices are no longer filtered or scheduled)."""
    _plan(st, lambda dr: st.r.parental.remove(name, dry_run=dr), yes, dry_run, "profile deleted", "the profile's devices")


COMMANDS = [parental]
