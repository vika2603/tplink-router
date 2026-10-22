from __future__ import annotations

import datetime as dt
import json

from .. import net, quality
from ..models import Inventory, Issue
from . import Service

# thresholds (see quality.LEGEND for grades)
WEAK_BACKHAUL_LEVEL = 2      # <= this /5
WEAK_BACKHAUL_DBM = -72
DEEP_CHAIN = 3               # hops
STRONG_ON_2G_DBM = -60       # device on 2.4G with signal at least this strong
LEVELS = {"error": 0, "warn": 1, "info": 2}
# data keys each issue code emits (single source for docs; tests assert issues only use these)
ISSUE_DATA_KEYS = {
    "NODE_OFFLINE": ["ip", "status", "state", "devices_behind"],
    "NODE_FLAPPING": ["status_changes_1h", "last_change_at", "devices_behind"],
    "NODE_PING_LOSS": ["loss", "avg"],
    "WEAK_BACKHAUL": ["uplink", "backhaul_signal", "backhaul_level", "backhaul_link", "devices_behind"],
    "DEEP_CHAIN": ["depth", "path"],
    "STRONG_ON_2G": ["ip", "signal", "node"],
    "WEAK_DEVICES": ["count", "bad", "near_boundary", "stale", "just_roamed", "devices"],
    "UNATTRIBUTED_DEVICES": ["count", "devices"],
    "INTERNET_DOWN": ["host"], "IPV6_DOWN": ["host"], "INTERNET_DEGRADED": ["loss", "avg"],
    "WAN_DISCONNECTED": ["internet", "type"],
    "SLOW_PORT": ["speed", "duplex"],
}


def port_issues(ports: list[dict]) -> list[Issue]:
    """A linked wired port that negotiated <= 100 Mbps: nearly always a damaged/old cable or a Fast-Ethernet device."""
    from .system import SLOW_LINK_MBPS
    return [Issue("info", "SLOW_PORT", p["port"],
                  f"{p['port']} is linked at only {p['speed_mbps']} Mbps ({p['duplex'] or '?'} duplex)",
                  "swap the cable (Cat5e+, all 8 wires) or check the device's NIC; `tpctl system ports`",
                  {"speed": f"{p['speed_mbps']}Mbps", "duplex": p["duplex"]})
            for p in ports if p["link"] == "up" and p["speed_mbps"] and p["speed_mbps"] <= SLOW_LINK_MBPS]


def _stale(c) -> bool:
    return c.signal_stale


