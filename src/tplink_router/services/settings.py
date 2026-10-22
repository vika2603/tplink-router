"""Read-only view of the router's settings pages, grouped like the web UI (secrets redacted).

Each area maps to the endpoints its web UI page reads (see references/api.md). Endpoints the model lacks are
skipped and reported, so one table works across models.
"""
from __future__ import annotations

import re

from . import Service

# Only real secrets: keys, passwords, PINs, tokens. (Snapshots use a broader rule; don't change that one, or old
# snapshots would diff against new ones.)
SECRET = re.compile(r"(^|_)(psk_key|key|password|passwd|pwd|secret|token|pin|wps_pin|private_key|preshared_key|psk)$", re.I)


def redact(obj, show: bool = False):
    if show:
        return obj
    if isinstance(obj, dict):
        return {k: ("***" if SECRET.search(k) and isinstance(v, str) and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj

# area -> (web UI page, [(label, endpoint, op)])
AREAS: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "lan": ("Advanced > Network > LAN", [
        ("ipv4", "admin/network?form=lan_ipv4", "read"), ("ipv6", "admin/network?form=lan_ipv6", "read"),
        ("jumbo_frame", "admin/network?form=lan_jumbo_frame", "read"), ("link_aggregation", "admin/network?form=lan_agg", "read"),
        ("flow_control", "admin/network?form=lan_fc", "read")]),
    "wan": ("Advanced > Network > Internet", [
        ("status", "admin/network?form=wan_ipv4_status", "read"), ("ipv4", "admin/network?form=status_ipv4", "read"),
        ("dns_privacy", "admin/network?form=wan_dohdot", "read"), ("mac_clone", "admin/network?form=mac_clone_advanced", "read"),
        ("port", "admin/network?form=wan_port_status", "read"), ("flow_control", "admin/network?form=wan_fc", "read"),
        ("dual_nat", "admin/status?form=wan_dual_nat_state", "read")]),
    "ipv6": ("Advanced > IPv6", [
        ("wan", "admin/network?form=wan_ipv6_status", "read"), ("lan", "admin/network?form=lan_ipv6", "read"),
        ("firewall_rules", "admin/nat?form=fr6", "load")]),
    "dhcp": ("Advanced > Network > DHCP Server", [
        ("server", "admin/dhcps?form=setting", "read"), ("reservations", "admin/dhcps?form=reservation", "load")]),
    "guest": ("Advanced > Wireless > Guest Network", [
        ("access", "admin/wireless?form=guest", "read"), ("bands", "admin/wireless?form=guest_2g", "read"),
        ("bandwidth", "admin/wireless?form=guestnetwork_bandwidth_ctrl", "read")]),
    "iot": ("Advanced > Wireless > IoT Network", [
        ("network", "admin/wireless?form=iot_2g", "read_spf"), ("isolation", "admin/iot_security?form=enable", "read")]),
    "mlo": ("Wireless > MLO Network", [("network", "admin/wireless?form=mlo_host", "read")]),
    "wireless_extra": ("Advanced > Wireless > Additional Settings", [
        ("additional", "admin/wireless?form=wireless_addition_setting", "read"), ("ofdma_mimo", "admin/wireless?form=ofdma_mimo", "read"),
        ("region", "admin/wireless?form=region", "read")]),
    "wps": ("Advanced > Wireless > WPS", [
        ("wps", "admin/wireless?form=syspara_wps", "read"), ("connect", "admin/wireless?form=wps_connect", "read")]),
    "schedules": ("Wireless Schedule · Eco Mode · Reboot schedule", [
        ("wifi_schedule", "admin/wireless_schedule_v2?form=settings", "read"),
        ("eco_mode", "admin/eco_mode_v2?form=settings", "read"), ("reboot_schedule", "admin/reboot?form=set", "read")]),
    "led": ("Advanced > System > LED Control", [
        ("led", "admin/ledgeneral?form=setting", "read"), ("night_mode", "admin/ledpm?form=setting", "read")]),
    "time": ("Advanced > System > Time", [
        ("time", "admin/time?form=settings", "read"), ("dst", "admin/time?form=dst", "read")]),
    "qos": ("HomeCare > QoS", [("qos", "admin/smart_network?form=qos", "read"),
                               ("speed_limits", "admin/smart_network?form=client_speed_limit", "read_max")]),
    "parental": ("HomeCare > Parental Controls", [
        ("profiles", "admin/avira_parental_control?form=avira_pactrl", "getOwnerTotalData")]),
    "access_control": ("Advanced > Security > Access Control", [
        ("enable", "admin/access_control?form=enable", "read"), ("mode", "admin/access_control?form=mode", "read"),
        ("blocked", "admin/access_control?form=black_list", "load"), ("allowed", "admin/access_control?form=white_list", "load")]),
    "firewall": ("Advanced > Security > Firewall", [
        ("spi", "admin/security_settings?form=new_enable", "read"), ("ip_mac_binding", "admin/imb?form=setting", "read")]),
    "nat": ("Advanced > NAT Forwarding", [
        ("nat", "admin/nat?form=setting", "read"), ("port_forwarding", "admin/nat?form=vs", "load"),
        ("port_triggering", "admin/nat?form=pt", "load"), ("dmz", "admin/nat?form=dmz", "read"),
        ("upnp", "admin/upnp?form=enable", "read"), ("alg", "admin/nat?form=alg", "read")]),
    "routing": ("Advanced > Network > Routing", [
        ("static", "admin/network?form=routes_static", "load"), ("system", "admin/network?form=routes_system", "load")]),
    "iptv": ("Advanced > Network > IPTV/VLAN", [("iptv", "admin/iptv?form=setting", "read")]),
    "ddns": ("Advanced > Network > Dynamic DNS", [
        ("provider", "admin/ddns?form=provider", "read"), ("tplink", "admin/ddns?form=tplink", "load")]),
    "vpn": ("Advanced > VPN Server / VPN Client", [
        ("client", "admin/vpn?form=enable", "read"), ("openvpn", "admin/openvpn?form=config", "read"),
        ("pptp", "admin/pptpd?form=config", "read"), ("l2tp", "admin/l2tpoveripsec?form=config", "read"),
        ("wireguard", "admin/wireguard?form=config", "read")]),
    "mesh": ("Advanced > EasyMesh", [("easymesh", "admin/easymesh?form=easymesh_enable", "read")]),
    "admin": ("Advanced > System > Administration (no credentials)", [
        ("remote", "admin/administration?form=remote", "read"), ("https", "admin/administration?form=https", "read"),
        ("mode", "admin/administration?form=mode", "read"), ("operation_mode", "admin/system?form=sysmode_v2", "read")]),
    "usb": ("Advanced > USB", [
        ("sharing", "admin/folder_sharing?form=server", "read"), ("media", "admin/folder_sharing?form=media", "read"),
        ("time_machine", "admin/time_machine?form=settings", "read")]),
}


# Switches `tpctl settings set` may change: key -> (endpoint, field, partial write?, disruptive?, what it does).
# Payload shapes follow the web UI (references/api.md); everything else stays read-only here (`api set` exists).
SWITCHES: dict[str, tuple[str, str, bool, bool, str]] = {
    "firewall.wan_ping": ("admin/security_settings?form=new_enable", "wan_ping", False, False,
                          "answer ping from the internet (off = stealthier; behind CGNAT nobody reaches it anyway)"),
    "firewall.lan_ping": ("admin/security_settings?form=new_enable", "lan_ping", False, False, "answer ping from the LAN"),
    "firewall.spi": ("admin/security_settings?form=new_enable", "enable", False, True,
                     "SPI firewall: off exposes LAN devices with public IPv6 addresses"),
    "iot.isolation": ("admin/iot_security?form=enable", "enable", True, False, "keep IoT-network devices away from the main LAN"),
    "schedules.wifi_schedule": ("admin/wireless_schedule_v2?form=settings", "enable", True, False,
                                "apply the Wi-Fi off-schedule (rules are edited in the web UI)"),
    "schedules.eco_mode": ("admin/eco_mode_v2?form=settings", "enable", True, True,
                           "Eco mode: lower radio power on its schedule (shorter range)"),
    "nat.boost": ("admin/nat?form=setting", "boost_enable", True, True,
                  "hardware NAT acceleration (off while QoS is on); the router re-applies NAT (~2 min)"),
    "qos.enable": ("admin/smart_network?form=qos", "enable", False, True,
                   "QoS: prioritises interactive traffic using the up/down bandwidth set in the web UI (set it to ~90 % of "
                   "the real line speed); turns NAT acceleration off; the cure for bufferbloat"),
    "lan.jumbo_frame": ("admin/network?form=lan_jumbo_frame", "enable", True, True,
                        "jumbo frames on the LAN ports (only if every wired device supports them); ports may re-link"),
    "wps.wps": ("admin/wireless?form=syspara_wps", "wps", False, True,
                "WPS push-button pairing (off is safer); the radios may restart"),
    "time.dst": ("admin/time?form=dst", "dst_enable", True, False,
                 "daylight saving time (rules: web UI); schedules shift with the clock"),
    "usb.media_sharing": ("admin/folder_sharing?form=media", "media_sharing", True, False,
                          "DLNA media server for a USB drive (TVs/consoles on the LAN can browse it)"),
    "admin.remote": ("admin/administration?form=remote", "enable", False, True,
                     "remote management: the router's admin page reachable from the internet over HTTPS (behind CGNAT "
                     "nobody can reach it anyway); off is safer"),
}
# switches whose payload comes from the web UI code but that were not round-tripped on a live router yet
UNVERIFIED = {"firewall.spi", "firewall.lan_ping", "iot.isolation", "schedules.eco_mode", "nat.boost", "qos.enable",
              "lan.jumbo_frame", "wps.wps", "admin.remote"}


def native(current, on: bool) -> str:
    """The router's own vocabulary for this field: yes/no or on/off (from the current value)."""
    if str(current).lower() in ("yes", "no"):
        return "yes" if on else "no"
    if isinstance(current, bool) or str(current).lower() in ("true", "false"):
        return "true" if on else "false"     # JSON-bool fields (e.g. wireless_schedule_v2) accept true/false
    return "on" if on else "off"


def flatten(prefix: str, v, out: dict) -> None:
    """Nested dict -> dotted keys; lists of rows stay as one value (JSON in records)."""
    if isinstance(v, dict):
        for k, x in v.items():
            flatten(f"{prefix}.{k}" if prefix else k, x, out)
    else:
        out[prefix] = v


class SettingsService(Service):
    def set_switch(self, key: str, on: bool, dry_run: bool = False):
        from ..errors import UsageError
        if key not in SWITCHES:
            raise UsageError(f"'{key}' is not a settable switch", key, sorted(SWITCHES), "setting")
        ep, field, partial, _, what = SWITCHES[key]
        cur = self.r.call(ep, "read") or {}
        if field not in cur:
            raise UsageError(f"this router's {ep} has no '{field}' field", key, sorted(cur), "setting")
        res = self.r.apply_form(ep, {field: native(cur.get(field), on)}, tag=f"set-{key}", partial=partial,
                                dry_run=dry_run, side_effect=what)
        for c in res.changes:
            c.key = key                     # report the switch name the user gave, not the raw field
        return res

    def areas(self) -> list[dict]:
        return [{"area": a, "page": page, "sections": [s[0] for s in secs]} for a, (page, secs) in AREAS.items()]

    def get(self, area: str, show_secrets: bool = False) -> dict:
        """{"area", "page", "values": {section.key: value}, "unsupported": [section]} for one area."""
        from ..errors import UsageError
        if area not in AREAS:
            raise UsageError(f"unknown settings area '{area}'", area, list(AREAS), "area")
        page, secs = AREAS[area]
        values, missing = {}, []
        for label, ep, op in secs:
            res = self.r.try_call(ep, op, quiet=True, default={"__missing__": True})
            if isinstance(res, dict) and res.get("__missing__"):
                missing.append(label)
                continue
            flatten(label, redact(res, show_secrets), values)
        return {"area": area, "page": page, "values": values, "unsupported": missing}
