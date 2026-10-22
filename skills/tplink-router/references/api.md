# TP-Link router web API reference (current "SG"/Vue web UI)

Verified on an Archer BE805 (fw 1.5.x) + EB431v EasyMesh satellites. Learned from the router's own web UI bundle plus live read-only probing. Other TP-Link "SG"-family models (Archer BE/AX with the Vue UI) use the same scheme, but field sets differ per model.

Legend: **R** = read verified live, **W** = write verified live, **UI-W** = UI writes it but untested here, **⚠** = action or secret, don't call casually.

Coverage: every route of the web UI was walked with the decrypted requests captured (see *Discovering endpoints*), plus a static scan of the whole UI bundle. 243 endpoints in total; `tpctl api endpoints [--grep X]` lists each with the operations the UI uses and the ones seen answering live.

Most of this is wrapped by `tpctl` commands / `tplink_router` services — check SKILL.md first. Use this file when adding a feature or calling `tpctl api get|set` / `r.call()` on something not wrapped yet; after verifying a new endpoint live, record it here (R/W markers) and consider wrapping it as a service method + CLI command.

## Transport

- `POST http://192.168.0.1/cgi-bin/luci/;stok=<token>/<path>`; body is urlencoded `operation=<op>&field=value…`, RSA+AES encrypted. `tplinkrouterc6u` (`TplinkRouterSG` client) handles login and encryption: `client.request(path, urlencoded_str)` returns the decrypted `data`.
- Login: right after a failed login the router refuses new logins briefly; the client retries 3× (0/3/8 s).
- **Error responses come back encrypted**, so the library only shows `Response with error … {"data":"<base64>"}`. You can't read the reason. In practice it means a missing or invalid field, or a write the firmware rejects in the current mode.
- Ops: `read` / `write` for single forms; `load` / `insert` / `update` / `remove` for lists; `read_spf` / `write_spf` for wireless radio forms. List ops send `new=<json>` / `old=<json>` / `key=` / `index=`.
- `/v2/admin/...` endpoints (dhcps reservation, imb bind_list, nat fr6/pt, routes_static/system, upnp service) are the **same URL without `/v2`**, sent as JSON. The plaintext is `JSON {operation, new: [row], index: 0, …}` (lists stay lists), the outer body is `{"sign","data"}`, and the request carries `Content-Type: application/json`. Signing and encryption are the same as for urlencoded requests. The library only speaks urlencoded and re-parses bodies as query strings, so tpctl has `Router.call_json()`. **Verified W:** DHCP reservation insert (the urlencoded insert is rejected on BE805 fw 1.5) and update. List `update` (web UI store): `{operation:"update", key:<UI row id>, old:<row>, new:<row>}`; on JSON endpoints `old`/`new` are **objects** (urlencoded forms get them as JSON strings). `key` is the UI's local row id (a random UUID; the router ignores it and matches on the row). Put the same `key` inside `old` and `new` like the UI does. The router answers with the new row Reads work through `load` either way.
- List ops (urlencoded, non-v2): `insert` = `new=<row JSON>&index=0`, `remove` = `key=<row key>&index=<i>`, `update` = `key=&old=<JSON>&new=<JSON>` (UI store `insert/remove/update`). `Router.list_change()` wraps insert/remove with snapshot + verify.
- Two endpoints live outside `admin/`: `device_config?form=config` (feature flags the UI reads on every page: `supportWifi7`, `isTriBand`, `easyMesh`, `mloNetwork`, `dialHiddenFunction` per WAN type…) and `login?form=get_login_mode` (`is_remote`).
- A firmware that lacks an endpoint the UI still calls answers `success:false, errorcode:"no such callback"` (seen: `admin/status?form=network_map`).
- Booleans: the UI's `serialize()` sends primitives as-is, so JS `true`/`false` arrive as the strings `"true"`/`"false"`. Objects are `JSON.stringify`'d, and `undefined`/`null` fields are left out. Python `True` urlencodes as `"True"`, so convert it first. **Not every form accepts `on`/`off`**: `mesh_sclient_detail` reads `led` as a JSON bool, and sending `led=on` turned the LED *off*.

### Discovering endpoints

