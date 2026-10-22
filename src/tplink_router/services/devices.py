from __future__ import annotations

import json
import re

from ..errors import UsageError
from ..models import Change, Device, Inventory, WriteResult
from . import Service

SORTS = {
    "node": lambda c: (c.node, c.band, c.label.lower()),
    "signal": lambda c: -(c.signal_dbm if isinstance(c.signal_dbm, int) else -200),      # strongest first
    # weakest settled reading first; stale readings and devices that just changed node (signal still settling) after
    "weakest": lambda c: (c.signal_stale or c.just_roamed, c.signal_dbm if isinstance(c.signal_dbm, int) else 999,
                          -(0 < min(x for x in (c.link_rx_mbps or 10**6, c.link_tx_mbps or 10**6)) < 20),
                          -__import__("tplink_router.quality", fromlist=["x"]).rank(c.path_grade),
                          min(x for x in (c.link_rx_mbps or 10**6, c.link_tx_mbps or 10**6)), c.label.lower()),
    "name": lambda c: c.label.lower(),
    "rate": lambda c: -c.link_rx_mbps,
    "ip": lambda c: tuple(int(x) for x in c.ip.split(".") if x.isdigit()),
    "band": lambda c: (c.band, c.node),
}


def normalize_band(b: str) -> str:
    b = b.upper().replace("GHZ", "G").replace(" ", "")
    return {"2G": "2.4G", "2.4": "2.4G", "24G": "2.4G", "5": "5G", "6": "6G", "WIRED": "wired", "LAN": "wired"}.get(b, b)


NAMES_EP = "admin/smart_network?form=device_priority"
BLACK_EP = "admin/access_control?form=black_list"   # router-side device names (the UI's client-name edit)
MAX_NAME = 63