def inventory_issues(inv: Inventory) -> list[Issue]:
    """Issues derivable from an Inventory (pings attached if available). Weak devices are grouped per node.
    Each issue: `detail` = sentence for humans, `data` = flat fields for agents."""
    out: list[Issue] = []
    for n in inv.nodes:
        if n.is_main:
            continue
        behind = n.devices_behind          # the same number the node record prints
        if not n.online:
            rejoining = n.status == "connected" and n.ip in ("", "0.0.0.0", None)
            flapping = n.status_flaps_30m >= 2
            state = (f"flapping ({n.status_flaps_30m} connect/disconnect changes in the last hour)" if flapping else
                     "rejoining (associated, no IP yet)" if rejoining else "offline")
            out.append(Issue("error", "NODE_OFFLINE", n.name,
                             f"{n.name} is {state} ({n.status}, ping {'down' if n.ping else 'n/a'}); {behind} devices were behind it",
                             (f"it keeps dropping: watch it (`tpctl events --about '{n.name}'`); if it continues, check its power "
                              "and placement / uplink signal; a reboot rarely fixes a flapping node" if flapping else
                              "it is re-joining the mesh: wait 1-3 min (`tpctl mesh wait`) before anything else" if rejoining else
                              "just changed Wi-Fi? `tpctl mesh wait`. Else plan a reboot: `tpctl mesh reboot "
                              f"{n.name} --dry-run` (needs it reachable), or ask the user to power-cycle it"),
                             {"ip": n.ip, "status": n.status,
                              "state": "flapping" if flapping else "rejoining" if rejoining else "offline",
                              "devices_behind": behind}))
            continue
        if n.status_flaps_30m >= 2:                       # online right now, but dropped repeatedly
            out.append(Issue("warn", "NODE_FLAPPING", n.name,
                             f"{n.name} dropped off the mesh {n.status_flaps_30m // 2 or 1}x in the last hour (online now); "
                             f"{behind} devices behind it get pushed to other nodes each time",
                             f"watch it (`tpctl events --about '{n.name}'`); check its power and placement / uplink signal",
                             {"status_changes_1h": n.status_flaps_30m,
                              "last_change_at": (n.last_status_change_at or "").replace("T", " ")[:16] or None,
                              "devices_behind": behind}))
        if n.ping and 10 <= n.ping.loss_pct < 100:     # needs >=1 in 10 lost on a confirmed (10-ping) sample
            out.append(Issue("warn", "NODE_PING_LOSS", n.name, f"{n.ping.loss_pct:.0f}% ping loss, avg {n.ping.avg_ms} ms",
                             "re-run `tpctl net ping --count 20`; persistent loss = weak backhaul",
                             {"loss": f"{n.ping.loss_pct:.0f}%", "avg": f"{n.ping.avg_ms}ms"}))
        if n.backhaul == "wifi" and ((n.backhaul_level is not None and n.backhaul_level <= WEAK_BACKHAUL_LEVEL)
                                     or (n.backhaul_dbm is not None and n.backhaul_dbm < WEAK_BACKHAUL_DBM)):
            out.append(Issue("warn", "WEAK_BACKHAUL", n.name,
                             f"wireless link to {n.parent_name} is weak: {n.backhaul_dbm} dBm, level {n.backhaul_level}/5, "
                             f"{n.backhaul_mbps} Mbps; {behind} devices depend on it",
                             "move it closer to its uplink, use Ethernet backhaul, or reboot it to re-pick a parent",
                             {"uplink": n.parent_name, "backhaul_signal": f"{n.backhaul_dbm}dBm",
                              "backhaul_level": f"{n.backhaul_level}/5", "backhaul_link": f"{n.backhaul_mbps}Mbps",
                              "devices_behind": behind}))
        if n.depth >= DEEP_CHAIN:
            out.append(Issue("warn", "DEEP_CHAIN", n.name, f"{n.depth} wireless hops from the main router via {n.parent_name}",
                             "each wireless hop roughly halves throughput; wired backhaul fixes it",
                             {"depth": n.depth, "path": [m.name for m in inv.path(n)]}))
    weak: dict[str, list] = {}
    for c in inv.devices:
        s = c.signal_dbm
        if c.band == "2.4G" and isinstance(s, int) and s >= STRONG_ON_2G_DBM:
            out.append(Issue("info", "STRONG_ON_2G", c.label, f"on 2.4G despite a strong {s} dBm signal at {c.node}",
                             "being on 2.4G is usually by design (2.4G-only chip) and not a problem by itself; "
                             "if the device supports 5G, forget+rejoin Wi-Fi on it",
                             {"ip": c.ip, "signal": f"{s}dBm", "node": c.node}))
        if c.signal_grade in quality.WEAK_GRADES and c.node != "unknown-satellite":   # == node devices_weak; unattributed: separately
            weak.setdefault(c.node, []).append(c)
    for node, cs in sorted(weak.items(), key=lambda kv: -len(kv[1])):
        cs.sort(key=lambda c: (_stale(c), c.signal_dbm, c.label))   # same order as weakest_device: settled readings first
        bad = [c for c in cs if c.signal_grade == "bad" and not _stale(c)]     # confirmed bad only (stale counted apart)
        out.append(Issue("info", "WEAK_DEVICES", node, f"{len(cs)} weak devices (< {quality.WEAK_BELOW_DBM} dBm, "
                         f"{len(bad)} of them confirmed bad): " + ", ".join(f"{c.label} ({c.signal_dbm} dBm)" for c in cs),
                         "coverage gap near this node if persistent; devices usually roam on their own (markers: ~ near a grade line, "
                         "? reading may be stale, ^ just changed node)",
                         {"count": len(cs), "bad": len(bad), "near_boundary": sum(quality.near_boundary(c.signal_dbm) for c in cs) or None,
                          "stale": sum(_stale(c) for c in cs) or None,
                          "just_roamed": sum(1 for c in cs if c.just_roamed) or None,
                          "devices": [f"{c.label}:{c.signal_dbm}dBm{'~' if quality.near_boundary(c.signal_dbm) else ''}"
                                      f"{'?' if _stale(c) else ''}{'^' if c.just_roamed else ''}" for c in cs]}))
    unk = [c for c in inv.devices if c.node == "unknown-satellite"]
    if unk:
        out.append(Issue("info", "UNATTRIBUTED_DEVICES", "mesh", f"{len(unk)} devices on a satellite that isn't reporting them",
                         "transient during re-onboarding; re-check in a few minutes",
                         {"count": len(unk), "devices": ",".join(c.label for c in unk[:10])}))
    return sorted(out, key=lambda i: LEVELS.get(i.level, 9))


