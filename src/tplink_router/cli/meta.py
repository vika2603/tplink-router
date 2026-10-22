"""Self-description, generated from the live click tree so it can't drift from the parser."""
import json

import click

from .. import human as h
from .. import output as o
from .. import quality
from ._errors import EXIT
from ._ctx import json_option, pass_state

# risk: read (no change) | write (changes config, no outage) | disruptive (drops connectivity / restarts things)
RISK = {
    "wifi set": ("disruptive", __import__("tplink_router.services.wifi", fromlist=["x"]).SIDE_EFFECT),
    "mesh reboot": ("disruptive", "that satellite and nodes behind it go offline ~1-3min"),
    "system reboot": ("disruptive", "whole network down ~2-3min"),
    "nat alg": ("write", "changes ALG on/off (read-only when no k=v given)"),
    "dhcp set-lease": ("write", "new lease applies at devices' next renewal"),
    "api set": ("write", "raw form write; may be disruptive depending on endpoint"),
    "devices block": ("disruptive", "that device loses network access until unblocked"),
    "devices unblock": ("write", "restores a blocked device"),
    "wifi guest": ("disruptive", "on/off/rename/password restarts the radios (read-only with no args)"),
    "dhcp set-dns": ("write", "DNS servers handed to devices (next renewal)"),
    "dhcp reserve": ("write", "device gets the reserved IP at its next renewal"),
    "dhcp unreserve": ("write", "removes a reservation"),
    "system upgrade": ("disruptive", "flashes the latest firmware and reboots: network down ~5 min"),
    "system auto-update": ("write", "on: the router installs new firmware at that hour and reboots"),
    "system time": ("write", "NTP servers / time zone / 12-24 h (read-only with no options)"),
    "system backup": ("read", "downloads the router's config backup to a local 0600 file"),
    "nat ipv6-allow": ("disruptive", "add: a port on one device becomes reachable from the IPv6 internet; rm is a plain write"),
    "qos set": ("write", "bandwidth/off is a plain write; `on` is disruptive (NAT acceleration off, NAT re-applied ~2 min)"),
    "qos limit": ("write", "caps one device's speed (or removes the cap)"),
    "wifi schedule": ("write", "Wi-Fi off-schedule rules / switch (read-only with no args)"),
    "parental add": ("disruptive", "bedtime cuts the profile's devices off daily (not live-tested; --yes)"),
    "parental pause": ("disruptive", "on: the profile's devices lose internet now (not live-tested; --yes)"),
    "parental rm": ("disruptive", "deletes a profile (not live-tested; --yes)"),
    "dhcp edit": ("write", "changes a reservation's IP / on-off (next renewal)"),
    "dhcp server": ("disruptive", "off: devices lose their address as leases expire (on is a plain write)"),
    "dhcp set-range": ("disruptive", "devices outside the new pool get new addresses at renewal"),
    "dhcp set-gateway": ("disruptive", "a wrong gateway cuts devices off the internet at renewal"),
    "settings set": ("write", "one whitelisted switch; firewall.spi / eco_mode / nat.boost are disruptive (plan + --yes)"),
    "wifi iot": ("disruptive", "IoT network on/off restarts the radios (read-only with no args)"),
    "nat trigger": ("write", "adds/removes a port triggering rule"),
    "lan route": ("write", "adds/removes a static route"),
    "lan bind": ("write", "ties an IP to a MAC (enforced while binding is on)"),
    "lan unbind": ("write", "removes an IP-MAC binding"),
    "nat upnp": ("write", "UPnP on/off (read-only with no args)"),
    "nat dmz": ("disruptive", "on exposes one device to all inbound IPv4"),
    "nat forward": ("write", "opens/closes an inbound port (unreachable behind CGNAT)"),
    "system led": ("write", "LEDs on/off"),
    "system night-mode": ("write", "LED night schedule"),
    "system reboot-schedule": ("write", "scheduled whole-network reboot"),
    "devices rename": ("write", "device name on the router (all router lists/app show it)"),
    "mesh set": ("write", "node name/location/LED; --uplink is disruptive (node re-attaches ~1-3min)"),
    "devices alias set": ("write", "local alias file only; router untouched"),
    "devices alias rm": ("write", "local alias file only; router untouched"),
    "snapshot save": ("write", "local file only"),
    "py": ("write", "arbitrary code with a logged-in session"),
    "net speed": ("read", "downloads ~100 MB from the internet on this machine"),
    "auth set-password": ("write", "stores the password (Keychain or 0600 file)"),
    "auth set-host": ("write", "saves the router address in ~/.config/tplink-router/config.json (local file only)"),
    "auth logout": ("write", "ends the router session"),
    "auth login": ("write", "replaces the router session"),
}
GLOBAL_PARAMS = {"help", "fmt", "detail", "fields", "no_track_global"}   # accepted by every command; listed once, not per command
GLOBAL_OPTIONS = "-o/--format human|records|json · --json · -v/--detail · --fields K1,K2 · --no-track · -h/--help"
NEEDS_ROUTER_FREE = {"commands", "schema", "snapshot list", "snapshot diff", "api endpoints", "devices alias list", "auth status", "auth set-host", "events", "devices roaming"}
RISK.setdefault("observe", ("write", "appends to the local event log only"))


