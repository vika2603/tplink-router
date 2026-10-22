import click

from .. import net as netmod
from .. import quality
from .. import human as h
from .. import output as o
from ._ctx import json_option, pass_state


@click.group("net", short_help='From this machine: ping, router-side ping/traceroute, internet speed test.')
def net():
    """From this machine: ping nodes/devices/internet, ping or traceroute from the router, internet speed test."""


@net.command("ping")
@click.option("--count", type=int, default=None, help="Pings per target (default 5; 20 with --device, enough for jitter/loss).")
@click.option("--no-mesh", is_flag=True, help="Internet only (no router login).")
@click.option("--device", "--client", "device_q", help="Also ping this device (label / name / IP / MAC). Phones may sleep or ignore ICMP.")
@json_option
@pass_state
def ping(st, count, no_mesh, device_q):
    count = count or (20 if device_q else 5)
    """Ping every mesh node + internet v4/v6 in parallel. --device X pings X, its node (baseline) and the internet.

    \b
    Examples:
      tpctl net ping --device living-room-tv
      tpctl net ping --device @weakest        # rank 1 of `devices list --weak`, in one call
      tpctl net ping --no-mesh
    """
    targets = []
    if device_q == "@weakest":                                     # the `devices list --weak` rank 1, same read
        inv = st.r.mesh.inventory()
        rank1 = st.r.devices.list(weak_below=-75, sort="weakest", inventory=inv)
        if not rank1:
            raise click.UsageError("no weak device right now")
        device_q = rank1[0].mac
    if device_q:
        c = st.r.devices.find(device_q)[0]
        inv = st.r.mesh.inventory()
        node = next((n for n in inv.nodes if n.name == c.node), inv.main)
        targets.append((f"device:{c.label}", c.ip))
        targets.append((f"node:{node.name}", node.ip))              # baseline: the hop before the device
        no_mesh = True                                               # --device = device + its node + internet
    if not no_mesh:
        targets += [(n.name, n.ip) for n in st.r.mesh.inventory().nodes]
    targets += [(f"internet-v4-{h}", h) for h in netmod.INTERNET_V4] + [(f"internet-v6-{h}", h) for h in netmod.INTERNET_V6]
    res = netmod.ping_many([ip for _, ip in targets], count)
    rows = [{"name": nm, "host": ip, "samples": count, **(o.to_dict(res[ip]) if ip in res else {"loss_pct": 100.0})} for nm, ip in targets]

    def ln(x):
        up = x.get("loss_pct", 100) < 100
        from .. import quality
        if not up and x["name"].startswith("device:"):
            return o.line("ping", role="device", name=x["name"].split(":", 1)[1], host=x["host"], avg="no_reply", grade="n/a",
                          note="many devices (iPhones, sleeping laptops) ignore ping; see diagnosis", samples=x["samples"])
        role, _, nm = x["name"].partition(":") if ":" in x["name"] and x["name"].split(":")[0] in ("device", "node") else \
            (("internet" if x["name"].startswith("internet") else "node"), "", x["name"])
        return o.line("ping", role=role, name=nm, host=x["host"], avg=f"{x.get('avg_ms')}ms" if up else "down",
                      max=o.unit(x.get("max_ms"), "ms"), jitter=o.unit(x.get("jitter_ms"), "ms"),
                      loss=f"{x.get('loss_pct', 100):.0f}%", grade=quality.ping(x.get("loss_pct"), x.get("avg_ms"), x.get("jitter_ms")),
                      grade_reason=quality.ping_reason(x.get("loss_pct"), x.get("avg_ms"), x.get("jitter_ms")),
                      samples=x["samples"])
    flaps = 0
    if device_q:
        ev = st.r.health.events(since_minutes=60, subject=c.label, limit=0, what=["moved", "joined", "left"])["events"]
        rm = next((d for d in st.r.health.roaming(60) if d.get("mac") == c.mac or d.get("label") == c.label), None)
        flaps = (rm["node_changes"] - rm.get("during_node_outage", 0)) if rm else 0   # same rule as `devices roaming`
    diag = _diagnose(rows, device_q and c, flaps) if device_q else None
    dev_row = next((x for x in rows if x["name"].startswith("device:")), {})
    if diag and dev_row.get("loss_pct", 100) >= 100 and device_q:   # no reply at all: measure a stand-in
        cands = sorted((d for d in inv.devices if d.mac != c.mac and d.signal_grade in ("poor", "bad")
                        and not d.signal_stale and d.band != "wired"), key=lambda d: d.signal_dbm or 0)
        same = [d for d in cands if d.node == c.node] + [d for d in cands if d.node != c.node]
        probe = [d for d in same[:3] if d.ip]
        if probe:                                   # measure a stand-in right away (same node first)
            nodes = {n.name: n.ip for n in inv.nodes}
            ips = [d.ip for d in probe] + sorted({nodes[d.node] for d in probe if nodes.get(d.node)})
            pr = netmod.ping_many(ips, min(count, 10))
            got = next((d for d in probe if pr.get(d.ip) and pr[d.ip].up), None)
            tried = []
            for d in probe:
                st_ = "answered" if pr.get(d.ip) and pr[d.ip].up else "no_reply"
                tried.append(f"{d.label}:{st_}" + ("(used)" if got is not None and d is got else ""))
            if got:
                p, npg = pr[got.ip], pr.get(nodes.get(got.node))
                hop = round(p.avg_ms - npg.avg_ms, 1) if npg and npg.up else None
                diag.pop("hint", None)
                diag |= {"stand_in": got.label, "stand_in_node": got.node, "stand_in_signal": o.unit(got.signal_dbm, "dBm"),
                         "stand_in_avg": o.unit(p.avg_ms, "ms"), "stand_in_jitter": o.unit(p.jitter_ms, "ms"),
                         "stand_in_wifi_hop": o.unit(hop, "ms"),
                         "stand_in_grade": quality.ping(p.loss_pct, p.avg_ms, p.jitter_ms),
                         "stand_in_tried": ",".join(tried),
                         "stand_in_same_node": "yes" if got.node == c.node else
                                               f"no (no other weak device on {c.node} answered)",
                         "stand_in_note": f"{c.label} ignores ping; {got.label} (also weak, "
                                          f"{'same node' if got.node == c.node else got.node}) answered: its Wi-Fi hop "
                                          "approximates what a weak device sees there"}
            else:
                diag["next_candidate"] = probe[0].label
                diag["hint"] = (f"no Wi-Fi-hop latency: {c.label} and {len(probe)} other weak device(s) didn't answer "
                                "ping (sleeping phones/Macs); retry while they're in use")
        elif cands:
            diag["next_candidate"] = cands[0].label
    if diag and device_q and c is not None and c.band != "wired":   # the PHY numbers the verdict used, as fields
        low = min((x for x in (c.link_rx_mbps, c.link_tx_mbps) if x), default=None)
        diag |= {"link_rx": o.unit(c.link_rx_mbps or None, "Mbps"), "link_tx": o.unit(c.link_tx_mbps or None, "Mbps"),
                 "slow_link": "yes" if low is not None and low < 20 else None,
                 "signal_grade": c.signal_grade,
                 "signal_stale": "maybe" if c.signal_stale else None,
                 "signal_source": ("placeholder" if c.signal_placeholder else
                                   "last_traffic" if not (c.link_rx_mbps or c.link_tx_mbps) else None),
                 "near_boundary": "yes" if quality.near_boundary(c.signal_dbm) else None,
                 "read_at": __import__("datetime").datetime.now().strftime("%H:%M:%S")}   # its own fresh read
    if diag and device_q and c is not None:
        n_ = next((n for n in inv.nodes if n.name == c.node), None)
        diag["node"] = c.node
        if n_ is not None and not n_.is_main:
            diag.pop("upstream", None)                     # said by path_grade/path_bottleneck (and in why)
            diag |= {"path_grade": n_.path_grade, "path_link": o.unit(n_.path_link_mbps, "Mbps"),
                     "path_bottleneck": n_.path_bottleneck if n_.path_link_mbps != n_.backhaul_mbps else None}
        if diag.get("stand_in_wifi_hop"):
            diag["why"] = (diag.get("why", "") + f"; measured on a stand-in weak device there, the Wi-Fi hop costs "
                           f"{diag['stand_in_wifi_hop']} ({diag.get('stand_in_grade')})")
    if diag and device_q and c is not None and c.signal_placeholder and "own signal unknown" not in diag.get("why", ""):
        diag["why"] = diag.get("why", "") + "; the device's own signal is unknown (satellite placeholder -90 dBm)"
    st.out({"pings": rows, "diagnosis": diag}, [*(ln(x) for x in rows), *([o.line("diagnosis", **diag)] if diag else [])],
           lambda: [*h.pings(rows), *([h.kvline("Diagnosis", diag["verdict"] + h.dim(" — " + diag["why"]), 10)] if diag else [])])


