from __future__ import annotations

import base64
import time
from collections import Counter
from typing import Callable

from .. import net, quality
from ..errors import UnsupportedError, UsageError
from ..models import Change, Device, Inventory, Node, WriteResult
from . import Service


DETAIL_EP = "admin/easymesh_network?form=mesh_sclient_detail"
LOCATIONS = ("bedroom", "hallway", "kitchen", "living_room", "master_bedroom", "office", "study", "other")


def _bool(v) -> bool:
    return v is True or str(v).lower() in ("true", "on", "1", "yes")


def _uplink_ap(v) -> dict | None:
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            v = __import__("json").loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) and v.get("mac") else None


def is_randomized_mac(mac: str) -> bool:
    """Locally-administered bit set (2nd hex digit in 2/6/A/E) = private/random address (iOS, Android, Windows)."""
    try:
        return bool(int(mac.replace(":", "-").split("-")[0], 16) & 0x02)
    except ValueError:
        return False


def assign_labels(devices: list[Device], memory: dict | None = None) -> None:
    """label = alias, else router name; a name shared by several devices gets '#<last 3 MAC bytes>'.
    With `memory` ({mac: {"base", "label"}}, persisted by the caller) labels are sticky: a device keeps its label
    while its name is unchanged, and a name that ever collided keeps getting the suffix, so a label doesn't flip
    between `iPhone` and `iPhone#AA-BB-CC` as other devices come and go."""
    memory = memory if memory is not None else {}
    counts = Counter((c.alias or c.name) for c in devices)
    taken = {v["label"]: m for m, v in memory.items()}
    for c in devices:
        base = c.alias or c.name or "unknown"
        prev = memory.get(c.mac)
        if prev and prev.get("base") == base and taken.get(prev["label"]) == c.mac:
            c.label = prev["label"]
            continue
        generic = base.lower() in ("network device", "unknown", "device", "--", "---")   # says nothing about the device
        clash = generic or counts[base] > 1 or any(lbl == base or lbl.startswith(base + "#") for lbl, m in taken.items() if m != c.mac)
        c.label = f"{base}#{c.mac[-8:]}" if clash else base
        memory[c.mac] = {"base": base, "label": c.label}
        taken[c.label] = c.mac
    seen: dict[str, str] = {}
    for c in devices:                      # never two devices with one label, whatever memory says
        if c.label in seen and seen[c.label] != c.mac:
            c.label = f"{c.alias or c.name or 'unknown'}#{c.mac[-8:]}"
            memory[c.mac] = {"base": c.alias or c.name or "unknown", "label": c.label}
        seen[c.label] = c.mac


