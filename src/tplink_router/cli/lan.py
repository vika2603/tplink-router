import click

from .. import human as h
from .. import output as o
from ._ctx import dry_run_option, json_option, pass_state


@click.group("lan")
def lan():
    """LAN lists: static routes and IP-MAC bindings."""


@lan.command("routes")
@json_option
@pass_state
def routes(st):
    """Static routes you added, and the router's own routing table."""
    d = st.r.lan.routes()
    st.out(d, [*(o.line("route", name=x.get("name"), target=x.get("target"), gateway=x.get("gateway"),
                        interface=x.get("interface"), enable=x.get("enable")) for x in d["static"]),
               o.line("static_routes", count=len(d["static"])),
               *(o.line("system_route", dest=x.get("dest"), gateway=x.get("gateway"), interface=x.get("interface")) for x in d["system"])],
           lambda: h.table(["NAME", "TARGET", "GATEWAY", "IF"], [[x.get("name") or "", x.get("target") or "", x.get("gateway") or "",
                                                                x.get("interface") or ""] for x in d["static"]]) or [h.dim("no static routes")])


@lan.command("route")
@click.argument("action", type=click.Choice(["add", "rm"]))
@click.argument("name")
@click.option("--to", "target", metavar="CIDR", help="add: destination network, e.g. 10.20.0.0/24.")
@click.option("--via", "gateway", metavar="IP", help="add: next hop.")
@click.option("--interface", type=click.Choice(["lan", "wan"]), default="lan", show_default=True)
@dry_run_option
@json_option
@pass_state
def route(st, action, name, target, gateway, interface, dry_run):
    """Add or remove a static route: `tpctl lan route add lab --to 10.20.0.0/24 --via 192.168.0.2 --dry-run`."""
    if action == "add":
        if not target or not gateway:
            raise click.UsageError("add needs --to CIDR and --via IP")
        res = st.r.lan.add_route(name, target, gateway, interface, dry_run=dry_run)
    else:
        res = st.r.lan.remove_route(name, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@lan.command("bindings")
@json_option
@pass_state
def bindings(st):
    """IP-MAC bindings and whether binding is enforced."""
    d = st.r.lan.bindings()
    st.out(d, [o.line("imb", enable=d["enable"], bindings=len(d["list"])),
               *(o.line("binding", name=x.get("name"), ip=x.get("ipaddr"), mac=x.get("mac"), enable=x.get("enable")) for x in d["list"])],
           lambda: [h.kvline("Binding", str(d["enable"]), 8), *h.table(["NAME", "IP", "MAC"], [[x.get("name") or "", x.get("ipaddr") or "",
                                                                                                x.get("mac") or ""] for x in d["list"]])])


@lan.command("bind")
@click.argument("query")
@click.option("--ip", help="IP to bind (default: the device's current IP).")
@dry_run_option
@json_option
@pass_state
def bind(st, query, ip, dry_run):
    """Bind a device (label / IP / MAC) to an IP."""
    res = st.r.lan.bind(query, ip, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@lan.command("unbind")
@click.argument("ip")
@dry_run_option
@json_option
@pass_state
def unbind(st, ip, dry_run):
    """Remove the binding for an IP."""
    res = st.r.lan.unbind(ip, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


COMMANDS = [lan]
