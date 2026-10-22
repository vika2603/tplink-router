"""speed.cloudflare.com, measured the way its web page measures: idle latency + jitter, then download and upload
with a growing request-size ladder (100 kB -> 25 MB; a size is skipped once a request takes >= 1 s), throughput =
90th percentile of per-request rates, and latency on a second connection while data flows (loaded latency).

Anonymous clients get a small request quota: the web page sends a Turnstile human-check token
(Authorization: Bearer) that we can't and don't obtain, so heavy use answers HTTP 429. `rate_limited` says so."""
from __future__ import annotations

import http.client
import statistics
import threading
import time
from typing import Callable

from ._http import HOST, UA, Conn, http_latency_ms, jitter, latency_ms, p90, server_ms, tcp_rtt_ms
from .models import Progress, SpeedResult

DOWN_STAGES = [(100_000, 6), (1_000_000, 5), (10_000_000, 4), (25_000_000, 3)]
UP_STAGES = [(100_000, 5), (1_000_000, 4), (10_000_000, 3), (25_000_000, 2)]
LATENCY_N = 12           # keep the request count modest: anonymous clients are rate-limited (HTTP 429)
FINISH_S = 1.0           # a request this long means bigger sizes would only add time
MIN_DURATION_S = 0.01    # shorter transfers are too noisy to rate


def run(progress: Callable[[Progress], None] | None = None, upload: bool = True, timeout: float = 15.0,
        max_seconds: float = 45.0, connections: int = 1) -> SpeedResult:
    """Run the whole test. `progress` is called after every request (cheap; drive an animation from it)."""
    cb = progress or (lambda p: None)
    res = SpeedResult()
    connections = max(1, min(int(connections), 8))
    if connections > 1:
        res.flows = {"down": connections, "up": connections}
    start = time.perf_counter()
    conn = Conn(timeout)
    pool = [conn] + [Conn(timeout) for _ in range(connections - 1)]

    cb(Progress("meta"))
    try:                                       # where we measure to (needs Origin/Referer, like the web page sends)
        c = http.client.HTTPSConnection(HOST, timeout=timeout)
        c.request("GET", "/meta", headers={"User-Agent": UA, "Origin": f"https://{HOST}", "Referer": f"https://{HOST}/"})
        import json
        m = json.loads(c.getresponse().read() or b"{}")
        c.close()
        colo = m.get("colo") if isinstance(m.get("colo"), dict) else {"iata": m.get("colo")}
        res.server = {"colo": colo.get("iata"), "city": colo.get("city"), "country": colo.get("cca2") or colo.get("country")}
        res.client = {"ip": m.get("clientIp"), "asn": m.get("asn"), "isp": m.get("asOrganization"), "city": m.get("city")}
    except (OSError, ValueError, http.client.HTTPException) as e:
        res.errors.append(f"meta: {e}")

    # -- idle latency on a warm connection; the first response also says where we measure to --
    lat: list[float] = []
    http_lat: list[float] = []
    n_lat = LATENCY_N
    for i in range(n_lat):
        try:
            ttfb, _, _, resp = conn.request("GET", "/__down?bytes=0")
        except OSError as e:
            res.errors.append(f"latency: {e}")
            res.rate_limited |= "429" in str(e)
            break
        if i:                                  # the first request pays connection setup
            lat.append(latency_ms(ttfb, resp))
            http_lat.append(http_latency_ms(ttfb, resp))
        cb(Progress("latency", i + 1, n_lat, samples=list(lat)))
    if lat:
        res.latency_ms = round(statistics.median(lat), 1)
        res.jitter_ms = round(jitter(lat) or 0.0, 1)
        res.latency_samples = list(lat)              # raw: the final sparkline must match the live one       # from TCP RTTs: server-time noise would dominate

    def loaded_probe(stop: threading.Event, out: list[float]):
        pc = Conn(timeout)
        while not stop.is_set():
            try:
                ttfb, _, _, resp = pc.request("GET", "/__down?bytes=0")
                out.append(latency_ms(ttfb, resp))
            except OSError:
                pass
            stop.wait(0.5)
        pc.close()

    def transfer(phase: str, stages: list[tuple[int, int]]) -> tuple[list[float], list[float], int]:
        rates: list[float] = []
        loaded: list[float] = []
        moved = 0
        stop = threading.Event()
        th = threading.Thread(target=loaded_probe, args=(stop, loaded), daemon=True)
        th.start()
        total = sum(n for _, n in stages)
        done = 0
        try:
            for size, count in stages:
                slow = False
                body = b"0" * size if phase == "upload" else None
                for _ in range(count):
                    if time.perf_counter() - start > max_seconds:
                        slow = True
                        break
                    def one(cn: Conn) -> tuple[float, float, int]:
                        if phase == "download":
                            ttfb, dur, n, resp = cn.request("GET", f"/__down?bytes={size}")
                            return ttfb, dur, n
                        ttfb, dur, n, resp = cn.request("POST", "/__up", body)
                        rtt = tcp_rtt_ms(resp) or res.latency_ms or 0.0
                        return ttfb, ttfb - rtt / 1000, size   # the body went up while we waited for the first byte
                    try:
                        if len(pool) == 1:
                            ttfb, secs, n = one(conn)
                        else:                        # N requests at once on N connections: rate = bytes / wall time
                            from concurrent.futures import ThreadPoolExecutor
                            t_b = time.perf_counter()
                            with ThreadPoolExecutor(len(pool)) as ex:
                                outs = list(ex.map(one, pool))
                            wall = time.perf_counter() - t_b
                            # download: data flows from the first response byte on; upload: from the start, minus one RTT
                            secs = wall - min(o[0] for o in outs) if phase == "download" else wall - (res.latency_ms or 0) / 1000
                            secs = max(secs, max(o[1] for o in outs))   # never shorter than the slowest flow's own transfer
                            n = sum(o[2] for o in outs)
                            ttfb = max(o[0] for o in outs)
                    except OSError as e:
                        res.errors.append(f"{phase}: {e}")
                        res.rate_limited |= "429" in str(e)
                        return rates, loaded, moved
                    moved += n
                    done += 1
                    mbps = n * 8 / secs / 1e6 if secs >= MIN_DURATION_S else None
                    if mbps is not None:
                        rates.append(mbps)
                    cb(Progress(phase, done, total, mbps, list(rates), size, moved))
                    if (secs if phase == "download" or len(pool) > 1 else ttfb) >= FINISH_S:
                        slow = True
                if slow:
                    break
        finally:
            stop.set()
            th.join(timeout=2)
        return rates, loaded, moved

    if lat:
        rates, loaded, res.bytes_down = transfer("download", DOWN_STAGES)
        res.download_samples = [round(x, 1) for x in rates]
        res.download_mbps = round(p90(rates), 1) if rates else None
        res.loaded_latency_down_ms = round(statistics.median(loaded), 1) if loaded else None
        if upload and res.download_mbps is not None:
            rates, loaded, res.bytes_up = transfer("upload", UP_STAGES)
            res.upload_samples = [round(x, 1) for x in rates]
            res.upload_mbps = round(p90(rates), 1) if rates else None
            res.loaded_latency_up_ms = round(statistics.median(loaded), 1) if loaded else None
    for cn in pool:
        cn.close()
    res.duration_s = round(time.perf_counter() - start, 1)
    cb(Progress("done"))
    return res