def _risk(path: str) -> tuple[str, str | None]:
    for prefix, v in RISK.items():
        if path == prefix or path.startswith(prefix + " "):
            return v
    return "read", None


def _walk(cmd: click.Command, ctx: click.Context, prefix: str = ""):
    if isinstance(cmd, click.Group):
        from . import ALIASES
        for name in sorted(cmd.list_commands(ctx)):
            if not prefix and name in ALIASES:            # aliases aren't separate commands in the catalog
                continue
            sub = cmd.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            yield from _walk(sub, click.Context(sub, parent=ctx, info_name=name), f"{prefix} {name}".strip())
        return
    params = []
    for p in cmd.params:
        if p.name in GLOBAL_PARAMS or (isinstance(p, click.Option) and "--json" in p.opts):
            continue
        params.append({
            "name": (p.metavar or p.name) if isinstance(p, click.Argument) else p.name,
            "kind": "argument" if isinstance(p, click.Argument) else "option",
            "flags": getattr(p, "opts", None) if isinstance(p, click.Option) else None,
            "required": p.required, "multiple": getattr(p, "multiple", False),
            "is_flag": getattr(p, "is_flag", False),
            "type": "choice" if isinstance(p.type, click.Choice) else p.type.name,
            "choices": list(p.type.choices) if isinstance(p.type, click.Choice) else None,
            "default": None if callable(p.default) or p.default in ((), False) else p.default,
            "help": getattr(p, "help", None)})
    risk, effect = _risk(prefix)
    yield {"command": f"tpctl {prefix}", "summary": cmd.get_short_help_str(200), "risk": risk, "side_effect": effect,
           "dry_run": any(p["name"] == "dry_run" for p in params), "needs_confirm_flag": any(p["name"] == "yes" for p in params),
           "confirm_exit_code": 4 if any(p["name"] == "yes" for p in params) else None,
           "router_login": prefix not in NEEDS_ROUTER_FREE, "params": params}


@click.command("commands")
@click.option("--risk", type=click.Choice(["read", "write", "disruptive"]), help="Filter by risk tier.")
@json_option
@pass_state
@click.pass_context
def commands_cmd(ctx, st, risk):
    """Machine-readable catalog of every command: params, choices, risk tier, dry-run support. No login."""
    root = ctx.find_root()
    rows = [r for r in _walk(root.command, root) if not risk or r["risk"] == risk]

    def text():
        yield o.line("global_options", accepted_by="every command", options=GLOBAL_OPTIONS)
        for r in rows:
            args = " ".join((f"<{p['name'].lower()}>" if p["kind"] == "argument" else
                             f"[{p['flags'][-1]}{'' if p['is_flag'] else '=' + (('|'.join(map(str, p['choices']))) if p['choices'] else p['type'])}]")
                            for p in r["params"])
            yield o.line("command", name=r["command"], args=args, risk=r["risk"], dry_run="yes" if r["dry_run"] else None,
                         needs_yes="yes" if r["needs_confirm_flag"] else None, login="no" if not r["router_login"] else None,
                         summary=r["summary"], side_effect=r["side_effect"])
    st.out(rows, text(), lambda: h.command_catalog(rows))