- **Static:** fetch the UI (`curl --compressed http://192.168.0.1/webpages/index.html` → `./js/index-*.js` → recursively fetch `[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8}\.js` chunks, ~380 files), then look for `"admin/<module>?form=<form>"` strings. Calls are `s.read(EP)` / `s.write(EP, data)` / `s.request(EP, {operation:"X", …})`, often via a `const c="admin/…"` variable. Default `grep` is ugrep here; use Python or `LC_ALL=C /usr/bin/grep` on the minified bundles. The UI's model files show exact write payloads (e.g. `model-*.js` for EasyMesh).
- **Live, with plaintext:** bodies are encrypted on the wire, so a network capture only shows URLs. Use Chrome DevTools Protocol logpoints in `update-store-*.js` instead (a breakpoint whose condition records data and returns `false`):
  1. in `encryptRequest`, at `return GdprService.needEncrypt(e)?…`, use condition `(e.__plain=o, false)`, where `o` is the serialized plaintext body;
  2. in `decryptResponse`, at `e.data=n}`, use condition `(window.__tp=(window.__tp||[])).push({url:r.url, body:r.__plain, res:n}), false`.

  Then visit pages and read `window.__tp`. Positions come from `Debugger.setBreakpointByUrl` with a `urlRegex`, using the line/column of those snippets in the served file. Logging in through the browser ends tpctl's session (one admin session; tpctl re-logs in by itself).

## Status / WAN

| Endpoint | Op | Notes |
|---|---|---|
| `admin/status?form=all` | R read | Huge flat dump of everything, **contains Wi‑Fi PSKs**. Includes `mlo_host_*`, `guest_*`, `iot_*` fields |
| `admin/status?form=internet` | R read | `internet_status`, `wan_internet_status` |
| `admin/status?form=wan_dual_nat_state` | R read | `wan_dual_nat: "1"` = WAN IP is private (CGNAT / double NAT) |
| `admin/status?form=wan_speed` | R read | Router's own speed-test record (`down_speed`/`up_speed`, unclear units, don't trust) |
| `admin/status?form=router` | R read | **Physical ports**: list of `{name: wan/lan1…lan4/lan10g, status: connected/unconnected, speed: Mbps, duplex, is_wan}`. Shows when a wired device or satellite negotiated only 100 Mbps (bad cable / old NIC) |
| `admin/network?form=port_speed_supported` | R read | `supported` speeds per port |
| `admin/network?form=wan_ipv6_status` | R read | v6 `conntype`, `enable` |
| `admin/network?form=wan_ipv6_{pppoe,dynamic,static,tunnel,pass}` | R read; ⚠ `connect`/`disconnect`/`renew`/`release` | Per-type IPv6 WAN configs |
| `admin/network?form=wan_ipv4_protos` | R read | Available WAN types `[{name,value}]` |
| `admin/network?form=status_ipv4` | R read | WAN/LAN IPv4, gateway, DNS, `wan_ipv4_uptime` (s) |
| `admin/network?form=wan_ipv4_status` | R read | `conntype` (pppoe/dhcp/static/l2tp/pptp) |
| `admin/network?form=wan_ipv4_pppoe` | R read | PPPoE config incl. `username`, `mtu`, `password` ⚠ |
| `admin/network?form=wan_ipv4_{dynamic,staticip,l2tp,pptp,dslite}` | R read | Inactive WAN-type configs |
| `admin/network?form=wan_ipv6_protos` | R read | staticv6 / dhcp6c / pppoev6 / 6to4 / passthrough |
| `client.get_ipv6_status()` | R | Library helper: WAN v6 addr, PD prefix, v6 DNS, conntype |
| `admin/network?form=lan_ipv6` | R read | `assign_type` (slaac / dhcpv6 / ndp_proxy / rdnss), prefixes |
| `admin/network?form=wan_dohdot` | R read | `dns_privacy`: none / doh / dot |
| `admin/network?form=wan_port_status` | R read | Which physical port is WAN (e.g. `wanlan2g5` = the 2.5G port) |
| `admin/network?form=port_speed_current` / `_supported` | R read | `auto`, 10000F…100F |
| `admin/network?form=mac_clone_advanced` | R read | WAN MAC clone (`mac_clone_type` default/computer/custom) |
| `admin/network?form=routes_system` | R load | Kernel routing table |
| `admin/network?form=routes_static` | R load, **W insert (JSON object row) / remove (urlencoded key=<row key>&index)** | Static routes `{key, enable, target (CIDR), gateway, interface LAN/WAN, name}` |
| `admin/network?form=lan_ipv4` | R read | LAN IP/mask |
| `admin/network?form=lan_jumbo_frame` | R read | `enable`, `limit` (bytes) |
| `admin/network?form=lan_agg` | R read | Link aggregation |
| `admin/network?form=lan_fc` / `wan_fc` | R read | Flow control tx/rx |
| `admin/network?form=wan_giga_lite` | R read | |
| `admin/iptv?form=setting` | R read | ISP VLAN profile: `internet_vid`, `mode` (ISP presets + Custom), `port_settings`, IGMP snooping. **⚠ On this kind of ISP the WAN itself rides on this form** (seen: `enable=on`, `mode=Custom`, `internet_vid=<the ISP's VLAN>`, `internet_tag=on`, i.e. the ISP needs a VLAN tag on the internet traffic): a wrong write here cuts the internet. The web UI's write payload was not found in the bundles; tpctl keeps the page read-only on purpose, including IGMP snooping (`igmp_snooping_enable`) and multicast-to-unicast on Wi-Fi (`mcwifi_enable`) |
| `admin/network?form=wan_retrieve_ipv4_pppoe` | ⚠ | Returns PPPoE credentials |
| `admin/network?form=wan_autodetect`, `*detect*` | ⚠ | Triggers WAN probing |