class MeshService(Service):
    def raw_nodes(self) -> list[dict]:
        return self.r.call("admin/easymesh_network?form=get_mesh_device_list_all")

    def _main_only(self) -> dict:
        """Synthesized main-router node for models without EasyMesh."""
        fw = self.r.lib_call("get_firmware")
        st = self.r.lib_call("get_status")
        return {"name": fw.model or "router", "role": "main_router", "model": fw.model, "mac": str(st.lan_macaddr or "main"),
                "ip": str(st.lan_ipv4_addr or self.r.config.host.split("//")[-1]), "status": "connected"}

    def _generic_devices(self, main_name: str) -> list[dict]:
        """Fallback device rows from the library's model-independent status (no per-node attribution, no rates)."""
        out = []
        for d in self.r.lib_call("get_status").devices:
            t = str(getattr(d.type, "value", d.type)).lower()     # e.g. host_5g, guest_2g, iot_6g, host_mlo, wired
            band = ("wired" if "wired" in t else "MLO" if "mlo" in t else "6G" if "6g" in t else "5G" if "5g" in t
                    else "2.4G" if "2g" in t else "unknown")
            ap = (d.ap_name or "").lstrip("*")
            out.append({"mac": str(d.macaddr), "ip": str(d.ipaddr or ""), "deviceName": d.hostname or "", "deviceTag": band,
                        "signal": d.signal if isinstance(d.signal, int) else None, "isAgent": bool(ap and ap != main_name),
                        "txrate": (d.tx_rate or 0) * 1000 if isinstance(d.tx_rate, int) else 0,
                        "rxrate": (d.rx_rate or 0) * 1000 if isinstance(d.rx_rate, int) else 0,
                        "onlineTime": d.online_time or 0, "ssid": None, "deviceType": None,
                        "isGuest": "guest" in t})
        return out

    def _seed_labels(self) -> dict:
        """First run of sticky labels: start from the labels the event history used, so they don't change."""
        import json
        f = self.r.config.state_dir / "events.jsonl"
        mem: dict = {}
        if f.exists():
            for ln in f.read_text().splitlines():
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("mac") and e.get("subject"):
                    mem[e["mac"]] = {"base": e["subject"].split("#")[0], "label": e["subject"]}
        return mem

    def detail(self, mac: str) -> dict:
        """Satellite detail: parent, backhaul dBm/level/link per band, uplink preference, its devices."""
        return self.r.try_call("admin/easymesh_network?form=mesh_sclient_detail", "read", mac=mac)

    def inventory(self, ping: bool = False, ping_count: int = 3) -> Inventory:
        """All nodes (resolved parent, depth, backhaul) + all devices attributed to a node. One snapshot in time.
        Within one command, repeated calls a few seconds apart return the same read (so a plan's effect, caveats and
        scope count the same devices); any write drops it."""
        if not getattr(self.r, "share_reads", False):          # library / `tpctl py`: always a fresh read
            return self._inventory(ping, ping_count)
        c = getattr(self.r, "_inv_cache", None)
        if c and not ping and time.time() - c[0] < 15:
            return c[1]
        inv = self._inventory(ping, ping_count)
        if not ping:
            self.r._inv_cache = (time.time(), inv)
        return inv

    def _inventory(self, ping: bool = False, ping_count: int = 3) -> Inventory:
        raw = self.raw_nodes() if self.r.caps.has("easymesh") else [self._main_only()]
        by_mac = {n["mac"]: n for n in raw}
        details, on_node = {}, {}
        for n in raw:
            if n.get("role") != "main_router" and self.r.caps.has("mesh_detail"):
                details[n["mac"]] = d = self.detail(n["mac"]) or {}
                for c in d.get("mesh_nclient_list") or []:
                    on_node[c["mac"]] = (n["name"], c)

        def parent_of(n):
            if n.get("role") == "main_router":
                return None
            p = details.get(n["mac"], {}).get("parent_device_mac") or n.get("parent_mac")
            if p and p not in by_mac:  # parent reported via another interface MAC
                p = next((m for m in by_mac if m[-8:] == p[-8:]), p)
            return p
        parents = {n["mac"]: parent_of(n) for n in raw}

        nodes = []
        for n in raw:
            d = details.get(n["mac"], {})
            depth, cur = 0, n["mac"]
            while parents.get(cur) in by_mac and depth < 8:
                depth, cur = depth + 1, parents[cur]
            dbm = d.get("signal_strength_5g")
            main = n.get("role") == "main_router"
            nodes.append(Node(
                name=n["name"], role=n.get("role"), model=n.get("model"), ip=n.get("ip"), mac=n["mac"],
                status=n.get("status"), parent_mac=parents[n["mac"]],
                parent_name=by_mac.get(parents[n["mac"]], {}).get("name", parents[n["mac"]]), depth=depth,
                backhaul="main" if main else "wired" if n.get("connect_type") == "wire" else "wifi",
                backhaul_dbm=dbm if dbm not in (None, -128) else None,
                backhaul_level=None if main else d.get("signal_level_5g", n.get("signal_strength")),
                backhaul_mbps=(int(d.get("link_speed_5g") or 0) // 1000) or None,
                uplink_pref=d.get("preferred_wifi_uplink_type"),
                uplink_choices=d.get("preferred_wifi_uplink_ap_available") or [],
                reboot_supported=n.get("support_reboot"), raw={"list": n, "detail": d}))
        main_name = next((n.name for n in nodes if n.is_main), "main")

        aliases = self.r.devices.aliases()
        v6: dict[str, list[str]] = {}
        for e in (self.r.try_call("admin/nat?form=client_list_v6", quiet=True, default=[]) or []) if self.r.caps.has("client_v6") else []:
            if isinstance(e, dict) and e.get("ip") and not str(e["ip"]).lower().startswith("fe80"):
                v6.setdefault(e.get("mac", ""), []).append(e["ip"])

        devices = []
        rows = (self.r.call("admin/smart_network?form=game_accelerator", "loadSpeed") if self.r.caps.has("client_rates")
                else self._generic_devices(main_name))
        if not rows and self.r.caps.has("client_rates"):
            # the router occasionally answers an empty list (seen with client_num=0 on every node too): re-read once;
            # if it stays empty while DHCP has live leases, it's a glitch, never "0 devices"
            time.sleep(1.0)
            rows = self.r.call("admin/smart_network?form=game_accelerator", "loadSpeed") or []
            if not rows:
                expected = sum(int(n.get("client_num") or 0) for n in raw)
                leases = self.r.try_call("admin/dhcps?form=client", "load", quiet=True, default=[]) or []
                if expected or (isinstance(leases, list) and leases):
                    self.r.warnings.append({"endpoint": "admin/smart_network?form=game_accelerator", "op": "loadSpeed",
                                            "fields": [], "detail": f"empty device list while mesh nodes report {expected} "
                                                                    f"devices and DHCP has {len(leases)} leases (router glitch)"})
        for x in rows:
            node, sat = on_node.get(x["mac"], (None, None))
            from_sat = x.get("signal") is None and (sat or {}).get("signal_strength") is not None
            sig = x.get("signal") if x.get("signal") is not None else (sat or {}).get("signal_strength")
            if sig in (0, -128):                          # router placeholders for "no reading"
                sig = None
            band = x.get("deviceTag", "")
            devices.append(Device(
                label="", name=x.get("deviceName", ""), ip=x.get("ip", ""), mac=x["mac"], band=band,
                node=node or (main_name if not x.get("isAgent") else "unknown-satellite"),
                signal_dbm=sig, signal_grade=None if band == "wired" or (from_sat and sig == -90) or (
                    isinstance(sig, int) and sig < -85 and max(int(x.get("txrate") or 0), int(x.get("rxrate") or 0)) // 1000 > 500)
                    else quality.signal(sig),
                link_tx_mbps=int(x.get("txrate") or 0) // 1000, link_rx_mbps=int(x.get("rxrate") or 0) // 1000,
                alias=aliases.get(x["mac"]), randomized_mac=is_randomized_mac(x["mac"]),
                ipv6=sorted(v6.get(x["mac"], [])),
                ssid=base64.b64decode(x["ssid"]).decode() if x.get("ssid") else None, type=x.get("deviceType"),
                guest=bool(x.get("isGuest")), online_min=int(float(x.get("onlineTime") or 0) // 60),
                signal_placeholder=bool(from_sat and sig == -90)))
        try:                                   # previous observation: who changed node since (signal may still settle)
            prev = __import__("json").loads((self.r.config.state_dir / "last_observation.json").read_text()).get("devices", {})
        except (OSError, ValueError):
            prev = {}
        recent = set()                         # moved in the last 3 minutes according to the event log
        moves_hour: dict = {}
        last_move: dict = {}
        node_flaps: dict = {}
        node_last: dict = {}
        try:
            import datetime as _dt
            cut = (_dt.datetime.now() - _dt.timedelta(minutes=3)).isoformat(timespec="seconds")
            hour_cut = (_dt.datetime.now() - _dt.timedelta(minutes=60)).isoformat(timespec="seconds")
            flap_cut = (_dt.datetime.now() - _dt.timedelta(minutes=60)).isoformat(timespec="seconds")
            lines = (self.r.config.state_dir / "events.jsonl").read_text().splitlines()[-300:]
            for ln in lines:
                e = __import__("json").loads(ln)
                if e.get("what") == "node_status" and e.get("at", "") >= flap_cut:
                    node_flaps[e.get("subject")] = node_flaps.get(e.get("subject"), 0) + 1
                    node_last[e.get("subject")] = e.get("at")
                if e.get("what") == "device_moved":
                    k = e.get("mac") or e.get("subject")
                    if e.get("at", "") >= cut:
                        recent.add(k)
                    if e.get("at", "") >= hour_cut:
                        moves_hour[k] = moves_hour.get(k, 0) + 1
                        last_move[k] = max(last_move.get(k, ""), e.get("at", ""))
        except (OSError, ValueError):
            pass
        for c in devices:
            p = prev.get(c.mac)
            c.moves_1h = moves_hour.get(c.mac, 0) + moves_hour.get(c.label, 0)
            c.last_move_at = max(last_move.get(c.mac, ""), last_move.get(c.label, "")) or None
            moved_now = bool(p and p.get("node") not in (None, c.node, "unknown-satellite") and c.node != "unknown-satellite")
            c.just_roamed = c.mac in recent or moved_now
            if moved_now:                       # seen in this read, not logged yet: the move is newer than the log
                c.last_move_at = _dt.datetime.now().isoformat(timespec="seconds")
                c.moves_1h = (c.moves_1h or 0) + 1
        lf = self.r.config.state_dir / "labels.json"
        try:
            memory = __import__("json").loads(lf.read_text()) if lf.exists() else self._seed_labels()
        except ValueError:
            memory = {}
        assign_labels(devices, memory)
        try:
            lf.parent.mkdir(parents=True, exist_ok=True)
            lf.write_text(__import__("json").dumps(dict(list(memory.items())[-2000:]), ensure_ascii=False))
        except OSError:
            pass
        for n in nodes:
            mine = [c for c in devices if c.node == n.name]
            sigs = sorted(c.signal_dbm for c in mine if isinstance(c.signal_dbm, int) and c.band != "wired")
            n.devices = len(mine)
            n.device_bands = dict(sorted(Counter(c.band for c in mine).items()))
            n.device_signal_worst = sigs[0] if sigs else None
            n.device_signal_median = sigs[len(sigs) // 2] if sigs else None
            n.devices_weak = sum(c.signal_grade in quality.WEAK_GRADES for c in mine)
            n.devices_weak_stale = sum(c.signal_grade in quality.WEAK_GRADES and c.signal_stale for c in mine)
            wl = [c for c in mine if isinstance(c.signal_dbm, int) and c.band != "wired"]
            fresh = [c for c in wl if not c.signal_stale]
            w = min(fresh or wl, key=lambda c: (c.signal_dbm, c.label)) if wl else None   # same choice as Inventory.weakest_device
            weak = [c for c in wl if c.signal_grade in quality.WEAK_GRADES]
            if weak and (w is None or w.signal_grade not in quality.WEAK_GRADES):
                w = min(weak, key=lambda c: (c.signal_dbm, c.label))   # only stale readings are weak: show that one, flagged
            n.weakest_stale = bool(w and w.signal_stale)
            n.weakest_device, n.weakest_signal_dbm = (w.label, w.signal_dbm) if w else (None, None)
            n.backhaul_grade = quality.backhaul(n.backhaul, n.backhaul_dbm, n.backhaul_level, n.status == "connected")
            if n.backhaul == "wifi":
                d = n.raw.get("detail") or {}
                ok5 = d.get("signal_strength_5g") not in (None, -128) or (d.get("link_speed_5g") or 0) > 0
                ok24 = d.get("signal_strength_24g") not in (None, -128) or (d.get("link_speed_24g") or 0) > 0
                n.backhaul_band = "5G" if ok5 else "2.4G" if ok24 else None
        inv = Inventory(nodes, devices)
        for n in nodes:
            n.status_flaps_30m = node_flaps.get(n.name, 0)
            n.last_status_change_at = node_last.get(n.name)
        for n in nodes:                                # one rule for records and the library: Inventory.weakest_device
            mine = [c for c in inv.devices_on(n) if isinstance(c.signal_dbm, int) and c.band != "wired"]
            w = inv.weakest_device(n)
            weak = [c for c in mine if c.signal_grade in quality.WEAK_GRADES]
            if weak and (w is None or w.signal_grade not in quality.WEAK_GRADES):
                w = min(weak, key=lambda c: (c.signal_dbm, c.label))   # only stale readings are weak: show one, flagged
            n.weakest_stale = bool(w and w.signal_stale)
            n.weakest_device, n.weakest_signal_dbm = (w.label, w.signal_dbm) if w else (None, None)
            worse = [c for c in mine if w and c.just_roamed and not c.signal_stale and c.signal_dbm < w.signal_dbm]
            n.weakest_excluded = (lambda x: f"{x.label}:{x.signal_dbm}dBm(just_roamed)")(min(worse, key=lambda c: c.signal_dbm)) if worse else None
        for n in nodes:
            n.devices_behind = len(inv.devices_behind(n))   # same meaning as the Inventory method, always set
            if not n.is_main and n.devices_behind > n.devices:          # it has nodes downstream: weakest of all behind it
                wb = inv.weakest_device(n, behind=True)
                n.weakest_behind, n.weakest_behind_dbm = (wb.label, wb.signal_dbm) if wb else (None, None)
        for n in nodes:
            hops = [m for m in inv.path(n) if not m.is_main and m.backhaul_grade]
            n.path_grade = max((m.backhaul_grade for m in hops), key=quality.rank) if hops else None
            hops = [m for m in inv.path(n) if not m.is_main and m.backhaul_mbps]
            slow = min(hops, key=lambda m: m.backhaul_mbps) if hops and n.online else None
            n.path_link_mbps = slow.backhaul_mbps if slow else None             # offline: no path to speak of
            n.path_bottleneck = slow.name if slow else None
        by_name = {n.name: n for n in nodes}
        for c in devices:
            n = by_name.get(c.node)
            if n is None or n.is_main:
                continue
            hops = [m for m in inv.path(n) if not m.is_main]
            worst = max(hops, key=lambda m: quality.GRADES.index(m.backhaul_grade) if m.backhaul_grade else -1, default=None)
            if worst is not None and worst.backhaul_grade:
                c.path_grade, c.weakest_hop = worst.backhaul_grade, worst.name
        if ping:
            self.attach_pings(inv, ping_count)
        return inv

    @staticmethod
    def attach_pings(inv: Inventory, count: int = 3) -> None:
        res = net.ping_many([n.ip for n in inv.nodes], count)
        for n in inv.nodes:
            n.ping = res.get(n.ip) or net.PingResult(n.ip, 100.0)
            n.ping_grade = quality.ping(n.ping.loss_pct, n.ping.avg_ms)
            if not n.ping.up and not n.is_main:
                n.backhaul_grade = "bad"

    def node(self, query: str, ping: bool = True) -> tuple[Node, Inventory]:
        inv = self.inventory(ping=False)
        n = inv.node(query)
        if ping:
            n.ping = net.ping(n.ip, 10) if n.ip != "0.0.0.0" else net.PingResult(n.ip, 100.0)
            n.ping_grade = quality.ping(n.ping.loss_pct, n.ping.avg_ms)
        return n, inv

    # -- node settings (the web UI's Edit dialog under Advanced -> EasyMesh) --
    @staticmethod
    def _settings(d: dict, main: bool) -> dict:
        """Comparable view of a node's settings from its mesh_sclient_detail read."""
        out = {"name": d.get("name"), "location": d.get("location") or ""}
        if main:
            return out | {"traffic_separation": _bool(d.get("traffic_separation")), "backhaul_steering": _bool(d.get("backhaul_steering"))}
        ap = _uplink_ap(d.get("preferred_wifi_uplink_ap"))
        return out | {"led": _bool(d.get("led")),
                      "uplink": (ap.get("device_name") or ap["mac"]) if d.get("preferred_wifi_uplink_type") == "manual" and ap else "auto"}

    def configure(self, query: str, name: str | None = None, location: str | None = None, led: bool | None = None,
                  uplink: str | None = None, inventory: Inventory | None = None, dry_run: bool = False) -> WriteResult:
        """Rename a node, set its location / LED, or set its preferred uplink (uplink="auto" or a node the router
        offers as a candidate). Sends exactly the web UI's payload with every other field at its current value."""
        self.r.caps.require("easymesh", "mesh node settings")
        if location is not None and location not in LOCATIONS + ("",):
            raise UsageError(f"location must be one of {', '.join(LOCATIONS)} (or '' = not set)", location, list(LOCATIONS), "location")
        if name is not None and not name.strip():
            raise UsageError("name must not be empty", name, None, "name")
        inv = inventory or self.inventory()
        n = inv.node(query)
        if n.is_main and (led is not None or uplink is not None):
            raise UsageError("the main router has no per-node LED/uplink setting (LED: main router's LED control)", query, None, "node")
        d = self.r.call(DETAIL_EP, "read", mac=n.mac) or {}
        cur = self._settings(d, n.is_main)
        want = dict(cur)
        ap = None
        if name is not None:
            want["name"] = name.strip()
        if location is not None:
            want["location"] = location
        if led is not None:
            want["led"] = bool(led)
        if uplink is not None and uplink.lower() == "auto":
            want["uplink"] = "auto"
        elif uplink is not None:
            ap = self._uplink_candidate(n, d, uplink, inv)
            want["uplink"] = ap["device_name"] or ap["mac"]
        plan = [Change(k, cur[k], want[k], want[k]) for k in want if want[k] != cur[k]] or \
               [Change(k, cur[k], want[k], want[k]) for k in ("name",)]
        uplink_changed = want.get("uplink") != cur.get("uplink")
        side = (f"{n.name} drops its backhaul and re-attaches (~1-3 min); devices on it and nodes behind it "
                f"({len(inv.devices_behind(n))} devices) reconnect" if uplink_changed else None)
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        body = {"mac": n.mac, "name": want["name"], "location": want["location"]}
        if n.is_main:
            body |= {"traffic_separation": str(want["traffic_separation"]).lower(),
                     "backhaul_steering": str(want["backhaul_steering"]).lower()}
        else:
            body |= {"led": str(want["led"]).lower(), "preferred_wifi_uplink_type": "auto" if want["uplink"] == "auto" else "manual"}
            if want["uplink"] != "auto":
                ap = ap or _uplink_ap(d.get("preferred_wifi_uplink_ap"))
                body["preferred_wifi_uplink_ap"] = __import__("json").dumps(
                    {"device_name": ap.get("device_name"), "mac": ap["mac"]}, ensure_ascii=False)
        snap = self.r.snapshot("mesh-set")
        self.r.call(DETAIL_EP, "write", **body)
        after = self._settings(self.r.call(DETAIL_EP, "read", mac=n.mac) or {}, n.is_main)
        return WriteResult([Change(c.key, c.old, after.get(c.key), c.target) for c in plan], str(snap), side,
                           "tpctl mesh wait" if uplink_changed else f"tpctl mesh node {after.get('name') or n.name}")

    def _uplink_candidate(self, n: Node, d: dict, query: str, inv: Inventory) -> dict:
        """The uplink must be one the router itself offers (preferred_wifi_uplink_ap_available), as in the web UI."""
        cands = [c for c in (d.get("preferred_wifi_uplink_ap_available") or []) if isinstance(c, dict) and c.get("mac")]
        if not cands:
            raise UnsupportedError(f"the router offers no uplink candidates for {n.name} (preferred_wifi_uplink_ap_available "
                                   "is empty; the web UI's picker is empty too), so it can't be pinned. Options: move it "
                                   "closer, wire its backhaul, or `tpctl mesh reboot` to make it re-pick its parent.")
        q = query.upper().replace(":", "-")
        target = next((m for m in inv.nodes_matching(query)), None)
        hit = [c for c in cands if q == str(c["mac"]).upper() or query in str(c.get("device_name") or "")
               or (target is not None and str(c["mac"])[-8:] == target.mac[-8:])]
        if len(hit) != 1:
            raise UsageError(f"uplink '{query}' is not one of the candidates the router offers for {n.name}", query,
                             [str(c.get("device_name") or c["mac"]) for c in cands], "uplink")
        return {"device_name": hit[0].get("device_name"), "mac": hit[0]["mac"]}

    def reboot(self, query: str) -> Node:
        """Reboot one satellite. The API answers with an error even when the reboot happens, so it is ignored."""
        self.r.caps.require("easymesh", "satellite reboot")
        inv = self.inventory()
        n = inv.node(query)
        if n.is_main:
            raise UsageError("that is the main router; use system.reboot()")
        self.r.try_call("admin/easymesh_network?form=available_mesh_device_manage", "reboot", mac=n.mac)
        return n

    def wait(self, timeout: int = 360, interval: int = 20,
             on_progress: Callable[[int, int, int, list[str]], None] | None = None) -> tuple[bool, int, list[str]]:
        """Poll until every node is connected and pingable. Returns (ok, elapsed_s, still_down)."""
        start = time.time()
        while True:
            raw = self.raw_nodes() if self.r.caps.has("easymesh") else [self._main_only()]
            pings = net.ping_many([n.get("ip") for n in raw], 2)
            bad = [n["name"] for n in raw if n.get("status") != "connected" or n.get("ip") in (None, "0.0.0.0")
                   or not pings.get(n["ip"], net.PingResult("", 100.0)).up]
            elapsed = int(time.time() - start)
            if on_progress:
                on_progress(elapsed, len(raw) - len(bad), len(raw), bad)
            if not bad:
                return True, elapsed, []
            if elapsed >= timeout:
                return False, elapsed, bad
            time.sleep(interval)
