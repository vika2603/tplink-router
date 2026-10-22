from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import UsageError
from ..models import Change
from . import Service

NOISY = re.compile(r"current_channel|\.(time|date|day)$|_error|uptime|\.capability\.")


def _flat(o, pre=""):
    if isinstance(o, dict) and not o and pre.endswith("list."):
        yield pre[:-1], "[]"                 # list forms answer {} when empty: same as an empty row list
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from _flat(v, f"{pre}{k}.")
    elif isinstance(o, list):
        yield pre[:-1], json.dumps(o, ensure_ascii=False)
    else:
        yield pre[:-1], o


# router field -> the name plans/`wifi show` use, so one key means one thing everywhere
FIELD_ALIASES = {"htmode": "width", "channel": "channel_setting", "smart_enable": "", "leasetime": "lease"}


def _short(key: str) -> str:
    """Router form keys -> CLI names: 'admin/wireless?form=wireless_5g.ssid' -> '5g.ssid';
    'admin/wireless?form=smart_connect.smart_enable' -> 'smart_connect'; 'admin/dhcps?form=setting.leasetime' -> 'dhcp.lease';
    'admin/nat?form=alg.sip' -> 'alg.sip'."""
    k = key.removeprefix("admin/")
    mod, _, rest = k.partition("?form=")
    if not rest:
        return k
    form, _, field = rest.partition(".")
    field = FIELD_ALIASES.get(field, field)
    if form.startswith("wireless_"):
        head = form.removeprefix("wireless_")                  # 5g, 2g, 6g, addition_setting …
    elif mod == "nat" and form == "alg":
        head = "alg"
    elif mod == "dhcps" and form == "setting":
        head = "dhcp"
    elif form == mod or form.startswith(mod) or mod == "wireless":
        head = form                                            # wireless: smart_connect, ofdma_mimo, guest …
    else:
        head = f"{mod}.{form}"
    return f"{head}.{field}" if field else head


def diff_dicts(a: dict, b: dict, include_noisy: bool = False) -> list[Change]:
    """Real changes only: a key missing on one side (endpoint/field not captured in the older snapshot) is not a
    change unless include_noisy. Volatile keys (current channel, clock) are skipped unless include_noisy."""
    fa, fb = dict(_flat(a)), dict(_flat(b))
    out = []
    for k in sorted(set(fa) | set(fb)):
        if not include_noisy and (NOISY.search(k) or k not in fa or k not in fb):
            continue
        if fa.get(k) != fb.get(k):
            rows = _list_diff(_short(k), fa.get(k), fb.get(k))
            out.extend([Change(_short(k), fa.get(k), fb.get(k))] if rows is None else rows)
    return out


def _list_diff(key: str, old, new) -> list[Change] | None:
    """Lists of rows (reservations, forwards, rules): one change per added/removed/edited row, keyed by
    mac/name/key, instead of the whole list twice. None when the values aren't row lists; [] when the rows only
    differ in UI row ids (`key`/`index`, which the router sometimes stores after a web-UI-style edit)."""
    def rows(v):
        if isinstance(v, str) and v.startswith("["):
            try:
                v = json.loads(v)
            except ValueError:
                return None
        return v if isinstance(v, list) and all(isinstance(x, dict) for x in v) else None
    a, b = rows(old), rows(new)
    if a is None or b is None:
        return None
    if not a and not b:
        return []
    idf = next((f for f in ("mac", "name", "key", "ipaddr") if all(f in x for x in a + b)), None)
    if not idf:
        return None
    ma, mb = {str(x[idf]): x for x in a}, {str(x[idf]): x for x in b}
    label = lambda x: " ".join(f"{k}={v}" for k, v in x.items() if k not in ("key", "index") and v not in ("", None))[:120]
    out = []
    for i in sorted(set(ma) | set(mb)):
        x, y = ma.get(i), mb.get(i)
        if x is None:
            out.append(Change(f"{key}[{i}]", "absent", label(y)))
        elif y is None:
            out.append(Change(f"{key}[{i}]", label(x), "absent"))
        else:
            # fields that changed identically (a rename updates comment + hostname) = one row
            grouped: dict[tuple, list[str]] = {}
            for f in sorted(set(x) | set(y)):
                if f not in ("key", "index") and x.get(f) != y.get(f):
                    grouped.setdefault((str(x.get(f)), str(y.get(f))), []).append(f)
            for (ov, nv), fs in grouped.items():
                name = "name" if set(fs) == {"comment", "hostname"} else "+".join(fs)
                out.append(Change(f"{key}[{i}].{name}", x.get(fs[0]), y.get(fs[0])))
    return out


def diff_files(old: Path, new: Path, include_noisy: bool = False) -> list[Change]:
    return diff_dicts(json.loads(Path(old).read_text()), json.loads(Path(new).read_text()), include_noisy)


UNITS = {"dhcp.lease": "min", "2g.width": "MHz", "5g.width": "MHz", "6g.width": "MHz"}


KEY_NOTES = {"mu_mimo": "legacy per-band field; the effective switch on Wi-Fi 7 models is wifi ofdma_mimo",
             "ofdma.enable": "legacy field; the effective switch is wifi ofdma_mimo"}


def key_note(key: str) -> str | None:
    return next((v for k, v in KEY_NOTES.items() if key.endswith(k) or f".{k}" in key), None)


