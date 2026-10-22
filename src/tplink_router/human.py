"""Human-oriented rendering for terminals: aligned tables, sections, color, the mesh as a tree.

Records (output.py) stay the machine contract; this module is presentation only and may change freely.
Width-aware (CJK = 2 columns) so tables with Chinese node names stay aligned.
"""
from __future__ import annotations

import os
import shutil
import sys
import unicodedata

from .models import Device, Inventory, Issue, Node, WriteResult

# ---- styling -------------------------------------------------------------
_COLOR = None


def color_on() -> bool:
    global _COLOR
    if _COLOR is None:
        _COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    return _COLOR


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if color_on() and s else s


def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def red(s): return _c("31", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def cyan(s): return _c("36", s)


GRADE_STYLE = {"excellent": green, "good": green, "fair": yellow, "poor": red, "bad": red}
LEVEL_STYLE = {"error": lambda s: bold(red(s)), "warn": yellow, "info": dim}


def grade(g: str | None, text: str | None = None) -> str:
    if not g:
        return dim(text or "—")
    return GRADE_STYLE.get(g, str)(text or g)


# ---- width-aware table -------------------------------------------------------
def width(s: str) -> int:
    import re
    plain = re.sub(r"\033\[[0-9;]*m", "", s)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1 for ch in plain)


def pad(s: str, n: int, right: bool = False) -> str:
    fill = " " * max(0, n - width(s))
    return fill + s if right else s + fill


