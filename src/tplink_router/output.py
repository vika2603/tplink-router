"""Agent-oriented rendering: one record per line `kind key=value ...`, explicit units, nothing truncated.

Values containing whitespace or quotes are JSON-quoted; None/"" fields are omitted; nested lists/dicts are inline JSON.
Every function returns lines (or prints via `emit`) so scripts can reuse them.
"""
import json
import re

from .models import Change, Device, Issue, Node, PingResult, WriteResult, to_dict

_NEEDS_QUOTE = re.compile(r'[\s"=]')


_IDENTITY = {"device": "label", "node": "name"}
DEVICE_KEYS = ["rank", "rank_note", "label", "ip", "mac", "band", "node", "signal", "signal_grade", "near_boundary", "signal_stale", "signal_source", "link", "link_rx", "link_tx", "name",
               "alias", "randomized_mac", "ipv6", "path_grade", "weakest_hop", "backhaul_limited", "slow_link", "just_roamed", "moves_1h", "last_move_at", "ssid", "type", "guest", "online"]
NODE_KEYS = ["name", "role", "model", "ip", "mac", "status", "depth", "uplink", "backhaul", "backhaul_grade",
             "backhaul_signal", "near_boundary", "backhaul_level", "backhaul_link", "backhaul_band", "path_grade", "path_link", "rank", "uplink_pref", "uplink_choices",
             "devices", "devices_behind", "device_bands", "device_signal_median", "device_signal_worst", "devices_weak", "weakest_device", "weakest_signal", "weakest_stale", "weakest_excluded", "devices_weak_stale", "weakest_behind", "weakest_behind_signal", "path_bottleneck",
             "ping", "loss", "ping_grade", "reboot_supported"]


def line(kind: str, /, **fields) -> str:
    if FIELDS and kind in _IDENTITY:
        keep = {_IDENTITY[kind], *FIELDS}
        if keep & {"signal", "backhaul_signal", "weakest_signal"}:
            keep |= {"near_boundary", "signal_stale", "signal_source"}   # the hedges travel with the reading they qualify
        fields = {k: v for k, v in fields.items() if k in keep}
    parts = [kind]
    for k, v in fields.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, bool):
            v = "yes" if v else "no"                 # never Python True/False in records
        if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float)) and "," not in str(x) for x in v):
            s = ",".join(str(x) for x in v)          # scalar lists: a,b,c
        elif isinstance(v, dict) and all(isinstance(x, (str, int, float, bool)) for x in v.values()):
            s = ",".join(f"{a}:{b}" for a, b in v.items())   # flat maps: a:1,b:2 (never nested JSON in a record)
        else:
            s = json.dumps(to_dict(v), ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)
        parts.append(f"{k}={json.dumps(s, ensure_ascii=False)}" if _NEEDS_QUOTE.search(s) else f"{k}={s}")
    return " ".join(parts)


def ts(v) -> str | None:
    """The one record time format: local 'YYYY-MM-DD HH:MM' (from ISO or 'YYYY-MM-DD HH:MM:SS'); tz: `tpctl schema`."""
    if not v or not isinstance(v, str) or not v[:4].isdigit():
        return v or None
    return v.replace("T", " ")[:16]


def unit(v, u):
    return None if v is None else f"{v}{u}"


def ping_str(p: PingResult | None):
    if p is None:
        return None, None
    return (f"{p.avg_ms}ms" if p.up and p.avg_ms is not None else "down"), f"{p.loss_pct:.0f}%"


SCHEMA_VERSION = 1   # bump only on breaking changes to record kinds/keys or the JSON envelope (additions don't bump)
DETAIL = False   # CLI sets this from --detail; library callers can pass detail=True per call
FIELDS: list[str] | None = None   # CLI --fields: project device/node records to these keys (identity key always kept)




