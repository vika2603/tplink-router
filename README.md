<div align="center">

# TP-Link Router

**See, diagnose and safely change your TP-Link mesh from the command line, or let an AI agent do it.**

[![test](https://github.com/vika2603/tplink-router/actions/workflows/test.yml/badge.svg)](https://github.com/vika2603/tplink-router/actions/workflows/test.yml)
[![license: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
![python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

`tpctl` CLI · `tplink_router` Python library · Claude Code plugin · `uv tool install git+https://github.com/vika2603/tplink-router`

</div>

One command tells you how the whole network is doing and what to fix first:

```console
$ tpctl overview
verdict status=warn errors=0 warnings=1 headline="WEAK_BACKHAUL:Basement" fix_first=WEAK_BACKHAUL:Basement
internet v4=8.2ms v6=7.9ms loss=0%,0% grade=good,good
node name=Basement uplink=Main backhaul=wifi backhaul_grade=poor backhaul_signal=-79dBm backhaul_link=520Mbps devices_behind=5
node name=Upstairs uplink=Main backhaul=wifi backhaul_grade=good backhaul_signal=-60dBm backhaul_link=1729Mbps devices=8
issue level=warn code=WEAK_BACKHAUL subject=Basement hint="Ethernet backhaul, or move it closer to Main"
```

- **Diagnosis, not just numbers:** every reading is graded and every problem comes with a cause and a fix. Which satellite
  is the bottleneck, whether a slow phone is its own Wi-Fi hop, the mesh or the ISP (`tpctl net ping --device @weakest`),
  who roams too much and whether a node outage caused it, what changed today.
- **Safe changes:** anything that drops connectivity prints a plan first (who drops, for how long, the risks in *your*
  topology) and runs only with `--yes`; every write takes a snapshot first and is verified afterwards.
- **One tool for people and agents:** tables at a terminal, `key=value` records when piped or called by an agent,
  `--json` for scripts, structured errors with exit codes. Wi-Fi, mesh, DHCP, NAT, QoS and speed limits, parental
  controls, schedules, firmware, backups and a speed test: [what's covered](skills/tplink-router/references/coverage.md).

Verified on an Archer BE805 with EB431v satellites; other models are handled through capability detection
(`tpctl capabilities`), and features a model lacks exit with code 8 instead of failing unpredictably.

## Install

Requires Python 3.11+.

- **CLI + library:** `uv tool install git+https://github.com/vika2603/tplink-router` (or `pipx install git+https://github.com/vika2603/tplink-router`).
- **As a Claude Code plugin:** in Claude Code run

  ```
  /plugin install tplink-router --marketplace vika2603/tplink-router
  ```

  (older Claude Code: `/plugin marketplace add vika2603/tplink-router`, then `/plugin install tplink-router@tplink-router`). Or clone the repo and run `claude --plugin-dir <clone>`. `bin/tpctl` lands on Claude's PATH and builds its environment with [uv](https://docs.astral.sh/uv/) on first use; the agent-facing guide is `skills/tplink-router/SKILL.md`.
- **From a clone:** `uv sync && uv run tpctl --help`; tests: `uv run pytest`.

Then run:

```sh
tpctl auth set-password          # prompts, verifies; macOS Keychain, elsewhere ~/.config/tplink-router/password (0600)
tpctl auth set-host 192.168.1.1   # only if your router isn't at http://192.168.0.1 (saved; TPLINK_HOST overrides)
tpctl capabilities               # what your model supports
tpctl overview                   # verdict, changes, status, Wi-Fi, mesh, devices, issues
```

## Highlights

- `tpctl overview` / `check`: verdict first, then changes since the last look, graded metrics (signal, backhaul, ping, path), and issues with codes and concrete hints. Readings near a grade boundary are flagged.
- Mesh insight:
  - each node's uplink chain, backhaul band/dBm/level/PHY, and path bottleneck;
  - `mesh show --sort path` ranks nodes worst first;
  - each device gets a `path_grade`/`weakest_hop`, which shows when the slowness is upstream.
- Diagnosis:
  - `net ping --device X` compares the device, its node and the internet, and gives a verdict;
  - when the device ignores ICMP, it falls back to router data and roaming events;
  - `net speed` measures this machine's down/up throughput;
  - `devices roaming` separates real roams from band switches.
- Safe writes:
  - disruptive commands print a plan and exit 4 without `--yes`. The plan covers scope, topology-aware caveats and the affected devices by name;
  - `--dry-run` and no-op skip;
  - a snapshot before every write, verification after (exit 7 on mismatch);
  - `wifi set band 5g --width 80 --channel 36` makes several changes with one radio restart.
- History: `tpctl events` (devices, nodes, key settings, recorded whenever tpctl observes), `snapshot timeline` (when each setting changed), `snapshot diff`.
- Session reuse: the router allows one admin session, so tpctl persists it (token only, 0600), shares it across parallel runs and re-logs in transparently. An unreachable router fails in under a second.
- Self-describing: `tpctl commands --json` (catalog with risk tiers) and `tpctl schema` (record fields generated from code, grades, exit codes, library attributes). Tests fail if the docs drift from the output.
- Scriptable: `tpctl py -c '…'` runs with a logged-in `r`. `from tplink_router import connect` gives the same safeguards plus helpers (`inv.ranked()`, `inv.path()`, `inv.weakest_device()`, `quality.worse()`).

## Local data (never part of the plugin)

| Path | What |
|---|---|
| `~/.config/tplink-router/password` | password fallback when the Keychain isn't used (0600) |
| `~/.config/tplink-router/config.json` | saved router address (`tpctl auth set-host`) |
| `~/.config/tplink-router/aliases.json` | your friendly device names |
| `~/.config/tplink-router/site.md` | optional notes about your network for the agent |
| `~/.local/state/tplink-router/` | session, capabilities cache, observations + event log, snapshots (`--no-track` / `TPCTL_NO_TRACK=1` to skip) |

## Development

```sh
uv run --group dev pytest        # offline tests against a fake router (no network)
```

Layout: `src/tplink_router/` holds `client.py` (session, calls, writes), `services/`, `models.py`, `quality.py`, `output.py` and `cli/`. Plugin metadata is in `.claude-plugin/plugin.json`, and the agent instructions plus API reference are in `skills/tplink-router/`.

## Status and scope

Verified on one setup (Archer BE805 router, EB431v EasyMesh satellites, firmware 1.5.x). The web-UI API was mapped from the
router's own UI bundle; other models and firmware may differ, and `tpctl capabilities` reports what a router supports.
`skills/tplink-router/references/api.md` records which endpoint was verified how, and `references/coverage.md` which web-UI
pages tpctl covers. Use at your own risk: writes that drop connectivity always need an explicit `--yes`.

## License

GPL-3.0-or-later (it builds on [tplinkrouterc6u](https://github.com/AlexandrErohin/TP-Link-Archer-C6U), GPLv3+).

TP-Link is a trademark of TP-Link Systems Inc. This is an independent, unofficial project, not affiliated with or endorsed by TP-Link.
