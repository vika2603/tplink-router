# tplink_router library (via `tpctl py`)

## Quick reference

When no command answers it, `tpctl py -c 'CODE'` (or `tpctl py - <<'EOF'`) runs Python with a logged-in Router `r`, the package as `tp`, and `line(kind, **kv)` / `show(obj)` for records, all in one login. Read-only unless the user agreed to a change.
- **Helpers:** `inv = r.mesh.inventory()` (a fresh live read each time; counts can differ by a few from a record a minute earlier), `inv.ranked("path")` → [(rank, node)], `inv.weakest_device(n)`, `inv.devices_on(n)`, `inv.devices_behind(n)`, `inv.path(n)`, `inv.downstream(n)`.
- **Attributes:** Node `name status online ip backhaul_grade backhaul_dbm backhaul_mbps path_grade path_link_mbps devices devices_behind devices_weak devices_weak_stale` (ints; `devices_weak` includes stale readings, same count as the records; `path_link_mbps` = the lowest backhaul_mbps on the path, read in the same inventory: equal to the node's own hop at depth 1, and like any PHY number it moves ±30-50 % between reads; device objects: `inv.devices_on(n)`); Device `label ip mac band node signal_dbm signal_grade near_boundary signal_stale just_roamed link_rx_mbps link_tx_mbps path_grade` (`near_boundary`/`signal_stale`/`just_roamed` are bools: True = what records print as `near_boundary=yes`, `signal_stale=maybe`, `just_roamed=yes`). `r.mesh.inventory()` never writes the event history (only the observing CLI commands do). Record names work as attributes and return the same plain ints (`n.backhaul_signal` = `n.backhaul_dbm`, `n.backhaul_link` = `n.backhaul_mbps`, `n.path_link` = `n.path_link_mbps`, `d.signal` = `d.signal_dbm`, `d.link_rx` = `d.link_rx_mbps`).
- **Units:** `line()` prints values exactly as given (no units added, and bools print yes/no; built-in records print stale hedges as `maybe`); write units into the key or value yourself, e.g. `line("sat", backhaul_dbm=n.backhaul_dbm)` or `tp.output.unit(v, "dBm")`.
- **Weakest device:** (the Node attribute `n.weakest_device` is just the record's label string, set only when a weak device is on n; the method below returns a Device of any grade) `inv.weakest_device(n)` = weakest device ON n whatever its grade (fresh readings first, never a `signal_stale` reading if another exists; a `just_roamed` one only when no settled device on n is weak; ties → alphabetical label; None when n has no wireless devices of its own). When ranking satellites use `w = inv.weakest_device(n) or inv.weakest_device(n, behind=True)`, or rank 1 (often a relay with 0 devices) comes out empty.
- **Ranking:** `inv.ranked("path")` (= `mesh show --sort path`; satellites only) ranks backhaul paths only (not device health, not flapping/offline status: use the overview `fix_first` for "what to fix") (`ranked("health")` adds the devices): worst path_grade first, ties → weaker signal on the path, then the node's own hop, then lower bottleneck Mbps, then name.
- Every attribute: `tpctl schema` (`library` lines).


Use only when no `tpctl` command answers the question. Every common combination (ranking with weakest device, backhaul-limited devices, devices behind a node) is a command now.

## Example: satellites worst first, with each one's weakest device

```bash
tpctl py -c '
inv = r.mesh.inventory(ping=False)
for rank, n in inv.ranked("path"):
    d = inv.weakest_device(n) or inv.weakest_device(n, behind=True)   # on n, else downstream (relays with 0 devices)
    line("sat", rank=rank, name=n.name, path_grade=n.path_grade, backhaul_dbm=n.backhaul_dbm,
         weakest=d.label if d else None, weakest_dbm=d.signal_dbm if d else None)'
```
(`tpctl mesh show --sort path` prints the same ranking with `weakest_device=` already: prefer the command.)

## Usage

```python
# tpctl py -c '…' | tpctl py script.py   → preloaded: r, tp, out, show(), line(), pp()
# standalone: uv run --project ${CLAUDE_PLUGIN_ROOT} python x.py  with  from tplink_router import connect, show
inv = r.mesh.inventory(ping=True)                # Inventory(nodes, devices); fallbacks when the model lacks mesh/rates
for n in inv.nodes_matching("garage"):           # never raises; inv.node(q)/inv.device(q) need a unique match
    line("chain", node=n.name, path=[m.name for m in inv.path(n)], behind=len(inv.devices_behind(n)))
show(r.health.issues(inv), inv.devices_matching("iphone"))     # same records as the CLI
show(r.wifi.set_txpower("middle", bands=["5g"], dry_run=True))  # WriteResult(changes, applied, dry_run, …)
```
Attributes = record keys, except unit-bearing ones are ints in the library: `Node`: name ip mac status depth **parent_name** (= `.uplink`) backhaul backhaul_grade backhaul_band **backhaul_dbm backhaul_level backhaul_mbps** path_grade **path_link_mbps** devices devices_behind devices_weak weakest_device ping(.avg_ms .loss_pct) ping_grade is_main online · `Device`: label name ip mac band node **signal_dbm** signal_grade **link_rx_mbps link_tx_mbps** path_grade weakest_hop backhaul_limited signal_stale near_boundary just_roamed moves_1h last_move_at alias randomized_mac ipv6 online_min. `inv.weakest_device(n)` covers devices ON that node (rules: see **Weakest device** above; downstream: `behind=True`). Full generated list: `tpctl schema` (`library` lines).
`inv.path(n)` = [n, parent, …, main]. `inv.ranked('path'|'backhaul')` → [(rank, node)] worst first (same as `mesh show --sort`; among equal grades the lower PHY bottleneck ranks worse); `inv.weakest_device(node)` (None when n has no wireless devices of its own); `ranked()` excludes the main router; `inv.devices_on(node)` takes a Node or a name. Grades: `tp.quality.worse(a, b)` → **bool** (a is worse than b), `tp.quality.worst(*gs)` → the worst grade, `rank(g)` → 0 excellent … 4 bad (None → -1).
Also `inv.parent/children/downstream(n)`, `inv.devices_on(name)`, `inv.summary()`, `r.caps.detect()/has()/bands`, `r.health.check()/overview()/verdict()`, `r.status.get()`, `r.wifi.get()`, `r.nat.set_alg(sip=False)`, `r.dhcp.set_lease(1440)`, `r.devices.rename(q, name)` (router-side), `r.mesh.configure(q, name=, location=, led=, uplink=)`, `r.devices.set_alias(q, name)` (local only), `r.mesh.reboot(q)`, `r.mesh.wait()`, `r.snapshots.diff(live=True)`, raw `r.call(ep, op, **f)` / `r.try_call` / `r.apply_form(ep, {...}, tag=…, dry_run=…)`, `r.warnings` (failed reads). Exceptions: `UsageError`, `AuthError`, `RouterError`, `UnsupportedError`.