def _taken(p: Path) -> str:
    n = p.stem
    return f"{n[:4]}-{n[4:6]}-{n[6:8]} {n[9:11]}:{n[11:13]}" if n[:8].isdigit() else n


class SnapshotService(Service):
    def timeline(self, since_minutes: int | None = None, key: str | None = None, include_noisy: bool = False) -> list[dict]:
        """When did each setting change? Walk consecutive snapshots (+ live): every change gets the window
        after=<older snapshot time> before=<newer snapshot time | now>."""
        import datetime as dt
        snaps = self.list(10000)
        if since_minutes is not None:
            cut = dt.datetime.now() - dt.timedelta(minutes=since_minutes)
            older = [f for f in snaps if dt.datetime.strptime(f.stem[:15], "%Y%m%d-%H%M%S") < cut]
            snaps = ([older[-1]] if older else []) + [f for f in snaps if f not in older]
        states = [(_taken(f), f.stem[16:], json.loads(f.read_text())) for f in snaps] + [("now", None, self.r.capture())]
        out = []
        for (t0, tag0, a), (t1, _, b) in zip(states, states[1:]):
            # a snapshot tagged by a tpctl write is taken right before that write: the change happened at ~t0
            by_tpctl = tag0 not in (None, "", "manual", "auto-daily", "diff")
            for c in diff_dicts(a, b, include_noisy):
                if key and key not in c.key:
                    continue
                unit = UNITS.get(c.key)
                ctx = None
                if c.key.endswith(".ipaddr"):            # an address alone misleads: say whether its switch is on
                    sib = c.key[: -len(".ipaddr")] + ".enable"
                    flat = {_short(k): v for k, v in _flat(states[-1][2])}      # the switch as it is NOW
                    if sib in flat:
                        ctx = f"{sib}={flat[sib]} now"
                what = None
                if "reservation.list[" in c.key and c.key.endswith(".name"):
                    what = f"device renamed (`devices rename` updates the DHCP reservation name too): {c.old} -> {c.new}"
                out.append({"key": c.key, "what": what, "old": c.old, "new": c.new, "unit": unit, "note": key_note(c.key), "context": ctx,
                            "after": None if by_tpctl else t0, "before": None if by_tpctl else t1,
                            "reverted": None,
                            "likely": t0 if by_tpctl else None, "by": "tpctl" if by_tpctl else "unknown (web UI/app/other)",
                            "basis": f"snapshot at {t0} was taken right before a tpctl '{tag0}' write" if by_tpctl
                            else "no tpctl write recorded in this window: happened somewhere between after and before"})
        # mark keys that ended where they started (net zero within the window)
        first, last = {}, {}
        for row in out:
            first.setdefault(row["key"], row["old"])
            last[row["key"]] = row["new"]
        for row in out:
            if str(first[row["key"]]) == str(last[row["key"]]):
                row["reverted"] = "yes"
        return out


    def save(self, tag: str = "manual") -> Path:
        return self.r.snapshot(tag)

    def list(self, last: int = 20) -> list[Path]:
        return sorted(self.r.config.snapshot_dir.glob("*.json"))[-last:]

    def resolve(self, name: str) -> Path:
        """Accept a path, a file name, or a unique substring of a snapshot name (as printed by `snapshot list`)."""
        p = Path(name).expanduser()
        if p.exists():
            return p
        hits = [f for f in self.list(10000) if name in f.name]
        if len(hits) != 1:
            raise UsageError(f"snapshot '{name}' matched {len(hits)}", name, [f.name for f in self.list(30)], "snapshot")
        return hits[0]

    def diff(self, old: str | Path | None = None, new: str | Path | None = None, last_two: bool = False,
             include_noisy: bool = False, since_minutes: int | None = None) -> tuple[str, str, list[Change]]:
        """Default: latest snapshot vs live router (in memory, nothing written). last_two: the two newest snapshots.
        old/new: paths, names or unique substrings. Returns (old_label, new_label, changes)."""
        snaps = self.list(10000)
        if old and new:
            a, b = self.resolve(str(old)), self.resolve(str(new))
            return a.name, b.name, diff_files(a, b, include_noisy)
        if old:
            a = self.resolve(str(old))
            return a.name, "live", diff_dicts(json.loads(a.read_text()), self.r.capture(), include_noisy)
        if since_minutes is not None:
            import datetime as dt
            cut = dt.datetime.now() - dt.timedelta(minutes=since_minutes)
            inwin = [f for f in snaps if dt.datetime.strptime(f.stem[:15], "%Y%m%d-%H%M%S") >= cut]
            base = inwin[0] if inwin else (snaps[-1] if snaps else None)
            if base is None:
                raise UsageError("no snapshots yet")
            return base.name, "live", diff_dicts(json.loads(base.read_text()), self.r.capture(), include_noisy)
        if last_two:
            if len(snaps) < 2:
                raise UsageError("need two snapshots for last_two")
            return snaps[-2].name, snaps[-1].name, diff_files(snaps[-2], snaps[-1], include_noisy)
        if not snaps:
            raise UsageError("no snapshots yet (one is taken automatically before every write; or `tpctl snapshot save`)")
        return snaps[-1].name, "live", diff_dicts(json.loads(snaps[-1].read_text()), self.r.capture(), include_noisy)
