"""Typed results. Every model is a dataclass; `to_dict()` gives JSON-safe data."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from .errors import UsageError


def to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses/containers to JSON-safe primitives (unknown types -> str)."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_dict(v) for k, v in asdict(obj).items() if k != "raw"}
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_dict(x) for x in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


@dataclass
class PingResult:
    host: str
    loss_pct: float
    avg_ms: float | None = None
    max_ms: float | None = None
    jitter_ms: float | None = None    # stddev of RTT

    @property
    def up(self) -> bool:
        return self.loss_pct < 100


@dataclass
class Node:
    name: str
    role: str                      # main_router | satellite_router
    model: str
    ip: str
    mac: str
    status: str                    # connected | disconnected
    parent_mac: str | None = None
    parent_name: str | None = None
    depth: int = 0                 # wireless/wired hops from main router
    backhaul: str = "main"         # main | wifi | wired
    backhaul_dbm: int | None = None
    backhaul_level: int | None = None  # 0-5
    backhaul_mbps: int | None = None
    backhaul_grade: str | None = None   # quality.backhaul()
    backhaul_band: str | None = None    # radio carrying the wireless backhaul: 5G | 2.4G (None for main/wired)
    path_grade: str | None = None       # worst backhaul grade from this node up to the main router (incl. its own)
    path_link_mbps: int | None = None   # bottleneck: lowest backhaul PHY rate on that path
    uplink_pref: str | None = None
    uplink_choices: list = field(default_factory=list)
    devices: int = 0               # devices we could attribute to this node
    device_bands: dict = field(default_factory=dict)        # {"5G": 7, "2.4G": 1}
    device_signal_worst: int | None = None                  # dBm, wireless devices only
    device_signal_median: int | None = None
    devices_behind: int = 0        # == len(inv.devices_behind(node)): devices on this node + all nodes downstream
    devices_weak: int = 0          # wireless devices with signal_grade poor|bad (< -75 dBm), see quality.is_weak
    weakest_device: str | None = None   # label of the lowest-signal wireless device with a fresh reading (== inv.weakest_device)
    weakest_signal_dbm: int | None = None
    devices_weak_stale: int = 0    # of devices_weak, those whose reading may be stale
    weakest_stale: bool = False    # the weakest_device reading itself may be stale
    weakest_behind: str | None = None   # no devices of its own: weakest device downstream (label), its dBm next
    weakest_behind_dbm: int | None = None
    weakest_excluded: str | None = None
    status_flaps_30m: int = 0
    last_status_change_at: str | None = None
    path_bottleneck: str | None = None   # node whose backhaul sets path_link_mbps      # connected<->disconnected changes in the last 30 min (event log)   # a weaker just-roamed device the weakest_device rule passed over
    reboot_supported: bool | None = None
    ping: PingResult | None = None
    ping_grade: str | None = None
    raw: dict = field(default_factory=dict, repr=False)  # list entry + mesh_sclient_detail (excluded from to_dict)

    @property
    def is_main(self) -> bool:
        return self.role == "main_router"

    @property
    def support_reboot(self) -> bool | None:   # backwards-compatible alias
        return self.reboot_supported

    # record key names, as aliases of the library attributes (same values, ints)
    backhaul_signal = property(lambda self: self.backhaul_dbm)
    backhaul_link = property(lambda self: self.backhaul_mbps)
    path_link = property(lambda self: self.path_link_mbps)
    weakest_signal = property(lambda self: self.weakest_signal_dbm)

    @property
    def uplink(self) -> str | None:
        """Same as the record key `uplink` (alias of parent_name)."""
        return self.parent_name

    @property
    def online(self) -> bool:
        return self.status == "connected" and self.ip != "0.0.0.0" and (self.ping is None or self.ping.up)


@dataclass
class Device:
    label: str                     # unique display name: alias, else name (+ "#<mac tail>" when the name is shared)
    name: str                      # name reported by the router (often generic, e.g. "iPhone")
    ip: str
    mac: str
    band: str                      # 2.4G | 5G | 6G | wired
    node: str                      # mesh node name it is attached to
    signal_dbm: int | None = None
    signal_grade: str | None = None
    link_tx_mbps: int = 0          # PHY link rate as reported by the router, NOT throughput
    link_rx_mbps: int = 0
    path_grade: str | None = None  # worst backhaul grade on the way from its node to the main router (None on main)
    weakest_hop: str | None = None # node whose backhaul set path_grade
    alias: str | None = None       # user-assigned (~/.config/tplink-router/aliases.json)
    randomized_mac: bool = False   # locally-administered MAC (iOS/Android private address) -> name/MAC may change
    ipv6: list = field(default_factory=list)
    ssid: str | None = None
    type: str | None = None
    guest: bool = False
    online_min: int = 0
    just_roamed: bool = False      # on a different node than at the previous observation: its signal may still settle
    moves_1h: int = 0              # node/band moves in the last hour (event log)
    last_move_at: str | None = None   # time of its latest logged move in that hour
    signal_placeholder: bool = False  # satellite-reported exactly -90 dBm: the router's stand-in when it has no fresh RSSI

    signal = property(lambda self: self.signal_dbm)          # record key names as aliases
    link_rx = property(lambda self: self.link_rx_mbps)
    link_tx = property(lambda self: self.link_tx_mbps)

    @property
    def near_boundary(self) -> bool:
        """Signal within 2 dB of the weak (-75) or bad (-82) threshold: the grade may flip on the next read."""
        from . import quality
        return self.band != "wired" and quality.near_boundary(self.signal_dbm)

    @property
    def signal_stale(self) -> bool:
        """Wireless signal that may be an old reading: no recent traffic sample (both PHY rates 0), or an implausible
        value (<= -95 dBm: a device that weak is leaving or already gone, the router keeps its last RSSI)."""
        if self.band == "wired":
            return False
        implausible = (isinstance(self.signal_dbm, int) and self.signal_dbm < -85
                       and max(self.link_rx_mbps or 0, self.link_tx_mbps or 0) > 500)   # far too fast for that signal
        return ((not self.link_rx_mbps and not self.link_tx_mbps) or self.signal_placeholder or implausible
                or (isinstance(self.signal_dbm, int) and self.signal_dbm <= -95))

    @property
    def backhaul_limited(self) -> bool:
        """The path to the main router is graded worse than the device's own signal: fixing the device's position
        won't help, the satellite backhaul is the bottleneck."""
        from . import quality
        if self.path_grade not in ("fair", "poor", "bad"):
            return False
        if self.signal_grade is None:                    # no usable reading of its own signal: can't compare
            return self.band == "wired"
        return quality.rank(self.path_grade) > quality.rank(self.signal_grade)


