import re

import click

from .. import human as h
from .. import output as o
from ._ctx import json_option, pass_state


def _collapse_flaps(events: list[dict], within_min: int = 10) -> list[dict]:
    """node_grade A->B then B->A on the same node within minutes = reading noise at a threshold: one
    `node_grade_flap` event (old=A via=B) instead of two changes."""
    import datetime as dt
    out, skip = [], set()
    for i, e in enumerate(events):
        if i in skip:
            continue
        if e.get("what") == "node_grade":
            for j in range(i + 1, min(i + 6, len(events))):
                f = events[j]
                if (f.get("what") == "node_grade" and f.get("subject") == e.get("subject") and f.get("old") == e.get("new")
                        and f.get("new") == e.get("old")):
                    try:
                        gap = (dt.datetime.fromisoformat(f["at"]) - dt.datetime.fromisoformat(e["at"])).total_seconds() / 60
                    except (KeyError, ValueError):
                        gap = 0
                    from .. import quality as _q
                    went_down = any(x.get("what") == "node_status" and x.get("subject") == e.get("subject")
                                    and x.get("at") in (e.get("at"), f.get("at")) for x in events)   # outage, not noise
                    if abs(gap) <= within_min and not went_down:   # a disconnect is an outage, not reading noise
                        one = abs(_q.rank(e.get("old")) - _q.rank(e.get("new"))) == 1
                        out.append({k: v for k, v in e.items() if k not in ("old", "new")} |
                                   {"what": "node_grade_flap", "grade": e.get("old"), "dipped_to": e.get("new"),
                                    "near_boundary": "yes" if one else None})
                        skip.add(j)
                        break
            else:
                out.append(e)
            continue
        out.append(e)
    return out


def _minutes(s: str | None) -> int | None:
    if not s:
        return None
    if s.strip().lower() == "today":
        import datetime as dt
        now = dt.datetime.now()
        return int((now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() // 60) + 1
    m = re.fullmatch(r"(\d+)\s*([mhd])", s.strip().lower())
    if not m:
        raise click.BadParameter("use e.g. 30m, 6h, 2d or today", param_hint="--since")
    return int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)]