RECORDS = {
    "verdict": "first line of overview/check: status ok|warn|error|partial, errors, warnings, infos, partial_reads, headline, at",
    "changes/change": "only when something changed since the previous overview/check (concise mode): what=node_parent|"
                      "node_status|node_backhaul|node_grade|device_joined|device_left|device_moved, subject, old, new",
    "wan": "overview: internet, ipv4, cgnat, uptime, ipv6 (LAN prefix), firewall",
    "wifi": "overview: ssid smart_connect, then per enabled band <band>_channel <band>_channel_mode (auto = the router picks "
            "it, the number is the current pick; fixed = pinned) <band>_width <band>_txpower",
    "node": None,      # generated from output.NODE_KEYS in _records()
    "device": None,    # generated from output.DEVICE_KEYS in _records()
    "band": "wifi show: band enable ssid channel (current) channel_setting width txpower security=WPA3|WPA2/WPA3|WPA2|open "
            "[hidden]; --detail adds security_raw (router codes), mode, psc, afc, unii4",
    "issue": None,     # generated from health.ISSUE_DATA_KEYS in _records()
    "issues": "issues count=0 when there is nothing to report (otherwise one `issue` line each)",
    "write": "status applied|skipped|dry_run|needs_confirmation; then changed/unchanged (or would_change/already) key old/current new/target; "
             "side_effect; next",
    "inbound": "computed: ipv4=blocked (CGNAT|NAT) or exposed via port_forwards/dmz/upnp; ipv6=blocked (firewall, 0 allow "
               "rules) | only N allowed rules | OPEN (firewall off); remote_admin (router admin from the WAN) vpn_server "
               "(enabled VPN servers) [dmz_staged = DMZ address set while DMZ is off] note. Answers 'reachable from the internet?'",
    "history/event": "`tpctl events`: history observations first last total; event at what subject [node band old new]. "
                     "what = device_joined|device_left|device_moved|node_parent|node_status|node_backhaul|node_grade|"
                     "node_grade_flap (A->B->A within minutes: noise; grade dipped_to near_boundary) | config_changed (key settings, incl. "
                     "web-UI changes; scope in the history note). Recorded only when tpctl observes",
    "diff": "`snapshot diff`: baseline baseline_taken against changes, then changed key old new (key = module.form.field)",
    "confirm": "disruptive command run without --yes: required=yes risk effect run=<exact command with --yes>; nothing "
               "was written; exit 4. Show the user the plan + effect, re-run `run` only after they agree",
    "snapshot": "`snapshot list`: name taken tag why. tag = the tpctl write it preceded (wifi-set, alg, dhcp, …; that write "
                "may have been a no-op), auto-daily (overview baseline), manual (`snapshot save`), diff (older tpctl versions)",
    "mismatch": "write acknowledged but the re-read value differs: key target actual old; exit 7",
    "truncated": "list cut by --limit: shown total hint",
    "warning": "type=partial_read: an endpoint failed; fields from it are missing. Exit code 6 when otherwise ok",
    "error": "on stderr: code (stable), exit, detail, usage/valid/available/did_you_mean/candidates, hint, retryable",
    "json": "--json: the same data as records, numbers without units (dBm, Mbps, ms are ints/floats), missing values "
            "as null (records omit them), timestamps as in records",
    "values": "booleans yes|no; router settings on|off (router-native); scalar lists a,b,c; flat maps k:v,k:v; never nested "
              "JSON in records (use --json). `nat show`, `system info`, `api get` include some router-native fields verbatim",
    # -- overview / status / mesh --
    "internet": "overview/status: v4 v6 latency to public anchors from this machine; loss; grade (per target, same order, same thresholds as all ping grades)",
    "devices_summary/devices_by_node/shown_by_node/devices": "devices list / overview (shown_by_node = per-node counts of the rows a filtered list shows): total (every device, wired included; same number as overview `devices total`); a filtered list adds shown= and shown_band_<band>= + band_<band> counts + read_at; devices per node "
                                               "(node=count, zero-device nodes only in unfiltered views)",
    "path": "mesh node: node to_main (this node -> … -> main router) downstream (nodes whose backhaul goes through it) "
            "devices_behind",
    "mesh_summary": "mesh show: first line, nodes online read_at (HH:MM:SS of this live read)",
    "ranking": "mesh show --sort path|backhaul|devices: by ranked_on order tiebreak [ties=a=b,…] main_router; nodes then carry rank "
               "[tied=yes]. devices list --sort weakest|signal: by order tiebreak [equal_dbm=a=b equal_dbm_note] (same dBm, "
               "already ordered by the tiebreak)",
    "node_detail": "mesh node -v: internet backhaul_2g_signal backhaul_2g_link mac_5g led location client_bind_supported",
    "mesh/dhcp/security/router/auth/session/check/supported/unsupported": "status/auth/capabilities sections: router-native "
               "key=value fields of that area (see the command's --help)",
    "result/wait": "mesh wait: wait elapsed up waiting_for (progress); result status=all_up|timeout elapsed still_down",
    "reboot/would_reboot": "mesh/system reboot: target ip mac sent (the API errors even on success); plan: nodes_down "
                           "devices_affected",
    # -- writes / plans --
    "changed/unchanged/would_change/already": "write result per key: key old new (target) — see `write`",
    "side_effect/would_side_effect/effect/caveat/note/next": "plan/write context: effect [Wi-Fi plans: drop drop_main drop_satellites drop_on_band drop_other_band may_blip wired_unaffected worst_case (= drop + may_blip, Wi-Fi devices only: wired_unaffected is not part of it; drop = drop_main + drop_satellites = drop_on_band + drop_other_band; may_blip = other-band devices on the main router, a set separate from drop_other_band (other bands of satellites whose backhaul restarts); main_devices/main_wifi = everything on the main router, for context; wireless only, wired_unaffected not included; their sum = devices in this read) outage_main outage_satellites; every Wi-Fi plan also has lose_ssid (devices whose band gets a new name; 0 unless renaming); zero counts are printed in plans but omitted from node/device records; mesh reboot: drop may_blip nodes_down duration roam_to] detail [scope] read_at this_machine; caveat detail "
               "(a risk in this topology); note detail (reassurance); side_effect detail; next run=<command to verify>",
    # -- history --
    "churn": "events --summary: devices_involved (devices with any join/leave/move in the window) still_gone "
             "still_gone_devices [more] (last event a leave and absent from the latest observation) as_of note",
    "device/last_move_at": "devices list / roaming: last_move_at = time of the latest logged node/band move in the last hour "
                           "(tells a fresh move from an old one)",
    "counts/setting_changes_net": "events --summary counts: ... setting_changes_net = snapshot-tracked settings different "
                                  "at the newest local snapshot vs the window start (no router call; `snapshot timeline "
                                  "--writes` net_changes compares with the live router and also sees rows the early "
                                  "snapshots missed, e.g. DHCP reservations, so it can be higher)",
    "roaming_summary": "devices roaming: flapping (devices with >= 3 node changes, online or not) [flapping_offline (of those, not seen at read_at)] unstable_weak / unstable_weak_devices (not a subset of flapping: unstable = >= 3 own node changes, or >= 4 node changes + band switches, at a fresh poor/bad signal; unstable_weak_stale = the others whose poor/bad reading may be old) moves_during_node_outage (always printed, 0 = checked, none) unknown_signal_flapping (flapping devices with no usable signal reading: can't judge) outage_note (of those, signal "
                       "fair-or-worse now) flapping_devices matters",
    "merged": "events --summary: flaps (node_grade_flap lines shown, each = 2 raw node_change events) note",
    "backup": "system backup: file bytes restore note",
    "timezone": "system time --zones: id name",
    "ipv6_allow/ipv6_allows": "nat ipv6-rules: name ip port protocol type enable; ipv6_allows count",
    "qos": "qos show: enable down up max note",
    "speed_limit/speed_limits": "qos limits: name mac enable down up (Mbps or unlimited); speed_limits limited",
    "wifi_schedule/schedule_rule": "wifi schedule: enable rules max_rules; schedule_rule n off_from on_at days bands enable",
    "writes/write/net": "snapshot timeline --writes: writes count listed untracked_folded untracked_tags undone_folded undone_tags (writes fully undone later, folded unless --all) changes net_changes; net key old new "
                        "at (net change over the window: first snapshot vs live where the snapshots cover it, else the "
                        "timeline's final state); write at tag by settings keys undone (all|N) [untracked=yes, only with --all]",
    "profile/profiles": "parental: profile name devices internet_blocked bedtime filters; profiles count max devices_per_profile",
    "untracked_writes": "snapshot timeline --writes: count at_tags (HH:MM tag, one per write that changed nothing the snapshots record) note",
    "recent_writes": "overview: tpctl writes since the previous observation, at least the last 30 min (since count last "
                     "tags=tag×n,… see); what each changed: `snapshot timeline --since 1h`; web-UI/app changes leave no "
                     "snapshot (the timeline still finds them)",
    "config_scope": "events --summary: tracked (the key settings config_changed covers) other_settings (where the rest is)",
    "counts": "events: count per event type: joined left moved config_changed node_events (= every node_* event: parent/status/grade "
              "changes, raw; same name as `--what node`; a node_grade_flap line merges 2 of them) most_active=label:n,…",
    "coverage/roaming": "devices roaming: coverage window history_first observations_in_window observations_total movers "
                        "reconnect_only movers_online_now read_at node_is (flapping counts: roaming_summary); rows: online first, then most node changes. "
                        "moves = node_changes (a different node) + band_switches (same node, other band); reconnects = "
                        "rejoins after leaving (not part of moves); movers = rows with moves > 0; reconnect_only = rows "
                        "with only reconnects; assessment: flapping >= 3 node changes, band steering >= 3 band switches "
                        "and no node change, band flapping >= 3 band switches with 1-2 node changes, reconnecting often >= 3 rejoins, else normal; roaming label moves node_changes band_switches reconnects nodes assessment "
                        "start_node (first node in the window) node band signal signal_grade near_boundary (now) [returned_via=reconnect|unrecorded: "
                        "back on start_node, but the way back was a leave+join (a reconnect) or happened unseen (between "
                        "observations / via unknown-satellite), so it is not in node_changes]; nodes = every "
                        "real node seen (unknown-satellite = a satellite briefly not reporting, never counted). Coverage keys "
                        "also: moves_are flapping_at note",
    "timeline": "snapshot timeline: rows (= changes listed) changes net_changes (keys whose value differs start vs end) net_keys snapshots_first snapshots reverted_hidden precision tz; "
                "then change key old new unit after before likely basis reverted",
    "observed": "observe: changes since",
    # -- diagnostics --
    "ping/diagnosis": "net ping: ping role (node|device|internet) name host avg max jitter loss grade grade_reason samples; "
                      "diagnosis verdict why last_hop_ms node_ms (its node's avg) basis link_rx link_tx slow_link signal_grade signal_stale signal_source near_boundary read_at moves_1h (this device's node/band moves in the "
                      "last hour) [upstream] [also] signal path_grade [hint next_candidate] [stand_in stand_in_node stand_in_signal "
                      "stand_in_avg stand_in_jitter stand_in_wifi_hop stand_in_grade (ping grade of the stand-in) stand_in_tried (candidates in order, same node first; each :answered|:no_reply, (used) marks the one taken; :no_reply = didn't answer) stand_in_same_node stand_in_note] (stand_in_*: another weak device measured "
                      "because this one ignores ping)",
    "router_ping/router_traceroute/hop": "net diag (from the router): router_ping target sent received loss avg max grade "
                                         "finished; traceroute: hop text…, router_traceroute target hops finished",
    "speed": "net speed: download upload latency jitter loaded_latency_down loaded_latency_up bufferbloat (= worst loaded - "
             "idle, never negative) [bufferbloat_reliable=no with one connection] engine connections server isp this_machine data duration [partial] [notes]",
    "port": "system ports: port link up|down speed duplex [wan]",
    "log/entry/log_type": "system log: log entries shown oldest newest; entry time type level text (oldest first, newest last; --limit keeps the newest N); "
                          "--types: log_type type entries",
    "firmware/auto_update/updates": "system firmware: firmware device model version latest up_to_date; auto_update enable "
                                    "time; updates available nodes",
    # -- settings / features --
    "switch": "settings set --list: key endpoint field disruptive live_verified (no = payload from the web UI code, not yet round-tripped on a router) what (switches `settings set KEY on|off` can change)",
    "area/setting": "settings: area area page [unsupported]; setting area key value | empty=yes (secrets ***)",
    "guest/guest_band": "wifi guest: guest enable lan_access isolate; guest_band band enable ssid security",
    "blocked": "devices blocked: name mac ip (or count=0)",
    "reservation/reservations": "dhcp reservations: name ip state(in_use|pending_renewal|offline|off) current_ip (when "
                                "different) node in_pool enable mac (or reservations count=0)",
    "iot/iot_band": "wifi iot: iot enable isolation; iot_band band enable ssid encryption",
    "port_trigger/port_triggers": "nat triggers: name trigger trigger_protocol open open_protocol enable; port_triggers count",
    "route/static_routes/system_route": "lan routes: route name target gateway interface enable; static_routes count; "
                                        "system_route dest gateway interface (the router's own table)",
    "imb/binding": "lan bindings: imb enable bindings; binding name ip mac enable",
    "upnp/upnp_mapping": "nat upnp: upnp enable mappings; upnp_mapping name ip external internal protocol",
    "port_forward/port_forwards": "nat forwards: name ip external internal protocol enable; port_forwards count reachable_ipv4",
    "nat": "nat show: nat enable boost_enable (hardware NAT acceleration; off while QoS is on) apply_wait_s (seconds the "
           "web UI waits after a NAT change); dmz enable [ipaddr only when on]",
    "alg/bands/time/lease/found": "router-native rows: nat alg (protocol=on|off), devices leases (name ip mac remaining), "
                                 "devices find (found count query)",
    "probe": "devices probe: label ip mac node band guess evidence bonjour_name model upnp_name manufacturer vendor "
             "[vendor_note] services bonjour_services web_title router_type [router_name] [via]",
    "identify/identified": "devices identify: identify label ip node guess evidence bonjour_name vendor [private_mac]; "
                           "identified devices with_guess",
    "alias/aliases": "devices alias list: alias mac name (or aliases count=0)",
    "endpoint/endpoints": "api endpoints: endpoint path ops live",
    "value": "api get: value key=… (router-native fields)",
    "login/logout/password": "auth login/logout/set-password results",
    "host": "auth set-host: saved (URL) file verified [note when $TPLINK_HOST overrides it]; auth status also prints "
            "host_source=env|config|default",
    # -- meta (this command) --
    "schema/format/time/record/grade/grades/exit_codes/library/env/global_options/record_vs_attribute/command":
        "tpctl schema / tpctl commands output itself",
}


