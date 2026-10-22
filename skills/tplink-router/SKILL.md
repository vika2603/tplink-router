---
name: tplink-router
description: Inspect and change a TP-Link router (current web UI, e.g. Archer BE/AX) and its EasyMesh satellites with the `tpctl` CLI / `tplink_router` Python library — Wi-Fi bands/SSID/smart connect/power/channels, connected devices and which node they're on, mesh topology and backhaul quality, satellite reboot, WAN/CGNAT/IPv6, firewall/NAT/ALG, DHCP, config snapshots/diffs, raw API. Use for anything about the home router, Wi-Fi, mesh, devices, or home network, including requests like 路由器 / WiFi / 网速慢 / 信号差 / 卫星 / Mesh / 某个设备连不上 / 谁在连网 / 改 WiFi 名称密码 / 重启路由.
---

# tpctl — TP-Link router + EasyMesh control

Doc version 2026-10-22.60 (`tpctl --version` for the code).

`tpctl` is on PATH via this plugin's `bin/` (it runs `${CLAUDE_PLUGIN_ROOT}` through `uv`; needs `uv` installed). Library code: `${CLAUDE_PLUGIN_ROOT}/src/tplink_router/`. Endpoint reference: `${CLAUDE_PLUGIN_ROOT}/skills/tplink-router/references/api.md`.

**Setup:** don't pre-check. Run the command you need. Only if it fails with `code=AUTH_FAILED` (exit 3) and `tpctl auth status` shows `password_source=none`, ask the user for the router admin password and run `printf %s "$PW" | tpctl auth set-password --stdin` (the user can also set `TPLINK_PASSWORD` or run `tpctl auth set-password` in a terminal). Non-default router address: `TPLINK_HOST=http://<ip>`. Then `tpctl capabilities` shows what this model supports. Commands for features it lacks exit 8 (`UNSUPPORTED`).

**Site notes:** `~/.config/tplink-router/site.md`, if it exists, holds this household's ISP, topology, known weak spots and past changes. Read it in the same call as your first command (`cat ~/.config/tplink-router/site.md 2>/dev/null; tpctl overview`), once per conversation. Update it after any change. Create it after the first overview if the user agrees. It is private and never part of the plugin.

**One `tpctl overview` already answers:** health verdict, which node to fix (`verdict fix_first=`), what changed since the previous observation by any tpctl run (often only a minute ago, so usually little: `changes`, `recent_writes`; for the last hour use `events --since 1h`, for "today" the recipe), internet latency, and exposure (`inbound`). Don't re-query those.

## Contract

