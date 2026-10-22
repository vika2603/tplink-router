import click

from .. import human as h
from .. import output as o
from ..services.devices import SORTS
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


@click.group("devices")
def devices():
    """Connected devices: list, find, identify what they are, rename, block, roaming."""


@devices.command("list")
@click.option("--node", help="Node name substring / IP / MAC.")
@click.option("--band", help="2.4G | 5G | 6G | wired (2g/5/6 also accepted).")
@click.option("--weak", "weak", type=int, is_flag=False, flag_value=-75, default=None,
              help="Only weak devices: bare --weak = signal_grade poor|bad (< -75 dBm, same as devices_weak / WEAK_DEVICES); --weak=N for a custom cutoff.")
@click.option("--sort", type=click.Choice(list(SORTS)), default=None,
              help="node (default) | signal (strongest first) | weakest | name | rate | ip | band. --weak defaults to weakest.")
@click.option("--backhaul-limited", is_flag=True,
              help="Only devices whose path to the main router is worse than their own signal (the satellite backhaul is the bottleneck).")
@click.option("--reverse", is_flag=True, help="Reverse the sort order.")
@click.option("--limit", type=int, default=50, show_default=True, help="Max rows (0 = all). A `truncated` record says so.")
@json_option
@pass_state
def list_(st, node, band, weak, sort, reverse, limit, backhaul_limited):
    """Devices with node, band, signal (+grade), PHY link rates. Filter/sort/limit; --fields to project.

    \b
    Examples:
      tpctl devices list --band 2.4G
      tpctl devices list --node garage --sort signal
      tpctl devices list --weak --fields ip,node,signal
      tpctl devices list --backhaul-limited      (moving the device won't help; fix the satellite's uplink)
    """
    inv = st.r.mesh.inventory()
    st.r.health.record(inv)                 # no-op under global --no-track
    sort = sort or ("weakest" if weak is not None else "node")
    rows = st.r.devices.list(node, band, weak, sort, inventory=inv, backhaul_limited=backhaul_limited)
    if reverse:
        rows = rows[::-1]
    from ..models import Inventory
    filtered = bool(node or band or weak is not None or backhaul_limited)
    s = Inventory([] if filtered else inv.nodes, rows).summary()      # zero-device nodes only in the unfiltered view
    s["stale"] = sum(1 for c in rows if c.signal_stale) or None
    if filtered:
        s["filtered"], s["all_devices"] = True, len(inv.devices)
    shown = rows if not limit or len(rows) <= limit else rows[:limit]
    trunc = [o.line("truncated", shown=len(shown), total=len(rows),
                    hint="--limit 0 for all, or add/tighten a filter (--band, --node, --weak)")] if len(shown) < len(rows) else []
    ranked_list = sort in ("weakest", "signal") and not reverse
    rank_head = []
    if ranked_list and shown and not reverse:
        top = [c for c in shown if c.signal_dbm == shown[0].signal_dbm and c.signal_stale == shown[0].signal_stale]
        rank_head = [o.line("ranking", by=sort, order="weakest first" if sort == "weakest" else "strongest first",
                            tiebreak="settled readings first (signal_stale / just_roamed rows go after them); equal dBm -> slow_link first, then worse path_grade, then lower PHY rate, then label",
                            equal_dbm="=".join(c.label for c in top) if len(top) > 1 else None,
                            equal_dbm_note="same dBm, ordered by the tiebreak above" if len(top) > 1 else None)]
    st.out({"devices": shown, "total": len(rows), "truncated": len(shown) < len(rows), "summary": s},
           [*rank_head, *(o.device_line(c, rank=i if ranked_list else None) for i, c in enumerate(shown, 1)), *trunc, *o.summary_lines(s)],
           lambda: [*h.devices(shown), *([h.dim(f"  … {len(rows) - len(shown)} more (--limit 0 for all)")] if trunc else []),
                    "", "  " + h.summary(s)])