## Security / NAT

| Endpoint | Op | Notes |
|---|---|---|
| `admin/security_settings?form=new_enable` | R read, **W write** (whole form) | SPI firewall `enable`, `lan_ping`, `wan_ping` on/off (wan_ping verified) |
| `admin/nat?form=fr6` | R load, **W insert (JSON, `/v2`) / remove (urlencoded `key`, `index`)** | **IPv6 firewall allow rules** (empty = all inbound v6 blocked). Row `{key: <uuid>, enable: on, type: CUSTOM\|HTTP\|FTP\|… (service preset), name, ip: <global IPv6>, ip_type: custom\|client, port: "N" or "N-M", protocol: ALL\|TCP\|UDP}`. Insert `new` is an **object**, not a list. The router keeps the client-made `key`; remove with that key. Verified 2026-10-22 (add + remove, BE805 fw 1.5.1) |
| `admin/nat?form=vs` / `pt` / `dmz` | R read | Port forwarding / port triggering / DMZ |
| `admin/nat?form=alg` | **R read, W write** | `sip h323 ipsec pptp l2tp tftp rtsp ftp` on/off; write the whole form (read-merge-write) |
| `admin/nat?form=setting` | R read, UI-W write | NAT `enable`, `boost_enable` (HW acceleration; off when QoS is on), `reboot_time` |
| `admin/upnp?form=enable` | R read, **W write** `enable=on/off` | UPnP on/off. Mappings: `upnp?form=service` load (v2 in the UI) |
| `admin/nat?form=client_list` / `client_list_v6` | R read | LAN hosts with v4 / v6 addresses |
| `admin/imb?form=setting` / `arp_list` / `bind_list` | R read/load; bind_list **W insert (JSON list row) / remove (key=<ip>&index)** | IP-MAC binding. Rows `{name, ipaddr, mac, key=ip, enable}`; enforced while `setting.enable=on` |
| `admin/iot_security?form=enable` | R read | Device isolation (IoT network) |
| `admin/access_control?form=enable` | R read, **W write** `enable=on/off` | Access control master switch. `host_mac` = the MAC of the machine talking to the router (never block it) |
| `admin/access_control?form=mode` | R read, UI-W write | `access_mode` black (blocklist) / white (allowlist) |
| `admin/access_control?form=black_list` | R load, **W insert/remove** | Blocked devices. Insert row `{name, deviceType, mac, ipaddr, host: HOST/NON_HOST, conn_type, key: <random id>}` (UI `C()`); remove `key=<row key>&index`. Only enforced while `enable=on` in black mode; an empty list in black mode restricts nothing |
| `admin/access_control?form=black_devices` / `white_devices` | R load; UI ⚠ `block` / `access` | Candidates (online + known devices: `mac, name, type, ipaddr, conn_type, host`). `host` values seen live: `HOST` / `NON_HOST` (the UI constant says `NOT HOST`) |
| `admin/nat?form=dmz` | R read, **W write** | `enable`, `ipaddr`. UI: on = `{enable:on, ipaddr}`, off = `{enable:off}` only (ipaddr is kept) |
| `admin/nat?form=vs` | R load, **W insert/remove** | Port forwarding rows `{name, ipaddr, external_port (N or A-B), internal_port, protocol ALL/TCP/UDP, enable on/off}`. Remove uses `key=<name>` + `index` |
| `admin/nat?form=pt` | R load, **W insert (JSON, `new` = the row object, not a list) / remove (urlencoded `key=<name>&index`)** | Port triggering rows `{name, trigger_port, trigger_protocol ALL/TCP/UDP, external_port, external_protocol, enable}`. A list-wrapped `new` answers `URL parsing failed` |
| `admin/avira_parental_control?form=avira_pactrl` | R `getOwnerTotalData`, **W** `addOwnerInList` / `internetBlock` (verified), `delOwnerInList` (UI) | Parental-control profiles: `ownerList` (`{}` when empty), limits (`totalOwnerMax`, `filterWebsiteMax`…). Rows: `ownerId` (router-assigned "0", "1"…), `name`, `age`, `internetBlocked` (bool), `clientList`, `allDeviceMac`, `bedtime {enable, mode: everyday, everyday {bedtimeBegin, bedtimeEnd} (minutes after midnight), workingDay[], customize[7]}`, filter lists. **addOwnerInList** (urlencoded): `ownerId=-1, name, age, internetBlocked=false, allDeviceMac=<JSON list of MACs>, filterCategoriesList=[], filterWebsiteList=[], bedtime=<JSON {enable, everyday{bedtimeBegin:"1260", bedtimeEnd:"420"}}>, filterFreeWebsiteList=[]` (verified 2026-10-22). **internetBlock** `ownerId, internetBlocked=true\|false` = pause now (true verified). **delOwnerInList** `ownerList=<JSON [ownerId]>` (verified 2026-10-22: the profile is gone on the next getOwnerTotalData). Others: getDevicesList, getFilterWebsiteInfo/setFilterWebsiteInfo, getFreeWebsite/setFreeWebsite, getBlockHistory, getTodayVisitSites |
| `admin/avira_network_check?form=avira_network_scan` | R `telemetry` | HomeShield network scan state |
| `admin/administration?form=remote` | R read, W write (web UI; not round-tripped) | Remote management: `enable`, `https_port`, `http_port`, `managers` (all / an IP), `enable_superadmin`, `web_address`. tpctl writes the whole form with `enable` changed |
| `admin/administration?form=https` / `mode` / `local` | R read | Local management |
| `admin/administration?form=account`, `encrypt_key`, `recovery` (contains `password`), `security` (security questions), `superadminconfig`, `security_question_pwd` | ⚠ | Credentials. All of them answer a plain `read` |

