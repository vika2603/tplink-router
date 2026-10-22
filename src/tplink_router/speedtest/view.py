"""Live terminal view for the speed test (stderr, TTY only): spinner, progress bars, live rate, sparkline."""
from __future__ import annotations

import sys
import threading
import time

from .. import human as h
from .models import Progress, SpeedResult

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BARS = "⡀⣀⣄⣤⣦⣶⣷⣿"         # braille: light sparkline that doesn't fill the whole cell like ▁..█ in many fonts
ROWS = ("latency", "download", "upload")
LABEL = {"latency": "Latency", "download": "Download", "upload": "Upload"}


def spark(xs: list[float], width: int = 18) -> str:
    xs = [x for x in xs if x is not None][-width:]
    if not xs:
        return ""
    lo, hi = min(xs), max(xs)
    return "".join(BARS[0 if hi == lo else int((x - lo) / (hi - lo) * (len(BARS) - 1))] for x in xs)


def bar(frac: float, width: int = 24) -> str:
    """Thin line gauge: ━ done, ─ (dim) remaining. One row high in any font."""
    frac = max(0.0, min(1.0, frac))
    full = round(frac * width)
    return h.cyan("━" * full) + h.dim("─" * (width - full))


def fmt_rate(mbps: float | None) -> str:
    if mbps is None:
        return "—"
    return f"{mbps / 1000:.2f} Gbps" if mbps >= 1000 else f"{mbps:.0f} Mbps" if mbps >= 100 else f"{mbps:.1f} Mbps"


class LiveView:
    """Feed Progress objects to update(); a background thread redraws ~10x/s so the spinner keeps moving."""

    def __init__(self, upload: bool = True, stream=sys.stderr, engine: str = "cloudflare"):
        self.out = stream
        self.engine = engine
        self.indeterminate = engine == "networkquality"
        self.rows = {r: Progress(r) for r in ROWS if (upload or r != "upload") and not (self.indeterminate and r == "latency")}
        self.active = "meta"
        self.t0 = time.time()
        self.tick = 0
        self.drawn = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.th = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self.out.write("\x1b[?25l")               # hide cursor
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.th.join(timeout=1)
        with self.lock:
            self._clear()
        self.out.write("\x1b[?25h")
        self.out.flush()

    def set_engine(self, engine: str) -> None:
        """Called when `auto` switches engine mid-run (e.g. Cloudflare rate-limited): reset the rows."""
        with self.lock:
            self.engine, self.indeterminate = engine, engine == "networkquality"
            self.rows = {r: Progress(r) for r in self.rows}
            self.active, self.t0 = "meta", time.time()

    def update(self, p: Progress) -> None:
        with self.lock:
            self.active = p.phase
            if p.phase in self.rows:
                self.rows[p.phase] = p

    def _loop(self):
        while not self.stop.is_set():
            with self.lock:
                self._draw()
            self.tick += 1
            self.stop.wait(0.1)

    def _clear(self):
        if self.drawn:
            self.out.write(f"\x1b[{self.drawn}F\x1b[J")
            self.drawn = 0

    def _line(self, name: str, p: Progress) -> str:
        order = list(self.rows)
        act = self.active if self.active in order else ("latency" if self.active == "meta" else None)
        state = "active" if name == act else ("done" if act is None or (act in order and order.index(name) < order.index(act)) else "wait")
        icon = h.cyan(SPIN[self.tick % len(SPIN)]) if state == "active" else h.green("✓") if state == "done" else h.dim("·")
        label = f"{LABEL[name]:<9}"
        if state == "wait":
            return f" {icon} {h.dim(label)} {h.dim('waiting')}"
        frac = p.done / p.total if p.total else 0.0
        if self.indeterminate and state == "active":     # engine without per-request progress: moving block
            w, pos = 24, self.tick % 24
            blk = "".join(h.cyan("━") if (i - pos) % w < 5 else h.dim("─") for i in range(w))
            return f" {icon} {h.bold(label)} {blk} {h.dim(f'{time.time() - self.t0:5.1f}s')}"
        if name == "latency":
            vals = p.samples
            now = f"{sorted(vals)[len(vals) // 2]:.1f} ms" if vals else "…"
            return f" {icon} {h.bold(label)} {bar(frac)} {h.bold(now.rjust(10))}  {h.dim(spark(vals))}"
        from ._http import p90
        rate = p90(p.samples) if state == "done" else p.current_mbps
        size = f"{p.size / 1e6:g} MB" if p.size and p.size >= 1e6 else f"{p.size // 1000} kB" if p.size else ""
        return (f" {icon} {h.bold(label)} {bar(frac)} {h.bold(fmt_rate(rate).rjust(10))}  {h.cyan(spark(p.samples))}"
                + (h.dim(f"  {size}") if state == "active" and size else ""))

    def _draw(self):
        lines = [h.dim(f"  {provider(self.engine)} · {time.time() - self.t0:4.1f}s"), *(self._line(n, p) for n, p in self.rows.items())]
        self._clear()
        self.out.write("\n".join(lines) + "\n")
        self.out.flush()
        self.drawn = len(lines)