Client = Device   # backwards-compatible name (pre-0.4)


@dataclass
class Issue:
    level: str                     # error | warn | info
    code: str
    subject: str
    detail: str                    # human sentence
    hint: str
    data: dict = field(default_factory=dict)   # machine fields (flat scalars/lists) emitted as first-class record keys


_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}


def _same(a, b) -> bool:
    sa, sb = str(a).strip().lower(), str(b).strip().lower()
    if sa == sb:
        return True
    return (sa in _TRUE and sb in _TRUE) or (sa in _FALSE and sb in _FALSE)


@dataclass
class Change:
    key: str
    old: Any
    new: Any
    target: Any = None             # requested value (writes); None for plain diffs

    @property
    def changed(self) -> bool:
        return not _same(self.old, self.new)

    @property
    def verified(self) -> bool:
        """After a write: router now reports the requested value. Switch vocabularies are equivalent: the router may
        read back JSON true/false for a field written as yes/no or on/off."""
        return self.target is None or _same(self.new, self.target)


@dataclass
class WriteResult:
    """Outcome of a write. applied=False when dry_run or when every target already matched (no-op skip)."""
    changes: list[Change]
    snapshot: str | None = None
    side_effect: str | None = None
    next: str | None = None
    dry_run: bool = False
    applied: bool = True
    skipped_reason: str | None = None

    @property
    def any_changed(self) -> bool:
        return any(c.changed for c in self.changes)

    @property
    def mismatches(self) -> list[Change]:
        """Applied but the re-read value differs from the target (exit 7 in the CLI)."""
        return [c for c in self.changes if self.applied and not self.dry_run and not c.verified]


