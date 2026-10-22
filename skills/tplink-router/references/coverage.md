# Web UI → tpctl coverage

Every page of the router's web UI (Vue "SG" UI, verified on Archer BE805 fw 1.5) and the tpctl command that covers it.
R = read, W = change. "settings X" = `tpctl settings X` (read-only view of that page). Use this to answer
"can tpctl do what the web page does?" and to find the command.

| Web UI page (route) | tpctl | |
|---|---|---|
| Network map (`/`) | `overview`, `mesh show`, `devices list` | R |
| Internet (`/internetBasic`, `/internetAdv`) | `status`, `settings wan` | R |
| Network status (`/networkStatus`) | `status` | R |
| Wireless (`/wirelessBasic`, `/wirelessSettingsAdv`) | `wifi show`, `wifi set …` | R W |
| Guest network (`/guestNetworkAdv`) | `wifi guest [on/off --ssid --password-stdin --open]` | R W |
| IoT network (`/iotNetworkAdv`) | `wifi iot [on/off]`, `settings iot` (SSID/password: web UI) | R W |
| Additional wireless settings (`/additionalSettings`) | `wifi show`, `settings wireless_extra`, `wifi set ofdma-mimo` | R W |
| Wireless schedule (`/wirelessSchedule`) | `wifi schedule [on|off|add --from --to --days --bands|rm N]` | R W |
| Eco mode (`/ecoMode`) | `settings schedules`, `settings set schedules.eco_mode on/off` | R W |
| WPS (`/wps`) | `settings wps`, `settings set wps.wps on/off` | R W |
| EasyMesh (`/easyMesh`) | `mesh show/node/set/reboot/wait` | R W |
| LAN (`/lanAdv`) | `settings lan`, `settings set lan.jumbo_frame on/off` (LAN IP: web UI) | R W |
| DHCP server (`/dhcpServer`) | `dhcp show/server on|off/set-range/set-gateway/set-lease/set-dns/leases/reservations/reserve/edit/unreserve` (the whole page) | R W |
| IPv6 (`/ipv6`) + IPv6 firewall | `status`, `settings ipv6`, `nat ipv6-rules`, `nat ipv6-allow add/rm` (IPv6 connection type: web UI) | R W |
| Routing (`/routingAdv`) | `lan routes`, `lan route add/rm` | R W |
| IPTV/VLAN (`/iptvAdv`) | `settings iptv` (read-only on purpose: the ISP VLAN for the internet itself lives in this form) | R |
| Dynamic DNS (`/ddnsAdv`) | `settings ddns` | R |
| Port forwarding (`/portForwarding`) | `nat forwards`, `nat forward add/rm` | R W |
| Port triggering (`/portTriggering`) | `nat triggers`, `nat trigger add/rm` | R W |
| NAT acceleration (NAT page) | `nat show`, `settings set nat.boost on/off` | R W |
| DMZ (`/dmz`) | `nat dmz on --to IP / off`, `settings nat` | R W |
| UPnP (`/upnp`) | `nat upnp [on/off]` (+ the mappings devices opened) | R W |
| ALG (`/alg`) | `nat alg [k=v]` | R W |
| Firewall (`/firewall`) | `nat show`, `settings set firewall.spi|wan_ping|lan_ping on/off` | R W |
| IP-MAC binding (`/ipMacBinding`) | `lan bindings`, `lan bind/unbind` | R W |
| Access control (`/accessControl`) | `devices block/unblock/blocked`, `settings access_control` | R W |
| IoT security (`/iotSecurity`) | `settings iot`, `settings set iot.isolation on/off` | R W |
| QoS (`/qos`, `/qosAdv`) + client speed limits | `qos show`, `qos set on|off --down --up`, `qos limits`, `qos limit DEVICE --down --up | off` (device priority: web UI) | R W |
| Parental controls (`/parentalControls*`) | `parental` (list), `parental add NAME --device Q --bedtime HH:MM-HH:MM`, `parental pause NAME on|off`, `parental rm NAME` (website filters: web UI) | R W |
| Network check / HomeShield scan (`/networkCheck*`) | `check` (tpctl's own health check) | R |
| VPN client/server (`/vpnClient`, `/pptp`, `/l2tpIpsec`, `/openVpn`, `/wireguardVpn`, `/connections`) | `settings vpn` (read-only by the owner's decision: edge feature, not needed) | R |
| USB sharing / Time Machine (`/storageSharing`, `/timeMachine`) | `settings usb`, `settings set usb.media_sharing on/off` (share accounts/folders: web UI) | R W |
| Time (`/timeSettings`) | `system time [--ntp --timezone --24h/--12h --zones]`, `settings set time.dst on/off` | R W |
| Operation mode (`/operationMode`) | `settings admin` (operation_mode) | R |
| Administration (`/administration`) | `settings admin`, `settings set admin.remote on/off` (credentials, HTTPS/local-management lists: web UI) | R W |
| LED (`/led`) | `system led on/off`, `system night-mode` | R W |
| Reboot (`/reboot`) | `system reboot`, `system reboot-schedule` | R W |
| Firmware (`/firmware`) | `system firmware` (main + satellites, online check), `system upgrade` (online), `system auto-update on/off --at H` (file upload: web UI) | R W |
| Backup/restore (`/backupRestore`) | `system backup` (the router's own .bin, restorable in the web UI) + `snapshot save/list/diff/timeline` (redacted JSON); restore: web UI | R W |
| System log (`/sysLog`) | `system log [--type --level --since --grep]` | R |
| Diagnostics (`/diagnostics`) | `net diag HOST [--traceroute]` | R |
| Client names (network map → client) | `devices rename` | W |
| — (not in the web UI) | `devices identify/probe`, `net speed`, `net ping --device`, `events`, `devices roaming`, `system ports` | R |

In scope: 39 of the 44 pages are fully covered. The other 5 are read-only on purpose: WAN connection type and operation
mode (a wrong write cuts the internet), IPTV/VLAN (this ISP's internet VLAN lives in that form), DDNS (needs a third-party or
TP-Link account, useless behind CGNAT) and VPN servers (edge feature; the owner decided not to cover it).

Not covered on purpose: TP-Link cloud account, Alexa / Google Assistant linking, usage-statistics opt-in,
Fing consent, quick setup, factory reset, firmware upload, changing the admin password or WAN credentials,
VPN account management (credentials). Raw access for anything else: `tpctl api get|set` with
`references/api.md`.

Writes that exist in the UI but are not wrapped (use `api set` with care): WAN/IPv6 connection type, LAN IP,
parental profiles, eco-mode schedule times, IoT network SSID/password, DDNS, VPN servers, IPTV, device priority,
USB share accounts.
