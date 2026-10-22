import click

from .. import human as h
from .. import output as o
from ..services.health import inventory_issues
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


@click.group("mesh")
def mesh():
    """Mesh nodes: topology, backhaul quality, node settings, reboot, wait for recovery."""


@mesh.command("show")
@click.option("--no-ping", is_flag=True, help="Skip pinging nodes from this machine.")
@click.option("--sort", type=click.Choice(["tree", "backhaul", "path", "health", "devices", "clients"]), default="tree", show_default=True,
              help="backhaul/path: worst first (ties: weaker signal); devices: most devices behind first (devices_behind shown when it differs from devices).")
@json_option
@pass_state
def show(st, no_ping, sort):
    """Every node: uplink, backhaul (wifi dBm, level/5, link Mbps | wired), depth, devices, ping."""
    sort = "devices" if sort == "clients" else sort      # old name
    inv = st.r.mesh.inventory(ping=not no_ping)
    st.r.health.record(inv)                 # no-op under global --no-track
    from .. import quality as q
    if sort in ("backhaul", "path", "health"):    # one source of truth: Inventory.ranked()
        inv.nodes[:] = [n for _, n in inv.ranked(sort)] + [n for n in inv.nodes if n.is_main]
    elif sort == "devices":
        # keys first: list.sort() empties the list while sorting, and inv.downstream() walks inv.nodes
        from .. import quality as _q
        keys = {n.mac: (-n.devices_behind, -len(inv.downstream(n)), _q.rank(n.path_grade or n.backhaul_grade), n.name)
                for n in inv.nodes}
        inv.nodes.sort(key=lambda n: (n.is_main, keys[n.mac]))      # unranked main router last, like the other sorts
    iss = [i for i in inventory_issues(inv) if i.subject in {n.name for n in inv.nodes}]
    ranks = {n.mac: i + 1 for i, n in enumerate([m for m in inv.nodes if not m.is_main])} if sort != "tree" else {}
    ties, unresolved, tie_effect = [], set(), None
    if sort == "devices":                     # same devices_behind: say so instead of silently picking one
        from itertools import groupby
        sats = [m for m in inv.nodes if not m.is_main]
        groups = [list(g) for _, g in groupby(sats, key=lambda m: keys[m.mac][:1])]   # same devices_behind
        top_tie = next((g for g in groups[:1] if len(g) > 1), None)          # only a tie for rank 1 changes the pick
        ties = ["=".join(m.name for m in top_tie)] if top_tie else []
        tie_effect = ", ".join(f"{m.name}: {len(inv.downstream(m)) + 1} node(s) down, {m.devices_behind} devices behind "
                               f"({m.devices} on it)"
                               for m in top_tie) if top_tie else None
        unresolved = {m.name for g in (list(g) for _, g in groupby(sats, key=lambda m: keys[m.mac][:3])) if len(g) > 1 for m in g}
    import datetime as _dt
    head = o.line("mesh_summary", nodes=len(inv.nodes), online=sum(n.online for n in inv.nodes),
                  read_at=_dt.datetime.now().strftime("%H:%M:%S"),
                  note="one live read: signals/PHY rates differ a little on the next read (see SKILL.md)")
    st.out({"nodes": inv.nodes, "issues": iss, "sort": sort, "ranks": ranks},
           [head, *([o.line("ranking", by=sort, ranked_on={"devices": "devices_behind (on it + every node downstream)",
                                                          "path": "path_grade", "backhaul": "backhaul_grade",
                                                          "health": "worse of path_grade and the weakest device's grade"}.get(sort), order="most devices behind first" if sort == "devices" else "worst first",
                      tiebreak="more nodes behind it (a reboot takes those down too), then healthier path_grade (rejoins more reliably)" if sort == "devices"
                      else "worse own backhaul (root cause), then more weak devices behind it, then weaker device signal, "
                           "then name" if sort == "health"
                      else "weaker signal on the path, then the node's own hop, then lower bottleneck Mbps, then name",
                      ties=ties or None, ties_at_rank=1 if ties else None, tie_effect=tie_effect if sort == "devices" else None,
                      resolved_by="tiebreak (nodes behind, then path_grade)" if ties and not unresolved
                      else ("partly: tied=yes rows are still equal" if ties else None),
                      main_router="excluded from ranks")] if ranks else []), *(o.node_line(n, rank=ranks.get(n.mac), tied=n.name in unresolved) for n in inv.nodes), *(o.tree_lines(inv.nodes) if o.DETAIL else []), *o.issue_lines(iss)],
           lambda: [*h.mesh(inv.nodes), *(h.issues(iss) if iss else [])])