class DeviceService(Service):
    # -- router-side names --
    def rename(self, query: str, name: str, inventory: Inventory | None = None, dry_run: bool = False) -> WriteResult:
        """Set a device's name ON THE ROUTER (same as editing it in the web UI); every router list shows it.
        Verified by re-reading."""
        name = (name or "").strip()
        if not name or len(name.encode()) > MAX_NAME:
            raise UsageError(f"name must be 1-{MAX_NAME} bytes", name, None, "name")
        self.r.caps.require("device_names", "renaming devices on the router")
        inv = inventory or self.r.mesh.inventory()
        c = inv.device(query)
        cur = self._name_row(c.mac)
        plan = [Change(f"name[{c.mac}]", cur.get("deviceName"), name, name)]
        side = "device uses a randomized MAC: the name sticks to this MAC only" if c.randomized_mac else None
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        if not plan[0].changed:
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.r.snapshot("devices-rename")
        new = dict(cur, deviceName=name)
        self.r.call(NAMES_EP, "update", key=cur["key"], new=json.dumps(new, ensure_ascii=False),
                    old=json.dumps(cur, ensure_ascii=False))
        after = self._name_row(c.mac)
        return WriteResult([Change(f"name[{c.mac}]", cur.get("deviceName"), after.get("deviceName"), name)], str(snap), side,
                           "tpctl devices find " + name)

    def _name_row(self, mac: str) -> dict:
        rows = self.r.call(NAMES_EP, "load") or []
        row = next((x for x in rows if isinstance(x, dict) and x.get("mac") == mac), None)
        if row is None or "key" not in row:
            raise UsageError(f"the router has no name entry for {mac} (device offline or not in its list)", mac, None, "device")
        return row

    # -- blocking (Advanced > Security > Access Control, blocklist mode) --
    def blocked(self) -> list[dict]:
        self.r.caps.require("access_control", "device blocking")
        return [{"name": x.get("name"), "mac": x.get("mac"), "ip": x.get("ipaddr")} for x in self.r.list_rows(BLACK_EP)]

    def block(self, query: str, dry_run: bool = False, inventory: Inventory | None = None) -> WriteResult:
        """Cut one device off the network (it stays associated but gets no internet/LAN) until unblock()."""
        self.r.caps.require("access_control", "device blocking")
        mode = (self.r.try_call("admin/access_control?form=mode") or {}).get("access_mode")
        if mode and mode != "black":
            raise UsageError("access control is in allowlist mode on this router: blocking one device would need the "
                             "allowlist edited instead (web UI: Security > Access Control)", query, None, "mode")
        mac = query.upper().replace(":", "-")
        if not re.fullmatch(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}", mac):
            mac = (inventory or self.r.mesh.inventory()).device(query).mac
        cand = next((x for x in self.r.list_rows("admin/access_control?form=black_devices") if x.get("mac") == mac), None)
        en = self.r.try_call("admin/access_control?form=enable") or {}
        if (cand and cand.get("host") == "HOST") or mac == str(en.get("host_mac", "")).upper():
            raise UsageError("that is the device running tpctl: blocking it would lock you out of the router", query, None, "device")
        row = {"name": (cand or {}).get("name") or mac, "deviceType": (cand or {}).get("type") or "other", "mac": mac,
               "ipaddr": (cand or {}).get("ipaddr") or "", "host": (cand or {}).get("host") or "NON_HOST",
               "conn_type": (cand or {}).get("conn_type") or "", "key": __import__("uuid").uuid4().hex[:16]}
        enabled = en.get("enable") == "on"
        if dry_run or enabled:
            res = self.r.list_change(BLACK_EP, tag="block", key_field="mac", label="blocked", add=row, dry_run=dry_run,
                                     side_effect=f"{row['name']} loses network access until `tpctl devices unblock {mac}`",
                                     next_cmd="tpctl devices blocked")
            if dry_run and not enabled:
                res.changes.insert(0, Change("access_control", "off", "on", "on"))
            return res
        snap = self.r.snapshot("block")
        self.r.call("admin/access_control?form=enable", "write", enable="on")
        res = self.r.list_change(BLACK_EP, tag="block", key_field="mac", label="blocked", add=row,
                                 side_effect=f"{row['name']} loses network access until `tpctl devices unblock {mac}`",
                                 next_cmd="tpctl devices blocked")
        now = (self.r.try_call("admin/access_control?form=enable") or {}).get("enable")
        res.changes.insert(0, Change("access_control", "off", now, "on"))
        res.snapshot = res.snapshot or str(snap)
        return res

    def unblock(self, query: str, dry_run: bool = False) -> WriteResult:
        self.r.caps.require("access_control", "device blocking")
        mac = query.upper().replace(":", "-")
        if not re.fullmatch(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}", mac):
            rows = self.r.list_rows(BLACK_EP)
            hit = [x for x in rows if query.lower() in str(x.get("name", "")).lower()]
            if len(hit) != 1:
                raise UsageError(f"blocked device '{query}' not found or ambiguous", query,
                                 [f"{x.get('name')} {x.get('mac')}" for x in rows], "device")
            mac = hit[0]["mac"]
        res = self.r.list_change(BLACK_EP, tag="unblock", key_field="mac", label="blocked", remove=mac, dry_run=dry_run,
                                 side_effect="the device can reach the network again (it may need to reconnect)",
                                 next_cmd="tpctl devices blocked")
        mode = (self.r.try_call("admin/access_control?form=mode") or {}).get("access_mode")
        on = (self.r.try_call("admin/access_control?form=enable") or {}).get("enable") == "on"
        # blocklist mode with nothing listed restricts nothing: switch it off again so the router is as before block()
        if res.applied and on and mode == "black" and not self.r.list_rows(BLACK_EP):
            self.r.call("admin/access_control?form=enable", "write", enable="off")
            now = (self.r.try_call("admin/access_control?form=enable") or {}).get("enable")
            res.changes.append(Change("access_control", "on", now, "off"))
        return res

    # -- aliases: local MAC -> friendly name map (router names are generic: 12x "iPhone") --
    @property
    def _alias_file(self):
        return self.r.config.config_dir / "aliases.json"

    def aliases(self) -> dict[str, str]:
        f = self._alias_file
        return json.loads(f.read_text()) if f.exists() else {}

    def set_alias(self, query: str, alias: str | None, inventory: Inventory | None = None, dry_run: bool = False) -> WriteResult:
        """Local-only label override (this machine; the router is not touched). Prefer rename(), which names the
        device on the router itself. alias=None removes it."""
        inv = inventory or self.r.mesh.inventory()
        c = inv.device(query)
        al = self.aliases()
        old = al.get(c.mac)
        if alias:
            al[c.mac] = alias
        else:
            al.pop(c.mac, None)
        if not dry_run and old != alias:
            self._alias_file.parent.mkdir(parents=True, exist_ok=True)
            self._alias_file.write_text(json.dumps(dict(sorted(al.items())), ensure_ascii=False, indent=1))
        return WriteResult([Change(f"alias[{c.mac}]", old, alias, alias)], None,
                           None if not c.randomized_mac else "device uses a randomized MAC: alias breaks if the device rotates it",
                           None, dry_run=dry_run)

    # -- queries --
    def list(self, node: str | None = None, band: str | None = None, weak_below: int | None = None,
             sort: str = "node", inventory: Inventory | None = None, backhaul_limited: bool = False) -> list[Device]:
        """Filtered device list. Pass an existing Inventory to avoid re-fetching in batch work."""
        if sort not in SORTS:
            raise UsageError(f"sort must be one of {list(SORTS)}")
        inv = inventory or self.r.mesh.inventory()
        rows = inv.devices
        if node:
            name = inv.node(node).name
            rows = [c for c in rows if c.node == name]
        if band:
            b = normalize_band(band)
            rows = [c for c in rows if c.band.upper() == b.upper()]
        if weak_below is not None:
            rows = [c for c in rows if isinstance(c.signal_dbm, int) and c.signal_dbm < weak_below
                    and c.signal_grade is not None]        # ungraded (placeholder / implausible) readings aren't weak
        if backhaul_limited:
            rows = [c for c in rows if c.backhaul_limited]
        return sorted(rows, key=SORTS[sort])

    def find(self, query: str, inventory: Inventory | None = None) -> list[Device]:
        """Label/alias/name substring (case-insensitive), exact IP, or MAC (any separator, partial ok)."""
        inv = inventory or self.r.mesh.inventory()
        q, mac = query.lower(), query.upper().replace(":", "-")
        rows = [c for c in inv.devices if q in c.label.lower() or q in c.name.lower() or query == c.ip
                or mac in c.mac or any(query == a for a in c.ipv6)]
        if not rows:
            raise UsageError(f"no device matches '{query}' ({len(inv.devices)} online)", query,
                             sorted(c.label for c in inv.devices), "device")
        return rows