def node_line(n: Node, detail: bool | None = None, rank: int | None = None, tied: bool = False) -> str:
    ping, loss = ping_str(n.ping)
    if FIELDS:
        detail = True                                # projection picks from the full set
    if not (DETAIL if detail is None else detail):
        return line("node", rank=rank, tied="yes" if tied else None, name=n.name, ip=n.ip, status=None if n.status == "connected" else n.status,
                    depth=n.depth or None, uplink=n.parent_name, backhaul=n.backhaul, backhaul_grade=n.backhaul_grade,
                    backhaul_signal=unit(n.backhaul_dbm, "dBm"),
                    near_boundary="yes" if n.backhaul == "wifi" and _nb(n.backhaul_dbm) and not _level_decides(n) else None,
                    backhaul_level=unit(n.backhaul_level, "/5") if n.backhaul == "wifi" else None,
                    backhaul_link=unit(n.backhaul_mbps, "Mbps"), backhaul_band=n.backhaul_band,
                    path_grade=n.path_grade if n.path_grade != n.backhaul_grade else None,
                    path_link=unit(n.path_link_mbps, "Mbps") if n.path_link_mbps != n.backhaul_mbps else None,
                    path_bottleneck=n.path_bottleneck if n.path_link_mbps != n.backhaul_mbps else None,
                    devices=n.devices,
                    devices_behind=n.devices_behind if not n.is_main else None,
                    devices_weak=n.devices_weak or None,
                    weakest_device=n.weakest_device if n.devices_weak else None,
                    weakest_signal=unit(n.weakest_signal_dbm, "dBm") if n.devices_weak else None,
                    weakest_stale="maybe" if n.devices_weak and n.weakest_stale else None,
                    weakest_excluded=n.weakest_excluded if n.devices_weak else None,
                    devices_weak_stale=n.devices_weak_stale or None,
                    weakest_behind=n.weakest_behind, weakest_behind_signal=unit(n.weakest_behind_dbm, "dBm"),
                    ping=ping, loss=loss, ping_grade=n.ping_grade)
    return line("node", name=n.name, role=n.role, model=n.model, ip=n.ip, mac=n.mac, status=n.status,
                depth=n.depth, uplink=n.parent_name, backhaul=n.backhaul, backhaul_grade=n.backhaul_grade,
                backhaul_signal=unit(n.backhaul_dbm, "dBm"),
                backhaul_level=unit(n.backhaul_level, "/5") if n.backhaul == "wifi" else None,
                backhaul_link=unit(n.backhaul_mbps, "Mbps"), backhaul_band=n.backhaul_band, path_grade=n.path_grade,
                path_link=unit(n.path_link_mbps, "Mbps"),
                uplink_pref=n.uplink_pref,
                uplink_choices=n.uplink_choices or None, devices=n.devices, devices_behind=n.devices_behind,
                device_bands=n.device_bands or None,
                device_signal_median=unit(n.device_signal_median, "dBm"), device_signal_worst=unit(n.device_signal_worst, "dBm"),
                devices_weak=n.devices_weak or None, weakest_device=n.weakest_device,
                weakest_signal=unit(n.weakest_signal_dbm, "dBm"), weakest_stale="maybe" if n.weakest_stale else None,
                weakest_excluded=n.weakest_excluded,
                devices_weak_stale=n.devices_weak_stale or None, weakest_behind=n.weakest_behind,
                weakest_behind_signal=unit(n.weakest_behind_dbm, "dBm"), ping=ping, loss=loss, ping_grade=n.ping_grade,
                reboot_supported=n.reboot_supported if not n.is_main else None)


def tree_lines(nodes: list[Node]) -> list[str]:
    """Indented human glance of the mesh, as `tree` records (indent = depth)."""
    kids: dict = {}
    for n in nodes:
        kids.setdefault(n.parent_mac, []).append(n)
    known = {n.mac for n in nodes}
    roots = kids.get(None, []) + [n for n in nodes if n.parent_mac and n.parent_mac not in known]
    out = []

    def walk(n, d):
        sig = f" {n.backhaul_dbm}dBm" if n.backhaul_dbm is not None else ""
        state = "" if n.online else " OFFLINE"
        out.append("tree " + "  " * d + f"{n.name} [{n.backhaul}{sig} {n.backhaul_grade or ''}] devices={n.devices}{state}".replace(" ]", "]"))
        for k in kids.get(n.mac, []):
            walk(k, d + 1)
    for r in roots:
        walk(r, 0)
    return out


