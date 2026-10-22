from __future__ import annotations

import re

from ..errors import UsageError
from ..models import WriteResult
from . import Service

VS_EP = "admin/nat?form=vs"
FR6_EP = "admin/nat?form=fr6"
ALGS = ("ftp", "tftp", "h323", "rtsp", "sip", "pptp", "l2tp", "ipsec")


class NatService(Service):
    def alg(self) -> dict:
        return self.r.try_call("admin/nat?form=alg") if self.r.caps.has("alg") else {}

    def set_alg(self, dry_run: bool = False, **changes: str | bool) -> WriteResult:
        """set_alg(sip=False, h323="off"). Keep pptp/l2tp/ipsec on for VPN devices."""
        self.r.caps.require("alg", "ALG settings")
        if bad := set(changes) - set(ALGS):
            raise UsageError(f"unknown ALG {bad}; valid {ALGS}")
        norm = {k: ("on" if v in (True, "on") else "off" if v in (False, "off") else None) for k, v in changes.items()}
        if None in norm.values():
            raise UsageError("ALG values must be on/off/True/False")
        return self.r.apply_form("admin/nat?form=alg", norm, tag="alg", dry_run=dry_run)

    def firewall(self) -> dict:
        return self.r.try_call("admin/security_settings?form=new_enable")

    def port_forwards(self) -> list[dict]:
        return self.r.list_rows(VS_EP)

    def add_forward(self, name: str, ip: str, external: str, internal: str | None = None, protocol: str = "all",
                    dry_run: bool = False) -> WriteResult:
        """Port forwarding rule (Advanced > NAT Forwarding > Port Forwarding). external: '8080' or '8000-8010'."""
        if not re.fullmatch(r"\d{1,5}(-\d{1,5})?", external) or (internal and not re.fullmatch(r"\d{1,5}(-\d{1,5})?", internal)):
            raise UsageError("ports must be N or N-M (1-65535)", external, None, "port")
        proto = protocol.upper()
        if proto not in ("ALL", "TCP", "UDP"):
            raise UsageError("protocol must be all, tcp or udp", protocol, ["all", "tcp", "udp"], "protocol")
        try:
            wan = self.r.status.wan()
        except Exception:                       # side-effect text only; never block the write on it
            wan = {}
        cg = wan.get("cgnat") or wan.get("dual_nat")
        side = ("WAN is behind CGNAT/double NAT: the rule saves but nothing from the internet reaches it" if cg else
                f"{ip}:{internal or external} becomes reachable from the internet on port {external}")
        return self.r.list_change(VS_EP, tag="port-forward", key_field="name", label="port_forward", dry_run=dry_run,
                                  add={"name": name, "ipaddr": ip, "external_port": external, "internal_port": internal or external,
                                       "protocol": proto, "enable": "on"}, side_effect=side, next_cmd="tpctl nat forwards")

    def triggers(self) -> list[dict]:
        return self.r.list_rows("admin/nat?form=pt")

    def add_trigger(self, name: str, trigger: str, open_ports: str, trigger_proto: str = "all", open_proto: str = "all",
                    dry_run: bool = False) -> WriteResult:
        """Port triggering: when a LAN device sends to `trigger`, the router opens `open_ports` back to it."""
        for p in (trigger, open_ports):
            if not re.fullmatch(r"\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*", p):
                raise UsageError("ports must be N, N-M or a comma list", p, None, "port")
        tp, op = trigger_proto.upper(), open_proto.upper()
        if not {tp, op} <= {"ALL", "TCP", "UDP"}:
            raise UsageError("protocol must be all, tcp or udp", trigger_proto, ["all", "tcp", "udp"], "protocol")
        return self.r.list_change("admin/nat?form=pt", tag="port-trigger", key_field="name", label="port_trigger",
                                  add={"name": name, "trigger_port": trigger, "trigger_protocol": tp, "external_port": open_ports,
                                       "external_protocol": op, "enable": "on"}, json_body=True, json_list=False, dry_run=dry_run,
                                  side_effect="inbound only while the trigger is active (behind CGNAT: unreachable anyway)",
                                  next_cmd="tpctl nat triggers")

    def remove_trigger(self, name: str, dry_run: bool = False) -> WriteResult:
        return self.r.list_change("admin/nat?form=pt", tag="port-trigger-rm", key_field="name", label="port_trigger",
                                  remove=name, dry_run=dry_run, next_cmd="tpctl nat triggers")

    def remove_forward(self, name: str, dry_run: bool = False) -> WriteResult:
        return self.r.list_change(VS_EP, tag="port-forward-rm", key_field="name", label="port_forward", remove=name,
                                  dry_run=dry_run, next_cmd="tpctl nat forwards")

    def ipv6_rules(self) -> dict:
        return self.r.try_call("admin/nat?form=fr6")

    def ipv6_allows(self) -> list[dict]:
        return self.r.list_rows(FR6_EP)

    def add_ipv6_allow(self, name: str, target: str, port: str, protocol: str = "all", dry_run: bool = False) -> WriteResult:
        """IPv6 firewall allow rule (Advanced > IPv6 firewall): inbound `port` from the internet to one device's
        global IPv6 address. target = an IPv6 address or a device (label/IP/MAC; its global address is used)."""
        import ipaddress
        import uuid
        if not re.fullmatch(r"\d{1,5}(-\d{1,5})?", port):
            raise UsageError("port must be N or N-M (1-65535)", port, None, "port")
        proto = protocol.upper()
        if proto not in ("ALL", "TCP", "UDP"):
            raise UsageError("protocol must be all, tcp or udp", protocol, ["all", "tcp", "udp"], "protocol")
        note = None
        try:
            ip = ipaddress.IPv6Address(target)
        except ValueError:
            c = self.r.mesh.inventory().device(target)
            glob = [a for a in c.ipv6 if ipaddress.IPv6Address(a).is_global]
            if not glob:
                raise UsageError(f"{c.label} has no global IPv6 address the router knows of; pass the address", target,
                                 None, "ip")
            ip = ipaddress.IPv6Address(glob[0])
            if len(glob) > 1:
                note = (f"{c.label} has {len(glob)} global IPv6 addresses (privacy addresses rotate); using {ip}. "
                        "A server should use its stable address: pass it explicitly")
        if not ip.is_global:
            raise UsageError(f"{ip} is not a global IPv6 address", str(ip), None, "ip")
        side = f"[{ip}]:{port}/{proto.lower()} becomes reachable from the whole IPv6 internet" + (f"; {note}" if note else "")
        return self.r.list_change(FR6_EP, tag="ipv6-allow", key_field="name", label="ipv6_allow", dry_run=dry_run,
                                  add={"key": str(uuid.uuid4()), "enable": "on", "type": "CUSTOM", "name": name, "ip": str(ip),
                                       "ip_type": "custom", "port": port, "protocol": proto},
                                  json_body=True, json_list=False, side_effect=side, next_cmd="tpctl nat ipv6-rules")

    def remove_ipv6_allow(self, name: str, dry_run: bool = False) -> WriteResult:
        return self.r.list_change(FR6_EP, tag="ipv6-allow-rm", key_field="name", label="ipv6_allow", remove=name,
                                  dry_run=dry_run, next_cmd="tpctl nat ipv6-rules")

    def upnp(self) -> dict:
        """UPnP switch + the port mappings devices opened through it."""
        d = self.r.try_call("admin/upnp?form=enable") or {}
        rows = self.r.try_call("admin/upnp?form=service", "load", quiet=True, default=[]) or []
        rows = rows.get("list", []) if isinstance(rows, dict) else rows
        return {"enable": d.get("enable"), "mappings": [x for x in rows if isinstance(x, dict)]}

    def set_upnp(self, on: bool, dry_run: bool = False) -> WriteResult:
        """UPnP lets LAN devices (consoles, P2P apps) open inbound ports themselves. Off = safer, on = fewer NAT
        problems for games/calls. Behind CGNAT it can't open anything to the internet either way."""
        return self.r.apply_form("admin/upnp?form=enable", {"enable": "on" if on else "off"}, tag="upnp", partial=True,
                                 dry_run=dry_run, key_prefix="upnp.")

    def set_dmz(self, on: bool, ip: str | None = None, dry_run: bool = False) -> WriteResult:
        """DMZ host: every unsolicited inbound IPv4 connection goes to `ip` (its whole firewall is exposed)."""
        if on:
            import ipaddress
            if not ip:
                raise UsageError("DMZ on needs the LAN device IP (--to)", None, None, "ip")
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise UsageError(f"invalid IP {ip}", ip, None, "ip")
            ch = {"enable": "on", "ipaddr": ip}
        else:
            ch = {"enable": "off"}
        try:
            cg = self.r.status.wan().get("cgnat")
        except Exception:
            cg = None
        side = (None if not on else "WAN is behind CGNAT: nothing from the internet reaches it anyway" if cg else
                f"{ip} receives every inbound IPv4 connection from the internet: only for a device that is itself hardened")
        return self.r.apply_form("admin/nat?form=dmz", ch, tag="dmz", partial=True, dry_run=dry_run, key_prefix="dmz.",
                                 side_effect=side)

    def settings(self) -> dict:
        return {"nat": self.r.try_call("admin/nat?form=setting"), "dmz": self.r.try_call("admin/nat?form=dmz"),
                "upnp": self.r.try_call("admin/upnp?form=enable"), "port_triggers": self.r.try_call("admin/nat?form=pt")}
