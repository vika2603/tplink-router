"""LAN-side lists: static routes and IP-MAC bindings (web UI: Network > Routing, Security > IP & MAC Binding)."""
from __future__ import annotations

import ipaddress
import re
import uuid

from ..errors import UsageError
from ..models import WriteResult
from . import Service

ROUTES_EP = "admin/network?form=routes_static"
BIND_EP = "admin/imb?form=bind_list"


class LanService(Service):
    def routes(self) -> dict:
        return {"static": self.r.list_rows(ROUTES_EP), "system": self.r.list_rows("admin/network?form=routes_system")}

    def add_route(self, name: str, target: str, gateway: str, interface: str = "LAN", dry_run: bool = False) -> WriteResult:
        """Static route: traffic for `target` (CIDR, e.g. 10.20.0.0/24) goes via `gateway` on LAN or WAN."""
        try:
            net = ipaddress.ip_network(target, strict=False)
            ipaddress.ip_address(gateway)
        except ValueError as e:
            raise UsageError(f"bad route: {e}", target, None, "route")
        if interface.upper() not in ("LAN", "WAN"):
            raise UsageError("interface must be lan or wan", interface, ["lan", "wan"], "interface")
        row = {"key": uuid.uuid4().hex[:16], "enable": "on", "target": str(net), "gateway": gateway,
               "interface": interface.upper(), "name": name}
        return self.r.list_change(ROUTES_EP, tag="route", key_field="name", label="route", add=row, json_body=True,
                                  json_list=False, dry_run=dry_run, side_effect=f"traffic for {net} now goes via {gateway}",
                                  next_cmd="tpctl lan routes")

    def remove_route(self, name: str, dry_run: bool = False) -> WriteResult:
        return self.r.list_change(ROUTES_EP, tag="route-rm", key_field="name", label="route", remove=name,
                                  dry_run=dry_run, next_cmd="tpctl lan routes")

    def bindings(self) -> dict:
        return {"enable": (self.r.try_call("admin/imb?form=setting") or {}).get("enable"), "list": self.r.list_rows(BIND_EP)}

    def bind(self, query: str, ip: str | None = None, name: str | None = None, dry_run: bool = False) -> WriteResult:
        """Pin a MAC to an IP (ARP binding). While binding is on, that IP only works for that MAC."""
        mac = query.upper().replace(":", "-")
        if not re.fullmatch(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}", mac):
            c = self.r.mesh.inventory().device(query)
            mac, ip, name = c.mac, ip or c.ip, name or c.label
        if not ip:
            raise UsageError("give --ip for a MAC that is offline", query, None, "ip")
        row = {"name": (name or mac)[:32], "ipaddr": ip, "mac": mac, "key": ip, "enable": "on"}
        return self.r.list_change(BIND_EP, tag="imb", key_field="ipaddr", label="binding", add=row, json_body=True,
                                  dry_run=dry_run, next_cmd="tpctl lan bindings",
                                  side_effect=f"{ip} is tied to {mac} (other devices can't use that IP while binding is on)")

    def unbind(self, ip: str, dry_run: bool = False) -> WriteResult:
        return self.r.list_change(BIND_EP, tag="imb-rm", key_field="ipaddr", label="binding", remove=ip, dry_run=dry_run,
                                  next_cmd="tpctl lan bindings")
