import click

from .. import human as h
from .. import output as o
from ._ctx import dry_run_option, json_option, parse_kv, pass_state, run_disruptive, yes_option


@click.group("nat")
def nat():
    """Firewall, NAT, ALGs, port forwards, IPv6 inbound rules."""


@nat.command("show")
@json_option
@pass_state
def show(st):
    """Firewall + NAT/boost + DMZ + UPnP + ALG + forwards + IPv6 rules."""
    r = st.r
    d = {"firewall": r.nat.firewall(), **r.nat.settings(), "alg": r.nat.alg(),
         "port_forwards": r.nat.port_forwards(), "ipv6_rules": r.nat.ipv6_rules()}
    if (d.get("dmz") or {}).get("enable") == "off":       # the address of a disabled DMZ is a leftover: hide it
        d["dmz"] = {"enable": "off"}
    if isinstance(d.get("nat"), dict) and "reboot_time" in d["nat"]:
        d["nat"] = {**d["nat"], "apply_wait_s": d["nat"].pop("reboot_time")}   # UI countdown after a NAT change (s)
    d["inbound"] = r.status.inbound()
    d["ipv6_allow_rules"] = len((d.get("ipv6_rules") or {}).get("list") or {})
    lines = [o.line("inbound", **d["inbound"]), o.line("ipv6_rules", count=d["ipv6_allow_rules"]),
             *(o.line("port_forward", **_fw(x)) for x in d["port_forwards"]), o.line("port_forwards", count=len(d["port_forwards"]))]
    for k, v in d.items():
        if k in ("inbound", "ipv6_allow_rules", "port_forwards"):
            continue
        lines += o.dict_lines(k, v) if isinstance(v, dict) and v else [o.line(k, entries=0)]
    st.out(d, lines, lambda: h.nat(d))


@nat.command("alg")
@click.argument("changes", nargs=-1)
@dry_run_option
@json_option
@pass_state
def alg(st, changes, dry_run):
    """Show ALGs, or set: `tpctl nat alg sip=off h323=off`."""
    if not changes:
        d = st.r.nat.alg()
        return st.out(d, [o.line("alg", **d)])
    res = st.r.nat.set_alg(dry_run=dry_run, **parse_kv(changes))
    st.out(res, o.write_lines(res), lambda: h.write(res))