## DHCP

| Endpoint | Op | Notes |
|---|---|---|
| `admin/dhcps?form=setting` | **R read, W write** | `enable` (on/off; `support_enable_list`), `leasetime` (min), `ipaddr_start`/`_end`, `pri_dns`/`snd_dns` (empty = router; verified W), `gateway`, `domain`. The UI writes the whole form `{enable, ipaddr_start, ipaddr_end, leasetime, gateway, pri_dns, snd_dns}`; its server on/off toggle writes `{enable}` alone. In AP/repeater-like modes (`isSupportDhcpAutoMode`) `enable` can be `auto`, written alone. Verified W 2026-10-22: pool start moved and restored (whole form). Writing the pool logs `DHCPS Service start` in the system log (the DHCP server restarts; leases are kept) |
| `admin/dhcps?form=client` | R load | Leases: `name`, `macaddr`, `ipaddr`, `leasetime` |
| `admin/dhcps?form=reservation` | R **load** (read is rejected), **W insert (JSON, see Transport) / update (JSON: `key`, `old`, `new` objects; verified enable on/off 2026-10-22) / remove (urlencoded `key=<MAC>&index`)** | Rows `{mac, ip, comment, hostname, enable}`; the UI edit sends `{key, mac, ip, hostname, enable}` (`tpctl dhcp edit` sends the loaded row with the changed fields). The router replaces `comment` with the device's host name. The library's `add_ipv4_reservation` (urlencoded insert) is rejected on BE805 fw 1.5. Observed 2026-10-22 (BE805 fw 1.5.1): a reservation for an IP that another client still holds a lease on (even when that client is offline; see `dhcps client` / `imb arp_list`) is not applied. The reserved client keeps getting its old IP, now as a `Permanent` lease (client sees lease_time 0xffffffff), even after a full release + fresh DISCOVER. Check `devices leases` for the target IP before reserving it. Moving the pool (`dhcps?form=setting ipaddr_start`) so that existing reservations fall outside it makes the router set those reservations to `enable=off` by itself (seen when the pool start went .10 → .100), so reservations must stay inside the pool. To free an IP held by another client's lease (no lease-delete API exists), reserve a different IP for that client's MAC. Verified: the blocking client's lease left the IP right away and the target client got it on its next DISCOVER. Changing the pool also restarts every lease's countdown. Removing one reservation (`remove key=<MAC>&index`) can leave another reservation at `enable=off`: seen when one row was removed, another row got disabled. Re-read `dhcps reservation` after every remove; remove + re-insert re-enables a row |

## Wireless

Radio forms use `read_spf`. To write, combine forms in one request with prefixed keys:
`admin/wireless?form=wireless_2g&form=wireless_5g&form=wireless_6g`, op `write_spf`, fields `wireless_<band>_<field>=…`.

