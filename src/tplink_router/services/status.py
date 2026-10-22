from __future__ import annotations

import ipaddress

from . import Service


def _private_ipv4(ip) -> bool | None:
    """WAN address in private/shared space (RFC1918 / 100.64/10) => CGNAT or double NAT."""
    try:
        a = ipaddress.ip_address(str(ip))
        return a.is_private or a in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return None


class StatusService(Service):
    def firmware(self) -> dict:
        f = self.r.lib_call("get_firmware")
        return {"model": f.model, "hardware": f.hardware_version, "firmware": f.firmware_version}

    def wan(self) -> dict:
        r = self.r
        v4 = r.call("admin/network?form=status_ipv4")
        pppoe = r.try_call("admin/network?form=wan_ipv4_pppoe", quiet=True) if v4.get("wan_ipv4_conntype") == "pppoe" else {}
        return {"internet": r.try_call("admin/status?form=internet").get("internet_status"),
                "type": v4.get("wan_ipv4_conntype"), "ipv4": v4.get("wan_ipv4_ipaddr"),
                "gateway": v4.get("wan_ipv4_gateway"),
                "cgnat": (r.try_call("admin/status?form=wan_dual_nat_state").get("wan_dual_nat") == "1"
                          if r.caps.has("dual_nat_state") else _private_ipv4(v4.get("wan_ipv4_ipaddr"))),
                "uptime_min": int(v4.get("wan_ipv4_uptime") or 0) // 60, "mtu": pppoe.get("mtu"),
                "dns": [x for x in (v4.get("wan_ipv4_pridns"), v4.get("wan_ipv4_snddns")) if x],
                "wan_mac": v4.get("wan_macaddr"), "lan_ip": v4.get("lan_ipv4_ipaddr")}

    def ipv6(self) -> dict:
        v6 = vars(self.r.lib_call("get_ipv6_status"))
        lan6 = self.r.try_call("admin/network?form=lan_ipv6", quiet=True)
        return {"enabled": v6.get("wan_ipv6_enabled"), "type": v6.get("_wan_ipv6_conntype"),
                "wan_addr": str(v6.get("_wan_ipv6_addr") or ""), "lan_prefix": str(v6.get("_ipv6_site_prefix") or ""),
                "lan_assign": lan6.get("assign_type"),
                "dns": [str(x) for x in (v6.get("_wan_ipv6_pridns"), v6.get("_wan_ipv6_snddns")) if x]}

    def security(self) -> dict:
        r, has = self.r, self.r.caps.has
        q = lambda ep, op="read": r.try_call(ep, op, quiet=True)   # optional on some models: absence isn't an error
        fw = r.try_call("admin/security_settings?form=new_enable") if has("firewall") else {}
        return {"spi_firewall": fw.get("enable", "unsupported" if not has("firewall") else None),
                "wan_ping": fw.get("wan_ping"), "lan_ping": fw.get("lan_ping"),
                "nat_boost": q("admin/nat?form=setting").get("boost_enable"),
                "upnp": q("admin/upnp?form=enable").get("enable"),
                "dmz": q("admin/nat?form=dmz").get("enable"),
                "port_forwards": len(q("admin/nat?form=vs") or {}),
                "ipv6_allow_rules": len((r.try_call("admin/nat?form=fr6") or {}).get("list") or {}) if has("ipv6_rules") else None,
                "remote_mgmt": q("admin/administration?form=remote").get("enable"),
                "iot_isolation": r.try_call("admin/iot_security?form=enable").get("enable") if has("iot_isolation") else None}

    def inbound(self, wan: dict | None = None, sec: dict | None = None) -> dict:
        """Can the internet initiate connections to LAN devices? Computed from CGNAT, forwards, firewall, IPv6 rules."""
        wan, sec = wan or self.wan(), sec or self.security()
        fwd = int(sec.get("port_forwards") or 0)
        exposed4 = [x for x, on in (("port_forwards", fwd > 0), ("dmz", sec.get("dmz") == "on"), ("upnp", sec.get("upnp") == "on")) if on]
        if wan.get("cgnat"):
            v4 = "blocked (CGNAT: no public IPv4; forwards/DMZ/UPnP can't help)"
        else:
            v4 = f"exposed via {', '.join(exposed4)}" if exposed4 else "blocked (NAT, no forwards/DMZ/UPnP)"
        rules, spi = sec.get("ipv6_allow_rules"), sec.get("spi_firewall")
        if spi != "on":
            v6 = "OPEN (SPI firewall off: every device with a global IPv6 is reachable)"
        elif rules:
            v6 = f"only {rules} allowed IPv6 rule(s) (`tpctl api get 'admin/nat?form=fr6'`)"
        else:
            v6 = "blocked (firewall on, 0 IPv6 allow rules)"
        # other ways in: the router's own remote admin and VPN servers (both need a reachable WAN to matter)
        tc = lambda ep: self.r.try_call(ep, quiet=True) or {}
        vpn = [n for n, ep, k in (("openvpn", "admin/openvpn?form=config", "enabled"), ("pptp", "admin/pptpd?form=config", "enabled"),
                                  ("l2tp", "admin/l2tpoveripsec?form=config", "enabled"), ("wireguard", "admin/wireguard?form=config", "enable"))
               if str(tc(ep).get(k)).lower() in ("on", "true", "yes", "1")]
        dmz = tc("admin/nat?form=dmz")
        return {"ipv4": v4, "ipv6": v6, "remote_admin": sec.get("remote_mgmt") or "unknown",
                "vpn_server": ",".join(vpn) or "off",
                "dmz_staged": dmz.get("ipaddr") if dmz.get("enable") != "on" and dmz.get("ipaddr") else None,
                "note": "remote_admin/vpn_server also need a reachable WAN (not behind CGNAT) to be usable from outside"}

    def get(self) -> dict:
        """Everything non-wireless in one dict: router, wan, ipv6, dhcp, security, alg."""
        wan, sec = self.wan(), self.security()
        return {"router": self.firmware(), "wan": wan, "ipv6": self.ipv6(), "dhcp": self.r.dhcp.get(), "security": sec,
                "inbound": self.inbound(wan, sec), "alg": self.r.nat.alg()}