def _level_decides(n: Node) -> bool:
    """The router's bar count already makes the grade worse than the dBm would: a dB of noise can't change it."""
    from . import quality
    lv = quality.LEVEL.get(n.backhaul_level) if isinstance(n.backhaul_level, int) else None
    return bool(lv) and quality.rank(lv) > quality.rank(quality.signal(n.backhaul_dbm))


def _nb(dbm) -> bool:
    from . import quality
    return quality.near_boundary(dbm)


def _link(c: Device) -> dict:
    """PHY rates; wireless device with no sample -> link=idle (router reports 0 when it has no recent traffic)."""
    if c.band != "wired" and not c.link_rx_mbps and not c.link_tx_mbps:
        return {"link": "idle", "signal_stale": "maybe",   # no traffic sample: the router may report an old RSSI
                "signal_source": ("placeholder" if c.signal_placeholder else
                                  "last_traffic" if c.signal_dbm is not None else None)}
    return {"link_rx": unit(c.link_rx_mbps, "Mbps") if c.link_rx_mbps else None,
            "link_tx": unit(c.link_tx_mbps, "Mbps") if c.link_tx_mbps else None,
            "signal_stale": "maybe" if c.signal_stale else None,             # implausible reading (<= -95 dBm)
            "signal_source": "placeholder" if c.signal_placeholder else (
                "implausible" if isinstance(c.signal_dbm, int) and c.signal_dbm < -85
                and max(c.link_rx_mbps or 0, c.link_tx_mbps or 0) > 500 else None),   # dBm vs PHY disagree
            "slow_link": "yes" if c.band != "wired" and 0 < min(x for x in (c.link_rx_mbps or 10**6, c.link_tx_mbps or 10**6)) < 20
            else None}


def device_line(c: Device, detail: bool | None = None, rank: int | None = None) -> str:
    rank_note = ("after settled readings: just roamed / maybe stale" if rank and (c.just_roamed or c.signal_stale) else None)
    if FIELDS:
        detail = True                                # projection picks from the full set
    if not (DETAIL if detail is None else detail):
        weak_path = c.path_grade in ("fair", "poor", "bad")
        return line("device", rank=rank, rank_note=rank_note, label=c.label, ip=c.ip, band=c.band, node=c.node, signal=unit(c.signal_dbm, "dBm"),
                    signal_grade=c.signal_grade, near_boundary="yes" if _nb(c.signal_dbm) else None,
                    **_link(c), path_grade=c.path_grade if weak_path else None,
                    weakest_hop=("own_node" if c.weakest_hop == c.node else c.weakest_hop) if weak_path else None, backhaul_limited="yes" if c.backhaul_limited else None,
                    just_roamed="yes" if c.just_roamed else None, moves_1h=c.moves_1h or None,
                    last_move_at=ts(c.last_move_at) if c.moves_1h else None)
    return line("device", label=c.label, ip=c.ip, mac=c.mac, band=c.band, node=c.node,
                signal=unit(c.signal_dbm, "dBm"), signal_grade=c.signal_grade,
                link_rx=unit(c.link_rx_mbps, "Mbps"), link_tx=unit(c.link_tx_mbps, "Mbps"),
                near_boundary=_nb(c.signal_dbm),
                name=c.name if c.name != c.label.split("#")[0] or c.alias else None, alias=c.alias,
                randomized_mac=c.randomized_mac, ipv6=",".join(c.ipv6) or None, path_grade=c.path_grade,
                weakest_hop=c.weakest_hop, backhaul_limited=c.backhaul_limited, ssid=c.ssid, type=c.type, guest=c.guest, online=unit(c.online_min, "min"))