@dataclass
class Inventory:
    nodes: list[Node]
    devices: list[Device]

    def device(self, query: str) -> Device:
        """Unique device by label/alias/name (case-insensitive exact first, then substring), IP, or MAC."""
        q, mac = query.lower(), query.upper().replace(":", "-")
        exact = [c for c in self.devices if q in (c.label.lower(), (c.alias or "").lower(), c.name.lower()) or query == c.ip or mac == c.mac]
        hit = exact or [c for c in self.devices if q in c.label.lower() or mac in c.mac]
        if len(hit) != 1:
            many = len(hit) > 1
            raise UsageError(f"device '{query}' is ambiguous ({len(hit)} matches)" if many else f"no device matches '{query}'",
                             query, [c.label for c in (hit if many else self.devices)], "device")
        return hit[0]

    def node(self, query: str) -> Node:
        """Match by name substring, IP, or MAC; must be unique."""
        q = query.upper().replace(":", "-")
        hit = [n for n in self.nodes if query in n.name or query == n.ip or q == n.mac]
        if len(hit) != 1:
            many = len(hit) > 1
            raise UsageError(f"node '{query}' is ambiguous ({len(hit)} matches)" if many else f"no mesh node matches '{query}'",
                             query, [n.name for n in (hit if many else self.nodes)], "node")
        return hit[0]

    @property
    def clients(self) -> list[Device]:          # backwards-compatible name
        return self.devices

    def nodes_matching(self, query: str) -> list[Node]:
        """All nodes whose name contains query (or equal IP/MAC). Never raises; may be empty."""
        q = query.upper().replace(":", "-")
        return [n for n in self.nodes if query in n.name or query == n.ip or q == n.mac]

    def devices_matching(self, query: str) -> list[Device]:
        q, mac = query.lower(), query.upper().replace(":", "-")
        return [c for c in self.devices if q in c.label.lower() or q in c.name.lower() or query == c.ip or mac in c.mac]

    def parent(self, n: Node) -> Node | None:
        return next((m for m in self.nodes if m.mac == n.parent_mac), None)

    def children(self, n: Node) -> list[Node]:
        return [m for m in self.nodes if m.parent_mac == n.mac]

    def path(self, n: Node) -> list[Node]:
        """[n, parent, ..., main] — the backhaul chain traffic from n takes."""
        out, cur = [n], n
        while (p := self.parent(cur)) and p not in out:
            out.append(p)
            cur = p
        return out

    def downstream(self, n: Node) -> list[Node]:
        """All nodes whose backhaul goes through n (excluding n)."""
        out, todo = [], self.children(n)
        while todo:
            m = todo.pop()
            out.append(m)
            todo += self.children(m)
        return out

    def devices_behind(self, n: Node) -> list[Device]:
        """Devices on n or on any node downstream of it (everyone affected if n's backhaul is bad)."""
        names = {n.name} | {m.name for m in self.downstream(n)}
        return [c for c in self.devices if c.node in names]

    @property
    def main(self) -> Node:
        return next(n for n in self.nodes if n.is_main)

    def devices_on(self, node: "Node | str") -> list[Device]:
        """Devices attached to a node (pass the Node or its name)."""
        name = node.name if isinstance(node, Node) else node
        return [c for c in self.devices if c.node == name]

    def weakest_device(self, node: "Node | str", include_stale: bool = False, behind: bool = False,
                       weak_only: bool = False) -> Device | None:
        """Lowest-signal wireless device on a node with a fresh reading (devices whose signal may be stale, i.e.
        `signal_stale`, are skipped unless include_stale=True or they are the only ones). None if it has none."""
        if behind:                          # on the node or on any node downstream of it
            n = node if isinstance(node, Node) else self.node(node)
            pool = self.devices_behind(n)
        else:
            pool = self.devices_on(node)
        cs = [c for c in pool if isinstance(c.signal_dbm, int) and c.band != "wired"
              and (not weak_only or c.signal_grade in ("poor", "bad"))]        # weak_only: like the CLI field
        if include_stale:
            fresh = cs
        else:
            fresh = [c for c in cs if not c.signal_stale] or cs
            settled = [c for c in fresh if not c.just_roamed]
            if settled:                     # a just-roamed reading counts only if no settled device is weak
                w = min(settled, key=lambda c: (c.signal_dbm, c.label.lower()))
                if w.signal_grade in ("poor", "bad") or not any(c.signal_grade in ("poor", "bad") for c in fresh):
                    return w
        return min(fresh, key=lambda c: (c.signal_dbm, c.label.lower())) if fresh else None

    def ranked(self, by: str = "path") -> list[tuple[int, Node]]:
        """Satellites worst first as (rank, node). by = path | backhaul | health (path + devices on the node). Ties: weaker signal on the path, then the node's
        own hop (the weak hop ranks above nodes behind it), then lower PHY bottleneck (PHY swings +-30-50% between reads, signal ~3 dB), then name, so the order is stable.
        Same order as `tpctl mesh show --sort <by>`."""
        from . import quality as q
        key = {"backhaul": lambda n: (-q.rank(n.backhaul_grade), n.backhaul_dbm or 0, n.backhaul_mbps or 0, n.name),
               "path": lambda n: (-q.rank(n.path_grade), min((m.backhaul_dbm or 0) for m in self.path(n) if not m.is_main)
                                  if not n.is_main else 0, n.backhaul_dbm or 0, n.path_link_mbps or 0, n.name),
               # overall: the worse of the backhaul path and the devices on the node, then how many devices are weak
               "health": lambda n: (-max(q.rank(n.path_grade), q.rank(q.signal(n.weakest_signal_dbm)) if n.weakest_signal_dbm else -1),
                                    -q.rank(n.backhaul_grade),                  # its own weak hop = a root cause: first
                                    -sum(1 for d in self.devices_behind(n) if d.signal_grade in q.WEAK_GRADES),
                                    n.weakest_signal_dbm or 0, n.name)}[by]
        return list(enumerate(sorted((n for n in self.nodes if not n.is_main), key=key), start=1))

    def summary(self) -> dict:
        by_node = {n.name: 0 for n in self.nodes} | dict(Counter(c.node for c in self.devices))
        return {"total": len(self.devices), "by_band": dict(Counter(c.band for c in self.devices)), "by_node": by_node}
