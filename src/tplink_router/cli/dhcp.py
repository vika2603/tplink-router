import click

from .. import human as h
from .. import output as o
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


@click.group("dhcp")
def dhcp():
    """DHCP server: on/off, address pool, lease time, gateway, DNS, leases, reservations."""


@dhcp.command("show")
@json_option
@pass_state
def show(st):
    """Server settings (on/off, pool, lease, gateway, DNS) and reservations."""
    d = st.r.dhcp.get()
    res = st.r.dhcp.overview()
    st.out({"settings": d, "reservations": res},
           [o.line("dhcp", enable=d["enable"], range=d["range"], pool_size=d["pool_size"], lease=o.unit(d["lease_min"], "min"),
                   gateway=d["gateway"], dns=",".join(d["dns"])),
            *(o.line("reservation", name=x["name"], ip=x["ip"], state=x["state"], mac=x["mac"]) for x in res)],
           lambda: h.dhcp({"settings": d, "reservations": res}))


@dhcp.command("server")
@click.argument("state", type=click.Choice(["on", "off"]))
@dry_run_option
@yes_option
@json_option
@pass_state
def server(st, state, dry_run, yes):
    """Turn the DHCP server on or off. Off is disruptive (plan, then --yes): devices lose their address as leases
    expire and new devices can't join, unless another DHCP server is on the LAN."""
    if state == "on":
        res = st.r.dhcp.set_server(True, dry_run=dry_run)
        return st.out(res, o.write_lines(res), lambda: h.write(res))
    run_disruptive(st, lambda dr: st.r.dhcp.set_server(False, dry_run=dr), yes, dry_run,
                   "devices lose their address as their leases expire; new devices can't get one",
                   "every device that uses DHCP (main router + satellites)",
                   lambda: ["only turn it off if another DHCP server (e.g. a Pi-hole / AdGuard) now serves this LAN",
                            "satellites get their address by DHCP too: they may drop off the mesh at lease expiry"])


@dhcp.command("set-range")
@click.argument("start")
@click.argument("end")
@dry_run_option
@yes_option
@json_option
@pass_state
def set_range(st, start, end, dry_run, yes):
    """Address pool handed to devices (planned, --yes to apply).

    `tpctl dhcp set-range 192.168.1.100 192.168.1.199 --dry-run`.

    Must lie inside the LAN subnet and not contain the router's address. Devices outside the new pool move at their
    next renewal (the plan names them), so this is planned first and applied with --yes.
    """
    st.r.dhcp.set_range(start, end, dry_run=True)          # validate before reading the impact
    run_disruptive(st, lambda dr: st.r.dhcp.set_range(start, end, dry_run=dr), yes, dry_run,
                   "devices outside the new pool get a new address at their next renewal",
                   "every DHCP device", lambda: st.r.dhcp.range_impact(start, end))


@dhcp.command("set-gateway")
@click.argument("ip", metavar="IP | router")
@dry_run_option
@yes_option
@json_option
@pass_state
def set_gateway(st, ip, dry_run, yes):
    """Default gateway handed to devices (planned, --yes to apply).

    `router` = the router itself, the normal setting. A wrong gateway cuts devices off the internet at their next
    renewal."""
    run_disruptive(st, lambda dr: st.r.dhcp.set_gateway(ip, dry_run=dr), yes, dry_run,
                   "devices use the new gateway at their next DHCP renewal", "every DHCP device",
                   lambda: [] if ip == "router" else ["the address must be a router that forwards to the internet, or "
                                                       "devices lose the internet; `tpctl dhcp set-gateway router` undoes it"])