@click.command("events")
@click.option("--since", help="Window, e.g. 30m, 6h, 2d (default: all).")
@click.option("--about", "subject", help="Only events whose device/node matches (substring).")
@click.option("--what", "what", help="Comma list: joined,left,moved,config,node (default all).")
@click.option("--limit", type=int, default=50, show_default=True, help="Newest N (0 = all).")
@click.option("--summary", is_flag=True, help="Counts per event type + most active devices; lists only node/config events (newest 20), not device churn.")
@json_option
@pass_state
def events_cmd(st, since, subject, what, limit, summary):
    """Network history: devices joined/left/moved, node uplink/status/backhaul-grade changes. No login.

    \b
    Recorded whenever tpctl observes the mesh (overview, check, mesh show, devices list).
    Examples:
      tpctl events --since 6h
      tpctl events --about watch
    """
    from ..client import connect
    d = connect().health.events(_minutes(since), subject, limit, what=[w.strip() for w in what.split(",")] if what else None)
    obs = d["observations"] or {}

    def text():
        rare = _collapse_flaps(connect().health.events(_minutes(since), subject, 20, what=["node", "config"])["events"]) if summary else []
        import datetime as _dt
        win_start = (_dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) if (since or "").lower() == "today"
                     else _dt.datetime.now() - _dt.timedelta(minutes=_minutes(since))).isoformat(timespec="seconds") if since else None
        gap = (f"{o.ts(win_start)} .. {o.ts(obs.get('first'))}" if win_start and obs.get("first") and win_start < obs["first"] else None)
        yield o.line("history", observations=obs.get("count", 0), first=o.ts(obs.get("first")),
                     last=o.ts(obs.get("last")), events_total=d["total"], listed=None if summary else len(d["events"]),
                     node_config_listed=len(rare) if summary else None,
                     shown_note="--summary lists only node/config events (listed of total); device churn is in counts"
                     if summary else None,
                     listed_merged_from=sum(2 for e in rare if e.get("what") == "node_grade_flap") or None if summary else None,
                     settings_history=f"tpctl snapshot timeline --since {since or '24h'}  (every setting; config_changed "
                                      "here tracks key settings only)",
                     coverage_gap=gap, coverage_note="no DEVICE history in this part of the window (tpctl wasn't "
                                                    "observing); settings history is separate (snapshots, from the first "
                                                    "tpctl write/overview): tpctl snapshot timeline" if gap else None,
                     note=d["note"] if o.DETAIL else "-v for what is and isn't recorded")
        net_settings = None
        if win_start:                                  # local snapshot files only: first in the window vs the newest
            try:
                from ..config import Config
                from ..services.snapshots import diff_files
                snaps = sorted(Config().snapshot_dir.glob("*.json"))
                cut = win_start.replace("-", "").replace(":", "").replace("T", "-")[:15]
                older = [f for f in snaps if f.stem[:15] < cut]
                inwin = [f for f in snaps if f.stem[:15] >= cut]
                base = (older[-1:] or inwin[:1])
                if base and inwin and base[0] != snaps[-1]:
                    net_settings = len(diff_files(base[0], snaps[-1]))
            except Exception:                           # noqa: BLE001 - a hint only
                net_settings = None
        shown_counts = {k.replace("device_", "").replace("node_change", "node_events"): v for k, v in d["counts"].items()
                        if summary is False or k != "config_changed" or o.DETAIL}
        yield o.line("counts", **shown_counts,
                     settings_hint="`tpctl snapshot timeline --since ... --writes` (net lines = what is different now)" if summary and not o.DETAIL else None,
                     setting_changes_net=net_settings if o.DETAIL else None,
                     setting_changes_scope="local snapshots (`snapshot timeline --writes` net_changes compares with the "
                                           "live router and can be higher)" if net_settings is not None and o.DETAIL else None,
                     setting_changes_note=("newest snapshot vs the window start, local files only (`snapshot timeline "
                                           "--writes` compares with the live router and also counts rows older snapshots "
                                           "didn't capture, so it can be a little higher)") if net_settings is not None and o.DETAIL else None,
                     most_active=d["top_subjects"],
                     see_also="tpctl snapshot timeline (every setting change: DHCP, names, DMZ address…)" if o.DETAIL else None,
                     counts_are=f"raw events in the window{' (' + since + ')' if since else ''} across devices_involved "
                                "devices; flaps not merged", config_changed_tracked_since=o.ts(obs.get("first")),
                     config_changed_is=None if (summary and not o.DETAIL) or (net_settings is not None and not o.DETAIL) else ("0: no tracked switch was still changed at a later observation since "
                                        f"{o.ts(obs.get('first'))} (earlier changes and writes undone in between aren't "
                                        "seen); every setting change: `snapshot timeline --writes`"
                                        if not d["counts"].get("config_changed") else
                                        "key switches (Wi-Fi, firewall, DMZ on/off, DHCP server…) as seen between "
                                        "observations; every setting change: `snapshot timeline --writes`"))
        if summary:                  # node/config events are few and matter: list them instead of only counting
            hs = connect().health
            allev = hs.events(_minutes(since), subject, 0, what=["joined", "left", "moved"])["events"]
            last: dict = {}
            for e in allev:                       # any later join/move means it is back
                last[e.get("mac") or e["subject"]] = e
            import json as _j
            try:
                snap = _j.loads(hs._state_file.read_text())
            except (OSError, ValueError):
                snap = {}
            now = snap.get("devices", {})
            present = set(now) | {v.get("label") for v in now.values()}
            subj_mac = {x["subject"]: x["mac"] for x in last.values() if x.get("mac")}
            def _named(k, e):                    # a generic name alone says nothing: add the MAC tail
                s_ = e["subject"]
                mac = e.get("mac") or subj_mac.get(s_) or (k if isinstance(k, str) and k.count("-") == 5 else None)
                generic = s_.lower() in ("network device", "iphone", "ipad", "mac", "android", "unknown", "device")
                if generic and not mac and "#" not in s_:
                    return f"{s_} (no MAC recorded)"
                return f"{s_}#{mac[-8:]}" if mac and "#" not in s_ and s_.lower() in (
                    "network device", "iphone", "ipad", "mac", "android", "unknown", "device") else s_
            gone = sorted({_named(k, e) for k, e in last.items()
                           if e["what"] == "device_left" and k not in present and e["subject"] not in present})
            yield o.line("churn", devices_involved=len(last), still_gone=len(gone), still_gone_devices=(gone if len(gone) <= 15 else gone[:12]) or None,
                         more=len(gone) - 12 if len(gone) > 15 else None, as_of=o.ts(snap.get("time")),
                         note="devices_involved = devices with a join/leave/move in the window; still_gone = their last "
                              "event was a leave and the latest observation (as_of) doesn't see them; a device may be back "
                              "since: `devices find` reads live")
            yield o.line("config_scope", tracked="Wi-Fi per band, smart connect, firewall, UPnP, DMZ on/off, port-forward "
                                                 "count, IPv6 rules, remote admin, DHCP server (on/off pool lease gateway DNS)",
                         other_settings="tpctl snapshot timeline (reservations, names, everything else)")
            merged = sum(1 for e in rare if e.get("what") == "node_grade_flap")   # also stated on the history line
            if merged:
                yield o.line("merged", flaps=merged, note=f"{merged} node_grade_flap line(s) stand for {2 * merged} raw node_change events")
            for e in rare:
                yield o.line("event", at=o.ts(e["at"]), **{k: v for k, v in e.items() if k != "at"})
            return
        if len(d["events"]) < d["total"]:
            yield o.line("truncated", shown=len(d["events"]), total=d["total"], hint="--limit 0 for all, or --since/--what/--about")
        for e in _collapse_flaps(d["events"]):
            yield o.line("event", at=o.ts(e["at"]), **{k: v for k, v in e.items() if k != "at"})

    def human():
        out = [h.dim(f"{obs.get('count', 0)} observations since {obs.get('first', '?')[:16].replace('T', ' ')} · "
                     f"last {obs.get('last', '?')[11:16]} · {d['total']} events")]
        rows = []
        for e in reversed(d["events"]):
            w = e["what"]
            what = {"device_joined": h.green("joined"), "device_left": h.red("left"), "device_moved": "moved"}.get(w, w.replace("node_", ""))
            detail = (f"{e.get('node', '')} {e.get('band', '')}".strip() if w in ("device_joined", "device_left")
                      else f"{e.get('old')} → {e.get('new')}")
            rows.append([e["at"][5:16].replace("T", " "), e.get("subject", ""), what, detail])
        return out + (h.table(["WHEN", "WHO", "WHAT", "DETAIL"], rows) if rows else ["  " + h.dim("no events in this window")])
    st.out(d, text(), human)


@click.command("observe")
@json_option
@pass_state
def observe_cmd(st):
    """Record one observation (mesh + key settings) for `tpctl events`; prints a single line. Meant for scheduling.

    \b
    History only exists for moments tpctl observed. For continuous history run this periodically, e.g. a
    user crontab entry:  */10 * * * * /path/to/tpctl observe >/dev/null 2>&1   (ask the owner before installing)
    """
    from ..services.health import config_brief
    inv = st.r.mesh.inventory()
    sec = st.r.status.security()                 # only what config_brief() tracks
    ch = st.r.health.changes_since_last(inv, save=True,
                                        config=config_brief({"security": sec}, st.r.wifi.bands_brief()))
    st.out({"recorded": True, "changes": ch}, [o.line("observed", changes=len(ch["changes"]), since=o.ts(ch["since"]))],
           lambda: [h.green("✓ observed") + h.dim(f" · {len(ch['changes'])} changes since {(ch['since'] or 'first run')[11:16]}")])


COMMANDS = [events_cmd, observe_cmd]