@nat.command("upnp")
@click.argument("state", required=False, type=click.Choice(["on", "off"]))
@dry_run_option
@json_option
@pass_state
def upnp(st, state, dry_run):
    """UPnP: show it and the port mappings devices opened (no args), or turn it on/off."""
    if state is None:
        d = st.r.nat.upnp()
        maps = d["mappings"]
        return st.out(d, [o.line("upnp", enable=d["enable"], mappings=len(maps)),
                          *(o.line("upnp_mapping", name=m.get("name"), ip=m.get("ipaddr"), external=m.get("external_port"),
                                   internal=m.get("internal_port"), protocol=m.get("protocol")) for m in maps)],
                      lambda: [h.kvline("UPnP", str(d["enable"]), 6), *h.table(["APP", "DEVICE", "EXTERNAL", "INTERNAL", "PROTO"],
                               [[m.get("name") or "", m.get("ipaddr") or "", str(m.get("external_port") or ""),
                                 str(m.get("internal_port") or ""), m.get("protocol") or ""] for m in maps])])
    res = st.r.nat.set_upnp(state == "on", dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@nat.command("dmz")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--to", "ip", metavar="IP", help="on: the LAN device that receives all inbound connections.")
@dry_run_option
@yes_option
@json_option
@pass_state
def dmz(st, state, ip, dry_run, yes):
    """DMZ host on/off. On exposes one device completely to the internet: plan + confirm (--yes)."""
    if state == "off":
        res = st.r.nat.set_dmz(False, dry_run=dry_run)
        return st.out(res, o.write_lines(res), lambda: h.write(res))
    run_disruptive(st, lambda dr: st.r.nat.set_dmz(True, ip, dry_run=dr), yes, dry_run,
                   f"{ip} gets every unsolicited inbound IPv4 connection (its own firewall is all that protects it)",
                   "one device; port forwards take precedence")


@nat.command("triggers")
@json_option
@pass_state
def triggers(st):
    """Port triggering rules."""
    rows = st.r.nat.triggers()
    st.out(rows, [*(o.line("port_trigger", name=x.get("name"), trigger=x.get("trigger_port"), trigger_protocol=x.get("trigger_protocol"),
                           open=x.get("external_port"), open_protocol=x.get("external_protocol"), enable=x.get("enable")) for x in rows),
                  o.line("port_triggers", count=len(rows))],
           lambda: h.table(["NAME", "TRIGGER", "OPENS", "ON"], [[x.get("name") or "", f"{x.get('trigger_port')}/{x.get('trigger_protocol')}",
                                                               f"{x.get('external_port')}/{x.get('external_protocol')}", x.get("enable") or ""]
                                                              for x in rows]) or [h.dim("no port triggering rules")])


@nat.command("trigger")
@click.argument("action", type=click.Choice(["add", "rm"]))
@click.argument("name")
@click.option("--on", "trigger", metavar="PORT[S]", help="add: outgoing port(s) that trigger it.")
@click.option("--open", "open_ports", metavar="PORT[S]", help="add: inbound port(s) opened back.")
@click.option("--protocol", type=click.Choice(["all", "tcp", "udp"]), default="all", show_default=True)
@dry_run_option
@json_option
@pass_state
def trigger(st, action, name, trigger, open_ports, protocol, dry_run):
    """Add or remove a port triggering rule: `tpctl nat trigger add game --on 6112 --open 6112-6119 --dry-run`."""
    if action == "add":
        if not trigger or not open_ports:
            raise click.UsageError("add needs --on and --open")
        res = st.r.nat.add_trigger(name, trigger, open_ports, protocol, protocol, dry_run=dry_run)
    else:
        res = st.r.nat.remove_trigger(name, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


def _fw(x: dict) -> dict:
    return {"name": x.get("name"), "ip": x.get("ipaddr"), "external": x.get("external_port"),
            "internal": x.get("internal_port"), "protocol": str(x.get("protocol") or "").lower() or None, "enable": x.get("enable")}


@nat.command("forwards")
@json_option
@pass_state
def forwards(st):
    """Port forwarding rules."""
    rows = [_fw(x) for x in st.r.nat.port_forwards()]
    inb = st.r.status.inbound()
    st.out(rows, [*(o.line("port_forward", **x) for x in rows), o.line("port_forwards", count=len(rows), reachable_ipv4=inb.get("ipv4"))],
           lambda: h.table(["NAME", "DEVICE", "EXTERNAL", "INTERNAL", "PROTO", "ON"],
                           [[x["name"] or "", x["ip"] or "", x["external"] or "", x["internal"] or "", x["protocol"] or "",
                             x["enable"] or ""] for x in rows]) or [h.dim("no port forwarding rules")])


@nat.command("forward")
@click.argument("action", type=click.Choice(["add", "rm"]))
@click.argument("name")
@click.option("--to", "ip", metavar="IP", help="add: LAN device IP.")
@click.option("--port", metavar="EXT[:INT]", help="add: external port or range (8000-8010), optional :internal.")
@click.option("--protocol", type=click.Choice(["all", "tcp", "udp"]), default="all", show_default=True)
@dry_run_option
@json_option
@pass_state
def forward(st, action, name, ip, port, protocol, dry_run):
    """Add or remove a port forwarding rule. Behind CGNAT it saves but can't be reached (the plan says so).

    \b
      tpctl nat forward add nas-https --to 192.168.0.50 --port 8443:443 --protocol tcp --dry-run
      tpctl nat forward rm nas-https
    """
    if action == "add":
        if not ip or not port:
            raise click.UsageError("add needs --to IP and --port EXT[:INT]")
        ext, _, internal = port.partition(":")
        res = st.r.nat.add_forward(name, ip, ext, internal or None, protocol, dry_run=dry_run)
    else:
        res = st.r.nat.remove_forward(name, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@nat.command("ipv6-rules")
@json_option
@pass_state
def ipv6_rules(st):
    """IPv6 firewall allow rules (inbound from the internet to one device's IPv6 address)."""
    rows = st.r.nat.ipv6_allows()
    st.out(rows, [*(o.line("ipv6_allow", name=x.get("name"), ip=x.get("ip"), port=x.get("port"),
                           protocol=str(x.get("protocol") or "").lower() or None, type=x.get("type"), enable=x.get("enable"))
                    for x in rows), o.line("ipv6_allows", count=len(rows))],
           lambda: h.table(["NAME", "IPv6", "PORT", "PROTO", "ON"], [[x.get("name") or "", x.get("ip") or "", x.get("port") or "",
                                                                    str(x.get("protocol") or "").lower(), x.get("enable") or ""]
                                                                   for x in rows]) or [h.dim("no IPv6 allow rules (all inbound IPv6 blocked)")])


@nat.command("ipv6-allow")
@click.argument("action", type=click.Choice(["add", "rm"]))
@click.argument("name")
@click.option("--to", "target", metavar="DEVICE|IPv6", help="add: device (its global IPv6) or an IPv6 address.")
@click.option("--port", metavar="PORT[-PORT]", help="add: port or range.")
@click.option("--protocol", type=click.Choice(["all", "tcp", "udp"]), default="tcp", show_default=True)
@dry_run_option
@yes_option
@json_option
@pass_state
def ipv6_allow(st, action, name, target, port, protocol, dry_run, yes):
    """Open (add) or close (rm) an inbound IPv6 port to one device. IPv6 is not behind CGNAT: an allow rule is really
    reachable from the internet, so add is planned first and applied with --yes.

    
      tpctl nat ipv6-allow add nas-https --to NAS --port 443 --dry-run
      tpctl nat ipv6-allow rm nas-https
    """
    if action == "rm":
        res = st.r.nat.remove_ipv6_allow(name, dry_run=dry_run)
        return st.out(res, o.write_lines(res), lambda: h.write(res))
    if not target or not port:
        raise click.UsageError("add needs --to and --port")
    st.r.nat.add_ipv6_allow(name, target, port, protocol, dry_run=True)       # validate first
    run_disruptive(st, lambda dr: st.r.nat.add_ipv6_allow(name, target, port, protocol, dry_run=dr), yes, dry_run,
                   "the port becomes reachable from the whole IPv6 internet", f"one device, port {port}/{protocol}",
                   lambda: ["only open a port a service listens on and that has its own login/TLS",
                            "privacy IPv6 addresses rotate: a rule for one stops matching when it changes"])


COMMANDS = [nat]