@mesh.command("node")
@click.argument("query")
@json_option
@pass_state
def node(st, query):
    """One node by name substring / IP / MAC: backhaul detail, uplink options, its devices, related issues.

    \b
    Examples:
      tpctl mesh node garage
      tpctl mesh node 192.168.0.25 -v
    """
    n, inv = st.r.mesh.node(query)
    rows = sorted(inv.devices_on(n.name), key=lambda c: -(c.signal_dbm if isinstance(c.signal_dbm, int) else -200))
    names = {n.name} | {c.name for c in rows}
    issues = [i for i in inventory_issues(inv) if i.subject in names]
    d = n.raw.get("detail") or {}
    extra = o.line("node_detail", internet=d.get("internet_status"), backhaul_2g_signal=o.unit(d.get("signal_strength_24g"), "dBm")
                   if d.get("signal_strength_24g") not in (None, -128) else None,
                   backhaul_2g_link=o.unit((int(d.get("link_speed_24g") or 0) // 1000) or None, "Mbps"),
                   mac_5g=d.get("mac_5g"), led=d.get("led"), location=d.get("custom_location"),
                   client_bind_supported=d.get("support_client_bind")) if d and o.DETAIL else None
    def human():
        path = " → ".join(m.name for m in inv.path(n))
        bh = "main router" if n.is_main else ("wired" if n.backhaul == "wired" else
             f"wifi {n.backhaul_dbm} dBm, level {n.backhaul_level}/5, link {n.backhaul_mbps} Mbps")
        ping = "—" if n.ping is None else ("down" if not n.ping.up else f"{n.ping.avg_ms:.1f} ms, {n.ping.loss_pct:.0f}% loss")
        return [h.bold(n.name) + h.dim(f"  {n.model} · {n.ip} · {n.mac}"),
                h.kvline("Status", h.green(n.status) if n.online else h.red(n.status)),
                h.kvline("Path", path), h.kvline("Backhaul", h.grade(n.backhaul_grade, bh) if not n.is_main else bh),
                h.kvline("Ping", h.grade(n.ping_grade, ping)),
                h.kvline("Behind it", f"{len(inv.downstream(n))} nodes, {len(inv.devices_behind(n))} devices"),
                h.section(f"Devices on this node ({len(rows)})"), *(h.devices(rows, show_node=False) or ["  none"]),
                *(h.issues(issues) if issues else [])]
    down = inv.downstream(n)
    path_rec = o.line("path", node=n.name, to_main=[m.name for m in inv.path(n)],
                      downstream=[m.name for m in down] or None, devices_behind=len(inv.devices_behind(n)))
    st.out({"node": n, "detail": d, "devices": rows, "issues": issues},
           [o.node_line(n), path_rec, *([extra] if extra else []), *(o.device_line(c) for c in rows), *o.issue_lines(issues)], human)


@mesh.command("set")
@click.argument("query")
@click.option("--name", help="New node name.")
@click.option("--location", type=click.Choice(["bedroom", "hallway", "kitchen", "living_room", "master_bedroom", "office",
                                                "study", "other", "none"]), help="Location tag (none = not set).")
@click.option("--led", type=click.Choice(["on", "off"]), help="Satellite LED (TP-Link satellites may follow the main router's LED control).")
@click.option("--uplink", metavar="auto|NODE", help="Preferred uplink: auto, or a node the router offers as a candidate. Disruptive.")
@dry_run_option
@yes_option
@json_option
@pass_state
def set_(st, query, name, location, led, uplink, dry_run, yes):
    """Node settings, as the web UI's Edit dialog: name, location, LED, preferred uplink.

    \b
    Name/location/LED apply directly (--dry-run to preview). --uplink re-attaches the node: plan + confirm (--yes).
      tpctl mesh set hallway --name "2F hallway" --location hallway
      tpctl mesh set garage --led off --dry-run
      tpctl mesh set garage --uplink auto --dry-run
    """
    if not any(v is not None for v in (name, location, led, uplink)):
        raise click.UsageError("give at least one of --name, --location, --led, --uplink")
    kw = dict(name=name, location="" if location == "none" else location,
              led=None if led is None else led == "on", uplink=uplink)
    inv = st.r.mesh.inventory()
    if uplink is None:
        res = st.r.mesh.configure(query, inventory=inv, dry_run=dry_run, **kw)
        return st.out(res, o.write_lines(res), lambda: h.write(res))
    n = inv.node(query)
    effect = (f"{n.name} drops its backhaul and re-attaches (~1-3 min); {len(inv.devices_behind(n))} devices on it/behind it "
              f"reconnect; recover with `tpctl mesh set {n.name} --uplink auto --yes`")
    run_disruptive(st, lambda dry: st.r.mesh.configure(query, inventory=inv, dry_run=dry, **kw), yes, dry_run, effect,
                   scope=f"{n.name} + {len(inv.downstream(n))} nodes behind it")


@mesh.command("reboot")
@click.argument("query", required=False)
@click.option("--most-devices", is_flag=True, help="Target the satellite with the most devices behind it (same order as "
              "`mesh show --sort devices`; ties: more nodes behind, then healthier path, then name).")
@click.option("--yes", "-y", is_flag=True, help="Confirm (without it: show what would happen, exit 4).")
@click.option("--dry-run", is_flag=True, help="Show the plan and the confirm command (exit 0); nothing happens.")
@json_option
@pass_state
def reboot(st, query, most_devices, yes, dry_run):
    """Reboot one satellite (it re-picks its uplink). Node must be reachable by the main router.

    \b
      tpctl mesh reboot hallway --dry-run
      tpctl mesh reboot --most-devices --dry-run
    """
    if bool(query) == bool(most_devices):
        raise click.UsageError("give a node name or --most-devices")
    if most_devices:
        inv0 = st.r.mesh.inventory()
        sats = [n for n in inv0.nodes if not n.is_main]
        if not sats:
            raise click.UsageError("no satellites")
        from .. import quality as q
        top = sorted(sats, key=lambda n: (-n.devices_behind, -len(inv0.downstream(n)), q.rank(n.path_grade), n.name))
        query = top[0].name
        tie = [n.name for n in top[1:] if n.devices_behind == top[0].devices_behind]
    else:
        tie = []
    if not yes or dry_run:
        import shlex
        from ._ctx import _this_machine
        n, inv = st.r.mesh.node(query, ping=False)
        down, cl = inv.downstream(n), inv.devices_behind(n)
        effect = (f"{n.name}" + (f" and the {len(down)} node(s) behind it ({', '.join(m.name for m in down)})" if down else "")
                  + f" go offline ~1-3 min (up to ~5 min if it re-onboards); {len(cl)} device{'s' * (len(cl) != 1)} roam to "
                  f"another node or drop until it is back: " + (", ".join(c.label for c in cl) if cl else "none"))
        caveats = []
        weak = [c.label + ("?" if c.signal_stale else "") for c in cl if c.signal_grade in ("poor", "bad")]
        dbm = {c.label + ("?" if c.signal_stale else ""): c.signal_dbm for c in cl}
        grade = {c.label + ("?" if c.signal_stale else ""): c.signal_grade for c in cl}
        rm = {d["mac"]: d for d in st.r.health.roaming(60) if d.get("mac")}
        flap = [c.label for c in cl if (rm.get(c.mac) or {}).get("node_changes", 0) >= 3   # same rule as devices roaming
                and c.signal_grade not in ("excellent", "good")                               # good signal = walking around
                and c.label + ("?" if c.signal_stale else "") not in weak]
        flap_weak = [c.label for c in cl if (rm.get(c.mac) or {}).get("node_changes", 0) >= 3
                     and c.label + ("?" if c.signal_stale else "") in weak]
        from .. import quality as _q
        weak = [f"{w} {dbm.get(w)} dBm {grade.get(w)}" + ("~" if _q.near_boundary(dbm.get(w)) else "")
                + (" (also flapping)" if w.rstrip("?") in flap_weak else "") for w in weak]
        if weak or flap:
            caveats.append("already struggling devices behind it: " + ", ".join(
                x for x in (f"weak (poor/bad) {', '.join(weak)}" if weak else "", f"flapping {', '.join(flap)}" if flap else "") if x)
                + " — they are the likeliest to drop instead of roaming"
                + ("; ? = old reading (signal_stale)" if any("?" in w for w in weak) else "")
                + ("; ~ = within 2 dB of a grade line (-75 weak / -82 bad): may read one grade better or worse next time"
                   if any("~" in w for w in weak) else ""))
        cand = [m for m in inv.nodes if m is not n and m not in down and m.status == "connected"
                and (m.is_main or m.path_grade not in ("poor", "bad"))]
        roam_to = [m.name for m in sorted(cand, key=lambda m: (m.name != n.parent_name, not m.is_main, m.name))][:3]
        if tie:
            caveats.append(f"tie: {', '.join(tie)} have as many devices behind them; picked {n.name} (more nodes behind, "
                           "then healthier path, then name)")
        for m in [n, *down]:
            if m.status_flaps_30m >= 2:
                caveats.append(f"{m.name} has been dropping off the mesh ({m.status_flaps_30m} status changes in the last "
                               "hour): after the reboot it may take longer to rejoin or drop again")
        if n.backhaul == "wifi" and n.backhaul_grade in ("fair", "poor", "bad"):
            caveats.append(f"{n.name} has a {n.backhaul_grade} backhaul ({n.backhaul_dbm} dBm): it may come back on a "
                           "different parent or take longer to rejoin")
        notes = ["ok: settings are kept; the main router and the other branches of the mesh stay up"]
        cmd = f"tpctl mesh reboot {shlex.quote(n.name)} --yes"
        if roam_to:
            notes.append("ok: likeliest roam targets (its uplink first, nodes with a poor path left out): "
                         + ", ".join(roam_to) + "; devices out of their range drop until it is back")
        st.exit_override = None if dry_run else 4
        return st.out({"confirmation_required": True, "target": n.name, "effect": effect, "caveats": caveats,
                       "confirm_command": cmd},
                      [o.line("write", status="dry_run" if dry_run else "needs_confirmation"),
                       o.line("would_reboot", target=n.name, ip=n.ip, selected_by="most devices behind" if most_devices else None),
                       o.line("effect", drop=len(cl), may_blip=0, nodes_down=len(down) + 1, duration="~1-3 min (up to ~5)",
                              detail=effect, scope=f"{len(down) + 1} node(s)", roam_to=roam_to or None,
                              read_at=__import__("datetime").datetime.now().strftime("%H:%M:%S"), this_machine=_this_machine(),
                              note="device counts are a live read at read_at; an earlier mesh show may differ by a few"),
                       *(o.line("caveat", detail=c) for c in caveats), *(o.line("note", detail=x[4:]) for x in notes),
                       o.line("confirm", required="yes", risk="disruptive", caveats=len(caveats) or None, run=cmd,
                              note="dry run: nothing happened; run this after the user agrees" if dry_run else None)],
                      lambda: [h.yellow(h.bold("Plan: ") + effect), *(h.red("  caveat: " + c) for c in caveats),
                               f"  run:  {h.bold(cmd)}"])
    n = st.r.mesh.reboot(query)
    st.out({"rebooted": n, "note": "API errors even on success; node down ~1-3min"},
           [o.line("reboot", target=n.name, ip=n.ip, mac=n.mac, sent="yes",
                   note="API reports an error even on success; node down ~1-3min"),
            o.line("next", run="tpctl mesh wait")])


@mesh.command("wait")
@click.option("--timeout", default=360, show_default=True, help="Seconds.")
@click.option("--interval", default=20, show_default=True, help="Seconds between polls.")
@json_option
@pass_state
@click.pass_context
def wait(ctx, st, timeout, interval):
    """Block until every node is connected + pingable. Exit 1 on timeout. Use after Wi-Fi writes/reboots."""
    progress = []

    def tick(el, up, total, bad):
        progress.append({"elapsed_s": el, "up": up, "total": total, "waiting_for": bad})
        if not st.json:
            print(o.line("wait", elapsed=f"{el}s", up=f"{up}/{total}", waiting_for=",".join(bad) or None), flush=True)
    ok, el, bad = st.r.mesh.wait(timeout, interval, tick)
    nxt = "tpctl mesh show   # verify backhaul/topology" if ok else \
          "tpctl mesh node <name>; then `tpctl mesh reboot <name> --yes` or ask user to power-cycle"
    wd = {"ok": ok, "elapsed_s": el, "still_down": bad, "progress": progress}
    st.out(wd,
           [o.line("result", status="all_up" if ok else "timeout", elapsed=f"{el}s", still_down=",".join(bad) or None),
            o.line("next", run=nxt)], lambda: h.mesh_wait(wd))
    ctx.exit(0 if ok else 1)


COMMANDS = [mesh]