@net.command("diag")
@click.argument("target")
@click.option("--traceroute", is_flag=True, help="Traceroute instead of ping.")
@click.option("--count", default=4, show_default=True, help="Ping count (1-50).")
@click.option("--max-hops", default=20, show_default=True, help="Traceroute TTL (1-30).")
@json_option
@pass_state
def diag(st, target, traceroute, count, max_hops):
    """Ping / traceroute FROM THE ROUTER (its own diagnostics tool).

    \b
    Compare with `tpctl net ping` (from this machine): router fine + this machine bad = this machine's
    Wi-Fi/LAN path; both bad = the internet connection.
      tpctl net diag 1.1.1.1
      tpctl net diag example.com --traceroute
    """
    d = st.r.system.diagnose(target, "traceroute" if traceroute else "ping", count=count, max_hops=max_hops)
    if d["kind"] == "ping":
        g = quality.ping(d.get("loss_pct"), d.get("avg_ms"))
        recs = [o.line("router_ping", target=target, sent=d.get("sent"), received=d.get("received"),
                       loss=None if d.get("loss_pct") is None else f"{d['loss_pct']:.0f}%", avg=o.unit(d.get("avg_ms"), "ms"),
                       max=o.unit(d.get("max_ms"), "ms"), grade=g, finished=d["finished"])]
    else:
        recs = [*(o.line("hop", text=x) for x in d.get("hops", [])), o.line("router_traceroute", target=target,
                                                                           hops=len(d.get("hops", [])), finished=d["finished"])]
    st.out(d, recs, lambda: [h.dim(f"from the router: {d['kind']} {target}"), *("  " + ln for ln in d["output"].splitlines())])


