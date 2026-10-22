import click

from .. import human as h
from .. import output as o
from .. import quality
from ._ctx import json_option, pass_state


@click.command("overview", short_help='Everything at a glance: verdict, what changed, internet, Wi-Fi, mesh, devices, issues.')
@click.option("--devices", "--clients", "with_devices", is_flag=True, help="Also list every device.")
@click.option("--strict", is_flag=True, help="Exit 1 when the verdict is warn/error (like `check`). Default: exit 0 if it ran.")
@json_option
@pass_state
@click.pass_context
def overview_cmd(ctx, st, with_devices=False, strict=False):
    """Everything in one call: verdict, changes since last look, status, wifi, mesh (+ping), devices, issues.

    \b
    Examples:
      tpctl overview                 # concise; first line is the verdict
      tpctl overview --devices -v    # every device and every field
      tpctl overview -o json         # {schema_version, command, ok, data}
    """
    d = st.r.health.overview(track=True, light=st.format == "records" and not o.DETAIL)
    inv = d["inventory"]

    def text():
        if o.DETAIL:
            yield o.verdict_line(d["verdict"])
            yield o.line("time", now=d["time"])
            yield from o.change_lines(d["changes"])
            yield from o.status_lines(d["status"])
            yield from o.wifi_lines(d["wifi"])
            yield from (o.node_line(n) for n in inv.nodes)
            yield from o.summary_lines(d["summary"])
            if with_devices:
                yield from (o.device_line(c) for c in sorted(inv.devices, key=lambda c: (c.node, c.band, c.label)))
            yield from o.issue_lines(d["issues"])
            return
        yield o.line("verdict", **d["verdict"], at=o.ts(d["time"]))
        yield from o.change_lines(d["changes"], quiet=True)
        if d.get("recent_writes"):
            from collections import Counter
            rw = d["recent_writes"]
            yield o.line("recent_writes", since=o.ts(rw[0]["at"]), count=len(rw), last=o.ts(rw[-1]["at"]),
                         tags=",".join(f"{k}×{v}" if v > 1 else k for k, v in Counter(x["tag"] for x in rw).items()),
                         see="tpctl snapshot timeline --since 1h --writes (untracked_writes: writes the snapshots "
                             "don't record; their state is in that command's own list)")
        st_, w = d["status"], d["wifi"]
        yield o.line("wan", internet=st_["wan"]["internet"], ipv4=st_["wan"]["ipv4"], cgnat="yes" if st_["wan"]["cgnat"] else "no",
                     uptime=o.unit(st_["wan"]["uptime_min"], "min"), ipv6_lan_prefix=st_["ipv6"].get("lan_prefix") or "none",
                     firewall=st_["security"]["spi_firewall"])
        if st_.get("inbound"):
            yield o.line("inbound", **st_["inbound"])
        yield o.line("internet", **{("v6" if ":" in hst else "v4"): (f"{p.avg_ms}ms" if p.up else "down") for hst, p in d["internet"].items()},
                     loss=",".join(f"{p.loss_pct:.0f}%" for p in d["internet"].values()), samples=3,
                     grade=",".join(str(quality.ping(p.loss_pct, p.avg_ms if p.up else None)) for p in d["internet"].values()))
        per_band = {}
        for b, x in w["bands"].items():
            if x["enable"] != "on":
                continue
            per_band |= {f"{b}_channel": x["channel"], f"{b}_channel_mode": "auto" if x.get("channel_setting") in ("auto", "0") else "fixed",
                         f"{b}_width": o.unit(x["width"], "MHz") if str(x["width"]).isdigit() else x["width"],
                         f"{b}_txpower": x["txpower"]}
        yield o.line("wifi", ssid=sorted({b["ssid"] for b in w["bands"].values() if b.get("ssid")}),
                     smart_connect=w["smart_connect"], **per_band)
        yield from (o.node_line(n) for n in inv.nodes)
        yield o.line("devices", total=d["summary"]["total"], **{f"band_{k}": v for k, v in sorted(d["summary"]["by_band"].items())})
        if with_devices:
            yield from (o.device_line(c) for c in sorted(inv.devices, key=lambda c: (c.node, c.band, c.label)))
        yield from o.issue_lines(d["issues"])
    full = d
    if not with_devices:
        d = {**d, "inventory": {"nodes": inv.nodes}}
    st.out(d, text(), lambda: h.overview(full, with_devices))
    if strict and full["verdict"]["status"] in ("warn", "error"):
        ctx.exit(1)


@click.command("check", short_help='Health check: issue codes with fixes; exit 1 on warnings/errors.')
@json_option
@pass_state
@click.pass_context
def check_cmd(ctx, st):
    """Health verdict: internet v4/v6, WAN, firewall, every node (ping, backhaul, depth), device outliers.

    First line `verdict status=ok|warn|error|partial`. Exit 1 if warn/error. Each issue has code + hint.
    """
    d = st.r.health.check(track=True)        # global --no-track / TPCTL_NO_TRACK is honoured inside

    def text():
        yield o.line("verdict", **d["verdict"], at=o.ts(d["time"]))
        yield from o.change_lines(d["changes"], quiet=not o.DETAIL)
        yield o.line("internet", wan=d["wan"]["internet"], **{hst: (f"{p.avg_ms}ms" if p.up else "down") for hst, p in d["internet"].items()})
        yield o.line("mesh", online=d["mesh_online"])
        yield from (o.node_line(n) for n in d["inventory"].nodes)
        if o.DETAIL:
            yield from o.summary_lines(d["summary"])
        yield from o.issue_lines(d["issues"])
    def human():
        inv = d["inventory"]
        net_ = " · ".join(f"{host} " + (h.grade(None, 'down') if not p.up else f"{p.avg_ms:.1f} ms") for host, p in d["internet"].items())
        return [*h.verdict(d["verdict"], d["time"][11:16], d["changes"]), *h.changes(d["changes"]), "",
                h.kvline("Internet", f"{d['wan']['internet']} · {net_}"), h.kvline("Mesh", f"{d['mesh_online']} nodes online"),
                h.section("Mesh"), *h.mesh(inv.nodes), h.section("Devices") + "  " + h.summary(d["summary"]),
                *h.issues(d["issues"], max_info=10)]
    st.out({k: v for k, v in d.items() if k != "inventory"} | {"nodes": d["inventory"].nodes}, text(), human)
    ctx.exit(0 if d["verdict"]["status"] in ("ok", "partial") else 1)


@click.command("status")
@json_option
@pass_state
def status_cmd(st):
    """Router, WAN (CGNAT/MTU/DNS), IPv6, DHCP, firewall/NAT/UPnP, ALGs."""
    s = st.r.status.get()
    st.out(s, o.status_lines(s), lambda: h.status_block(s))


COMMANDS = [overview_cmd, check_cmd, status_cmd]
