# tplink-router

`tplink_router` is a Python library for TP-Link routers; `tpctl` is the CLI built on it.

A Claude Code plugin plus the `tpctl` CLI and `tplink_router` Python library for TP-Link routers with the current web UI (Archer BE/AX family) and their EasyMesh satellites. Agents and humans share the same tool. At a terminal you get tables and trees. When piped or called by an agent you get line-oriented `key=value` records. `--format human|records|json` picks one explicitly. Every failure is a structured error, and writes support dry-run, no-op skip, snapshot and verify.

Verified on Archer BE805 with EB431v satellites. Other models are handled through capability detection (`tpctl capabilities`). Features a model lacks exit with code 8 instead of failing unpredictably.

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