VERDICTS = ("last_hop_signal", "likely_power_save", "last_hop_jitter", "roaming_flaps", "upstream_backhaul", "internet",
            "ok", "router_data_only")


def _diagnose(rows, c, flaps: int = 0) -> dict:
    """Attribute the problem: last Wi-Fi hop vs roaming vs upstream backhaul vs internet. When the device ignores ICMP
    (iPhones usually do), fall back to router-side data (signal, PHY link, path, roaming events) and say so."""
    cl = next(x for x in rows if x["name"].startswith("device:"))
    nd = next(x for x in rows if x["name"].startswith("node:"))
    inet = [x for x in rows if x["name"].startswith("internet") and x.get("avg_ms") is not None]
    if flaps >= 3:
        also = None
        if 0 < cl.get("loss_pct", 100) < 100 and (cl.get("jitter_ms") or 0) > 100 and c and c.signal_grade not in ("poor", "bad"):
            also = "likely_power_save (huge jitter/loss at an ok signal: a phone dozing between pings)"
        why = f"{flaps} node changes of its own in the last hour (as `devices roaming` counts them): sessions reset on every roam"
        weak_now = bool(c and (c.signal_grade in ("poor", "bad") or
                               0 < min(x for x in (c.link_rx_mbps or 10**6, c.link_tx_mbps or 10**6)) < 20))
        if weak_now:
            why += (f"; and its signal is {c.signal_dbm} dBm ({c.signal_grade}), PHY {c.link_rx_mbps}/{c.link_tx_mbps} Mbps: "
                    "the roaming is probably a symptom of weak coverage there")
        answered = cl.get("loss_pct", 100) < 100 and cl.get("avg_ms") is not None
        hop = round(cl["avg_ms"] - (nd.get("avg_ms") or 0), 1) if answered else None
        if hop is not None:
            why += f"; the Wi-Fi hop adds {hop} ms right now"
        return {"verdict": "roaming_flaps", "why": why, "last_hop_ms": hop, "node_ms": nd.get("avg_ms"), "moves_1h": flaps, "also": also,
                "basis": ("events" if cl.get("loss_pct", 100) >= 100 else "events+ping") + ("+signal" if weak_now else ""),
                "signal": f"{c.signal_dbm}dBm" if c and c.signal_dbm is not None else None,
                "node": getattr(c, "node", None) if c else None}
    if cl.get("loss_pct", 100) >= 100:
        low = min(x for x in (c.link_rx_mbps, c.link_tx_mbps) if x) if c and (c.link_rx_mbps or c.link_tx_mbps) else None
        if c and c.signal_grade in ("poor", "bad"):
            stale = not (c.link_rx_mbps or c.link_tx_mbps)
            v, why = "last_hop_signal", (f"device signal {c.signal_dbm} dBm ({c.signal_grade})"
                                         + (f", PHY link down to {low} Mbps (slow_link)" if low is not None and low < 20 else "")
                                         + (" — reading may be stale (no traffic sample)" if stale else ""))
        elif c and c.path_grade in ("poor", "bad"):
            v, why = "upstream_backhaul", f"path to the main router is {c.path_grade} via {c.weakest_hop}"
        elif low is not None and low < 20:
            v, why = "last_hop_signal", f"PHY link only {low} Mbps despite {c.signal_grade} signal (interference or distance)"
        else:
            v, why = "router_data_only", (f"signal {c.signal_dbm} dBm ({c.signal_grade}), PHY {c.link_rx_mbps}/{c.link_tx_mbps} "
                                          f"Mbps and path look fine; problem is likely the app/server or the device")
        node_bad = quality.ping(nd.get("loss_pct"), nd.get("avg_ms"), nd.get("jitter_ms")) in ("poor", "bad")
        up = None
        if c and v != "upstream_backhaul" and (c.path_grade in ("poor", "bad") or node_bad):
            up = (f"{c.path_grade} via {c.weakest_hop}" if c.path_grade in ("poor", "bad") else "") + \
                 (f"{'; ' if c.path_grade in ('poor', 'bad') else ''}its node pings poor ({nd.get('avg_ms')} ms, jitter "
                  f"{nd.get('jitter_ms')} ms)" if node_bad else "")
            why += f"; also upstream: {up}"
        return {"verdict": v, "why": why + " (device ignores ping: judged from router data)", "last_hop_ms": None,
                "node_ms": nd.get("avg_ms"),
                "basis": ("router_data+ping" if node_bad else "router_data") + ("+signal" if v == "last_hop_signal" else ""),
                "moves_1h": flaps, "upstream": up,
                "signal": f"{c.signal_dbm}dBm" if c and c.signal_dbm is not None else None,
                "path_grade": c.path_grade if c else None,
                "hint": "no Wi-Fi-hop latency: the device didn't answer ping (sleeping phone/Mac or firewall); retry while "
                        "it's in use, or measure the next device in `devices list --weak`"}
    hop = round((cl["avg_ms"] or 0) - (nd.get("avg_ms") or 0), 1)
    weak = c.signal_grade in ("poor", "bad") if c else False
    phy = max(getattr(c, "link_rx_mbps", 0) or 0, getattr(c, "link_tx_mbps", 0) or 0) if c else 0
    fast_phy = phy > 300
    settled_weak = weak and not getattr(c, "signal_stale", False)
    if fast_phy and hop > 100 and not settled_weak:   # a fast link can't explain 100+ ms: the device is dozing
        v, why = "likely_power_save", (f"Wi-Fi hop adds {hop} ms (jitter {cl.get('jitter_ms')} ms) although its PHY link "
                                       f"is {phy} Mbps: typical of a sleeping "
                                       "Mac/phone answering pings late" + ("; its dBm reading may be stale too" if getattr(c, "signal_stale", False) else "")
                                       + ". Re-test while it is busy")
    elif weak and hop > 15 and (nd.get("jitter_ms") or 0) < 20:
        v, why = "last_hop_signal", f"device signal {c.signal_dbm} dBm ({c.signal_grade}) and the Wi-Fi hop adds {hop} ms"
        low = min((x for x in (getattr(c, "link_rx_mbps", 0), getattr(c, "link_tx_mbps", 0)) if x), default=None)
        if low is not None and low < 20:
            why += f"; its PHY link is down to {low} Mbps (slow_link)"
        if (cl.get("jitter_ms") or 0) > 100:
            why += "; the huge jitter fits the weak signal (retries), not power-save, since the node hop is steady"
    elif hop > 20 and (cl.get("jitter_ms") or 0) < 15:
        v, why = "likely_power_save", (f"Wi-Fi hop adds {hop} ms with steady timing at {c.signal_grade if c else '?'} signal: "
                                       "typical of an idle phone/laptop in power-save; re-test while it is busy")
    elif (cl.get("jitter_ms") or 0) >= 20 and (nd.get("jitter_ms") or 0) >= 0.6 * cl["jitter_ms"]:
        v, why = "upstream_backhaul", (f"jitter is already {nd['jitter_ms']} ms at its node (device {cl['jitter_ms']} ms): it comes "
                                       f"from upstream" + (f" ({c.path_grade} path via {c.weakest_hop})" if c and c.weakest_hop else ""))
    elif (cl.get("jitter_ms") or 0) >= 20:
        low = min((x for x in ((c.link_rx_mbps, c.link_tx_mbps) if c else ()) if x), default=None)
        if weak or (low is not None and low < 20):     # jitter from retries at a weak signal / slow PHY: name the cause
            v, why = "last_hop_signal", (f"device signal {c.signal_dbm} dBm ({c.signal_grade})"
                                         + (f", PHY link down to {low} Mbps (slow_link)" if low is not None and low < 20 else "")
                                         + f": the Wi-Fi hop adds {hop} ms with jitter {cl['jitter_ms']} ms vs "
                                           f"{nd.get('jitter_ms')} ms at its node (retries)")
        else:
            v, why = "last_hop_jitter", f"jitter {cl['jitter_ms']} ms on the Wi-Fi hop vs {nd.get('jitter_ms')} ms at its node (interference/contention)"
    elif c and c.path_grade in ("poor", "bad"):
        v, why = "upstream_backhaul", (f"Wi-Fi hop adds only {hop} ms right now, but the path to the main router is "
                                       f"{c.path_grade} via {c.weakest_hop}"
                                       + (f"; the device's own signal is also {c.signal_grade} ({c.signal_dbm} dBm): "
                                          "fix both" if weak else ""))
    elif inet and max(x["avg_ms"] for x in inet) > 40:
        v, why = "internet", "local hops fine; internet latency is high"
    elif weak or (c and min((x for x in (c.link_rx_mbps, c.link_tx_mbps) if x), default=999) < 20):
        # ping alone can look clean at a bad signal: tiny packets get through, real traffic doesn't
        low = min((x for x in (c.link_rx_mbps, c.link_tx_mbps) if x), default=None)
        v, why = "last_hop_signal", (f"ping is clean right now (Wi-Fi hop +{hop} ms), but the device signal is {c.signal_dbm} dBm "
                                     f"({c.signal_grade})" + (f" and its PHY link is down to {low} Mbps" if low and low < 50 else "")
                                     + ": throughput and calls suffer; move it or a node closer")
    else:
        v, why = "ok", f"Wi-Fi hop adds {hop} ms; mesh and internet look fine"
    up = None
    if c and c.path_grade in ("poor", "bad") and v != "upstream_backhaul":
        up = f"{c.path_grade} via {c.weakest_hop}"
        why += f"; also, the path to the main router is {up} (a second bottleneck once the Wi-Fi hop is fixed)"
    return {"verdict": v, "why": why, "last_hop_ms": hop, "node_ms": nd.get("avg_ms"),
            "basis": "ping+signal" if v == "last_hop_signal" or weak else "ping",
            "moves_1h": flaps, "upstream": up, "signal": f"{c.signal_dbm}dBm" if c and c.signal_dbm is not None else None,
            "path_grade": c.path_grade if c else None}


