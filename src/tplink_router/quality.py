"""Turn raw numbers into explicit grades so consumers don't re-derive thresholds.

Grades, best -> worst: excellent, good, fair, poor, bad. `None` = no data.
"""
from __future__ import annotations

GRADES = ("excellent", "good", "fair", "poor", "bad")

# Wi-Fi device / backhaul RSSI (dBm)
SIGNAL = ((-55, "excellent"), (-67, "good"), (-75, "fair"), (-82, "poor"))
WEAK_GRADES = ("poor", "bad")
WEAK_BELOW_DBM = -75      # the single definition of a "weak" device: signal_grade poor|bad == signal < -75 dBm


WEAK_THRESHOLDS = (-75, -82)   # the edges that decide "weak" (poor) and "bad"; other grade edges don't change advice


def near_boundary(dbm, margin_db: int = 2) -> bool:
    """True if a reading is within `margin_db` of the weak (-75) or bad (-82) threshold: whether it counts as weak
    may flip on the next read (±3 dB noise). Edges between excellent/good/fair aren't flagged (same advice)."""
    if not isinstance(dbm, int) or dbm in (-128, 0):
        return False
    return any(abs(dbm - limit) <= margin_db for limit in WEAK_THRESHOLDS)


def is_weak(dbm) -> bool:
    return signal(dbm) in WEAK_GRADES
# satellite backhaul level as reported by the router (0-5)
LEVEL = {5: "excellent", 4: "excellent", 3: "good", 2: "poor", 1: "bad", 0: "bad"}


def signal(dbm: int | None) -> str | None:
    if not isinstance(dbm, int) or dbm in (-128, 0):
        return None
    for limit, grade in SIGNAL:
        if dbm >= limit:
            return grade
    return "bad"


def backhaul(kind: str, dbm: int | None, level: int | None, online: bool = True) -> str | None:
    if not online:
        return "bad"
    if kind == "wired":
        return "excellent"
    if kind != "wifi":
        return None
    by_level = LEVEL.get(level) if isinstance(level, int) else None
    by_dbm = signal(dbm)
    cands = [g for g in (by_level, by_dbm) if g]
    return max(cands, key=GRADES.index) if cands else None   # the worse of the two


def ping_reason(loss_pct: float | None, avg_ms: float | None, jitter_ms: float | None = None) -> str | None:
    """Which rule set the ping grade (same rules as ping())."""
    if loss_pct is None:
        return None
    if loss_pct >= 100:
        return "no reply"
    if loss_pct > 10:
        return f"loss {loss_pct:.0f}% > 10%"
    a = avg_ms or 0
    g = "excellent" if a < 5 else "good" if a < 15 else "fair" if a < 40 else "poor"
    parts = [("avg >= 40 ms" if a >= 40 else "avg 15-40 ms" if a >= 15 else "avg 5-15 ms" if a >= 5 else "avg < 5 ms") + f" ({a:.1f})"]
    if jitter_ms is not None and jitter_ms > 20:
        cap = "fair" if jitter_ms <= 40 else "poor"
        if GRADES.index(cap) > GRADES.index(g):
            parts.append(f"jitter {jitter_ms:.1f} ms > {20 if cap == 'fair' else 40} caps it at {cap}")
            g = cap
        else:
            parts.append(f"jitter {jitter_ms:.1f} ms (already {g})")
    if 0 < loss_pct <= 10 and g in ("excellent", "good"):
        parts.append(f"loss {loss_pct:.0f}% caps it at fair")
    return "; ".join(parts)


def ping(loss_pct: float | None, avg_ms: float | None, jitter_ms: float | None = None) -> str | None:
    if loss_pct is None:
        return None
    if loss_pct >= 100:
        return "bad"
    if loss_pct > 10:
        return "poor"
    g = "excellent" if (avg_ms or 0) < 5 else "good" if avg_ms < 15 else "fair" if avg_ms < 40 else "poor"
    if jitter_ms is not None and jitter_ms > 20:          # spiky latency hurts calls/games even with a fine average
        g = worst(g, "fair" if jitter_ms <= 40 else "poor")
    return "fair" if loss_pct > 0 and g in ("excellent", "good") else g


def rank(grade: str | None) -> int:
    """0 = excellent … 4 = bad; None -> -1. Compare grades with rank(a) > rank(b) ("a is worse")."""
    return GRADES.index(grade) if grade in GRADES else -1


def worse(a: str | None, b: str | None) -> bool:
    """True if grade a is worse than grade b."""
    return rank(a) > rank(b)


def worst(*grades: str | None) -> str | None:
    gs = [g for g in grades if g]
    return max(gs, key=GRADES.index) if gs else None


LEGEND = {
    "signal": "device/backhaul RSSI dBm: excellent >=-55, good >=-67, fair >=-75, poor >=-82, bad <-82. "
              "Instantaneous reading: expect +-3 dB between runs; don't treat a one-grade flip near a boundary as a change",
    "weak": "a weak device = signal_grade poor|bad = signal < -75 dBm. Used by node devices_weak, WEAK_DEVICES, devices list --weak",
    "backhaul": "wired=excellent; wifi = worse of RSSI grade and router level (4-5 excellent, 3 good, 2 poor, 0-1 bad); offline=bad",
    "ping": "from this machine: loss>=100% bad, >10% poor, any loss caps at fair; avg <5ms excellent, <15 good, <40 fair; "
            "jitter (RTT stddev) >20ms caps at fair, >40ms at poor",
}
