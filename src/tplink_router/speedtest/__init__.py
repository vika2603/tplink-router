"""Internet speed test of THIS machine. Router-independent: `from tplink_router import speedtest`.

    res = speedtest.run()                         # auto: speed.cloudflare.com, Apple's servers if rate-limited
    res = speedtest.run("apple", connections=8)   # fill the line: real bufferbloat, any OS
    res.download_mbps, res.upload_mbps, res.latency_ms, res.loaded_latency_down_ms, res.rpm

Engines (all speak to the providers' public test servers directly; no third-party tools needed):
    cloudflare      like speed.cloudflare.com: request-size ladder, one connection by default (~7 s)
    apple           Apple's responsiveness servers (macOS networkQuality's), 8 connections (~14 s)
    ookla           nearest Speedtest.net server, 8 connections (~14 s)
    networkquality  macOS's own networkQuality tool (macOS only)
Pass `progress=callable` to receive Progress updates (see `view.LiveView` for a terminal animation).
"""
from __future__ import annotations

from typing import Callable

from . import apple, cloudflare, networkquality, ookla
from .models import Progress, SpeedResult

ENGINES = ("cloudflare", "apple", "ookla", "networkquality")
DEFAULT_CONNECTIONS = {"cloudflare": 1, "apple": 8, "ookla": 8}


def available(engine: str) -> bool:
    return engine in ("cloudflare", "apple", "ookla") or (engine == "networkquality" and networkquality.available())


def run(engine: str = "auto", progress: Callable[[Progress], None] | None = None, upload: bool = True,
        connections: int | None = None, on_engine: Callable[[str], None] | None = None) -> SpeedResult:
    """Run one engine. auto = cloudflare; if Cloudflare rate-limits this machine before any download sample,
    rerun on Apple's servers (the result's `engine` and `errors` say so). on_engine(name) fires before each run."""
    def one(name: str) -> SpeedResult:
        if on_engine:
            on_engine(name)
        n = connections or DEFAULT_CONNECTIONS.get(name, 1)
        if name == "cloudflare":
            return cloudflare.run(progress, upload=upload, connections=n)
        if name in ("apple", "ookla"):
            return (apple if name == "apple" else ookla).run(progress, upload=upload, connections=n)
        if name == "networkquality":
            return networkquality.run(progress, upload=upload)
        raise ValueError(f"unknown engine {name!r}; choose from {ENGINES}")
    res = one("cloudflare" if engine == "auto" else engine)
    if engine == "auto" and res.rate_limited and res.download_mbps is None:
        res = one("apple")
        res.errors.append("speed.cloudflare.com rate-limited this machine (HTTP 429): measured on Apple's servers")
    return res


__all__ = ["ENGINES", "Progress", "SpeedResult", "available", "run"]
