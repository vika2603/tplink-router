"""Speedtest.net (Ookla) servers, driven directly: no Ookla CLI needed, any OS.

Server list: https://c.speedtest.net/speedtest-servers-static.php (nearest ~10 by GeoIP; the www.speedtest.net
APIs answer 403 to non-browsers). Each server speaks HTTP(S) on :8080 with /hello (latency), /download?size=N and
POST /upload. The one with the lowest latency of the first few is used."""
from __future__ import annotations

import concurrent.futures as cf
import http.client
import re
import time
import urllib.request

from . import saturate
from ._http import UA
from .models import SpeedResult

SERVERS = "https://c.speedtest.net/speedtest-servers-static.php"


def servers(timeout: float = 10.0) -> list[dict]:
    xml = urllib.request.urlopen(urllib.request.Request(SERVERS, headers={"User-Agent": "Mozilla/5.0 " + UA}),
                                 timeout=timeout).read().decode(errors="replace")
    out = []
    for tag in re.findall(r"<server [^>]+>", xml):
        a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if a.get("host"):
            out.append(a)
    return out


def _hello_ms(host: str, port: int, timeout: float = 3.0) -> float | None:
    try:
        c = http.client.HTTPSConnection(host, port, timeout=timeout)
        c.request("GET", "/hello", headers={"User-Agent": UA})
        c.getresponse().read()
        t0 = time.perf_counter()
        c.request("GET", "/hello", headers={"User-Agent": UA})
        c.getresponse().read()
        ms = (time.perf_counter() - t0) * 1000
        c.close()
        return ms
    except (OSError, http.client.HTTPException):
        return None


def run(progress=None, upload: bool = True, connections: int = 8, timeout: float = 10.0) -> SpeedResult:
    res = SpeedResult(engine="ookla")
    try:
        cands = servers(timeout)[:5]
    except (OSError, ValueError) as e:
        res.errors.append(f"ookla server list: {e}")
        return res
    def split(s):
        h, _, p = s["host"].rpartition(":")
        return h, int(p or 8080)
    with cf.ThreadPoolExecutor(5) as ex:
        pings = list(ex.map(lambda s: _hello_ms(*split(s)), cands))
    best = min(((p, s) for p, s in zip(pings, cands) if p is not None), default=None, key=lambda x: x[0])
    if not best:
        res.errors.append("no Speedtest.net server answered")
        return res
    s = best[1]
    host, port = split(s)
    res.client = {"isp": None}
    ep = saturate.Endpoints(host=host, port=port, latency_path="/hello", download_path="/download?size=25000000",
                            upload_path="/upload",
                            server={"colo": s.get("sponsor"), "city": s.get("name"), "country": s.get("cc")})
    return saturate.run(ep, res, progress, upload, flows=max(2, connections), timeout=timeout)