def verdict(issues: list[Issue], warnings: list[dict] | None = None) -> dict:
    """One-line conclusion: status ok|warn|error|partial, counts, headline of the top problems."""
    n = {lv: sum(i.level == lv for i in issues) for lv in LEVELS}
    status = "error" if n["error"] else "warn" if n["warn"] else "ok"
    if warnings and status != "error":
        status = "partial"
    top = [f"{i.code}:{i.subject}" for i in issues if i.level in ("error", "warn")][:4]
    nodeish = [i for i in issues if i.level in ("error", "warn") and isinstance((i.data or {}).get("devices_behind"), int)]
    # fix first: most devices behind it; ties -> error before warn (offline before weak/flapping)
    def weight(i):
        w = i.data["devices_behind"]
        if i.code == "NODE_FLAPPING" and i.data.get("last_change_at"):
            try:
                age = (dt.datetime.now() - dt.datetime.fromisoformat(i.data["last_change_at"])).total_seconds() / 60
            except ValueError:
                age = 0
            if age > 30:                       # stable for half an hour: counts half against a problem present now
                w = w / 2
        return (w, i.level == "error")
    ff = max(nodeish, key=weight, default=None)
    return {"status": status, "errors": n["error"], "warnings": n["warn"], "infos": n["info"],
            "partial_reads": len(warnings or []) or None, "headline": "; ".join(top) or "no problems",
            "fix_first": f"{ff.code}:{ff.subject}" if ff else None,
            "fix_first_why": (f"{ff.data['devices_behind']} devices behind it (most of the node problems"
                              + (f"; last drop {ff.data['last_change_at'][11:16]}" if ff.data.get("last_change_at") else "")
                              + "; a node stable for 30+ min counts half)") if ff else None}


def config_brief(status: dict | None, wifi: dict | None) -> dict:
    """Key settings already fetched by overview/check, flattened; diffed between observations to catch changes made
    anywhere (web UI, app, tpctl)."""
    out = {}
    for b, x in ((wifi or {}).get("bands") or {}).items():
        for k in ("enable", "ssid", "channel_setting", "width", "txpower", "encryption", "hidden"):
            out[f"wifi.{b}.{k}"] = x.get(k)
    if wifi and wifi.get("smart_connect") is not None:
        out["wifi.smart_connect"] = wifi["smart_connect"]
    sec = (status or {}).get("security") or {}
    for k in ("spi_firewall", "upnp", "dmz", "port_forwards", "ipv6_allow_rules", "remote_mgmt"):
        if sec.get(k) is not None:
            out[f"security.{k}"] = sec[k]
    dh = (status or {}).get("dhcp") or {}
    for k in ("enable", "range", "lease_min", "gateway", "dns"):
        if dh.get(k) is not None:
            out[f"dhcp.{k}"] = ",".join(dh[k]) if isinstance(dh[k], list) else dh[k]
    return out