def provider(engine: str, flows: int | None = None) -> str:
    """The same header text in the live view and the final view."""
    base = {"apple": "Apple responsiveness servers", "networkquality": "macOS networkQuality",
            "ookla": "Speedtest.net (Ookla)"}.get(engine, "speed.cloudflare.com")
    if engine in ("apple", "ookla") or (flows and flows > 1):
        base += " · parallel connections"
    return base


def summary(r: SpeedResult, via: str) -> list[str]:
    """Final view = the live view's last frame (same header, rows, bars, sparklines), then the extra lines aligned
    under the same columns, so the screen doesn't change shape when the test ends."""
    def grade_lat(ms):
        return "excellent" if ms < 15 else "good" if ms < 40 else "fair" if ms < 80 else "poor"
    flows = (r.flows or {}).get("down")
    out = [h.dim(f"  {provider(r.engine, flows)} · {r.duration_s:4.1f}s · done")]
    rows = [("Latency", f"{r.latency_ms:.1f} ms" if r.latency_ms is not None else None, r.latency_samples, False),
            ("Download", fmt_rate(r.download_mbps) if r.download_mbps is not None else None, r.download_samples, True),
            ("Upload", fmt_rate(r.upload_mbps) if r.upload_mbps is not None else None, r.upload_samples, True)]
    for name, val, samples, cyan in rows:
        if val is None:
            if name == "Upload" and not r.upload_samples and r.download_mbps is not None:
                out.append(f" {h.dim('·')} {h.dim(name.ljust(9))} {h.dim('skipped')}")
            continue
        sp = spark(samples)
        out.append(f" {h.green('✓')} {h.bold(name.ljust(9))} {bar(1.0)} {h.bold(val.rjust(10))}  "
                   f"{h.cyan(sp) if cyan else h.dim(sp)}")
    pad = "   "
    detail = lambda k, v: f"{pad}{h.dim(k.ljust(9))} {v}"
    if r.latency_ms is not None:
        bits = []
        if r.loaded_latency_down_ms is not None or r.loaded_latency_up_ms is not None:
            worst = max(x for x in (r.loaded_latency_down_ms, r.loaded_latency_up_ms) if x is not None)
            bits.append(h.grade(grade_lat(worst), f"{r.loaded_latency_down_ms or 0:.0f} ms down / "
                                                  f"{r.loaded_latency_up_ms or 0:.0f} ms up"))
        if r.jitter_ms is not None:
            bits.append(f"jitter {r.jitter_ms:.1f} ms idle")
        if bits:
            out.append(detail("Loaded", h.dim(" · ").join(bits)))
    loaded = [x for x in (r.loaded_latency_down_ms, r.loaded_latency_up_ms) if x is not None]
    if loaded and r.latency_ms is not None:
        bloat = max(0.0, max(loaded) - r.latency_ms)
        g, verdict = (("excellent", "none: calls and games stay smooth during big transfers") if bloat < 30 else
                      ("fair", "moderate: latency rises while something big transfers") if bloat < 100 else
                      ("poor", "heavy: calls/games lag whenever anything downloads (QoS helps)"))
        if r.engine == "cloudflare" and (flows or 1) <= 1:
            verdict += " (one connection: --engine apple to be sure)"
        out.append(detail("Bloat", h.grade(g, f"+{bloat:.0f} ms") + h.dim(" — " + verdict)))
    if r.engine in ("apple", "networkquality") and r.rpm:
        out.append(detail("RPM", h.dim(f"{r.rpm.get('down') or '?'} down / {r.rpm.get('up') or '?'} up "
                                       f"(round trips per minute under load; higher is better)")))
    srv = (f"{r.server.get('city')} ({r.server.get('colo')})" if r.server.get("city") else r.server.get("colo")) or "?"
    where = " · ".join(x for x in (srv, r.client.get("isp") and f"ISP {r.client['isp']}", f"this machine {via}") if x)
    out.append(detail("Server", h.dim(where)))
    out.append(detail("Data", h.dim(f"{(r.bytes_down + r.bytes_up) / 1e6:.0f} MB")))
    if r.rate_limited and r.engine == "cloudflare":
        out.append(pad + h.yellow("partial: speed.cloudflare.com rate-limited this machine (HTTP 429): fewer samples. "
                                  "The web page avoids this with a human check; here: retry in ~10 min or --engine apple"))
    return out
