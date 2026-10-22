import click

from .. import human as h
from .. import output as o
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


@click.group("qos", short_help="QoS: bandwidth shaping (bufferbloat fix) and per-device speed limits.")
def qos():
    """QoS (HomeCare > QoS): line bandwidth for traffic shaping, per-device speed limits."""


def _show_lines(d):
    return [o.line("qos", enable=d["enable"], down=o.unit(d["down_mbps"], "Mbps"), up=o.unit(d["up_mbps"], "Mbps"),
                   max=o.unit(d["max_mbps"], "Mbps"),
                   note="bandwidth only matters while QoS is on; set ~90 % of `tpctl net speed`" if d["enable"] != "on" else None)]


@qos.command("show")
@json_option
@pass_state
def show(st):
    """QoS on/off and the bandwidth it shapes to."""
    d = st.r.qos.get()
    st.out(d, _show_lines(d), lambda: [h.kvline("QoS", h.green("on") if d["enable"] == "on" else h.dim("off")),
                                       h.kvline("Bandwidth", f"↓ {d['down_mbps']:g} Mbps  ↑ {d['up_mbps']:g} Mbps")])


@qos.command("set")
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@click.option("--down", type=float, metavar="MBPS", help="Download bandwidth (~90 % of the measured speed).")
@click.option("--up", type=float, metavar="MBPS", help="Upload bandwidth (~90 % of the measured speed).")
@dry_run_option
@yes_option
@json_option
@pass_state
def set_(st, state, down, up, dry_run, yes):
    """Turn QoS on/off and/or set its bandwidth. The cure for lag/bufferbloat under load:

    \b
      tpctl net speed                              # measure first (loaded latency = bufferbloat)
      tpctl qos set on --down 900 --up 45 --dry-run
    Turning it on disables NAT acceleration (the router re-applies NAT, ~2 min): planned, then --yes.
    """
    en = None if state is None else state == "on"
    if en:
        run_disruptive(st, lambda dr: st.r.qos.set(en, down, up, dry_run=dr), yes, dry_run,
                       "QoS on: NAT acceleration turns off, the router re-applies NAT (~2 min of brief stalls)",
                       "all internet traffic", lambda: ["max throughput can drop on very fast lines (no NAT acceleration); "
                                                        "set bandwidth to ~90 % of the real speed or QoS shapes too much"])
        return
    res = st.r.qos.set(en, down, up, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@qos.command("limits")
@click.option("--all", "show_all", is_flag=True, help="Every known device, also those without a limit.")
@json_option
@pass_state
def limits(st, show_all):
    """Per-device speed limits (only limited devices unless --all)."""
    rows = [x for x in st.r.qos.limits() if show_all or x["enable"] == "on"]
    st.out(rows, [*(o.line("speed_limit", name=x["name"], mac=x["mac"], enable=x["enable"],
                           down=o.unit(x["down_mbps"], "Mbps") if x["down_mbps"] is not None else "unlimited",
                           up=o.unit(x["up_mbps"], "Mbps") if x["up_mbps"] is not None else "unlimited") for x in rows),
                  o.line("speed_limits", limited=sum(x["enable"] == "on" for x in rows))],
           lambda: h.table(["DEVICE", "LIMIT", "DOWN", "UP"],
                           [[x["name"] or x["mac"], x["enable"] or "", f"{x['down_mbps']:g} Mbps" if x["down_mbps"] is not None else "—",
                             f"{x['up_mbps']:g} Mbps" if x["up_mbps"] is not None else "—"] for x in rows]) or [h.dim("no device is speed-limited")])


@qos.command("limit")
@click.argument("query")
@click.argument("state", type=click.Choice(["off"]), required=False)
@click.option("--down", type=float, metavar="MBPS", help="Download cap (omit = unlimited).")
@click.option("--up", type=float, metavar="MBPS", help="Upload cap (omit = unlimited).")
@dry_run_option
@json_option
@pass_state
def limit(st, query, state, down, up, dry_run):
    """Cap one device's speed, or remove the cap. QUERY = label / IP / MAC.

    \b
      tpctl qos limit "Kid's iPad" --down 10 --up 2 --dry-run
      tpctl qos limit "Kid's iPad" off
    """
    res = st.r.qos.set_limit(query, down, up, off=state == "off", dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


COMMANDS = [qos]