@net.command("speed")
@click.option("--engine", type=click.Choice(["auto", "cloudflare", "apple", "ookla", "networkquality"]), default="auto",
              show_default=True,
              help="auto: speed.cloudflare.com, Apple's servers if rate-limited. cloudflare: like the web page (~7 s). "
                   "apple / ookla: Apple's or the nearest Speedtest.net server, parallel connections fill the line "
                   "(~14 s). networkquality: macOS's own tool. All but networkquality work on any OS.")
@click.option("--connections", type=click.IntRange(1, 16), default=None,
              help="Parallel connections (default: cloudflare 1, like the web page; apple/ookla 8).")
@click.option("--no-upload", is_flag=True, help="Download only.")
@click.option("--mb", type=int, hidden=True, help="(ignored: sizes adapt to the link)")
@json_option
@pass_state
def speed_cmd(st, engine, connections, no_upload, mb):
    """Internet speed of THIS machine: latency, download, upload, latency under load (bufferbloat). No router login.

    \b
    Talks to the providers' public test servers directly (no Ookla/Apple tools needed). At a terminal it
    animates; piped/agents get one `speed` record. Uses ~0.1-1.5 GB depending on the link and engine.
      tpctl net speed                     like speed.cloudflare.com
      tpctl net speed --engine apple      fill the line: does latency explode under load?
    Library: `from tplink_router import speedtest; speedtest.run("ookla")`.
    """
    import sys
    from .. import speedtest
    from ..speedtest import view
    if not speedtest.available(engine if engine != "auto" else "cloudflare"):
        raise click.UsageError("--engine networkquality needs macOS; use --engine apple (same servers) elsewhere")
    live = st.format == "human" and sys.stderr.isatty()
    if live:
        with view.LiveView(upload=not no_upload, engine="cloudflare" if engine == "auto" else engine) as lv:
            res = speedtest.run(engine, lv.update, upload=not no_upload, connections=connections, on_engine=lv.set_engine)
    else:
        res = speedtest.run(engine, upload=not no_upload, connections=connections)
    via = {True: "wired", False: "wifi", None: "unknown link"}[_wired()]
    if not res.ok:
        from ..errors import SpeedTestError
        err = "; ".join(res.errors) or "no measurement"
        raise SpeedTestError(f"speed test failed: {err}" + (
            " — retry in ~10 min or use --engine apple / ookla" if res.rate_limited else
            ". Check this machine's internet with `tpctl net ping --no-mesh`"), retry_after_s=600 if res.rate_limited else None)
    loaded = [x for x in (res.loaded_latency_down_ms, res.loaded_latency_up_ms) if x is not None]
    bloat = round(max(0.0, max(loaded) - res.latency_ms), 1) if loaded and res.latency_ms is not None else None
    one_conn = res.engine == "cloudflare" and (res.flows or {}).get("down", 1) <= 1
    d = {**res.__dict__, "latency_samples": [round(x, 2) for x in res.latency_samples], "bufferbloat_ms": bloat, "this_machine": via}
    srv = res.server or {}
    st.out(d, [o.line("speed", download=o.unit(res.download_mbps, "Mbps"), upload=o.unit(res.upload_mbps, "Mbps"),
                      latency=o.unit(res.latency_ms, "ms"), jitter=o.unit(res.jitter_ms, "ms"),
                      loaded_latency_down=o.unit(res.loaded_latency_down_ms, "ms"),
                      loaded_latency_up=o.unit(res.loaded_latency_up_ms, "ms"), bufferbloat=o.unit(bloat, "ms"),
                      bufferbloat_reliable="no (one connection rarely fills the line: use --engine apple)" if one_conn and bloat is not None else None,
                      engine=res.engine, connections=(res.flows or {}).get("down") or 1,
                      server=(f"{srv.get('city')} ({srv.get('colo')})" if srv.get("city") else srv.get("colo")),
                      isp=(res.client or {}).get("isp"), this_machine=via,
                      partial="yes (speed.cloudflare.com rate-limited this machine: fewer samples; retry in ~10 min "
                              "or --engine apple)" if res.rate_limited and res.engine == "cloudflare" else None,
                      data=f"{(res.bytes_down + res.bytes_up) / 1e6:.0f}MB", duration=f"{res.duration_s}s",
                      notes=res.errors or None)],
           lambda: view.summary(res, via))


def _wired() -> bool | None:
    """Is this machine's default route on Ethernet? macOS (route + networksetup), Linux (ip route + sysfs).
    None when it can't tell."""
    import os
    import subprocess
    import sys
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
            iface = next((l.split()[-1] for l in r.splitlines() if "interface:" in l), "")
            hw = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
            block = hw.split(f"Device: {iface}")[0].rsplit("Hardware Port:", 1)[-1] if iface else ""
            return bool(iface) and "Wi-Fi" not in block
        if sys.platform.startswith("linux"):
            r = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5).stdout.split()
            iface = r[r.index("dev") + 1] if "dev" in r else ""
            return bool(iface) and not os.path.exists(f"/sys/class/net/{iface}/wireless")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


COMMANDS = [net]