| Endpoint | Op | Notes |
|---|---|---|
| `admin/wireless?form=wireless_{2g,5g,6g}` | **R read_spf, W write_spf** | `ssid`, `psk_key` ⚠, `encryption`, `psk_version` (sae_transition / sae_only), `channel` (auto/N), `current_channel`, `htmode` (20/40/80/160/320/auto), `hwmode`, `txpower` (low/middle/high), `hidden`, `enable`, `psc` (6g), `afc_enable` (6g), `auto_channel_unii_4` (5g), `mu_mimo` (legacy, see below) |
| `admin/wireless?form=wireless_5g_2` | R read_spf | Second 5G radio on tri-band-5G models (all fields empty when absent) |
| `admin/wireless?form=smart_connect` | **R read, W write** | `smart_enable` on/off (band steering). **While it's on, single-band SSID writes are rejected. Write all bands together** |
| `admin/wireless?form=ofdma_mimo` | R read, UI-W write | `setting`: `all` (OFDMA+MU-MIMO) / `ofdma_only` / `disable`. **This is the real switch on this model** |
| `admin/wireless?form=ofdma`, `twt`, per-band `mu_mimo` | R / W accepted | Legacy forms. The UI hides them when `ofdmaMumimo`/Wi‑Fi 7 is supported, so their values say nothing about actual state |
| `admin/wireless?form=wireless_addition_setting` | R read | `beacon_int`, `dtim_period`, `rts`, `frag`, `wmm`, `zerowait_dfs`, `isolate`, `airtime_fairness` |
| `admin/wireless?form=region` | R read | `country`, per-band channel capability lists |
| `admin/wireless?form=channelExtraInfo_5g` | R read | Allowed 5G channels per width |
| `admin/wireless?form=guest` | R read | Guest `access` (LAN access), `isolate` |
| `admin/wireless?form=guest_2g&form=guest_5g&form=guest_2g5g[&form=guest_5g_2][&form=guest_6g]` | R read, UI-W write (tpctl implements it; a write restarts radios) | Guest network, one combined form. Keys `guest_<band>_{enable,ssid,hidden,encryption,psk_version,psk_key,disabled_by}`. UI on Wi-Fi 6E models: per band 2g/5g/5g_2/6g; otherwise `2g5g`. Security: `encryption` none/psk/psk_sae/owe/portal + `psk_version` rsn/sae_transition/sae_only/owe_only. 6G: password → psk_sae + sae_only, no password → owe + owe_only |
| `admin/wireless?form=guestnetwork_bandwidth_ctrl` | R read | Guest per-band limits |
| `admin/wireless?form=iot_{2g,5g}` | R read | IoT SSID |
| `admin/wireless_schedule_v2?form=settings` | R read, **W write `enable`** | Wi‑Fi off schedule. Reads JSON booleans (`enable: false`) but takes `enable=true/false` (verified); rules via insert/update/remove | Rules: `read` returns them in `list` (`{}` when empty), max `max_rules` (20). **insert** (urlencoded, `new=<JSON>`, `index=0`): `{key: "", enable: true, repeat_days: "mon,tue,…" (sun..sat), time_start: "HH:MM", time_end: "HH:MM" (may be before start = next day), band_list: "2g,5g,6g", next_time: "off"}`. The router assigns `key` ("1", "2"…) and answers `{new: row}`. **remove** `key=<key>&index=<i>`; **update** key/old/new (UI). Verified insert + remove 2026-10-22 with the switch off
| `admin/wireless?form=eco` · `admin/eco_mode_v2?form=settings` | R read | Power saving (`power_mode` balanced) |
| `admin/wireless?form=wireless_extended_network` / `wireless_connect_to_network` | R read | Repeater / WDS mode only |
| `admin/wireless?form=mlo_host` | R read | Wi‑Fi 7 MLO network: `enable`, `ssid`, `psk_key` ⚠, `band_select`/`selected_bands`/`support_bands`, `encryption`, `hidden` |
| `admin/wireless?form=syspara_{2g,5g,6g}` | R read | Legacy per-radio dump: `bssid`, `channel`, `beacon_int`, `dtim_period`, `rts`, `frag`, `isolate`, `airtime_fairness`, `wds_mode`, `psk_key` ⚠ |
| `admin/wireless?form=iot_2g&form=iot_5g[&form=iot_5g_2]` | R read_spf, UI-W write_spf (tpctl `wifi iot`) | IoT network: `iot_<band>_{enable,ssid,psk_key ⚠,encryption,hidden,disabled_by}`. A single `form=iot_2g` read returns unprefixed keys; adding `form=iot_6g` is rejected (no IoT on 6G) |
| `admin/wireless?form=twt` | R read | `enable` (Target Wake Time; legacy, see ofdma note) |
| `admin/wireless?form=multi_network` | R load | Extra SSIDs list (empty here) |
| `admin/wireless?form=syspara_wps` / `wps_connect` / `wps_pin` | R read | WPS `wps` on/off, `available`, `wps_pin` ⚠, per-band locks. ⚠ ops start a WPS session |
| `admin/wireless_schedule_v2?form=settings` · `admin/eco_mode_v2?form=settings` | R `inforce` | `inforce` = a schedule/eco window is active right now (the UI warns before radio edits) |
| `admin/wireless?form=survey` | ⚠ | Channel scan, disrupts radios |