- Output format: `--format human|records|json` (or `TPCTL_FORMAT`). Default is **records when stdout isn't a TTY** (your Bash calls) and human tables/trees at a terminal. Records: one per line `kind key=value …`, explicit units (dBm, Mbps, ms, min), nothing truncated; spaces/quotes/`=` → JSON-quoted, scalar lists `a,b,c`. `--json` = `--format json`. Human output is presentation only; never parse it. To show the user a readable view, run with `--format human`. `tpctl schema` = record kinds, fields, grade thresholds, exit codes. `tpctl commands [--risk disruptive] [--json]` = every command with params/choices/risk/dry-run. Neither needs a login.
- Records are **concise by default** (zero counts and empty fields are omitted); a node line shows `path_grade` only when worse than its own hop and `path_link` + `path_bottleneck` (the hop that sets it) only when the bottleneck is a hop further up (only fields that answer questions: overview ≈ 750 tokens on a small mesh, ~2.5-3k with 6 nodes / 30 devices and many issues, a node ≈ 130). `-v/--detail` adds every field (MACs, models, modes …) and is a strict superset with the same key names. `tpctl schema` lists both sets.
- `overview`/`check` records, in order:
  - `verdict status=ok|warn|error|partial … headline=… at="YYYY-MM-DD HH:MM"`;
  - `changes since="YYYY-MM-DD HH:MM" count=N` (when the previous observation was) + one `change` line per difference (node parent/status/grade, devices joined/left/moved); full history: `tpctl events`;
  - `recent_writes count= tags=` (only when there were tpctl writes): tpctl writes since the previous look (min. 30 min); mention it; what they changed: `snapshot timeline --since 1h --writes`;
  - `wan internet= ipv4= cgnat= uptime= ipv6_lan_prefix= firewall=` and `inbound ipv4= ipv6= remote_admin= vpn_server= dmz_staged=` (exposure);
  - `internet v4= v6= loss= grade=` latency from this machine;
  - `wifi ssid= smart_connect= <band>_channel= <band>_channel_mode= <band>_width= <band>_txpower=`, one `node` line per node, `devices total= band_<band>=`;
  - issues with flat data fields per code (e.g. `WEAK_BACKHAUL uplink= backhaul_signal= backhaul_level= backhaul_link= devices_behind=`; every code's keys: `tpctl schema`); a repeated code omits its hint.
- Grades: excellent > good > fair > poor > bad (`signal_grade`, `backhaul_grade`, `ping_grade` on nodes, concise and `-v`; `net ping` rows call it `grade` + `grade_reason`).
  - **Weak device = `signal_grade` poor|bad = < −75 dBm** (bad = < −82 dBm; exactly −75 is still fair, −82 still poor), the one definition behind node `devices_weak`, `WEAK_DEVICES` and `devices list --weak`.
  - Signal values are instantaneous: expect ±3 dB between runs on an active device (more on a phone in a pocket).
  - Records (devices, and nodes for `backhaul_signal`) flag `near_boundary=yes` within 2 dB of the weak (−75) or bad (−82) threshold, the two edges that change advice; `node_grade` events carry it too when the flip is one grade at a threshold.
  - `backhaul_level` is the router's own coarse 0-5 bar count: grades use the worse of it and the dBm.
  - In `WEAK_DEVICES devices=` a near-threshold value gets a `~` (and `^` = just changed node), so hedge those grades and don't report a one-grade flip as a change.
  - A wireless device with `link=idle` (no recent traffic sample) or an implausible reading (≤ −95 dBm; a satellite's stock −90 dBm `signal_source=placeholder`, which also gets no grade; or < −85 dBm with a PHY rate > 500 Mbps, `signal_source=implausible`) may show an old signal (`signal_stale=maybe`; `?` in `WEAK_DEVICES devices=`, `stale=` count): say "last reported" and re-check after it has traffic before calling it weak.
- `overview` exits 0 whenever it ran (read the verdict); gate on `tpctl check` or `overview --strict` (exit 1 on warn/error).
- Devices carry a unique, sticky `label` (alias, else router name; `#MAC-tail` once that name has been shared by two devices, kept from then on).
  - The MAC is the permanent key (`-v`).
  - `randomized_mac=yes` = private MAC.
  - `link_tx/link_rx` = PHY rate, not throughput, in the direction the router reports (TP-Link doesn't document it; treat the lower one as the bottleneck)
  - (`link=idle` = no recent sample).
  - Nodes carry `backhaul_band` (5G/2.4G) — plans say explicitly whether the mesh backhaul is on the band being changed (`note … unaffected` vs `caveat`).
  - `path_grade`/`weakest_hop` appear (concise) when a backhaul upstream is fair/poor/bad; on nodes, concise `path_grade` appears only when it differs from the node's own `backhaul_grade` (i.e. the weak hop is further up); `-v` always shows them — the usual reason an area is slow.
  - A machine on Wi-Fi and Ethernet at once appears as two devices (different MACs).
  - Every command (and every `--sort` run) is a fresh live read; the table below lists how much each number moves. Specifically:
    - a satellite that briefly doesn't report its devices shows them as `node=unknown-satellite` (also an `UNATTRIBUTED_DEVICES` info);
    - device counts differ by 1-2 seconds apart, more over minutes when phones sleep or leave (check `events`/`devices roaming` before calling it a bug);
    - decide on grades/signal, not one Mbps or ms reading;
    - for exact cross-checks read once: `tpctl py`, or one `--json` call.
  - Records/`line()` omit fields whose value is None; `--json` keeps them as `null` and gives numbers without units.
- Exit: 0 ok · 1 check found warn/error / `mesh wait` timeout · 2 usage · 3 auth · **4 confirmation required** · 5 router rejected/unreachable (`retryable` says if retrying helps) · 6 partial data (`warning type=partial_read` lines) · 7 write verify mismatch (`mismatch` record) · 8 unsupported on this model · 10 internal.
- `--json` envelope: `{schema_version, command, ok, data, [partial_reads], [confirmation_required]}`; errors on stderr `{schema_version, ok:false, error:{…}}`. `schema_version` only bumps on breaking changes.
- Lists: `devices list` defaults to `--limit 50` and emits `truncated shown= total=` when cut; `--fields ip,signal` projects device/node records from the full field set (label/name always kept; unknown names exit 2 with the valid list).
- Errors: one record on stderr `error code=… exit=… detail=… [usage valid available did_you_mean] hint=…` (JSON with `--json`, multi-line for humans at a TTY).
- Bare `tpctl` = help, no login.

**Numbers that differ between two commands** (each command is its own live read):

| Field | Expect between reads | Why |
|---|---|---|
| device `signal`, node `backhaul_signal` | ±3-4 dB settled; ±5 dB or more with `just_roamed=yes` or `moves_1h` > 0 | instantaneous RSSI; hedge `near_boundary=yes` |
| `link_rx/tx`, `backhaul_link`, `path_link` | ±30-50 % typical; can halve, double or move 4-5× within seconds | Wi-Fi rate adaptation: judge grades, not one Mbps value |
| `devices`, `devices_behind`, band counts | ±1-2 per minute, more over longer gaps or while `devices roaming` shows flapping devices or a node drops | devices roam, sleep and leave (`events`) |
| a plan's counts vs. an earlier command | a few devices | each command reads once; inside one command (effect, caveats, issues) all counts come from the same read |
| node/device `ping` | a few ms wired; up to ~10 ms on a wireless-backhaul node (more on a poor one); `overview` pings everything in one parallel batch, so its node pings can differ by up to ~10 ms either way | load and retries on the hop |
| `signal_stale=maybe` rows (`signal_source=last_traffic`: the dBm is from the device's last traffic; `signal_source=placeholder`: a satellite's stock −90 dBm with no fresh reading) | can jump 10-20 dB | no recent traffic sample: an old reading |
| a device's signal in `devices roaming` / plans vs. an earlier `devices list --weak` | can cross −75 or −82 within a minute | every command reads live: a device can turn weak (or recover) between two commands |
| device `moves_1h` vs `devices roaming` node_changes + band_switches | ±1 | `moves_1h` counts every logged move in the 60 min before its read; roaming doesn't count a hop to/from `unknown-satellite` as a node change (it can still be listed in `nodes=` and be the current `node=`) |
| node `devices_weak`, `weakest_device`/`weakest_signal` | several devices within a minute when many sit near −75 dBm | `^` just-roamed / `~` near-boundary devices cross the line between reads; equal dBm ties go to the alphabetically first label |

## Session & credentials

Read commands that observe the mesh (`overview`, `check`, `mesh show`, `devices list`) append to the local event history. `--no-track` (a global flag, accepted by every command, incl. `py`) or `TPCTL_NO_TRACK=1` turns that off.

Password source order: `$TPLINK_PASSWORD` → macOS Keychain service `tplink-router` → `~/.config/tplink-router/password` (0600). The router allows ONE admin session, so tpctl keeps it in `~/.local/state/tplink-router/session.json` (0600, token only, bound to host+password hash, 20 min idle TTL). Runs reuse it (~0.2 s vs ~2 s), parallel runs share it under a lock, and a stale one (timeout, user logged into the web UI) is replaced transparently, including before the first write. `tpctl auth status [--check] | login | logout` (logout frees the slot for the web UI). Never test with a wrong password: the router throttles logins.

## Recipes

| User says | Do |
|---|---|
| vague "网络怎么样 / how's the network" | `tpctl overview` (if it says `devices total=0` or `verdict status=partial`, the router glitched: re-run it once before reporting) → verdict, error/warn issues, notable `change` lines (info issues only if asked) |
| "X is slow" | 1. `tpctl devices find X` (skip if you already have its `device` record, e.g. from `devices list --weak`) · 2. `tpctl net ping --device X` (20 pings by default; `--device @weakest` = rank 1 of `devices list --weak`, one call) → `diagnosis verdict= why= basis= node_ms= signal= signal_source=` (`placeholder` = the device's own signal is unknown) (details below; if the device ignores ping the verdict comes from router data/events — say so, and if it says it moved, re-check with `devices find X` before advising) · 3. only if the internet pings aren't good or the whole network feels slow: `tpctl net speed` (measures THIS machine's line, `this_machine=wired|wifi`: it tells ISP vs home network, not the slow device's own speed) · 4. judge device `signal_grade`/`slow_link`, node `backhaul_grade`, `path_grade`, depth, band |
| "cut off / pause a device (e.g. kid's tablet)" | `tpctl devices block Q --dry-run` → confirm → `--yes`; undo `tpctl devices unblock Q` (switches access control off again when the list empties) |
| "guest Wi-Fi" | `tpctl wifi guest` (show) → `printf %s "$PW" \| tpctl wifi guest on --password-stdin --dry-run` (radio restart: confirm first) |
| "which DNS / 改 DNS" | `tpctl dhcp set-dns 1.1.1.1 1.0.0.1 --dry-run` (or an ad-blocker's LAN IP alone; `router` = default) |
| "always same IP / NAS / printer" | `tpctl dhcp reserve Q --dry-run` (current IP by default; already reserved → updates it) |
| "change / pause a reservation, is it applied?" | `tpctl dhcp edit Q --ip IP / --off / --on --dry-run`; `tpctl dhcp reservations` (state in_use / pending_renewal / offline / off) |
| "kids' devices: pause internet / bedtime / 家长控制" | `tpctl parental` (profiles), `tpctl parental add NAME --device Q [--device Q2] --bedtime 21:30-07:00 --dry-run`, `parental pause NAME on|off --dry-run`, `parental rm NAME --dry-run` (disruptive: plan, then --yes) |
| "limit a device's speed / 限速" | `tpctl qos limit Q --down 10 --up 2 --dry-run`, remove: `qos limit Q off`; list: `qos limits` |
| "lag when someone downloads / bufferbloat / 打游戏卡" | `tpctl net speed` (loaded latency), then `tpctl qos set on --down <90 % of down> --up <90 % of up> --dry-run` (disruptive: NAT acceleration off) |
| "turn Wi-Fi off at night / 定时关 WiFi" | `tpctl wifi schedule add --from 23:30 --to 07:00 [--days weekdays] [--bands 2g,5g] --dry-run`, then `wifi schedule on`; `wifi schedule` lists, `rm N` deletes |
| "back up the router config / 备份配置" | `tpctl system backup` (the router's own .bin, restore in the web UI) |
| "router clock / NTP / time zone" | `tpctl system time [--ntp S --timezone NAME --dry-run]` |
| "firmware update / 升级固件" | `tpctl system firmware` (check), `tpctl system upgrade --dry-run` (disruptive), `system auto-update on|off --at H` |
| "open a port over IPv6" | `tpctl nat ipv6-allow add NAME --to DEV --port P --dry-run` (really reachable: IPv6 has no CGNAT) |
| "DHCP pool / gateway / server off" | `tpctl dhcp set-range START END --dry-run`, `set-gateway IP\|router`, `server off`: disruptive, the plan names devices that move and reservations the router would disable |
| "open a port" | `tpctl nat forward add NAME --to IP --port EXT[:INT] --dry-run` (the plan says when CGNAT makes it unreachable) |
| "is it my machine or the router's uplink" | `tpctl net ping` (from here) vs `tpctl net diag 1.1.1.1` (from the router) |
| "any other setting on the web UI" / "can tpctl do X from the web page" | `references/coverage.md` (every web-UI page → its tpctl command) → `tpctl settings <area>` for read-only pages |
| "what is this device / 不认识的设备" | `tpctl devices identify` (every online device: Bonjour name+model, MAC vendor, router type → `guess` + `evidence`; `--unknown`, `--ports`) → `tpctl devices probe Q` (one device: + UPnP, open service ports, web title) |
| "worst / weakest wireless device" | `tpctl devices list --weak` → `rank=1` (see note A) |
| "a wired device / NAS / TV box is slow" | `tpctl system ports` (`SLOW_PORT` = linked at ≤ 100 Mbps → cable or NIC), then `tpctl devices find X` |
| "did the internet drop / 是不是断过网" | `tpctl system log --grep "interface wan" --since 7d` (WAN disconnect/reconnect times; `--type ppp` for PPPoE dial-ups). EasyMesh events aren't logged there, use `tpctl events` |
| "is the firmware up to date" | `tpctl system firmware` (main + every satellite, `up_to_date`, auto-update) |
| "rename this device / node" | device: `tpctl devices rename Q NAME --dry-run` (router-side); node: `tpctl mesh set Q --name N --dry-run` |
| "is it the device or the satellite" | `tpctl devices list --backhaul-limited` (path worse than the device's own signal = moving the device won't help); nodes carry `weakest_device=`/`weakest_signal=` when they have weak devices |
| "an area/floor is bad" / "which node should I fix" | **Node problems before backhaul work, weighed by `devices_behind`:** `NODE_OFFLINE` (`state=rejoining`: wait with `mesh wait`; `flapping`: watch it, check power/placement; `offline`: plan a reboot / power-cycle) and `NODE_FLAPPING` (online now but dropped repeatedly within the last hour) come first when they carry devices; one with 0 devices behind it ranks after a poor backhaul that carries many. The overview's `fix_first` applies this: most `devices_behind` wins, a flapping node stable for 30+ min counts half, errors win ties. **Already ran overview? Its `verdict fix_first=` is the answer (no extra call).** Otherwise the node to fix = `tpctl mesh show --sort path` rank 1; `--sort health` instead answers "where do devices suffer most"; each node line names `weakest_device=` (on the node itself, when it has weak devices) and, for a node with nodes downstream, `weakest_behind=` (the weakest of everything behind it = py `weakest_device(n, behind=True)`); `tpctl mesh node <node>` for a depth ≥ 2 node's `path to_main= downstream=` line or the devices ON it; `tpctl devices list --node <node> --sort weakest` for its devices |
| "can it be reached from the internet / 外网能访问吗" | the `inbound` line of an `overview` you already ran (else `tpctl status`; `dmz_staged=` = an address saved while DMZ is off: harmless now, and useless under CGNAT even if turned on) → `inbound ipv4=… ipv6=…` (computed from CGNAT, forwards, firewall, IPv6 allow rules) |
| "calls drop / who roams a lot" | `tpctl devices roaming [--since 1h]`; the answer to "does it matter" is the `roaming_summary` line: `unstable_weak_devices=` (roaming at a weak signal: it matters), `matters=`, `moves_during_node_outage=` (forced by a node drop: fix the node) (per device: node changes = real roams vs band switches, `nodes` = every node seen incl. the starting one, current node/signal, history coverage) → `tpctl net ping --device X` |
| "can't connect / device gone" | `tpctl devices find X` → `tpctl devices leases` → `tpctl mesh show` |
| split bands / per-band names | `tpctl wifi set smart-connect off --ssid-5g NAME` (one plan; per-band `ssid --band` is refused while smart connect is on) |
| change several Wi-Fi settings of one band | `tpctl wifi set band 5g --width 80 --channel 36` (one plan, one restart) instead of separate commands |
| change something | **plan with `--dry-run`** (works on every write incl. reboots; agent safety layers may block a bare disruptive command) → tell the user plan + effect + caveats → on OK run the printed `confirm run=` command → Wi-Fi: `tpctl mesh wait && tpctl check` → update site notes |
| "reboot the satellite with most devices" | `tpctl mesh reboot --most-devices --dry-run` (one call: picks rank 1 of `mesh show --sort devices`, names ties; see note C) |
| "compare a number across commands" / exact cross-check | each command is its own live read (table above); for a same-read comparison use one `tpctl py` call (or one `--json` call) and read every number from that |
| "what changed today" | one call: `tpctl snapshot timeline --since today --writes; tpctl events --since today --summary` (`--writes` = every tpctl write incl. undone ones and the net changes in its header; web-UI changes appear as `tag="outside tpctl"`); see note B; always state `coverage_gap` (device history usually starts when tpctl first observed) and mention `untracked_writes` (writes whose state the snapshots don't record) |
| "what changed recently" | state the coverage first (device history starts at `history first=`, config history at the oldest snapshot). Devices/mesh: `tpctl events [--since 6h] [--about X]`. Settings, per write: `tpctl snapshot timeline --since 6h --writes` (`net` lines = what is different now, with old/new/at). Settings, every step: `snapshot timeline --since 7d [--key ssid]`. Net state vs. a point in time: `snapshot diff --since 7d` |

**Recipe notes**

- **A · weakest device:** settled readings sort first; `signal_stale=maybe` / `just_roamed=yes` rows come after them, so `rank=1` is normally the answer. `slow_link=yes` = a PHY rate < 20 Mbps even when the signal looks ok.
- **B · what changed:** `--writes` header counters:

  | counter | meaning |
  |---|---|
  | `count` | tpctl writes in the window (= listed + untracked_folded + undone_folded) |
  | `listed` | write rows shown (still at least partly in effect) |
  | `untracked_folded` | writes that changed nothing the snapshots record |
  | `undone_folded` / `undone_tags` | writes fully undone later (`--all` lists them) |
  | `changes` | timeline rows (one per setting step; a setting changed twice counts twice) |
  | `net_changes` | settings different now than at the window start = the `net` lines |
 Roaming rows with `returned_via=reconnect|unrecorded` are back on their start node by a way that isn't a node change. `snapshot timeline` hides changes later undone (`reverted_hidden=N`). What was tried, or `recent_writes tags=` the timeline doesn't show: `snapshot timeline --since today --writes` (rows = writes still (partly) in effect; fully undone ones are counted in `undone_folded`/`undone_tags`, `--all` lists them). `--all` lists every row (larger). Net end state vs. the start of the day: `snapshot diff --since today`. `by=tpctl` rows carry the exact time (`likely=` in the timeline, `at=` in `--writes`). `events` `config_changed` only sees a tracked switch that is still changed at the next observation: a write undone before tpctl looked again (most tests) shows only in `--writes`. Event counts are raw (flaps merged only in the listed events; read glitches never recorded). Always state the `history coverage_gap`: device history exists only while tpctl was observing.
- **C · most devices:** a tie for rank 1 is broken by more nodes behind it, then healthier path_grade; ranks on `devices_behind` (on it + on every node behind it, which a reboot also takes down) = the most disruptive reboot. `ties=`/`tie_effect=` name a tie for rank 1 and what each choice takes down: tell the user. If they meant the busiest node itself, compare `devices`.

**`net ping --device X` details:** default 5 pings, 20 with `--device`. Ping `grade`: 100 % loss → bad; loss > 10 % → poor; else avg < 5 ms excellent / < 15 good / < 40 fair / else poor, then caps: jitter > 20 ms → at most fair (> 40 ms → poor), any loss → at most fair (`grade_reason` names the rule that decided). `last_hop_ms` = device avg − its node's avg (what the Wi-Fi hop adds). `diagnosis verdict` ∈ last_hop_signal | likely_power_save | last_hop_jitter | roaming_flaps | upstream_backhaul | internet | ok | router_data_only, `basis` ∈ ping | ping+signal | router_data | router_data+signal | router_data+ping(+signal) | events | events+ping | events+signal | events+ping+signal (`+signal` = the weak signal/slow PHY also decided it). A clean ping never overrides a poor/bad signal or a < 20 Mbps PHY link (`slow_link=yes`). iPhones often ignore ping: then the verdict comes from router data and says so, and tpctl pings up to 3 other weak devices (same node first) in the same run: `stand_in= stand_in_wifi_hop= stand_in_jitter=` is the measured Wi-Fi-hop cost of a weak device there (`stand_in_wifi_hop` = its avg − its node's avg from a second ping run at the same moment, so it can differ by a few ms from `stand_in_avg − node_ms`) (report it as that, not as the device's own number). `net speed` with one connection (default engine) marks `bufferbloat_reliable=no`; `--engine apple` gives the firm number.

## Commands

Full catalog: `tpctl commands` (or `references/commands.md`); `tpctl <group> --help` for options. Groups: overview · check · status · events · devices · mesh · wifi · dhcp · nat · lan · qos · parental · settings · system · net · snapshot · api · auth · py.

**Disruptive commands** (`wifi set …`, `wifi guest|iot on/off`, `mesh reboot`, `mesh set --uplink`, `devices block`, `nat dmz on`, some `settings set`, `system reboot`) never write without `--yes`. With `--dry-run` they print the plan and exit 0 (preferred). Without either flag they print the same plan plus `confirm … run="<exact command> --yes"` and exit 4. Relay plan and effect to the user and run `run` only after they agree. If nothing would change, there is no confirmation (exit 0).

## Library (escape hatch)

When no command answers it: `tpctl --no-track py -c 'CODE'` with a logged-in Router `r`, `line(kind, **kv)` / `show(obj)` for records; read-only unless the user agreed.

```python
inv = r.mesh.inventory(ping=False)                 # one fresh live read
for rank, n in inv.ranked("path"):                 # satellites only, worst path first
    w = inv.weakest_device(n, weak_only=True); where = "on"
    if w is None:                                  # behind=True = on n or any node downstream
        w, where = inv.weakest_device(n, behind=True, weak_only=True), "on_or_behind"
    if w is None:                                  # nothing weak there: the weakest of any grade
        w, where = inv.weakest_device(n, behind=True), "any_grade"
    line("sat", rank=rank, name=n.name, status=n.status, path_grade=n.path_grade, devices_behind=n.devices_behind,
         weakest=w and w.label, where=w and where, weakest_dbm=w and w.signal_dbm, grade=w and w.signal_grade, stale=w and w.signal_stale)
```

- Node: `n.name n.status n.backhaul_grade n.path_grade` (str), `n.devices n.devices_behind n.devices_weak` (int). Device: `w.label w.node w.signal_grade` (str; grade None for a placeholder reading), `w.signal_dbm` (int dBm; records print `-80dBm`), `w.signal_stale w.just_roamed w.near_boundary` (bool; `line()` prints yes/no, records print stale as `maybe`).
- `inv.weakest_device(n)` (`weak_only=True`: poor/bad only, like the CLI field): any grade (on a healthy node it can be excellent); skips maybe-stale readings when a fresh one exists; a just-roamed one only if no settled device is weak; ties → label. The CLI node field `weakest_device` differs: set only when the node has weak devices.
- `ranked("path")`: path only (not device health, not flapping/offline: what to fix first = overview `fix_first`); ties → weaker path signal, the node's own hop, lower bottleneck Mbps, name.
- More attributes and helpers: `references/library.md`.

## Rules

- `risk=disruptive` (Wi-Fi writes, reboots) cuts connectivity: the tool itself stops at exit 4; get the user's OK before re-running with `--yes`. If this machine is on Wi-Fi, warn that tpctl itself may lose the router for a minute.
- Never call ⚠ endpoints from the API reference (firmware, cloud, quick_setup, detect, credential forms) without an explicit request.
- Don't trust one mesh reading right after a change; `tpctl mesh wait`, then re-check.
- Extending: service method in `src/tplink_router/services/` → CLI command in `cli/` (+ `cli/meta.py` RISK entry if it writes) → renderer in `output.py` → test in `tests/` with the fake router. Verify new endpoints read-only first, then record them in the API reference.
