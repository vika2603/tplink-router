"""macOS's own `networkQuality` tool (Apple's reference client for the same servers as engine `apple`)."""
from __future__ import annotations

import shutil
import time
from typing import Callable

from .models import Progress, SpeedResult


def available() -> bool:
    return shutil.which("networkQuality") is not None


def run(progress: Callable[[Progress], None] | None = None, upload: bool = True, timeout: float = 90.0) -> SpeedResult:
    """macOS `networkQuality`: saturates the link with several parallel connections (closer to the line's real
    capacity than one connection) and measures responsiveness while it is full: the honest bufferbloat test.
    Apple's own servers; no rate limit. Progress: it only reports at the end, so the callback gets phase/elapsed."""
    import json as _json
    import subprocess
    cb = progress or (lambda p: None)
    res = SpeedResult(engine="networkquality")
    t0 = time.perf_counter()
    cmd = ["networkQuality", "-c", "-s"] + (["-u"] if not upload else [])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while proc.poll() is None:
        el = time.perf_counter() - t0
        phase = "download" if el < 13 or not upload else "upload"      # networkQuality: ~4-12 s per phase
        cb(Progress(phase, int(el), 30 if upload else 14))
        if el > timeout:
            proc.kill()
            res.errors.append("networkQuality timed out")
            break
        time.sleep(0.3)
    out, err = proc.communicate()
    try:
        d = _json.loads(out or "{}")
    except ValueError:
        d = {}
        res.errors.append(f"networkQuality: {(err or out or '').strip()[:160]}")
    if d.get("dl_throughput"):
        res.download_mbps = round(d["dl_throughput"] / 1e6, 1)
    if d.get("ul_throughput"):
        res.upload_mbps = round(d["ul_throughput"] / 1e6, 1)
    res.latency_ms = round(d["base_rtt"], 1) if d.get("base_rtt") else None
    for k, attr in (("dl_responsiveness", "loaded_latency_down_ms"), ("ul_responsiveness", "loaded_latency_up_ms")):
        if d.get(k):
            setattr(res, attr, round(60000 / d[k], 1))      # RPM -> ms per round trip under load
    res.rpm = {"down": round(d["dl_responsiveness"]) if d.get("dl_responsiveness") else None,
               "up": round(d["ul_responsiveness"]) if d.get("ul_responsiveness") else None}
    res.flows = {"down": d.get("dl_flows"), "up": d.get("ul_flows")}
    res.server = {"colo": d.get("test_endpoint"), "city": None, "country": None}
    res.bytes_down, res.bytes_up = int(d.get("dl_bytes_transferred") or 0), int(d.get("ul_bytes_transferred") or 0)
    res.duration_s = round(time.perf_counter() - t0, 1)
    cb(Progress("done"))
    return res