Any wireless write restarts the radios: wireless clients drop for 30–60 s, auto channels get re-picked, and **EasyMesh satellites re-onboard (up to ~5 min, and topology can change)**.

## Clients

| Endpoint | Op | Notes |
|---|---|---|
| `admin/smart_network?form=game_accelerator` | **R loadSpeed** | Best client list: `deviceName`, `mac`, `ip`, `deviceTag` (2.4G/5G/6G/wired), `isAgent` (on a satellite), `signal` (dBm, main router only), `txrate`/`rxrate` (kbps), `ssid` (base64), `onlineTime`. Op `loadDevice` returns the list the network map uses: adds `enableLimit`/`downloadLimit`/`enablePriority`/`timePeriod`/`remainTime`/`trafficUs…`, no rates |
| `admin/smart_network?form=device_priority` | R **load**, **W update** | Same clients plus priority/limit flags, one row per MAC with a `key`. **Device name = `deviceName`, stored on the router.** To rename, send `update` with `key=<row.key>`, `old=<row JSON>`, `new=<row JSON with deviceName changed>` (whole row, as the UI does). Verified: the name shows up right away in `game_accelerator`, `dhcps client`, `nat client_list`, `imb arp_list` and `client_list_v6`. The UI's client-name edit is this endpoint. Spaces in `deviceName` are accepted and kept as-is (BE805 fw 1.5.1, verified 2026-10-22); the DHCP reservation `comment` follows the new name |
| `admin/smart_network?form=client_speed_limit` | R `read_max`, **W write** | `max_rules` (64), `downloadLimitMax`/`uploadLimitMax` (kbps). Write = one device: `{mac, enableLimit: on\|off, downloadLimit, uploadLimit}` in **Kbps** (1 Mbps = 1024; `-1` = no limit in that direction; UI presets 1 Mbps / 10 Mbps down, 200 Kbps / 2 Mbps up). Current values appear on `game_accelerator loadSpeed` rows (`enableLimit`, `downloadLimit`, `uploadLimit`, `speedLimitOnline`); unlimited-but-never-set rows show 1024/200 with enableLimit=off. Verified 2026-10-22 (100 Mbps cap on → off) |
| `admin/smart_network?form=get_host_info` | R read | The browser host's own `host_mac`, `wire_type` |
| `admin/smart_network?form=qos` | R read, **W write** | QoS (off; enabling it disables NAT boost). Fields: `enable`, `up_band`/`down_band` + `up_unit`/`down_unit` (mbps\|kbps), `max_up_band`/`max_down_band`, `max_wan_speed`, priority shares `high`/`middle`/`low`, `time`. The UI writes `{enable, up_band, max_up_band, up_unit, down_band, max_down_band, down_unit}`. Verified W 2026-10-22: up_band 10000→9000→10000 with QoS off (enable=on not run live) |
| `admin/smart_network?form=client_options` | R read | Filter options (bands, SSIDs) |

## EasyMesh

| Endpoint | Op | Notes |
|---|---|---|
| `admin/easymesh_network?form=get_mesh_device_list_all` | R read | Nodes: `name`, `model`, `ip`, `mac`, `role` (main_router / satellite_router), `parent_mac`, `connect_type` (wireless / wire), `signal_strength` 0–5, `client_num`, `status`, `support_reboot` |
| `admin/easymesh_network?form=mesh_sclient_list_all` | R read | Satellites only, same fields |
| `admin/easymesh_network?form=available_mesh_device_manage` | op `reboot` + `mac=` | Reboots a satellite. **Returns an error even when the reboot actually happens.** Also `unlink` / `link` / `add_onboarding` ⚠, and `bind_to_current_ap` (`agent_mac`, `client_mac`, `bind`) pins a *client* to a node |
| `admin/easymesh_network?form=mesh_sclient_detail` | **R read + `mac=`** (main router too); **W write** (node settings, see below) | Per node: `name`, `location` (enum, `""` = not set), `custom_location` (mirrors the name, the UI ignores it), `led` (bool), `support_led_control`, `support_client_bind`, `internet_status`. Satellites also have `parent_device_mac`, `level` (hops), backhaul `signal_strength_5g`/`_24g` (dBm, -128 = n/a), `signal_level_5g` (0-5), `link_speed_5g`/`_24g` (kbps), `preferred_wifi_uplink_type` (auto/manual), `preferred_wifi_uplink_ap` (`{device_name, mac}` when manual), `preferred_wifi_uplink_ap_available` (the uplink candidates), and `mesh_nclient_list` (its clients: mac, ip, name, signal_strength dBm, connection_type, txrate/rxrate Mbps). The main router also has `traffic_separation`, `backhaul_steering` (bools) |
| `admin/easymesh?form=easymesh_enable` | R read | Mesh on/off |
| `admin/easymesh?form=search_slave` | ⚠ start/stop/check | Pairing search |
| `admin/easymesh_network?form=change_satellite` | ⚠ write `change_satellite=true` | |
| `admin/easymesh_network?form=ts_ssid_check` | R `status` (`ts_enable`); ⚠ `check` | Traffic-separation SSID check |
| `admin/easymesh_network?form=get_mesh_topo` | R read | Returns `{}` on BE805 fw 1.5.1; the topology comes from `get_mesh_device_list_all` + per-node `parent_device_mac` |
| `admin/easymesh_network?form=ap_avoidance_enable_status` | R read | `{"status": "Timeout"}` here |
| `admin/firmware?form=slave_cmd` | R `slave_get_info` | Per satellite: `name`, `model`, `mac`, `ip`, `version` (empty here), `latest_version`, `is_latest`, `releaseNote` |