def issue_lines(issues: list[Issue]) -> list[str]:
    if not issues:
        return [line("issues", count=0)]
    order = {"error": 0, "warn": 1, "info": 2}
    envelope = {"level", "code", "subject", "detail", "hint"}
    seen: set[str] = set()
    out = []
    for i in sorted(issues, key=lambda i: order.get(i.level, 9)):
        hint = i.hint if (DETAIL or i.code not in seen) else None   # same code again: hint already given above
        seen.add(i.code)
        out.append(line("issue", level=i.level, code=i.code, subject=i.subject,
                        **({"detail": i.detail} if not i.data or DETAIL else {}),
                        **{(f"data_{k}" if k in envelope else k): v for k, v in i.data.items()}, hint=hint))
    return out


def change_line(c: Change) -> str:
    from .services.snapshots import UNITS, key_note
    return line("changed" if c.changed else "unchanged", key=c.key, old=c.old, new=c.new, unit=UNITS.get(c.key),
                note=key_note(c.key))


def _with_unit(key: str, v):
    """Plan values get the units records use elsewhere (width in MHz, lease in min)."""
    if isinstance(v, (int, str)) and str(v).isdigit():
        if key.endswith(".width"):
            return f"{v}MHz"
        if key.endswith(".lease") or key.endswith("leasetime"):
            return f"{v}min"
    return v


def write_lines(w: WriteResult, status: str | None = None, with_effect: bool = True) -> list[str]:
    status = status or ("dry_run" if w.dry_run else "skipped" if not w.applied else "applied")
    out = [line("write", status=status, reason=w.skipped_reason, snapshot=w.snapshot)]
    if w.dry_run:
        out += [line("would_change" if c.changed else "already", key=c.key, current=_with_unit(c.key, c.old),
                     target=_with_unit(c.key, c.new)) for c in w.changes]
        if w.side_effect and with_effect:
            out.append(line("would_side_effect", detail=w.side_effect))
        return out
    out += [line("mismatch", key=c.key, target=c.target, actual=c.new, old=c.old) if not c.verified and w.applied
            else change_line(c) for c in w.changes]
    if w.side_effect and w.applied:
        out.append(line("side_effect", detail=w.side_effect))
    if w.next and w.applied:
        out.append(line("next", run=w.next))
    return out


def summary_lines(s: dict) -> list[str]:
    import datetime as _dt
    count = {"total": s.get("all_devices"), "shown": s["total"]} if s.get("filtered") else {"total": s["total"]}
    pre = "shown_band_" if s.get("filtered") else "band_"
    return [line("devices_summary", **count, **{f"{pre}{k}": v for k, v in sorted(s["by_band"].items())},
                 stale=s.get("stale"), read_at=_dt.datetime.now().strftime("%H:%M:%S")),
            *([line("shown_by_node" if s.get("filtered") else "devices_by_node", **s["by_node"],
                    read_at=_dt.datetime.now().strftime("%H:%M:%S"))] if s["by_node"] else [])]


def dict_lines(kind: str, d: dict) -> list[str]:
    """Flat dict -> one line; nested dict values become their own `<kind>.<key>` lines."""
    flat = {k: v for k, v in d.items() if not isinstance(v, dict) and not str(k).startswith("_")}
    out = [line(kind, **flat)] if flat else []
    for k, v in d.items():
        if isinstance(v, dict):
            out += dict_lines(f"{kind}.{k}", v)
    return out


def status_lines(s: dict) -> list[str]:
    w = s["wan"]
    out = [line("router", **s["router"]),
           line("wan", internet=w["internet"], type=w["type"], ipv4=w["ipv4"], gateway=w["gateway"],
                cgnat="yes" if w["cgnat"] else "no", uptime=unit(w["uptime_min"], "min"), mtu=w["mtu"],
                dns=",".join(w["dns"]), wan_mac=w["wan_mac"], lan_ip=w["lan_ip"]),
           line("ipv6", **{k: (",".join(v) if isinstance(v, list) else v) for k, v in s["ipv6"].items()}),
           line("dhcp", **{k: (",".join(v) if isinstance(v, list) else v) for k, v in s["dhcp"].items()
                           if k != "lease_min"}, lease=unit(s["dhcp"]["lease_min"], "min")),
           line("security", **s["security"]),
           line("inbound", **s["inbound"]) if s.get("inbound") else None,
           line("alg", **{k: v for k, v in s["alg"].items() if not k.startswith("_")})]
    out = [x for x in out if x]
    return out