def _state_of(inv: Inventory, config: dict | None = None) -> dict:
    return {"config": config or {}, "nodes": {n.mac: {"name": n.name, "parent": n.parent_name, "status": n.status, "backhaul": n.backhaul,
                              "grade": n.backhaul_grade, "dbm": n.backhaul_dbm, "level": n.backhaul_level} for n in inv.nodes},
            "devices": {c.mac: {"label": c.label, "node": c.node, "band": c.band} for c in inv.devices}}


def diff_states(old: dict, new: dict) -> list[dict]:
    ch = []
    for mac, n in new["nodes"].items():
        o = old.get("nodes", {}).get(mac)
        if not o:
            ch.append({"what": "node_added", "subject": n["name"]})
            continue
        status_changed = o.get("status") != n.get("status")
        for k in ("parent", "status", "backhaul", "grade"):
            if k == "grade" and status_changed:
                continue                                   # an outage is the node_status event; its grade is n/a
            if o.get(k) != n.get(k):
                e = {"what": f"node_{k}", "subject": n["name"], "old": o.get(k), "new": n.get(k)}
                lv = (o.get("level"), n.get("level"))
                if k == "grade" and abs(quality.rank(o.get(k)) - quality.rank(n.get(k))) == 1 and (
                        quality.near_boundary(n.get("dbm")) or quality.near_boundary(o.get("dbm"))
                        or (None not in lv and abs(lv[0] - lv[1]) == 1)):
                    e["near_boundary"] = "yes"          # one grade, at a dBm threshold or one bar: reading noise
                ch.append(e)
    for mac, o in old.get("nodes", {}).items():
        if mac not in new["nodes"]:
            ch.append({"what": "node_removed", "subject": o["name"]})
    oc, nc = old.get("devices", {}), new["devices"]
    for mac, c in nc.items():
        if mac not in oc:
            ch.append({"what": "device_joined", "subject": c["label"], "node": c["node"], "band": c["band"], "mac": mac})
        elif "unknown-satellite" in (oc[mac]["node"], c["node"]):
            continue                                       # transient: a satellite briefly not reporting its devices
        elif (oc[mac]["node"], oc[mac]["band"]) != (c["node"], c["band"]):
            ch.append({"what": "device_moved", "subject": c["label"], "old": f"{oc[mac]['node']}/{oc[mac]['band']}",
                       "new": f"{c['node']}/{c['band']}", "mac": mac})
    for mac, c in oc.items():
        if mac not in nc:
            ch.append({"what": "device_left", "subject": c["label"], "node": c["node"], "mac": mac})
    oconf, nconf = old.get("config") or {}, new.get("config") or {}
    for k in sorted(set(oconf) & set(nconf)):          # only keys seen both times (light vs full overview differ)
        if oconf[k] != nconf[k]:
            ch.append({"what": "config_changed", "subject": k, "old": oconf[k], "new": nconf[k]})
    return ch


def roaming_assessment(d: dict) -> str:
    """One roaming verdict for `devices roaming`, the reboot plan and the library (counts in the window)."""
    forced = d.get("during_node_outage", 0)
    own = d["node_changes"] - forced                                # moves the device made itself
    good_now = d.get("signal_grade") in ("excellent", "good")
    if d["node_changes"] >= 3 and forced * 3 >= d["node_changes"]:  # a third or more forced by node outages
        return "moved largely because a node dropped (during_node_outage): fix the node, not the device"
    if own >= 3:
        fast = own > 6 or len([x for x in d.get("nodes", []) if x != "unknown-satellite"]) >= 4
        return ("flapping between nodes: calls/streams reset on each roam" if not good_now else
                "flapping fast across many nodes even at a good signal: roaming too eagerly (overlapping coverage), "
                "sessions reset" if fast else
                "flapping between nodes at a good signal now: mostly walking around / overlap, sessions still reset")
    if d["band_switches"] >= 3 and not d["node_changes"]:
        return "band steering on one node: usually harmless"
    if d["band_switches"] >= 3:
        return ("band flapping (+ a node change): sessions blip on each switch; harmless at a good signal, "
                "a coverage-edge symptom at a weak one")
    return "reconnecting often" if d["reconnects"] >= 3 else "normal"


