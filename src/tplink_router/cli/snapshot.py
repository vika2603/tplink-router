from pathlib import Path

import click

from .. import human as h
from .. import output as o
from ..services.snapshots import diff_files
from ._ctx import json_option, pass_state


@click.group("snapshot")
def snapshot():
    """Redacted config snapshots (taken automatically before every write) and diffs."""


@snapshot.command("save")
@click.option("--tag", default="manual", show_default=True)
@json_option
@pass_state
def save(st, tag):
    p = st.r.snapshots.save(tag)
    st.out({"path": str(p)}, [o.line("snapshot", path=p)], lambda: [h.green("✓ saved ") + str(p)])


@snapshot.command("list")
@click.option("--last", default=20, show_default=True)
@json_option
@pass_state
def list_(st, last):
    # no router session needed
    from ..config import Config
    files = sorted(Config().snapshot_dir.glob("*.json"))[-last:]
    def meta(f):
        n = f.stem
        taken = f"{n[:4]}-{n[4:6]}-{n[6:8]} {n[9:11]}:{n[11:13]}:{n[13:15]}" if n[:8].isdigit() else None
        return {"name": f.name, "taken": taken[:16] if taken else None, "tag": n[16:] or None}
    st.out([meta(f) | {"path": str(f)} for f in files],
           [o.line("snapshot", **meta(f), why={"auto-daily": "daily baseline"}.get(meta(f)["tag"] or "", "before a tpctl write"
                                                                                          if (meta(f)["tag"] or "") not in ("manual", "diff") else meta(f)["tag"]))
            for f in files],
           lambda: h.snapshots([str(f) for f in files]))


@snapshot.command("diff")
@click.argument("snaps", nargs=-1)
@click.option("--since", "since", help="Baseline = oldest snapshot within the window (e.g. 24h, 7d) vs live.")
@click.option("--last-two", is_flag=True, help="Compare the two newest snapshots instead of latest vs live.")
@click.option("--all", "include_noisy", is_flag=True, help="Include volatile keys and keys present on one side only.")
@json_option
@pass_state
def diff(st, snaps, since, last_two, include_noisy):
    """Config changes. Default: latest snapshot vs the router now (read-only, nothing written).

    \b
    SNAPS: 0 = latest vs live · 1 = that snapshot vs live · 2 = OLD NEW · --since 7d = oldest in window vs live.
    Names as printed by `snapshot list`
    (substrings ok, e.g. `125747`). Device/mesh history is `tpctl events`, not this.
    """
    if len(snaps) > 2:
        raise click.UsageError("pass at most two snapshots")
    from .events import _minutes
    old, new, changes = st.r.snapshots.diff(*(list(snaps) + [None, None])[:2], last_two=last_two,
                                            include_noisy=include_noisy, since_minutes=_minutes(since))
    taken = f"{old[:4]}-{old[4:6]}-{old[6:8]} {old[9:11]}:{old[11:13]}" if old[:8].isdigit() else None
    d = {"old": old, "old_taken": taken, "new": new, "changes": changes}
    st.out(d, [o.line("diff", baseline=old, baseline_taken=taken, against=new, changes=len(changes),
                      note="snapshots: before every tpctl write + one per day from overview; device history = tpctl events"),
               *(o.change_line(c) for c in changes)],
           lambda: h.diff(d))


