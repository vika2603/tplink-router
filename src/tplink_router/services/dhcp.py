from __future__ import annotations

import ipaddress
import re

from ..errors import UsageError
from ..models import WriteResult
from . import Service


SET_EP = "admin/dhcps?form=setting"
RES_EP = "admin/dhcps?form=reservation"
MAC_RE = re.compile(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}")


class DhcpService(Service):
    def get(self) -> dict:
        d = self.r.try_call(SET_EP)
        try:
            size = int(ipaddress.ip_address(d.get("ipaddr_end"))) - int(ipaddress.ip_address(d.get("ipaddr_start"))) + 1
        except ValueError:
            size = None
        return {"enable": d.get("enable"), "range": f"{d.get('ipaddr_start')}-{d.get('ipaddr_end')}",
                "start": d.get("ipaddr_start"), "end": d.get("ipaddr_end"), "pool_size": size,
                "lease_min": d.get("leasetime"), "gateway": d.get("gateway"),
                "dns": [x for x in (d.get("pri_dns"), d.get("snd_dns")) if x] or ["router"]}

    def _lan(self):
        """(LAN network, router's LAN IP)."""
        lan = self.r.try_call("admin/network?form=lan_ipv4") or {}
        ip = lan.get("ipaddr") or (self.r.try_call(SET_EP, quiet=True) or {}).get("gateway") or "192.168.0.1"
        mask = lan.get("mask_type") if lan.get("mask_type") not in (None, "", "custom") else lan.get("custom_value")
        return ipaddress.ip_network(f"{ip}/{mask or '255.255.255.0'}", strict=False), ipaddress.ip_address(ip)

    def _check_ip(self, ip: str, what: str = "IP"):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            raise UsageError(f"invalid {what} {ip}", ip, None, "ip")
        net, router = self._lan()
        if a not in net or a in (net.network_address, net.broadcast_address):
            raise UsageError(f"{what} {ip} is outside the LAN {net} (or is its network/broadcast address)", ip, None, "ip")
        return a, net, router

    def set_server(self, on: bool, dry_run: bool = False) -> WriteResult:
        """DHCP server on/off. Off: new or renewing devices get no address (only static-IP devices keep working)."""
        return self.r.apply_form(SET_EP, {"enable": "on" if on else "off"}, tag="dhcp-server", partial=True,
                                 dry_run=dry_run, key_prefix="dhcp.",
                                 side_effect=None if on else "devices lose their address as leases expire; new devices "
                                                             "can't join unless they use a static IP")

    def set_range(self, start: str, end: str, dry_run: bool = False) -> WriteResult:
        """Address pool handed out to devices (inside the LAN subnet, not containing the router's own IP)."""
        a, net, router = self._check_ip(start, "start")
        b, _, _ = self._check_ip(end, "end")
        if int(b) < int(a):
            raise UsageError(f"range end {end} is before start {start}", end, None, "ip")
        if a <= router <= b:
            raise UsageError(f"the range {start}-{end} contains the router's own address {router}", start, None, "ip")
        return self.r.apply_form(SET_EP, {"ipaddr_start": start, "ipaddr_end": end}, tag="dhcp-range", dry_run=dry_run,
                                 key_prefix="dhcp.", side_effect="devices outside the new range get a new address at "
                                                                 "their next renewal; reserved devices keep theirs")

    def range_impact(self, start: str, end: str) -> list[str]:
        """Caveats for a new pool: which online devices would have to change address."""
        a, b = ipaddress.ip_address(start), ipaddress.ip_address(end)
        reserved = {str(x.get("mac", "")).upper() for x in self.reservations() if x.get("enable") == "on"}
        online = {c.mac: c for c in self.r.mesh.inventory().devices}
        moved, stale = [], 0
        for c in self.r.try_call("admin/dhcps?form=client", "load", default=[]) or []:
            mac = str(c.get("macaddr", "")).upper()
            try:
                ip = ipaddress.ip_address(c.get("ipaddr"))
            except ValueError:
                continue
            if a <= ip <= b or mac in reserved:
                continue
            if mac in online:
                moved.append(f"{online[mac].label} ({ip})")
            else:
                stale += 1
        out = []
        outside = [x for x in self.reservations() if x.get("enable") == "on" and not a <= ipaddress.ip_address(x["ip"]) <= b]
        if outside:
            out.append(f"the router turns off reservations outside the pool by itself: {len(outside)} would be disabled ("
                       + ", ".join(f"{x.get('hostname') or x.get('mac')} {x.get('ip')}" for x in outside[:8])
                       + "); move them first with `tpctl dhcp edit Q --ip` or pick a pool that contains them")
        if moved:
            out.append(f"{len(moved)} online device(s) outside {start}-{end} get a new address at their next renewal: "
                       + ", ".join(moved[:12]) + (f" … (+{len(moved) - 12})" if len(moved) > 12 else ""))
        if stale:
            out.append(f"ok: {stale} more lease(s) outside the pool belong to offline devices; they get a new address "
                       "when they come back")
        out.append("devices set up with a fixed IP inside the old pool (printers, NAS, port forwards, DMZ) may now "
                   "collide with or fall outside the new pool: check `tpctl nat forwards` and `tpctl lan bindings`")
        return out

    def set_gateway(self, ip: str | None, dry_run: bool = False) -> WriteResult:
        """Default gateway handed to devices. None/"router" = the router itself (the normal setting)."""
        net, router = self._lan()
        ip = str(router) if ip in (None, "", "router") else ip
        a, _, _ = self._check_ip(ip, "gateway")
        return self.r.apply_form(SET_EP, {"gateway": ip}, tag="dhcp-gateway", dry_run=dry_run, key_prefix="dhcp.",
                                 side_effect="devices use the new gateway at their next renewal"
                                 + ("" if a == router else "; if nothing routes at that address they lose the internet"))

    def set_lease(self, minutes: int, dry_run: bool = False) -> WriteResult:
        if not 2 <= int(minutes) <= 2880:
            raise UsageError("lease must be 2-2880 minutes")
        return self.r.apply_form(SET_EP, {"leasetime": str(minutes)}, tag="dhcp", dry_run=dry_run,
                                 side_effect="devices pick up the new lease at their next renewal")

    def set_dns(self, primary: str | None, secondary: str | None = None, dry_run: bool = False) -> WriteResult:
        """DNS servers handed to devices by DHCP. primary=None (or "router") = the router itself (it forwards to the
        ISP / its own DNS settings). Devices pick it up at their next lease renewal."""
        if primary in (None, "", "router"):
            primary, secondary = "", ""
        for ip in (primary, secondary):
            if ip:
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    raise UsageError(f"invalid DNS server address {ip}", ip, None, "ip")
        return self.r.apply_form(SET_EP, {"pri_dns": primary, "snd_dns": secondary or ""}, tag="dhcp-dns",
                                 dry_run=dry_run, key_prefix="dhcp.",
                                 side_effect="devices use the new DNS at their next DHCP renewal (reconnect Wi-Fi to apply now)")

    def leases(self) -> list[dict]:
        return self.r.call("admin/dhcps?form=client", "load")

    def reservations(self) -> list[dict]:
        if not self.r.caps.has("dhcp_reservations"):
            return []
        return self.r.list_rows(RES_EP)

    def _mac_ip(self, query: str, ip: str | None):
        """Device query (label/IP/MAC) or a bare MAC -> (MAC, IP, label)."""
        mac = query.upper().replace(":", "-")
        if MAC_RE.fullmatch(mac):
            if not ip:
                dev = next((c for c in self.r.mesh.inventory().devices if c.mac == mac), None)
                ip = dev.ip if dev else None
            return mac, ip, mac
        c = self.r.mesh.inventory().device(query)
        return c.mac, ip or c.ip, c.label

    def reserve(self, query: str, ip: str | None = None, comment: str = "", dry_run: bool = False) -> WriteResult:
        """Always give this device the same IP (default: the one it has now). Takes effect at its next DHCP renewal."""
        self.r.caps.require("dhcp_reservations", "DHCP reservations")
        mac, ip, label = self._mac_ip(query, ip)
        if not ip:
            raise UsageError(f"{label} is offline: pass the IP to reserve", query, None, "ip")
        self._check_free(ip, mac)
        if any(str(x.get("mac", "")).upper() == mac for x in self.reservations()):   # already reserved: edit it in place
            return self.edit(mac, ip=ip, enable=True, dry_run=dry_run)
        return self.r.list_change(RES_EP, tag="dhcp-reserve", key_field="mac", label="reservation",
                                  add={"mac": mac, "ip": ip, "comment": comment or label[:32], "enable": "on"},
                                  dry_run=dry_run, json_body=True,
                                  side_effect=f"{label} gets {ip} at its next DHCP renewal (reconnect to apply now)",
                                  next_cmd="tpctl dhcp reservations")

    def _check_free(self, ip: str, mac: str) -> None:
        a, _, router = self._check_ip(ip)
        if a == router:
            raise UsageError(f"{ip} is the router's own address", ip, None, "ip")
        taken = [x for x in self.reservations() if x.get("ip") == ip and str(x.get("mac", "")).upper() != mac]
        if taken:
            raise UsageError(f"{ip} is already reserved for {taken[0].get('hostname') or taken[0].get('mac')}", ip, None, "ip")
        d = self.get()
        try:
            if not ipaddress.ip_address(d["start"]) <= a <= ipaddress.ip_address(d["end"]):
                raise UsageError(f"{ip} is outside the DHCP pool {d['range']}: the router disables reservations outside "
                                 "the pool", ip, None, "ip")
        except (ValueError, TypeError):
            pass
        held = [x for x in (self.r.try_call("admin/dhcps?form=client", "load", default=[]) or [])
                if x.get("ipaddr") == ip and str(x.get("macaddr", "")).upper() != mac]
        if held:
            who = held[0].get("name") or held[0].get("macaddr")
            raise UsageError(f"{ip} is still leased to {who} ({held[0].get('macaddr')}); the router won't apply a reservation "
                             f"for an IP another device holds. Free it first: reserve a different IP for {who} "
                             f"(`tpctl dhcp reserve {held[0].get('macaddr')} --ip <other>`), then retry", ip, None, "ip")

    def edit(self, query: str, ip: str | None = None, enable: bool | None = None, dry_run: bool = False) -> WriteResult:
        """Change an existing reservation's IP and/or turn it on/off (off = kept but not applied)."""
        self.r.caps.require("dhcp_reservations", "DHCP reservations")
        mac = query.upper().replace(":", "-")
        if not MAC_RE.fullmatch(mac):
            mac = self._mac_ip(query, None)[0]
        changes = {}
        if ip:
            self._check_free(ip, mac)
            changes["ip"] = ip
        if enable is not None:
            changes["enable"] = "on" if enable else "off"
        if not changes:
            raise UsageError("nothing to change: give --ip and/or --on/--off", query, None, "option")
        what = []
        if ip:
            what.append(f"gets {ip} at its next DHCP renewal (reconnect to apply now)")
        if enable is False:
            what.append("is no longer pinned: it may get a different pool address at its next renewal")
        return self.r.list_edit(RES_EP, tag="dhcp-edit", key_field="mac", ident=mac, changes=changes, label="reservation",
                                dry_run=dry_run, side_effect=("the device " + " and ".join(what)) if what else None,
                                next_cmd="tpctl dhcp reservations")

    def overview(self) -> list[dict]:
        """Reservations joined with leases and the live device list: is each device online, and does it already use
        the reserved address?"""
        rows = self.reservations()
        leases = {str(x.get("macaddr", "")).upper(): x for x in (self.r.try_call("admin/dhcps?form=client", "load", default=[]) or [])}
        try:
            devs = {c.mac: c for c in self.r.mesh.inventory().devices}
        except Exception:                                 # noqa: BLE001 - the join is best-effort
            devs = {}
        try:
            d = self.get()
            pool = (ipaddress.ip_address(d["start"]), ipaddress.ip_address(d["end"]))
        except (ValueError, TypeError):
            pool = None
        out = []
        for x in rows:
            mac = str(x.get("mac", "")).upper()
            c, lease = devs.get(mac), leases.get(mac)
            seen = [ip for ip in ((c.ip if c else None), (lease or {}).get("ipaddr")) if ip]
            now = x.get("ip") if x.get("ip") in seen else (seen[0] if seen else None)   # a device can hold 2 (Wi-Fi + wired)
            try:
                in_pool = pool[0] <= ipaddress.ip_address(x.get("ip")) <= pool[1] if pool else None
            except ValueError:
                in_pool = None
            state = ("off" if x.get("enable") != "on" else "offline" if not c else
                     "in_use" if now == x.get("ip") else "pending_renewal")
            out.append({"name": (c.label if c else None) or x.get("hostname") or x.get("comment"), "mac": mac,
                        "ip": x.get("ip"), "enable": x.get("enable"), "online": bool(c), "current_ip": now,
                        "node": c.node if c else None, "in_pool": in_pool, "state": state,
                        "lease_left": (lease or {}).get("leasetime")})
        return out

    def unreserve(self, query: str, dry_run: bool = False) -> WriteResult:
        self.r.caps.require("dhcp_reservations", "DHCP reservations")
        mac = query.upper().replace(":", "-")
        if not MAC_RE.fullmatch(mac):
            mac = self._mac_ip(query, None)[0]
        before = {str(x.get("mac", "")).upper(): x.get("enable") for x in self.reservations()}
        res = self.r.list_change(RES_EP, tag="dhcp-unreserve", key_field="mac", label="reservation", remove=mac,
                                 dry_run=dry_run, next_cmd="tpctl dhcp reservations")
        if dry_run or not res.applied:
            return res
        # the router sometimes switches another reservation off on remove (BE805 fw 1.5): put it back
        for x in self.reservations():
            m = str(x.get("mac", "")).upper()
            if before.get(m) == "on" and x.get("enable") != "on":
                fix = self.r.list_edit(RES_EP, tag="dhcp-edit", key_field="mac", ident=m, changes={"enable": "on"},
                                       label="reservation")
                res.changes += [c for c in fix.changes]
                res.side_effect = ((res.side_effect + "; ") if res.side_effect else "") + \
                    f"the router had switched {x.get('hostname') or m}'s reservation off on remove: re-enabled"
        return res