def _records() -> dict:
    """RECORDS with the key lists filled in from code, so the schema can't drift from the output."""
    from .. import output
    from ..services.health import ISSUE_DATA_KEYS
    r = dict(RECORDS)
    r["node"] = ("concise: [rank tied] name ip [status if not connected] [depth] uplink backhaul backhaul_grade backhaul_signal "
                 "[near_boundary] backhaul_level (router's own 0-5 bars, coarse) backhaul_link backhaul_band "
                 "[path_grade path_link when the weak hop is further up] devices [devices_behind = devices on this node + every node "
                 "downstream; always on satellites, on the main router only when it differs] [devices_weak weakest_device "
                 "weakest_signal [weakest_stale] [devices_weak_stale] = weakest device ON this node: a weak one when any is "
                 "weak (fresh readings first; weakest_stale=maybe if only a stale reading is weak; weakest_excluded=label:dBm(just_roamed) "
                 "names a weaker device that just roamed in and was passed over because its reading hasn't settled)] [weakest_behind weakest_behind_signal = "
                 "weakest device on this node or downstream, on nodes with nodes behind them; any grade] ping loss ping_grade. "
                 "py: the Node attribute `weakest_device` is this record's label string; the method `inv.weakest_device(n)` "
                 "returns the Device whatever its grade. all keys (-v/--fields): "
                 + " ".join(output.NODE_KEYS))
    r["device"] = ("concise: label ip band node signal signal_grade [near_boundary] link_rx link_tx | link=idle signal_stale=maybe signal_source=last_traffic (the dBm is the router's last reading from when the device last sent traffic, often far lower than now: don't call it the weakest device) | signal_source=placeholder (a satellite reported exactly -90 dBm, its stand-in when it has no fresh RSSI; signal_stale=maybe) "
                   "[path_grade weakest_hop "
                   "when an upstream backhaul is fair/poor/bad] [backhaul_limited=yes] [slow_link=yes: a PHY rate < 20 Mbps] [just_roamed=yes: changed node in the last 3 min (event log) or since the previous observation; its signal may swing a lot for a minute]. all keys (-v/--fields): " + " ".join(output.DEVICE_KEYS))
    r["issue"] = ("level code subject <data keys per code> hint (hint omitted when the same code repeats; -v adds detail). "
                  "data keys: " + "; ".join(f"{k}: {' '.join(v)}" for k, v in ISSUE_DATA_KEYS.items()))
    return r