@devices.command("rename")
@click.argument("query")
@click.argument("name")
@dry_run_option
@json_option
@pass_state
def rename(st, query, name, dry_run):
    """Rename a device ON THE ROUTER (same as the web UI's name edit); every router list and the app show it.

    \b
    QUERY = label / name / IP / MAC (as in `devices find`). Verified by re-reading.
      tpctl devices rename 192.168.0.50 "Living room TV" --dry-run
      tpctl devices rename "iPhone#5A-3C-01" "Mum's iPhone"
    """
    res = st.r.devices.rename(query, name, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@devices.command("block")
@click.argument("query")
@dry_run_option
@yes_option
@json_option
@pass_state
def block(st, query, dry_run, yes):
    """Cut a device off the network (router access control, blocklist) until `devices unblock`.

    \b
    QUERY = label / name / IP / MAC; an offline device by MAC. Turns access control on if it is off
    (blocklist mode: only listed devices are affected). Refuses the machine running tpctl.
      tpctl devices block "Kid's iPad" --dry-run
    """
    run_disruptive(st, lambda dr: st.r.devices.block(query, dry_run=dr), yes, dry_run,
                   "that device keeps Wi-Fi association but gets no network access until unblocked",
                   "one device; everything else unchanged")


@devices.command("unblock")
@click.argument("query")
@dry_run_option
@json_option
@pass_state
def unblock(st, query, dry_run):
    """Give a blocked device its network access back (QUERY = MAC or part of its blocked name)."""
    res = st.r.devices.unblock(query, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@devices.command("blocked")
@json_option
@pass_state
def blocked(st):
    """Devices on the router's blocklist."""
    rows = st.r.devices.blocked()
    st.out(rows, [o.line("blocked", **x) for x in rows] or [o.line("blocked", count=0)],
           lambda: h.table(["NAME", "MAC", "IP"], [[x["name"] or "", x["mac"] or "", x["ip"] or ""] for x in rows])
           or [h.dim("no blocked devices")])


def _router_view(st, c, inv) -> dict:
    """What the router knows about a device, plus mesh-node detection, for probe/identify."""
    node = next((n for n in inv.nodes if n.ip == c.ip), None)
    return {"label": c.label, "router_name": c.name, "ip": c.ip, "mac": c.mac, "band": c.band, "node": c.node,
            "signal": c.signal_dbm, "router_type": c.type, "online_min": c.online_min,
            "randomized_mac": c.randomized_mac, "mesh_role": node.role if node else None}


@devices.command("probe")
@click.argument("query")
@click.option("--no-ports", is_flag=True, help="Skip the service-port check (Bonjour/UPnP/vendor only).")
@json_option
@pass_state
def probe_cmd(st, query, no_ports):
    """What is this device? Router data + Bonjour name/model + UPnP + MAC vendor + open service ports + web title.

    \b
    Probes from this machine (read-only: TCP connects to ~25 common ports, one HTTP GET, UPnP description).
    On macOS, if Local Network privacy blocks this program, it runs through the system Python automatically.
      tpctl devices probe 192.168.0.50
      tpctl devices probe "network device#AB-CD-EF"
    """
    from .. import probe as pr
    inv = st.r.mesh.inventory()
    c = inv.device(query)
    d = _router_view(st, c, inv)
    try:
        d |= pr.probe(c.ip, c.mac, st.r.config.state_dir / "oui", ports=not no_ports, bonjour=pr.browse(c.ip))
    except OSError as e:
        d["probe_error"] = str(e)
    kind, ev = pr.guess(d)
    d |= {"guess": kind, "evidence": ev}
    rec = {k: d.get(k) for k in ("label", "ip", "mac", "node", "band", "guess")} | {
        "evidence": "; ".join(ev) or None, "bonjour_name": (d.get("bonjour_names") or [None])[0], "model": d.get("model"),
        "upnp_name": d.get("upnp_name"), "manufacturer": d.get("manufacturer"), "vendor": d.get("vendor"),
        "vendor_note": None if d.get("vendor") else d.get("vendor_note"), "services": d.get("services"),
        "bonjour_services": d.get("bonjour_services"), "web_title": d.get("web_title"), "router_type": d.get("router_type"),
        "router_name": d.get("router_name") if d.get("router_name") != d.get("label") else None, "via": d.get("via"),
        "probe_error": d.get("probe_error")}
    st.out(d, [o.line("probe", **rec)],
           lambda: [h.bold(c.label) + h.dim(f"  {c.ip} · {c.mac}"),
                    h.kvline("Looks like", h.bold(kind or "unknown") + (h.dim("  (" + "; ".join(ev) + ")") if ev else ""), 13),
                    *(h.kvline(k, str(v), 13) for k, v in (
                        ("Bonjour", ", ".join(d.get("bonjour_names") or []) or None), ("Model", d.get("model")),
                        ("UPnP", " · ".join(x for x in (d.get("upnp_name"), d.get("manufacturer"), d.get("upnp_model")) if x) or None),
                        ("Vendor", d.get("vendor") or d.get("vendor_note")), ("Router says", f"{d.get('router_name')} · {d.get('router_type') or '?'}"),
                        ("Connected", f"{c.node} · {c.band}" + (f" · {c.signal_dbm} dBm" if c.signal_dbm is not None else "")),
                        ("Open ports", ", ".join(d.get("services") or []) or ("none" if not no_ports else None)),
                        ("Web page", d.get("web_title")), ("Note", d.get("via") or d.get("probe_error"))) if v)])


@devices.command("identify")
@click.option("--ports", is_flag=True, help="Also port-check devices still unidentified (slower, ~1 s each).")
@click.option("--unknown", is_flag=True, help="Only devices that stay unidentified.")
@json_option
@pass_state
def identify(st, ports, unknown):
    """Best guess for EVERY online device (one Bonjour browse + MAC vendors, a few seconds). Answers "what is
    this device I don't recognise?" for the whole network at once."""
    from .. import probe as pr
    inv = st.r.mesh.inventory()
    sample = next((c.ip for c in inv.devices if c.ip), inv.main.ip)
    try:
        bj = pr.browse(sample)
    except OSError as e:
        bj = {}
        st.r.warnings.append({"endpoint": "mdns", "op": "browse", "fields": [], "detail": str(e)[:120]})
    cache = st.r.config.state_dir / "oui"
    me = pr.local_ips()
    rows = []
    for c in sorted(inv.devices, key=lambda c: c.label.lower()):
        d = _router_view(st, c, inv) | pr._bonjour_fields(bj.get(c.ip) or {}) | ({"this_machine": True} if c.ip in me else {})
        vendor, note = pr.oui_vendor(c.mac, cache)
        d |= {"vendor": vendor, "vendor_note": note}
        kind, ev = pr.guess(d)
        if not kind and ports and c.ip:
            try:
                d |= pr.probe(c.ip, c.mac, cache, ports=True, bonjour=bj)
                kind, ev = pr.guess(d)
            except OSError:
                pass
        rows.append(d | {"guess": kind, "evidence": ev})
    shown = [r for r in rows if not unknown or not r["guess"] or r["guess"].startswith(("phone / tablet", "computer"))]
    st.out(shown, [*(o.line("identify", label=r["label"], ip=r["ip"], node=r["node"], guess=r["guess"] or "unknown",
                              evidence="; ".join(r["evidence"]) or None, bonjour_name=(r.get("bonjour_names") or [None])[0],
                              vendor=r.get("vendor"), private_mac=r["randomized_mac"] or None) for r in shown),
                   o.line("identified", devices=len(rows), with_guess=sum(1 for r in rows if r["guess"]),
                          hint=None if ports else "--ports also port-checks the unidentified ones")],
           lambda: h.table(["DEVICE", "IP", "LOOKS LIKE", "BONJOUR NAME / VENDOR"],
                           [[r["label"], r["ip"] or "", r["guess"] or h.dim("unknown"),
                             (r.get("bonjour_names") or [None])[0] or r.get("vendor") or (h.dim("private MAC") if r["randomized_mac"] else "")]
                            for r in shown]))


@devices.group("alias")
def alias():
    """Local-only label override on this machine (router untouched). Prefer `devices rename`, which names it on the router."""


@alias.command("set")
@click.argument("query")
@click.argument("name")
@dry_run_option
@json_option
@pass_state
def alias_set(st, query, name, dry_run):
    """Name a device found by label / IP / MAC: `tpctl devices alias set 192.168.0.50 "Living room TV"`."""
    res = st.r.devices.set_alias(query, name, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@alias.command("rm")
@click.argument("query")
@dry_run_option
@json_option
@pass_state
def alias_rm(st, query, dry_run):
    """Remove a device's alias."""
    res = st.r.devices.set_alias(query, None, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@alias.command("list")
@json_option
@pass_state
def alias_list(st):
    """All aliases (MAC -> name). No login."""
    from ..config import Config
    import json as _j
    f = Config().config_dir / "aliases.json"
    al = _j.loads(f.read_text()) if f.exists() else {}
    st.out(al, [o.line("alias", mac=m, name=n) for m, n in al.items()] or [o.line("aliases", count=0)],
           lambda: h.aliases(al))


@devices.command("roaming")
@click.option("--since", default="1h", show_default=True, help="Window, e.g. 30m, 6h.")
@click.option("--offline", is_flag=True, help="Event log only: skip the live signal/node lookup (no router login).")
@click.option("--all", "show_all", is_flag=True, help="Also list devices that only reconnected (no moves).")
@json_option
@pass_state
def roaming(st, since, offline, show_all):
    """Who roams a lot: per device node changes (real roams) vs band switches, nodes visited, current signal.

    \b
    moves = node_changes + band_switches. reconnects = times it rejoined after leaving.
    assessment: "flapping between nodes" (>= 3 node changes: calls/streams reset each time) |
    "band steering on one node" (>= 3 band switches, harmless) | "reconnecting often" (>= 3 rejoins) | normal.
    node/band/signal/signal_grade = where it is NOW (same names as `devices list`).
    """
    from .events import _minutes
    from ..client import connect
    hs = connect().health if offline else st.r.health
    rows = hs.roaming(_minutes(since))
    cov = hs.last_roaming_coverage
    if not offline and rows:
        devs = st.r.mesh.inventory().devices
        by, by_mac = {c.label: c for c in devs}, {c.mac: c for c in devs}
        for r_ in rows:
            tail = r_["label"].rsplit("#", 1)[1].upper() if "#" in r_["label"] else None
            c = (by_mac.get(r_.get("mac") or "") or by.get(r_["label"])
                 or (next((d for d in devs if tail and d.mac.endswith(tail)), None)))   # label lost its #tail since
            if c is not None:
                r_["label"] = c.label                 # current name (the device may have been renamed since)
                r_["mac"] = c.mac
                r_["signal_stale"] = "maybe" if c.signal_stale and c.signal_dbm is not None else None
                r_["signal_source"] = (None if c.signal_dbm is None else "placeholder" if c.signal_placeholder else
                                       "last_traffic" if not (c.link_rx_mbps or c.link_tx_mbps) else None)
                if c.node == "unknown-satellite":
                    known = [x for x in r_["nodes"] if x != "unknown-satellite"]
                    r_["last_known_node"] = known[-1] if known else None
            from .. import quality as _q
            r_["start_node"] = r_["nodes"][0] if r_["nodes"] else None
            if r_.get("last_move_at"):
                r_["last_move_at"] = o.ts(r_["last_move_at"])
            if c is not None and c.node not in r_["nodes"]:
                r_["nodes"] = [*r_["nodes"], c.node]          # every node seen, incl. where it is now
            r_ |= ({"node": c.node, "band": c.band, "signal_dbm": c.signal_dbm, "signal_grade": c.signal_grade,
                    "near_boundary": "yes" if _q.near_boundary(c.signal_dbm) else None} if c else {"node": "offline"})
            if c is not None and r_["start_node"] == c.node and len(r_["nodes"]) > 1 and r_.get("node_changes", 0) % 2:
                # back on its start node, but the way back wasn't a recorded move
                r_["returned_via"] = "reconnect" if r_.get("reconnects") else "unrecorded"
    merged: dict = {}
    for r_ in rows:                               # two history keys can resolve to one live device
        k = r_.get("mac") or r_["label"]
        if k in merged:
            m = merged[k]
            for f in ("moves", "node_changes", "band_switches", "reconnects"):
                m[f] += r_[f]
            m["nodes"] = list(dict.fromkeys(m["nodes"] + r_["nodes"]))
        else:
            merged[k] = r_
    rows = list(merged.values())
    for r_ in rows:                               # same rules as HealthService.roaming, after merging
        from ..services.health import roaming_assessment
        r_["assessment"] = roaming_assessment(r_)
    quiet = [r_ for r_ in rows if not r_["moves"]]
    rows.sort(key=lambda r_: (r_.get("node") == "offline", -r_["node_changes"], -r_["moves"]))
    if not show_all:
        rows = [r_ for r_ in rows if r_["moves"]]
    head = o.line("coverage", window=f"{cov['window_min']}min", history_first=o.ts(cov["history_first"]),
                  observations_in_window=cov["observations_in_window"], observations_total=cov["observations_total"],
                  movers=len([r_ for r_ in rows if r_["moves"]]), reconnect_only=len(quiet) if not show_all and quiet else None,
                  reconnect_only_devices=",".join(r_["label"] for r_ in quiet) if not show_all and quiet else None,
                  movers_online_now=None if offline else sum(1 for r_ in rows if r_.get("node") not in (None, "offline")),
                  read_at=__import__("datetime").datetime.now().strftime("%H:%M:%S"),
                  node_is="live read now (offline = not connected at read_at)" if not offline else "not looked up (--offline)",
                  moves_are="node_changes + band_switches (reconnects counted separately)",

                  flapping_at=">= 3 node changes in the window, not counting those during_node_outage",
                  note="moves are only seen between tpctl observations; few observations in the window = moves may be missed")
    own = lambda r_: r_["node_changes"] - r_.get("during_node_outage", 0)
    fwall = [r_ for r_ in rows if (own(r_) >= 3 or own(r_) + r_.get("band_switches", 0) >= 4)   # unstable: nodes or bands
             and r_.get("signal_grade") in ("poor", "bad")]
    fw = [r_["label"] for r_ in fwall if not r_.get("signal_stale")]          # confirmed weak only
    fws = [r_["label"] for r_ in fwall if r_.get("signal_stale")]
    summ = o.line("roaming_summary", flapping=sum(1 for r_ in rows if own(r_) >= 3),
                  flapping_devices=[r_["label"] for r_ in rows if own(r_) >= 3] or None,
                  flapping_offline=sum(1 for r_ in rows if own(r_) >= 3 and r_.get("node") == "offline") or None, unstable_weak=len(fw) or None,
                  unstable_weak_devices=fw or None,
                  unstable_weak_stale=len(fws) or None, unstable_weak_stale_devices=fws or None,
                  moves_during_node_outage=sum(r_.get("during_node_outage", 0) for r_ in rows),
                  unknown_signal_flapping=[r_["label"] for r_ in rows if own(r_) >= 3 and r_.get("signal_grade") is None
                                           and r_.get("node") != "offline"] or None,
                  outage_note="node changes within 3 min of a node dropping or returning were forced by that outage, not "
                              "by the device (fix the node first)" if any(r_.get("during_node_outage") for r_ in rows) else None,
                  matters="flapping + weak signal = calls/streams suffer (check with `net ping --device`); flapping at good "
                          "signal = normal walking around") if not offline else None
    head = head if summ is None else head
    st.out({"coverage": cov, "devices": rows}, [head, *([summ] if summ else []), *(o.line("roaming", **{("signal" if k == "signal_dbm" else k): (o.unit(v, "dBm") if k == "signal_dbm" else v)
                                     for k, v in r.items() if (k not in ("mac", "moves")) or o.DETAIL}) for r in rows)] or [head, o.line("roaming", devices=0)],
           lambda: [h.dim(f"window {cov['window_min']} min · history since {(cov['history_first'] or '?')[:16]}"),
                    *(h.table(["DEVICE", "NODE CHANGES", "BAND SWITCHES", "NOW", "ASSESSMENT"],
                              [[r["label"], str(r["node_changes"]), str(r["band_switches"]),
                                f"{r.get('node', '')} {o.unit(r.get('signal_dbm'), ' dBm') or ''}".strip(), r["assessment"]] for r in rows])
                      or [h.dim("no roaming in this window")])])


@devices.command("find")
@click.argument("queries", nargs=-1, required=True, metavar="QUERY...")
@json_option
@pass_state
def find(st, queries):
    """By name substring, exact IP, or (partial) MAC; several queries in one read: `tpctl devices find mac iphone 192.168.0.5`."""
    inv = st.r.mesh.inventory()
    rows, lines, missing = [], [], []
    for q in queries:
        try:
            hit = st.r.devices.find(q, inventory=inv)
        except Exception:
            missing.append(q)
            continue
        rows += [c for c in hit if c not in rows]
        lines += [o.device_line(c) for c in hit]
        lines.append(o.line("found", count=len(hit), query=q))
    lines += [o.line("found", count=0, query=q, note="not online now (devices leases / events show past devices)") for q in missing]
    st.out(rows, lines, lambda: h.devices(rows))


@devices.command("leases")
@json_option
@pass_state
def leases(st):
    """DHCP leases (includes devices currently asleep/offline)."""
    rows = st.r.dhcp.leases()
    st.out(rows, [o.line("lease", name=x.get("name"), ip=x.get("ipaddr"), mac=x.get("macaddr"),
                         remaining=x.get("leasetime")) for x in rows], lambda: h.leases(rows))


COMMANDS = [devices]