**Node settings write** (the UI's *Edit* dialog under Advanced → EasyMesh) is `write` to `mesh_sclient_detail` with exactly these fields (UI `model-*.js`, function `P()`):

- **satellite:** `mac`, `name`, `location`, `led` (`"true"`/`"false"`), `preferred_wifi_uplink_type` (`auto`/`manual`), and `preferred_wifi_uplink_ap` = `JSON {"device_name","mac"}` (only when manual).
- **main router:** `mac`, `name`, `location`, `traffic_separation`, `backhaul_steering`.
- `location` ∈ `bedroom hallway kitchen living_room master_bedroom office study other`, or `""` for not set. Free text is invalid.
- Always send the current values of the fields you don't change. Note: the UI says TP-Link satellites follow the main router's LED control (`ledgeneral`/`ledpm`), so a per-node `led` may be overridden.

**Uplink pinning** (偏好的連線 AP → 手動): the picker lists only `preferred_wifi_uplink_ap_available`. On BE805 fw 1.5.1 + EB431v that list is **empty for every satellite**, and the web UI's picker shows "沒有項目" (no items) too, so a satellite can't be pinned to a parent on this setup. Re-check that list on other firmware before writing `manual`. Rebooting a node makes it re-pick its parent. Wired backhaul shows as `connect_type: "wire"`. The UI flags a weak node in the network map's connection column with "訊號太差，請將此設備移動至靠近您主路由器的地方擺放" (signal too weak, move it closer to the main router).

## System / misc

| Endpoint | Op | Notes |
|---|---|---|
| `client.get_firmware()` | R | Model, hardware, firmware version |
| `admin/ledgeneral?form=setting` | R read, **W write** `enable=on/off` (only that field) | Main LED switch (satellites follow it) |
| `admin/ledpm?form=setting` | R read, **W write** | LED night mode: `enable`, `time_start`, `time_end` (HH:MM), `ledpm_support` (send back as read) |
| `admin/system?form=sysmode_v2` | R read | router / AP mode |
| `admin/time?form=settings` | R read, **W write**, `refresh`, `gmt` | NTP servers, `timezone` index (ids → names: the UI's list, `tpctl system time --zones`, e.g. 107 China, 109 Singapore, 47 UTC). The UI writes `{type: auto, timezone, ntp_svr1, ntp_svr2}` for automatic time. Manual time is `{type: manual\|pc, timezone, date: MM/DD/YYYY, time: HH:mm:ss}`. Don't write the whole read-back form: it carries the live `date`/`time`. Verified W 2026-10-22: ntp_svr2 changed and reverted. `operation=refresh` re-syncs and returns the form plus `status`. 24-hour clock is a separate form: `admin/time?form=hour24` write `{hour24_enable: on\|off}` (verified) |
| `admin/ledgeneral?form=setting` / `admin/ledpm?form=setting` | R read | LED on/off, night mode |
| `admin/ddns?form=provider` / `tplink` / `noip` / `dyndns` | R read | DDNS (useless when the WAN is behind CGNAT) |
| `admin/openvpn?form=config` / `admin/pptpd?form=config` / `admin/wireguard?form=config` / `admin/l2tpoveripsec?form=config` | R read | VPN servers. wireguard config contains keys ⚠ |
| `admin/vpn?form=enable` | R read | VPN client |
| `admin/folder_sharing?form=server/auth/media/mode` (read), `settings/partial` (load, `partial` has share passwords ⚠), `admin/time_machine?form=settings`, `admin/disk_setting?form=metadata` (op `scan`), `timeline`, `security_scan` (`read_scan_frequency`) | R | USB storage |
| `admin/pptpd?form=config`, `admin/l2tpoveripsec?form=config` | R read, W write (enable rejected) | Turning the server on (whole form, `enabled=on`) is rejected on BE805 fw 1.5.1 with no VPN accounts and a CGNAT WAN (2026-10-22); `enabled=off` writes are accepted. Cause not confirmed; tpctl keeps VPN servers read-only |
| `admin/folder_sharing?form=media` | R read, **W write** | `media_sharing` on/off (DLNA). A partial write works (verified on → off 2026-10-22, no USB drive attached) |
| `admin/vpn?form=server` (`load_basic`), `vpn_user_list` (`get_mode`), `thirdvpn` (`cloud_check`) · `admin/vpnconn?form=config` (`list`) · `admin/{pptpd,l2tpoveripsec}?form=accounts`, `admin/wireguard?form=account` (load) | R | VPN client/server state, accounts ⚠ |
| `admin/syslog?form=log` | **R load** | **System log**, the router returns it newest first (tpctl prints oldest first), ~1200 entries: `{time, type, level, content}`. `type` ∈ `syslog?form=types` (PPP, NETWORK, NAT, DHCPC, ACCESS_CONTROL, LED_CONTROLLER, HOMESHIELD…). WAN drops show as `NETWORK Disconnect interface wan` + `Reload config`, PPPoE dial-up as `PPP` lines. No EasyMesh/backhaul events are logged |
| `admin/syslog?form=types` / `filter` / `mail` | R load / read / read | Log types, current filter (`type`, `level`), mail-log settings |
| `admin/firmware?form=upgrade` | R read | `model`, `hardware_version`, `firmware_version`, `upgradetime` |
| `admin/cloud_account?form=cloud_upgrade` | R read, W `upgrade` | Online check: `latest_version`, `latest_flag` (true = up to date), `detail`. `operation=upgrade` = the UI's "Update" button: download, flash, reboot (not run; tpctl refuses when latest_flag is true). `latest_version` lacks the `(NNNN)` suffix of `firmware?form=upgrade firmware_version`: compare with latest_flag, not strings |
| `admin/firmware?form=auto_upgrade` | R read, **W write** | Auto-update `enable` (on/off), `time` (hour 0-23), `delay`. Verified W 2026-10-22 (on → off, whole form) |
| `admin/firmware?form=config_multipart` | **multipart POST** `operation=backup` | **Config backup download** = the UI's Backup button. Plain `multipart/form-data` with the session's stok + sysauth cookie; not session-encrypted. Answer: `application/octet-stream`, `Content-Disposition: attachment; filename="backup-<model>-<date>.bin"`, ~29 KB, encrypted by the router. Verified 2026-10-22. `operation=restore` + `file` restores (not used by tpctl). `firmware?form=config` `operation=factory` (`all: false\|true`) = factory reset (never used) |
| `admin/reboot?form=set` | R read, **W write** | **Scheduled reboot**: `enable` on/off, `cycle` (week…), `day` (Mon…), `time` HH:MM. Write the whole form |
| `admin/diag?form=diag` | **R `start` / `continue` / `stop`** | Router-side ping / traceroute. Start with `type=0, ipaddr, ipaddr_ping, count, pktsize` (ping) or `type=1, ipaddr, ipaddr_trace, ttl` (traceroute), then poll `continue` (`type, ipaddr`) every 1 s until `finish="1"`. `result` holds the text (`Packets: Sent=… (x% loss)`, `Round-trip min/avg/max = …`) |
| `admin/time?form=dst` | R read, **W write** | Daylight-saving rules + router clock (`date`, `time`). A partial write `{dst_enable: on\|off}` works (verified on → off 2026-10-22). The UI writes the whole form minus `dst_status` |
| `admin/firmware?form=config` / `config_replace` | R read / `get_restore_result` | Backup/restore page state |
| `admin/status?form=user_experience_plan_switch` | R read | Usage-statistics sharing `enable` |
| `admin/privacy_policy?form=fing_auth_state` | R read | Fing device-recognition consent |
| `admin/cloud_account?form=get_deviceInfo` / `remind` / `check_internet` / `get_token` ⚠ | R read | TP-Link cloud |
| `admin/system?form=reboot` | ⚠ op `write` or `reboot` | Reboots the main router. The UI's Reboot button sends `operation=reboot`, other flows send `write`; both answer `reboot_time` |
| `admin/firmware?*`, `admin/cloud_account?*`, `admin/quick_setup?*`, `admin/disk_setting?form=format/remove/scan`, `admin/syslog?form=save_log`, `admin/qos?form=update_database`, `admin/diag?form=diag` | ⚠ | Actions, upgrades, factory flows. Never call blindly |

Full list of the 243 endpoints referenced by the UI, with operations: `tpctl api endpoints --grep X` (data file `src/tplink_router/data/endpoints.txt`). Library equivalents: `r.call(ep, op, **fields)` / `r.try_call` / `r.write_merged` / `r.write_wifi`.