RECORD_TO_ATTR = {"node": {"uplink": "parent_name (or .uplink)", "backhaul_signal": "backhaul_dbm (int)",
                            "backhaul_level": "backhaul_level (int 0-5)", "backhaul_link": "backhaul_mbps (int)",
                            "path_link": "path_link_mbps (int)", "weakest_signal": "weakest_signal_dbm (int)",
                            "weakest_behind_signal": "weakest_behind_dbm (int)",
                            "ping": "ping.avg_ms", "loss": "ping.loss_pct"},
                   "device": {"signal": "signal_dbm (int)", "link_rx": "link_rx_mbps (int)", "link_tx": "link_tx_mbps (int)",
                              "online": "online_min (int)"}}


def _library_fields() -> dict:
    """Dataclass attributes per model (generated, so it can't drift) + where record keys differ."""
    from dataclasses import fields
    from .. import models
    out = {}
    for cls in (models.Node, models.Device, models.Issue, models.Change, models.WriteResult, models.PingResult):
        out[cls.__name__] = [f.name for f in fields(cls) if f.name != "raw"]
    out["record_key_differences"] = RECORD_TO_ATTR
    out["note"] = "record key == attribute name except unit-bearing values (record '-78dBm' string, attribute int)"
    return out


@click.command("schema")
@json_option
@pass_state
def schema_cmd(st):
    """Output contract: record kinds and fields, units, grade thresholds, exit codes. No login."""
    d = {"schema_version": __import__("tplink_router.output", fromlist=["x"]).SCHEMA_VERSION,
         "json_envelope": "{schema_version, command, ok, data, [partial_reads], [confirmation_required]}; errors on stderr: "
                          "{schema_version, ok:false, error:{code, exit, detail, ...}}",
         "format": "--format human|records|json (default: records when stdout is not a TTY, human at a terminal). "
                   "records: one per line `kind key=value ...`; values with spaces/quotes/= are JSON-quoted; "
                   "scalar lists comma-joined; nested values inline JSON; None fields omitted. human: presentation only, "
                   "don't parse. --json = --format json.",
         "time": {"format": "YYYY-MM-DD HH:MM, local time (router log entries keep :SS)",
                  "tz": (lambda z: f"{z[:3]}:{z[3:]}")(__import__("datetime").datetime.now().astimezone().strftime("%z"))},
         "records": _records(), "grades": {"order": list(quality.GRADES), **quality.LEGEND}, "exit_codes": EXIT,
         "library": _library_fields(),
         "env": {"TPLINK_HOST": "router URL", "TPLINK_PASSWORD": "password", "TPCTL_FORMAT": "human|records|json default output format",
                 "TPCTL_DEBUG": "print tracebacks", "NO_COLOR": "disable color"}}

    def text():
        yield o.line("schema", version=d["schema_version"], json_envelope=d["json_envelope"])
        yield o.line("format", detail=d["format"])
        yield o.line("time", **d["time"])
        for k, v in _records().items():
            yield o.line("record", kind=k, fields=v)
        yield o.line("grades", order=list(quality.GRADES))
        for k, v in quality.LEGEND.items():
            yield o.line("grade", metric=k, thresholds=v)
        yield o.line("exit_codes", **EXIT)
        for name, flds in d["library"].items():
            if isinstance(flds, list):
                yield o.line("library", model=name, attributes=flds)
        for kind, m in RECORD_TO_ATTR.items():
            yield o.line("record_vs_attribute", kind=kind, **{k: v.split(" ")[0] for k, v in m.items()})
        yield o.line("env", **d["env"])
    st.out(d, text())


COMMANDS = [commands_cmd, schema_cmd]