def wifi_lines(w: dict) -> list[str]:
    keep = None if DETAIL else ("smart_connect", "ofdma_mimo", "region", "guest_network")
    out = [line("wifi", **{k: v for k, v in w.items() if k not in ("bands", "beacon_ms") and (keep is None or k in keep)},
                beacon=unit(w["beacon_ms"], "ms") if DETAIL else None)]
    for b, x in w["bands"].items():
        width = x["width"] if x["width"] in (None, "auto") else f"{x['width']}MHz"
        if DETAIL:
            out.append(line("band", band=b, enable=x["enable"], ssid=x["ssid"], channel=x["channel"],
                            channel_setting=x["channel_setting"], width=width, txpower=x["txpower"], security=x["security"],
                            security_raw=f"{x['encryption']}/{x['psk_version']}", mode=x["mode"], hidden=x["hidden"],
                            psc=x["psc"], afc=x["afc"], unii4=x["unii4"]))
        else:
            out.append(line("band", band=b, enable=x["enable"], ssid=x["ssid"], channel=x["channel"],
                            channel_setting=x["channel_setting"], width=width, txpower=x["txpower"], security=x["security"],
                            hidden=x["hidden"] if x["hidden"] == "on" else None))
    return out


def emit(data, text_lines, as_json: bool) -> None:
    if as_json:
        print(json.dumps(to_dict(data), ensure_ascii=False, indent=1))
    else:
        for ln in text_lines:
            print(ln)


def verdict_line(v: dict) -> str:
    return line("verdict", **v)


def change_lines(ch: dict, quiet: bool = False) -> list[str]:
    """quiet: emit nothing when there's nothing new (concise mode); changes themselves are always emitted."""
    if not ch or ch.get("since") is None:
        return [] if quiet else [line("changes", since="none (first observation)")]
    out = [line("changes", since=ts(ch["since"]), minutes_ago=ch["minutes_ago"], count=len(ch["changes"]),
                history="tpctl events" if quiet else None, suppressed=ch.get("suppressed"))]
    return out + [line("change", **c) for c in ch["changes"]]


def warning_lines(warnings: list[dict]) -> list[str]:
    return [line("warning", type="partial_read", endpoint=w["endpoint"], op=w["op"],
                 impact="fields from this endpoint are missing/None in the output above") for w in warnings]


def render(obj) -> list[str]:
    """Records for any model, list of models, WriteResult, Inventory, or plain dict."""
    from .models import Inventory
    if isinstance(obj, Node):
        return [node_line(obj)]
    if isinstance(obj, Device):
        return [device_line(obj)]
    if isinstance(obj, Issue):
        return issue_lines([obj])
    if isinstance(obj, WriteResult):
        return write_lines(obj)
    if isinstance(obj, Change):
        return [change_line(obj)]
    if isinstance(obj, Inventory):
        return [*(node_line(n) for n in obj.nodes), *tree_lines(obj.nodes), *(device_line(c) for c in obj.devices),
                *summary_lines(obj.summary())]
    if isinstance(obj, (list, tuple)):
        if obj and all(isinstance(x, Issue) for x in obj):
            return issue_lines(list(obj))
        return [ln for x in obj for ln in render(x)]
    if isinstance(obj, dict):
        return dict_lines("data", obj)
    return [line("value", value=obj)]


def show(*objs) -> None:
    """Print records for anything (models, lists, dicts). The scripting counterpart of CLI output."""
    for obj in objs:
        for ln in render(obj):
            print(ln)