@dhcp.command("set-lease")
@click.argument("minutes", type=int)
@dry_run_option
@json_option
@pass_state
def set_lease(st, minutes, dry_run):
    """Lease time in minutes (2-2880)."""
    res = st.r.dhcp.set_lease(minutes, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@dhcp.command("set-dns")
@click.argument("servers", nargs=-1, required=True, metavar="PRIMARY [SECONDARY] | router")
@dry_run_option
@json_option
@pass_state
def set_dns(st, servers, dry_run):
    """DNS servers handed to devices (or `router`).

    `tpctl dhcp set-dns 1.1.1.1 8.8.8.8`, or `router` to use the router itself.

    \b
    Common picks: 1.1.1.1 / 1.0.0.1 (Cloudflare), 8.8.8.8 / 8.8.4.4 (Google), 9.9.9.9 (Quad9, blocks malware).
    A local ad-blocker (AdGuard/Pi-hole): its LAN IP as PRIMARY and nothing else (a public SECONDARY bypasses it).
    """
    if len(servers) > 2:
        raise click.UsageError("at most two servers")
    p = None if servers[0] == "router" else servers[0]
    res = st.r.dhcp.set_dns(p, servers[1] if len(servers) > 1 else None, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@dhcp.command("leases")
@json_option
@pass_state
def leases(st):
    """Current address leases (who has which IP, time left)."""
    rows = st.r.dhcp.leases()
    st.out(rows, [o.line("lease", name=x.get("name"), ip=x.get("ipaddr"), mac=x.get("macaddr"),
                         remaining=x.get("leasetime")) for x in rows], lambda: h.leases(rows))


@dhcp.command("reservations")
@json_option
@pass_state
def reservations(st):
    """Reservations: online? already on the reserved IP?

    A reservation makes a device always get the same IP.

    \b
    state: in_use (online with the reserved IP) · pending_renewal (online, still on its old IP until it renews or
    reconnects) · offline · off (reservation kept but disabled; `tpctl dhcp edit X --on`)
    """
    rows = st.r.dhcp.overview()
    st.out(rows, [o.line("reservation", name=x["name"], ip=x["ip"], state=x["state"], current_ip=x["current_ip"]
                         if x["current_ip"] != x["ip"] else None, node=x["node"], in_pool=x["in_pool"], enable=x["enable"],
                         mac=x["mac"]) for x in rows] or [o.line("reservations", count=0)],
           lambda: h.dhcp_reservations(rows))


@dhcp.command("reserve")
@click.argument("query")
@click.option("--ip", help="Address to reserve (default: the device's current IP).")
@click.option("--comment", default="", help="Label stored with the reservation (default: device label).")
@dry_run_option
@json_option
@pass_state
def reserve(st, query, ip, comment, dry_run):
    """Always give a device the same IP. QUERY = label / IP / MAC (an offline device: MAC + --ip).
    Already reserved: updates that reservation (new --ip, turned back on).

    \b
      tpctl dhcp reserve NAS --dry-run
      tpctl dhcp reserve 3C-22-FB-00-11-22 --ip 192.168.1.50 --comment printer
    """
    res = st.r.dhcp.reserve(query, ip, comment, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@dhcp.command("edit")
@click.argument("query")
@click.option("--ip", help="New reserved address.")
@click.option("--on/--off", "enable", default=None, help="Enable or disable the reservation (disabled = kept, not applied).")
@dry_run_option
@json_option
@pass_state
def edit(st, query, ip, enable, dry_run):
    """Change an existing reservation: new IP and/or on/off. QUERY = MAC, label or IP.

    \b
      tpctl dhcp edit NAS --ip 192.168.1.60 --dry-run
      tpctl dhcp edit NAS --off            # keep it, stop applying it
    To change the name: `tpctl devices rename` (the router keeps the reservation name in sync).
    """
    res = st.r.dhcp.edit(query, ip=ip, enable=enable, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@dhcp.command("unreserve")
@click.argument("query")
@dry_run_option
@json_option
@pass_state
def unreserve(st, query, dry_run):
    """Remove a device's address reservation (QUERY = MAC, label or IP)."""
    res = st.r.dhcp.unreserve(query, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


COMMANDS = [dhcp]