def table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset(), indent: int = 2) -> list[str]:
    if not rows:
        return []
    cols = len(headers)
    w = [max(width(h), *(width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sp = " " * indent
    out = [sp + "  ".join(dim(pad(h, w[i], i in right)) for i, h in enumerate(headers)).rstrip()]
    out += [sp + "  ".join(pad(r[i], w[i], i in right) for i in range(cols)).rstrip() for r in rows]
    return out


def section(title: str) -> str:
    return "\n" + bold(title)


def kvline(label: str, value: str, w: int = 10) -> str:
    """'Label     value', wrapping long comma/· lists under the value column."""
    avail = max(30, term_width() - w - 1)
    if width(value) <= avail or ", " not in value:
        return f"{dim(pad(label, w))} {value}"
    lines, cur = [], ""
    for part in value.split(", "):
        nxt = f"{cur}, {part}" if cur else part
        if width(nxt) > avail and cur:
            lines.append(cur + ",")
            cur = part
        else:
            cur = nxt
    lines.append(cur)
    return "\n".join(f"{dim(pad(label if i == 0 else '', w))} {ln}" for i, ln in enumerate(lines))


def term_width() -> int:
    return shutil.get_terminal_size((100, 20)).columns


# ---- pieces ------------------------------------------------------------------
def verdict(v: dict, when: str | None = None, changes: dict | None = None) -> list[str]:
    icon = {"ok": green("✓ OK"), "warn": yellow("⚠ WARN"), "error": bold(red("✗ ERROR")), "partial": yellow("◐ PARTIAL")}
    parts = []
    if v["errors"]:
        parts.append(red(f"{v['errors']} error{'s' * (v['errors'] > 1)}"))
    if v["warnings"]:
        parts.append(yellow(f"{v['warnings']} warning{'s' * (v['warnings'] > 1)}"))
    if v["infos"]:
        parts.append(dim(f"{v['infos']} info"))
    head = f"{icon.get(v['status'], v['status'])}  " + (" · ".join(parts) or green("no problems"))
    if v["status"] != "ok" and v["headline"] != "no problems":
        head += dim("  — ") + v["headline"].replace(":", " ").replace("; ", ", ")
    out = [bold(head)]
    if when and not (changes and changes.get("changes")):     # the Changes section names the time itself
        bits = [when]
        if changes is not None and not changes.get("since"):
            bits.append("first look (changes tracked from now on)")
        elif changes is not None:
            bits.append(f"no changes since {changes['minutes_ago']} min ago")
        out.append(dim(" · ".join(bits)))
    if v.get("partial_reads"):  # None when 0
        out.append(yellow(f"some data could not be read ({v['partial_reads']} endpoints) — see warnings at the end"))
    return out


def changes(ch: dict) -> list[str]:
    if not ch or not ch.get("changes"):
        return []
    n = len(ch["changes"])
    out = [section(f"{n} change{'s' * (n != 1)} since {ch['since'][11:16]}") + dim(f"  ({ch.get('minutes_ago', '?')} min ago)")]
    # joins/leaves and node events first (rarer, more telling), then moves; long lists fold
    order = sorted(ch["changes"], key=lambda c: (c["what"] == "device_moved", c["what"]))
    limit = 8
    for c in order[:limit]:
        w = c["what"]
        if w == "device_moved":
            out.append(f"  {c['subject']}  moved  {dim(c['old'])} → {c['new']}")
        elif w == "device_joined":
            out.append(f"  {green('+')} {c['subject']}  joined on {c['node']} ({c['band']})")
        elif w == "device_left":
            out.append(f"  {red('−')} {c['subject']}  left {dim(c['node'])}")
        elif w.startswith("node_"):
            out.append(f"  {bold(c['subject'])}  {w[5:]}: {dim(str(c.get('old')))} → {c.get('new')}")
        else:
            out.append(f"  {w} {c.get('subject')}")
    if n > limit:
        moves = sum(1 for c in order[limit:] if c["what"] == "device_moved")
        out.append(dim(f"  … {n - limit} more ({moves} moves) — tpctl events --since 1h"))
    return out


def mesh(nodes: list[Node]) -> list[str]:
    """Tree in the first column, then uplink quality, devices, ping."""
    kids: dict = {}
    for n in nodes:
        kids.setdefault(n.parent_mac, []).append(n)
    known = {n.mac for n in nodes}
    roots = kids.get(None, []) + [n for n in nodes if n.parent_mac and n.parent_mac not in known]
    rows = []

    def walk(n: Node, prefix: str, last: bool, top: bool):
        branch = "" if top else ("└─ " if last else "├─ ")
        name = prefix + branch + (bold(n.name) if n.is_main else n.name)
        if n.is_main:
            bh = dim("main router")
        elif not n.online:
            bh = bold(red("OFFLINE"))
        elif n.backhaul == "wired":
            bh = green("wired")
        else:
            sig = f"{n.backhaul_dbm} dBm" if n.backhaul_dbm is not None else f"{n.backhaul_level}/5"
            bh = grade(n.backhaul_grade, f"wifi {sig} {n.backhaul_grade or ''}".strip())
        link = f"{n.backhaul_mbps} Mbps" if n.backhaul_mbps else ""
        weak = f"  {red(str(n.devices_weak) + ' weak')}" if n.devices_weak else ""
        if n.ping is None:
            ping = dim("—")
        elif not n.ping.up:
            ping = red("down")
        else:
            ping = grade(n.ping_grade, f"{n.ping.avg_ms:.1f} ms" + (f" {n.ping.loss_pct:.0f}% loss" if n.ping.loss_pct else ""))
        upstream = (grade(n.path_grade, f"{n.path_grade} via upstream") if n.path_grade and n.path_grade != n.backhaul_grade
                    and n.path_grade in ("fair", "poor", "bad") else "")
        rows.append([name, bh, link, upstream, f"{n.devices}{weak}", ping])
        ch = kids.get(n.mac, [])
        for i, k in enumerate(ch):
            walk(k, prefix + ("" if top else ("   " if last else "│  ")), i == len(ch) - 1, False)
    for r in roots:
        walk(r, "", True, True)
    if not any(r[3] for r in rows):                      # drop the PATH column when nothing is limited upstream
        rows = [r[:3] + r[4:] for r in rows]
        return table(["NODE", "BACKHAUL", "LINK", "DEVICES", "PING"], rows, right={2})
    return table(["NODE", "BACKHAUL", "LINK", "PATH", "DEVICES", "PING"], rows, right={2})


def devices(cs: list[Device], show_node: bool = True) -> list[str]:
    rows = []
    for c in cs:
        sig = grade(c.signal_grade, f"{c.signal_dbm} dBm") if c.signal_dbm is not None else dim("—")
        rate = f"{c.link_rx_mbps}/{c.link_tx_mbps}" if (c.link_rx_mbps or c.link_tx_mbps) else dim("idle" if c.band != "wired" else "—")
        via = grade(c.path_grade, f"{c.path_grade} via {c.weakest_hop}") if c.path_grade in ("fair", "poor", "bad") else ""
        row = [c.label, c.ip, c.band, sig, rate, via]
        if show_node:
            row.insert(3, c.node)
        rows.append(row)
    hdr = ["DEVICE", "IP", "BAND", "SIGNAL", "LINK rx/tx", "UPSTREAM"]
    if show_node:
        hdr.insert(3, "NODE")
    if not any(r[-1] for r in rows):
        hdr, rows = hdr[:-1], [r[:-1] for r in rows]
    return table(hdr, rows, right={len(hdr) - 1} if hdr[-1] == "LINK rx/tx" else {len(hdr) - 2})


def summary(s: dict) -> str:
    order = ["2.4G", "5G", "6G", "wired"]
    bands = " · ".join(f"{b} {s['by_band'][b]}" for b in order if b in s["by_band"])
    return f"{bold(str(s['total']))} devices  {dim(bands)}"


def humanize(detail: str) -> str:
    """'uplink=X signal=-78dBm level=2/5' -> 'uplink X · signal -78 dBm · level 2/5'."""
    import re
    if "=" not in detail:
        return detail
    parts = re.findall(r"(\w+)=(\S+)", detail)
    if not parts:
        return detail
    fmt = lambda v: re.sub(r"^(-?\d+)(dBm|Mbps|ms)$", r"\1 \2", v)
    return " · ".join(f"{k.replace('_', ' ')} {fmt(v)}" for k, v in parts)


def issues(iss: list[Issue], max_info: int = 3) -> list[str]:
    if not iss:
        return [section("Issues"), "  " + green("none")]
    out = [section("Issues")]
    infos = [i for i in iss if i.level == "info"]
    for i in [i for i in iss if i.level != "info"] + infos[:max_info]:
        tag = LEVEL_STYLE[i.level](pad(i.level.upper(), 5))
        out.append(f"  {tag}  {bold(i.subject)}  {i.code.lower().replace('_', ' ')}")
        out.append(f"         {dim(i.detail)}")
        if i.level != "info":
            out.append(f"         {cyan('→')} {i.hint}")
    if len(infos) > max_info:
        out.append(dim(f"  … {len(infos) - max_info} more info items (tpctl check, or --format records for all)"))
    return out


def status_block(s: dict) -> list[str]:
    w, v6, sec = s["wan"], s["ipv6"], s["security"]
    inet = green("connected") if w["internet"] == "connected" else red(str(w["internet"]))
    wan = f"{inet} · {str(w['type']).upper()} {w['ipv4']}" + (yellow(" (CGNAT: no public IPv4)") if w["cgnat"] else "") + \
          dim(f" · up {w['uptime_min'] // 60}h{w['uptime_min'] % 60:02d}m")
    ipv6 = (green("✓ ") + v6["lan_prefix"] + dim("/64 · ") + str(v6["lan_assign"])) if v6.get("lan_prefix") else red("not available")
    onoff = lambda x, good="on": (green(x) if x == good else yellow(x)) if x else dim("—")
    secs = f"firewall {onoff(sec['spi_firewall'])} · UPnP {onoff(sec['upnp'], 'off')} · DMZ {onoff(sec['dmz'], 'off')} · " \
           f"{sec['port_forwards']} port forwards · remote admin {onoff(sec['remote_mgmt'], 'off')}"
    d = s["dhcp"]
    return [kvline("Router", f"{s['router']['model']} {dim('fw ' + s['router']['firmware'])}"),
            kvline("Internet", wan), kvline("IPv6", ipv6), kvline("Security", secs),
            *([kvline("Inbound", f"IPv4 {s['inbound']['ipv4']} · IPv6 {s['inbound']['ipv6']}")] if s.get("inbound") else []),
            kvline("DHCP", f"{d['range']} · lease {d['lease_min']} min")]


def wifi_block(w: dict) -> list[str]:
    bands = []
    for b, x in w["bands"].items():
        width_ = x["width"] if x["width"] in (None, "auto") else f"{x['width']} MHz"
        st = "" if x["enable"] == "on" else red(" off")
        bands.append(f"{b.replace('g', 'G').replace('_2', '-2')} ch{x['channel']} {dim(width_)}{st}")
    ssids = sorted({x["ssid"] for x in w["bands"].values() if x.get("ssid")})
    sc = w["smart_connect"]
    return [kvline("Wi-Fi", f"SSID {bold(', '.join(ssids))} · smart connect {sc} · " + " · ".join(bands)),
            kvline("", dim(f"OFDMA/MU-MIMO {w['ofdma_mimo']} · region {w['region']} · guest {w['guest_network']}"))]


def write(res: WriteResult, effect: bool = True, title: str | None = None) -> list[str]:
    if res.dry_run:
        out = [bold(cyan(title or "DRY RUN — nothing was written"))]
        for c in res.changes:
            out.append(f"  {c.key}: {c.old} → {bold(str(c.new))}" if c.changed else dim(f"  {c.key}: already {c.old}"))
        if res.side_effect and effect:
            out.append(yellow(f"  would cause: {res.side_effect}"))
        return out
    if not res.applied:
        return [green("✓ nothing to do") + dim(f" — {res.skipped_reason}")]
    bad = res.mismatches
    out = [green("✓ applied") if not bad else bold(red("✗ applied but not verified"))]
    for c in res.changes:
        if not c.verified:
            out.append(red(f"  {c.key}: asked {c.target}, router reports {c.new} (was {c.old})"))
        else:
            out.append(f"  {c.key}: {dim(str(c.old))} → {bold(str(c.new))}" if c.changed
                       else yellow(f"  {c.key}: still {c.new}"))
    if res.side_effect:
        out.append(yellow(f"  note: {res.side_effect}"))
    if res.next:
        out.append(dim(f"  next: {res.next}"))
    if res.snapshot:
        out.append(dim(f"  snapshot: {res.snapshot}"))
    return out


def warnings(ws: list[dict]) -> list[str]:
    if not ws:
        return []
    return [section(yellow("Warnings"))] + [yellow(f"  could not read {w['endpoint']} ({w['op']}) — related fields are blank")
                                          for w in ws]


def _pgrade(p):
    from . import quality
    return quality.ping(p.loss_pct, p.avg_ms)


def overview(d: dict, with_devices: bool) -> list[str]:
    inv: Inventory = d["inventory"]
    out = verdict(d["verdict"], d["time"][11:16], d["changes"])
    out += changes(d["changes"])
    lat = " · ".join(f"{'IPv6' if ':' in hst else 'IPv4'} " + (grade(_pgrade(p), f"{p.avg_ms:.1f} ms") if p.up else red("down"))
                     for hst, p in d.get("internet", {}).items())
    out += [""] + status_block(d["status"]) + ([kvline("Latency", lat)] if lat else []) + wifi_block(d["wifi"])
    out += [section("Mesh")] + mesh(inv.nodes)
    out += [section("Devices") + "  " + summary(d["summary"])]
    if with_devices:
        out += devices(sorted(inv.devices, key=lambda c: (c.node, c.band, c.label)))
    else:
        out.append(dim("  tpctl devices list  for the full list"))
    out += issues(d["issues"])
    return out


# ---- generic fallback: records -> human ----------------------------------------
import json as _json
import re as _re

_FIELD = _re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
_HIDE = {"mac", "role", "client_class", "fp"}          # noise for humans in generic views


def parse_record(ln: str) -> tuple[str, dict] | None:
    if not ln or " " not in ln and "=" in ln:
        return None
    kind, _, rest = ln.partition(" ")
    if not _re.fullmatch(r"[a-z_.]+", kind):
        return None
    fields = {}
    for k, v in _FIELD.findall(rest):
        if v.startswith('"'):
            try:
                v = _json.loads(v)
            except ValueError:
                pass
        fields[k] = v
    return kind, fields


def _label(k: str) -> str:
    return k.replace("_", " ")


def from_records(lines) -> list[str]:
    """Consecutive records of the same kind -> table; a lone record -> aligned key/value block."""
    recs, raw = [], []
    for ln in lines:
        p = parse_record(ln)
        if p:
            recs.append(p)
        elif ln.strip():
            raw.append(ln)
    out: list[str] = []
    i = 0
    while i < len(recs):
        kind, f = recs[i]
        j = i
        while j + 1 < len(recs) and recs[j + 1][0] == kind:
            j += 1
        group = recs[i:j + 1]
        if len(group) == 1:
            keys = [k for k in f if k not in _HIDE]
            w = max((width(_label(k)) for k in keys), default=0)
            if out:
                out.append("")
            out.append(bold(_label(kind)))
            out += [f"  {dim(pad(_label(k), w))}  {_fmt(f[k])}" for k in keys]
        else:
            cols = []
            for _, g in group:
                cols += [k for k in g if k not in cols and k not in _HIDE]
            if out:
                out.append("")
            out.append(bold(_label(kind)) + dim(f"  ({len(group)})"))
            out += table([_label(c).upper() for c in cols], [[_fmt(g.get(c, "")) for c in cols] for _, g in group])
        i = j + 1
    return out + raw


def _fmt(v) -> str:
    s = str(v)
    if s in ("True", "yes", "on", "ok", "connected"):
        return green(s)
    if s in ("False", "no", "off"):
        return dim(s)
    if s in ("down", "error", "bad", "poor", "disconnected"):
        return red(s)
    return s


# ---- dedicated views for the remaining commands --------------------------------
def _dur(s: int | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    return f"{s}s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600}h{(s % 3600) // 60:02d}m"


def auth_status(d: dict) -> list[str]:
    src = {"env": "environment variable TPLINK_PASSWORD", "keychain": "macOS Keychain (tplink-router)",
           "file": "~/.config/tplink-router/password", "arg": "passed in code", None: None}[d["password_source"]]
    out = [kvline("Router", d["host"]),
           kvline("Password", green("✓ ") + src if src else red("✗ not set") + dim("  → tpctl auth set-password"))]
    s = d.get("session")
    if not s:
        out.append(kvline("Session", dim("none yet (next command logs in)")))
    elif s["expired_by_ttl"]:
        out.append(kvline("Session", yellow("expired") + dim(f" (idle {_dur(s['idle_s'])}; next command logs in again)")))
    else:
        left = s["ttl_s"] - s["idle_s"]
        out.append(kvline("Session", green("active") + dim(f" · logged in {_dur(s['created_s_ago'])} ago · idle {_dur(s['idle_s'])}"
                                                           f" · expires in {_dur(left)} if unused")))
    if d.get("check"):
        out.append(kvline("Check", green("✓ router accepted the session") + dim(f" ({d['check']['session_source']})")))
    return out


def pings(rows: list[dict]) -> list[str]:
    from . import quality
    t = []
    for x in rows:
        up = x.get("loss_pct", 100) < 100
        g = quality.ping(x.get("loss_pct"), x.get("avg_ms"))
        lat = grade(g, f"{x['avg_ms']:.1f} ms") if up and x.get("avg_ms") is not None else red("down")
        loss = dim("0%") if x.get("loss_pct") == 0 else red(f"{x.get('loss_pct', 100):.0f}%")
        t.append([x["name"], x["host"], lat, dim(f"{x['max_ms']:.1f} ms") if x.get("max_ms") else dim("—"), loss])
    return table(["TARGET", "ADDRESS", "AVG", "MAX", "LOSS"], t, right={2, 3, 4})


def snapshots(paths: list[str]) -> list[str]:
    import datetime as dt
    from pathlib import Path
    rows = []
    for p in reversed(paths):
        name = Path(p).stem                         # 20261022-125810-alg
        try:
            when = dt.datetime.strptime(name[:15], "%Y%m%d-%H%M%S").strftime("%m-%d %H:%M:%S")
        except ValueError:
            when = "?"
        rows.append([when, name[16:] or "-", dim(Path(p).name)])
    return table(["TAKEN", "BEFORE", "FILE"], rows) + [dim(f"  in {Path(paths[0]).parent}")] if paths else [dim("no snapshots yet")]


def diff(d: dict) -> list[str]:
    from pathlib import Path
    head = bold("Config diff") + dim(f"  {Path(d['old']).name} → {d['new'] if d['new'] == 'live' else Path(d['new']).name}")
    if not d["changes"]:
        return [head, "  " + green("no differences")]
    rows = [[c.key, dim(str(c.old)), bold(str(c.new))] for c in d["changes"]]
    return [head, *table(["SETTING", "BEFORE", "AFTER"], rows)]


def dhcp(d: dict) -> list[str]:
    s = d["settings"]
    out = [kvline("Server", (green("on") if s["enable"] == "on" else red(str(s["enable"]))) + f" · range {s['range']}"
                   + (dim(f" ({s['pool_size']} addresses)") if s.get("pool_size") else "")),
           kvline("Lease", f"{s['lease_min']} min" + dim(f" ({int(s['lease_min']) / 60:g} h)") if str(s['lease_min']).isdigit() else str(s['lease_min'])),
           kvline("Gateway", f"{s['gateway']} · DNS {', '.join(s['dns'])}")]
    if d["reservations"]:
        out += [section(f"Reservations ({len(d['reservations'])})")]
        out += dhcp_reservations(d["reservations"])
    return out


def dhcp_reservations(rows: list[dict]) -> list[str]:
    if not rows:
        return [dim("no reservations")]
    st = {"in_use": green("in use"), "pending_renewal": yellow("pending renewal"), "offline": dim("offline"), "off": dim("off")}
    return table(["NAME", "RESERVED", "STATE", "NOW", "NODE", "MAC"],
                 [[x["name"] or dim("?"), x["ip"] or "", st.get(x["state"], x["state"]),
                   dim(x["current_ip"] or "") if x["current_ip"] != x["ip"] else "", dim(x["node"] or ""), dim(x["mac"])]
                  for x in rows])


def leases(rows: list[dict]) -> list[str]:
    def left(v):
        try:
            v = str(v)
            return v if ":" in v else _dur(int(v))
        except ValueError:
            return str(v)
    return table(["NAME", "IP", "MAC", "LEASE LEFT"],
                 [[x.get("name") or dim("?"), x.get("ipaddr", ""), dim(x.get("macaddr", "")), dim(left(x.get("leasetime")))]
                  for x in sorted(rows, key=lambda x: tuple(int(p) for p in str(x.get("ipaddr", "0")).split(".") if p.isdigit()))],
                 right={3}) + ["", dim(f"  {len(rows)} leases")]


def system_info(d: dict) -> list[str]:
    fw, t, led, night = d["firmware"], d["time"], d.get("led", {}), d.get("night_mode", {})
    ntp = ", ".join(x for x in (t.get("ntp_svr1"), t.get("ntp_svr2")) if x)
    out = [kvline("Model", f"{fw['model']} " + dim(f"({fw['hardware']})")), kvline("Firmware", fw["firmware"]),
           kvline("Clock", f"{t.get('date', '')} {t.get('time', '')} " + dim(f"· {'auto (NTP)' if t.get('type') == 'auto' else t.get('type')} · {ntp}")),
           kvline("LEDs", (green("on") if led.get("enable") == "on" else dim("off")) +
                  (dim(f" · night mode {night.get('time_start')}–{night.get('time_end')}") if night.get("enable") == "on" else dim(" · night mode off")))]
    rs = d.get("reboot_schedule") or {}
    if rs:
        out.append(kvline("Auto reboot", f"{rs.get('cycle')} {rs.get('day') or ''} {rs.get('time')}".replace("  ", " ")
                          if rs.get("enable") == "on" else dim("off")))
    return out


def nat(d: dict) -> list[str]:
    fw, n = d.get("firewall", {}), d.get("nat", {})
    onoff = lambda v, good="on": (green(v) if v == good else yellow(v)) if v else dim("—")
    alg = d.get("alg", {})
    out = [kvline("Firewall", f"SPI {onoff(fw.get('enable'))} · answer WAN ping {onoff(fw.get('wan_ping'), 'off')}"),
           kvline("NAT", f"{onoff(n.get('enable'))} · hardware boost {onoff(n.get('boost_enable'))}"),
           kvline("Exposure", f"UPnP {onoff(d.get('upnp', {}).get('enable'), 'off')} · DMZ {onoff(d.get('dmz', {}).get('enable'), 'off')}"
                              f" · port forwards {len(d.get('port_forwards') or {})} · IPv6 allow rules "
                              f"{len((d.get('ipv6_rules') or {}).get('list') or {})}"),
           kvline("ALG", "on: " + green(", ".join(k for k, v in sorted(alg.items()) if v == "on") or "none") +
                  dim("  ·  off: " + (", ".join(k for k, v in sorted(alg.items()) if v == "off") or "none")))]
    return out


def command_catalog(rows: list[dict]) -> list[str]:
    risk = {"read": dim("read"), "write": yellow("write"), "disruptive": red("disruptive")}
    groups: dict = {}
    for r in rows:
        parts = r["command"].split()
        groups.setdefault(parts[1] if len(parts) > 2 else "", []).append(r)
    out = []
    for g, rs in groups.items():
        out.append(section(g or "top-level"))
        out += table(["COMMAND", "RISK", "", "SUMMARY"],
                     [[r["command"].removeprefix("tpctl "), risk[r["risk"]],
                       dim("dry-run" if r["dry_run"] else "--yes" if r["needs_confirm_flag"] else ""), r["summary"][:70]] for r in rs])
    return out + ["", dim("  details: tpctl <command> --help · machine-readable: tpctl commands --json")]


def mesh_wait(d: dict) -> list[str]:
    if d["ok"]:
        return [green(f"✓ all nodes back online") + dim(f" after {d['elapsed_s']}s"), dim("  next: tpctl mesh show")]
    return [red(f"✗ timed out after {d['elapsed_s']}s") + f" — still down: {', '.join(d['still_down'])}",
            dim("  next: tpctl mesh node <name>, then tpctl mesh reboot <name> --yes, or power-cycle it")]


def aliases(al: dict) -> list[str]:
    if not al:
        return [dim("no aliases yet  → tpctl devices alias set <device|ip|mac> <name>")]
    return table(["NAME", "MAC"], [[n, dim(m)] for m, n in sorted(al.items(), key=lambda kv: kv[1].lower())])