@snapshot.command("timeline")
@click.option("--since", help="Only changes in this window, e.g. 24h, 7d.")
@click.option("--key", help="Only keys containing this text, e.g. ssid, leasetime.")
@click.option("--all", "show_reverted", is_flag=True, help="Also changes later undone (what was tried).")
@click.option("--writes", "by_write", is_flag=True, help="One row per write (time, tag, settings, undone?): compact, with --all implied.")
@click.option("--raw", "include_noisy", is_flag=True, hidden=True, help="Also volatile counters (uptime, clock) and read errors.")
@json_option
@pass_state
def timeline(st, since, key, show_reverted, by_write, include_noisy):
    """WHEN settings changed: each change with the window it happened in (between two snapshots, or last one and now).

    \b
    Precision = snapshot spacing (before every tpctl write + daily from overview). Key settings changed after
    event logging began also appear in `tpctl events` (what=config_changed) with observation times.
    """
    from .events import _minutes
    rows = st.r.snapshots.timeline(_minutes(since), key, include_noisy)
    import datetime as _dt
    tz = _dt.datetime.now().astimezone().strftime("%z")
    net = sorted({r_["key"] for r_ in rows if not r_["reverted"]})
    show_reverted_explicit = show_reverted
    show_reverted = show_reverted or by_write or include_noisy
    hidden = 0 if show_reverted else sum(1 for r_ in rows if r_["reverted"])
    if not show_reverted:
        rows = [r_ for r_ in rows if not r_["reverted"]]
    if by_write:
        groups: dict[tuple, dict] = {}
        for r_ in rows:
            basis = r_.get("basis") or ""
            tag = basis.split("'")[1] if r_["by"] == "tpctl" and "'" in basis else "outside tpctl"
            when = (r_["likely"] or "").lstrip("~") or f"{r_['after']} … {r_['before']}"   # tpctl writes: exact time
            g = groups.setdefault((when, tag),
                                  {"at": when, "tag": tag, "by": r_["by"], "keys": [], "undone": 0})
            g["keys"].append(r_["key"])
            g["undone"] += bool(r_["reverted"])
            if r_["reverted"]:
                g.setdefault("undone_keys", []).append(r_["key"])
        import datetime as _d
        cut = (_d.datetime.now() - _d.timedelta(minutes=_minutes(since))) if since else None
        seen = {(g["at"], g["tag"]) for g in groups.values()}
        for f in st.r.snapshots.list(10000):                     # writes that changed no snapshot-tracked setting
            try:
                t = _d.datetime.strptime(f.stem[:15], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            tag = f.stem[16:]
            at = t.strftime('%Y-%m-%d %H:%M')
            if (cut and t < cut) or tag in ("", "manual", "auto-daily", "diff") or (at, tag) in seen:
                continue
            if key:
                continue
            groups[(at, tag)] = {"at": at, "tag": tag, "by": "tpctl", "keys": [], "undone": 0, "untracked": True}
            seen.add((at, tag))
        ws = sorted(groups.values(), key=lambda w: w["at"])
        untracked = [w for w in ws if w.get("untracked")]
        undone_ws = [w for w in ws if w.get("undone_keys") and len(set(w["undone_keys"])) == len(set(w["keys"]))]
        if not show_reverted_explicit:                      # compact: fold untracked and fully undone writes
            ws = [w for w in ws if not w.get("untracked") and w not in undone_ws]
        # net changes = the window's first snapshot vs live (same as `snapshot diff --since`), time of the last change
        last_at = {}
        for r_ in rows:
            last_at[r_["key"]] = r_["likely"] or r_["before"] or ""
        netrows = {}
        if since:
            try:
                _, _, net_ch = st.r.snapshots.diff(since_minutes=_minutes(since))
            except Exception:                          # noqa: BLE001 - fall back to the timeline's own net keys
                net_ch = []
            for c in net_ch:
                at = last_at.get(c.key) or max((v for k, v in last_at.items() if k.startswith(c.key + ".") or c.key.startswith(k + ".")), default=None)
                netrows[c.key] = {"key": c.key, "old": c.old, "new": c.new, "at": at}
        related = lambda a, b: a == b or a.startswith(b + ".") or b.startswith(a + ".") or a.startswith(b + "[") or b.startswith(a + "[")
        first_old, last_new = {}, {}
        for r_ in rows:
            first_old.setdefault(r_["key"], r_["old"])
            last_new[r_["key"]] = r_["new"]
        for k in net:                                    # what the diff can't see (older snapshots lacked the form)
            if any(related(k, d) for d in netrows):
                continue
            if "]." in k and k.split("].")[0] + "]" in net:
                continue                                 # the row itself is listed: its final state covers the field
            fam = [r_ for r_ in rows if related(r_["key"], k)]
            earliest = fam[0]["key"] == k if fam else True
            first_rel = fam[0] if fam else None
            netrows[k] = {"key": k, "old": first_old[k] if earliest else
                          f"existed ({first_rel['key'].split('].')[-1]} {first_rel['old']} until "
                          f"{(first_rel['likely'] or first_rel['after'] or '')[11:16]})",
                          "new": last_new[k], "at": last_at.get(k)}
        net_shown = len(netrows)
        return st.out({"writes": ws, "net": list(netrows.values()), "untracked": len(untracked)},
                      [o.line("writes", count=len(ws) + (0 if show_reverted_explicit else len(untracked) + len(undone_ws)), listed=len(ws),
                                  untracked_folded=len(untracked) if untracked and not show_reverted_explicit else None,
                                  untracked_tags=",".join(f"{t}:{n}" for t, n in __import__("collections").Counter(
                                      w["tag"] for w in untracked).items()) if untracked and not show_reverted_explicit else None,
                                  undone_folded=len(undone_ws) if undone_ws and not show_reverted_explicit else None,
                                  undone_tags=",".join(f"{t}:{n}" for t, n in __import__("collections").Counter(
                                      w["tag"] for w in undone_ws).items())
                                  if undone_ws and not show_reverted_explicit else None,
                                  changes=len(rows), net_changes=net_shown,
                                  outside_tpctl=sum(1 for w in ws if w["by"] != "tpctl"),
                                  outside_note="changes made in the web UI/app show as tag=\"outside tpctl\"; they are only "
                                               "seen between two snapshots (snapshots start at the first tpctl write/overview)",
                                  note="one row per tpctl write (each took a snapshot first); settings = distinct snapshot-tracked "
                                       "settings it changed, undone = how many of them are back where the window started "
                                       "(all = fully undone); untracked=yes = the write changed nothing the snapshots record (speed "
                                       "limits, older snapshots without that form, or a no-op), folded into untracked_folded "
                                       "unless --all; details: drop --writes (and --key K)"),
                           *([o.line("untracked_writes", count=len(untracked),
                                     at_tags=",".join(f"{w['at'][11:16]} {w['tag']}" for w in untracked),
                                     note="these changed nothing the snapshots record; the command that made them shows its "
                                          "own state (e.g. `qos limits`, `parental`, `nat ipv6-rules`)")]
                             if untracked and not show_reverted_explicit else []),
                           *(o.line("net", key=d["key"], old=d["old"] if d["old"] not in (None, "") else "(empty)",
                                   new=d["new"] if d["new"] not in (None, "") else "(empty)", at=d["at"]) for d in netrows.values()),
                           *(o.line("write", at=w["at"], tag=w["tag"], by=w["by"], settings=len(set(w["keys"])),
                                    keys=",".join(dict.fromkeys(w["keys"])) or None,
                                    undone=("all" if len(set(w["undone_keys"])) == len(set(w["keys"])) else len(set(w["undone_keys"])))
                                    if w.get("undone_keys") else None,
                                    untracked="yes" if w.get("untracked") else None) for w in ws)],
                      lambda: h.table(["WHEN", "WRITE", "SETTINGS", "UNDONE"],
                                      [[w["at"], w["tag"], ", ".join(dict.fromkeys(w["keys"]))[:60] or "(untracked)",
                                        ("all" if len(set(w["undone_keys"])) == len(set(w["keys"])) else str(len(set(w["undone_keys"]))))
                                        if w.get("undone_keys") else ""] for w in ws]))
    snaps = st.r.snapshots.list(10000)
    first = snaps[0].stem if snaps else None
    first = f"{first[:4]}-{first[4:6]}-{first[6:8]} {first[9:11]}:{first[11:13]}" if first and first[:8].isdigit() else None
    st.out(rows, [o.line("timeline", shown=len(rows), changes=len(rows),
                         net_note="net_changes = keys whose value differs between start and end; intermediate steps of one key "
                                  "(e.g. two renames in a row) are separate rows" if len(net) != len(rows) else None, net_changes=len(net), net_keys=net or None,
                         snapshots_first=first, snapshots=len(snaps),
                         reverted_hidden=hidden or None,
                         reverted_hint="changes later undone in the window; --all shows them" if hidden else None,
                         precision="between consecutive snapshots; each tpctl write is its own row (likely= its time)",
                         tz=f"{tz[:3]}:{tz[3:]}"),
                  *(o.line("change", **{k: ("(empty)" if k in ("old", "new") and v in ("", None) else v) for k, v in r.items()})
                    for r in rows)],
           lambda: h.table(["SETTING", "BEFORE → AFTER", "WHEN"],
                           [[r["key"], f"{r['old']} → {r['new']}" + (f" {r['unit']}" if r.get("unit") else ""),
                             (r["likely"] + " (tpctl)") if r["likely"] else f"{r['after']} … {r['before']}"] for r in rows])
           or [h.dim("no setting changes in this window")])


COMMANDS = [snapshot]