class HealthService(Service):
    def issues(self, inv: Inventory | None = None) -> list[Issue]:
        """Issues for an inventory (fetched with pings if not given)."""
        return inventory_issues(inv or self.r.mesh.inventory(ping=True))

    def verdict(self, issues: list[Issue]) -> dict:
        return verdict(issues, self.r.warnings)

    @property
    def _state_file(self):
        return self.r.config.state_dir / "last_observation.json"

    @property
    def _events_file(self):
        return self.r.config.state_dir / "events.jsonl"

    @property
    def _obs_file(self):
        return self.r.config.state_dir / "observations.json"

    def changes_since_last(self, inv: Inventory, save: bool = True, config: dict | None = None) -> dict:
        """Diff vs the previous observation (overview/check/mesh show/devices list), store this one, and append the
        changes to the event log (events.jsonl) so history survives between runs."""
        f = self._state_file
        now = dt.datetime.now().isoformat(timespec="seconds")
        if any("empty device list" in str(w.get("detail", "")) for w in self.r.warnings):
            save = False                 # a glitched read would log every device as left (and rejoined next time)
        old = json.loads(f.read_text()) if f.exists() else None
        prev_n = len((old or {}).get("devices") or (old or {}).get("clients") or {})
        if save and prev_n >= 5 and not inv.devices:
            save = False                 # everyone gone at once = a read glitch, not an exodus: don't poison history
            self.r.warnings.append({"endpoint": "admin/smart_network?form=game_accelerator", "op": "loadSpeed", "fields": [],
                                    "detail": f"empty device list right after {prev_n} devices were seen (read glitch); "
                                              "not recorded in history"})
        if old and "clients" in old and "devices" not in old:   # file written before the client->device rename
            old["devices"] = old.pop("clients")
        if old:                                           # carry last known node through transient unattributed reads
            for c in inv.devices:
                prev = (old.get("devices") or {}).get(c.mac)
                if c.node == "unknown-satellite" and prev and prev["node"] != "unknown-satellite":
                    c.node = prev["node"]
        if not config and old:                            # observation without config (mesh show): carry the last one
            config = old.get("config")
        new = _state_of(inv, config) | {"time": now}
        changes = diff_states(old, new) if old else []
        glitch = any("empty device list" in str(w.get("detail", "")) for w in self.r.warnings)
        if glitch:                       # don't present a glitched read as 'everyone left'
            changes = [c for c in changes if not c["what"].startswith("device_")]
        if save and __import__("os").environ.get("TPCTL_NO_TRACK"):
            save = False
        if save:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(new, ensure_ascii=False))
            self._log(now, changes, first=old is None)
        if not old:
            return {"since": None, "minutes_ago": None, "changes": []}
        age = int((dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(old["time"])).total_seconds() // 60)
        return {"since": old["time"], "minutes_ago": age, "changes": changes,
                **({"suppressed": "device changes hidden: the router returned an empty device list (read glitch)"} if glitch else {})}

    def _log(self, now: str, changes: list[dict], first: bool) -> None:
        with open(self._events_file, "a") as fh:
            for c in changes:
                fh.write(json.dumps({"at": now, **c}, ensure_ascii=False) + "\n")
        obs = json.loads(self._obs_file.read_text()) if self._obs_file.exists() else {"first": now, "count": 0}
        obs |= {"last": now, "count": obs.get("count", 0) + 1, "recent": (obs.get("recent") or [])[-999:] + [now]}
        self._obs_file.write_text(json.dumps(obs))
        lines = self._events_file.read_text().splitlines()
        if len(lines) > 5000:                               # bounded log
            self._events_file.write_text("\n".join(lines[-4000:]) + "\n")

    def record(self, inv: Inventory) -> None:
        """Record an observation without printing changes (mesh show / devices list keep history complete).
        Disabled by TPCTL_NO_TRACK=1 or the commands' --no-track."""
        import os
        if not os.environ.get("TPCTL_NO_TRACK"):
            self.changes_since_last(inv, save=True)

    def events(self, since_minutes: int | None = None, subject: str | None = None, limit: int = 100,
               what: list[str] | None = None) -> dict:
        """Event history (joins/leaves/moves, node parent/status/grade changes) between observations."""
        obs = json.loads(self._obs_file.read_text()) if self._obs_file.exists() else None
        rows = []
        if self._events_file.exists():
            for ln in self._events_file.read_text().splitlines():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
        for e in rows:                                    # entries logged before the client->device rename
            if str(e.get("what", "")).startswith("client_"):
                e["what"] = "device_" + e["what"][7:]
        rows = [e for e in rows if "unknown-satellite" not in (str(e.get("old")), str(e.get("new")), str(e.get("node")))]
        if since_minutes is not None:
            cut = (dt.datetime.now() - dt.timedelta(minutes=since_minutes)).isoformat(timespec="seconds")
            rows = [e for e in rows if e["at"] >= cut]
        if subject:
            q = subject.lower()
            rows = [e for e in rows if q in str(e.get("subject", "")).lower() or q in str(e.get("node", "")).lower()]
        if what:
            alias = {"joined": "device_joined", "left": "device_left", "moved": "device_moved", "config": "config_changed"}
            want = {alias.get(w, w) for w in what}
            rows = [e for e in rows if e["what"] in want or ("node" in want and e["what"].startswith("node_"))]
        total = len(rows)
        from collections import Counter as _C
        counts = {k: 0 for k in ("device_joined", "device_left", "device_moved", "config_changed", "node_change")}
        for e in rows:
            k = "node_change" if e["what"].startswith("node_") else e["what"]
            counts[k] = counts.get(k, 0) + 1
        top = _C(e.get("subject") for e in rows if e["what"].startswith("device_")).most_common(5)
        return {"counts": counts, "top_subjects": [f"{k}:{v}" for k, v in top], "observations": obs, "total": total, "events": rows[-limit:] if limit else rows,
                "note": "recorded only when tpctl observes (overview/check/mesh show/devices list) since `first`; gaps "
                        "between observations collapse into one diff; a device briefly offline shows as left+joined; "
                        "config_changed covers key settings from `first` on: Wi-Fi per band (enable ssid channel width txpower "
                        "security hidden), smart connect, firewall, UPnP, DMZ on/off, number of port forwards, IPv6 rules, remote admin, DHCP server "
                        "(on/off, pool, lease, gateway, DNS). Everything else (reservations, device names, LEDs…) and older "
                        "changes: snapshot timeline"}

    def roaming(self, since_minutes: int = 60) -> list[dict]:
        """Per-device roaming in the window: node changes (real roams) vs band switches on the same node."""
        ev = self.events(since_minutes=since_minutes, limit=0, what=["moved", "joined", "left"])["events"]
        outages: dict[str, list] = {}               # node -> times its status changed (drops and returns)
        for e in self.events(since_minutes=since_minutes + 5, limit=0, what=["node"])["events"]:
            if e.get("what") == "node_status":
                try:
                    outages.setdefault(e["subject"], []).append(dt.datetime.fromisoformat(e["at"]))
                except (KeyError, ValueError):
                    pass

        def forced(e, a, b) -> bool:
            try:
                t = dt.datetime.fromisoformat(e["at"])
            except (KeyError, ValueError):
                return False
            return any(abs((t - x).total_seconds()) <= 180 for n in (a, b) for x in outages.get(n, []))
        per: dict[str, dict] = {}
        # older events carry only the label: map label -> MAC from events that have both, so one device = one row
        label_mac = {e["subject"]: e["mac"] for e in ev if e.get("mac")}
        for e in ev:
            if not e.get("mac") and e["subject"] in label_mac:
                e = {**e, "mac": label_mac[e["subject"]]}
            # group by MAC when the event has it (renamed devices stay one row); label = the newest name seen
            d = per.setdefault(e.get("mac") or e["subject"], {"label": e["subject"], "mac": e.get("mac"), "moves": 0,
                                                              "node_changes": 0, "band_switches": 0, "reconnects": 0, "nodes": []})
            d["label"] = e["subject"]
            if e["what"] == "device_moved":
                (on, ob), (nn, nb) = (str(e.get("old", "/")).split("/", 1), str(e.get("new", "/")).split("/", 1))
                d["moves"] += 1
                d["last_move_at"] = max(d.get("last_move_at") or "", e.get("at", ""))
                if on != nn and forced(e, on, nn):
                    d["during_node_outage"] = d.get("during_node_outage", 0) + 1
                d["node_changes" if on != nn else "band_switches"] += 1
                for x in (on, nn):
                    if x not in d["nodes"] and x != "unknown-satellite":
                        d["nodes"].append(x)
            elif e["what"] == "device_joined":
                d["reconnects"] += 1
                if e.get("node") and e["node"] not in d["nodes"] and e["node"] != "unknown-satellite":
                    d["nodes"].append(e["node"])
        rows = [d for d in per.values() if d["moves"] or d["reconnects"]]
        obs = json.loads(self._obs_file.read_text()) if self._obs_file.exists() else {}
        cut = (dt.datetime.now() - dt.timedelta(minutes=since_minutes)).isoformat(timespec="seconds")
        recent = obs.get("recent") or []
        in_win = sum(t >= cut for t in recent)
        exact = bool(recent) and (recent[0] < cut or len(recent) >= obs.get("count", 0))
        self.last_roaming_coverage = {"window_min": since_minutes, "history_first": obs.get("first"),
                                      "observations_in_window": in_win if exact else f">={in_win}",
                                      "observations_total": obs.get("count", 0)}
        for d in rows:
            d["assessment"] = roaming_assessment(d)
        return sorted(rows, key=lambda d: (-d["node_changes"], -d["moves"], -d["reconnects"]))

    def internet(self) -> dict:
        return net.ping_many(net.INTERNET_V4 + net.INTERNET_V6, 4)

    def check(self, track: bool = True) -> dict:
        """Full health check: probes, WAN, firewall, mesh, devices. `verdict.status` ok|warn|error|partial."""
        issues: list[Issue] = []
        inet = self.internet()
        for h, p in inet.items():
            v6 = ":" in h
            if not p.up:
                issues.append(Issue("warn" if v6 else "error", "IPV6_DOWN" if v6 else "INTERNET_DOWN", h,
                                    f"{h} unreachable from this machine", "tpctl status (wan/ipv6 lines)", {"host": h}))
            elif p.loss_pct > 0 or (p.avg_ms or 0) > 50:
                issues.append(Issue("warn", "INTERNET_DEGRADED", h, f"{p.loss_pct:.0f}% loss, avg {p.avg_ms} ms to {h}",
                                    "re-run; check for saturating uploads/downloads",
                                    {"loss": f"{p.loss_pct:.0f}%", "avg": f"{p.avg_ms}ms"}))
        wan = self.r.status.wan()
        if wan["internet"] != "connected":
            issues.append(Issue("error", "WAN_DISCONNECTED", "wan", f"WAN is {wan['internet']} ({wan['type']})",
                                "upstream down: check modem/ONT and cable, then `tpctl status`",
                                {"internet": wan["internet"], "type": wan["type"]}))
        sec = self.r.status.security()
        if sec["spi_firewall"] != "on":
            issues.append(Issue("warn", "FIREWALL_OFF", "spi", "SPI firewall disabled while LAN devices may have public IPv6",
                                "tpctl api set 'admin/security_settings?form=new_enable' enable=on"))
        inv = self.r.mesh.inventory(ping=True, ping_count=5)
        if self.r.caps.has("ports"):
            issues += port_issues(self.r.system.ports())
        issues = sorted(issues + inventory_issues(inv), key=lambda i: LEVELS.get(i.level, 9))
        return {"time": dt.datetime.now().isoformat(timespec="seconds"),
                "verdict": verdict(issues, self.r.warnings), "internet": inet, "wan": wan, "security": sec,
                "mesh_online": f"{sum(n.online for n in inv.nodes)}/{len(inv.nodes)}", "summary": inv.summary(),
                "issues": issues, "changes": self.changes_since_last(inv, save=track), "inventory": inv}

    def overview(self, track: bool = True, light: bool = False) -> dict:
        """Everything in one call: verdict, status, wifi, inventory (with pings), summary, issues, changes.
        light=True fetches only what the concise records print (wan, ipv6, firewall, band summary): ~1.5 s faster."""
        inv = self.r.mesh.inventory(ping=False)
        res = net.ping_many([n.ip for n in inv.nodes] + net.INTERNET_V4[:1] + net.INTERNET_V6, 3)   # one parallel batch
        for n in inv.nodes:
            n.ping = res.get(n.ip) or net.PingResult(n.ip, 100.0)
            n.ping_grade = quality.ping(n.ping.loss_pct, n.ping.avg_ms)
            if not n.ping.up and not n.is_main:
                n.backhaul_grade = "bad"
        retest = [n for n in inv.nodes if n.ping and 0 < n.ping.loss_pct < 100]
        if retest:                                       # one lost packet in 3 isn't loss: confirm with 10 pings
            again = net.ping_many([n.ip for n in retest], 10)
            for n in retest:
                n.ping = again.get(n.ip) or n.ping
                n.ping_grade = quality.ping(n.ping.loss_pct, n.ping.avg_ms)
        internet = {h: res[h] for h in net.INTERNET_V4[:1] + net.INTERNET_V6 if h in res}
        issues = inventory_issues(inv)
        if self.r.caps.has("ports"):
            issues += port_issues(self.r.system.ports())
        for h, p in internet.items():
            if not p.up:
                issues.insert(0, Issue("warn" if ":" in h else "error", "IPV6_DOWN" if ":" in h else "INTERNET_DOWN", h,
                                       f"{h} unreachable from this machine", "tpctl check", {"host": h}))
        if light:
            wan_, sec_ = self.r.status.wan(), self.r.status.security()
            status = {"wan": wan_, "ipv6": self.r.status.ipv6(), "security": sec_, "inbound": self.r.status.inbound(wan_, sec_),
                      "dhcp": self.r.dhcp.get()}
            wifi = self.r.wifi.bands_brief()
        else:
            status, wifi = self.r.status.get(), self.r.wifi.get()
        changes = self.changes_since_last(inv, save=track, config=config_brief(status, wifi))
        return {"time": dt.datetime.now().isoformat(timespec="seconds"), "verdict": verdict(issues, self.r.warnings),
                "status": status, "wifi": wifi, "inventory": inv, "summary": inv.summary(), "issues": issues,
                "internet": internet, "changes": changes, "recent_writes": self.recent_writes(changes.get("since")),
                "auto_snapshot": self._daily_baseline() if track and not __import__("os").environ.get("TPCTL_NO_TRACK") else None}

    def recent_writes(self, since: str | None, min_minutes: int = 30) -> list[dict]:
        """tpctl writes (the snapshot each one took first) since the previous observation, or at least the last
        `min_minutes`; newest last. Writes made in the web UI / app leave no snapshot (`snapshot timeline` finds them)."""
        cut = dt.datetime.now() - dt.timedelta(minutes=min_minutes)
        if since:
            cut = min(cut, dt.datetime.fromisoformat(since))
        out = []
        for p in self.r.snapshots.list(500):
            try:
                t = dt.datetime.strptime(p.stem[:15], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            tag = p.stem[16:]
            if t >= cut and tag not in ("auto-daily", "manual", ""):
                out.append({"tag": tag, "at": t.isoformat(timespec="seconds")})
        return out

    def _daily_baseline(self, max_age_h: int = 24) -> str | None:
        """Keep a config baseline at least daily so `snapshot diff --since` also covers changes made outside tpctl."""
        snaps = self.r.snapshots.list(1)
        if snaps:
            age = dt.datetime.now() - dt.datetime.strptime(snaps[-1].stem[:15], "%Y%m%d-%H%M%S")
            if age < dt.timedelta(hours=max_age_h):
                return None
        return str(self.r.snapshots.save("auto-daily"))
