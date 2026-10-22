"""Fill the line with parallel connections and measure latency while it is full (the RPM / bufferbloat method of
Apple's networkQuality and Ookla's multi-connection test). Engines supply the endpoints."""
from __future__ import annotations

import http.client
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ._http import UA, jitter
from .models import Progress, SpeedResult

UP_REQUEST = 4_000_000          # bytes per upload POST (Content-Length; accepted by Apple's slurp and Ookla's /upload)
BLOCK = 256 * 1024


@dataclass
class Endpoints:
    host: str
    port: int = 443
    tls: bool = True
    latency_path: str = "/"         # tiny response: idle + loaded latency
    download_path: str = "/"        # big (or endless) response; requested again when it ends
    upload_path: str = "/"
    server: dict | None = None


def _conn(ep: Endpoints, timeout: float):
    cls = http.client.HTTPSConnection if ep.tls else http.client.HTTPConnection
    return cls(ep.host, ep.port, timeout=timeout)


def _latency(c, path: str) -> float:
    t0 = time.perf_counter()
    c.request("GET", path, headers={"User-Agent": UA})
    c.getresponse().read()
    return (time.perf_counter() - t0) * 1000


def run(ep: Endpoints, res: SpeedResult, progress: Callable[[Progress], None] | None = None, upload: bool = True,
        flows: int = 8, seconds: float = 6.0, timeout: float = 10.0) -> SpeedResult:
    """Idle latency, then download and upload phases of `seconds` each on `flows` connections. Throughput =
    bytes moved in the second half of a phase / its duration (after TCP ramp-up); loaded latency = median of
    tiny requests on a separate warm connection during the phase."""
    cb = progress or (lambda p: None)
    t_start = time.perf_counter()
    res.flows = {"down": flows, "up": flows if upload else None}
    res.server = res.server or ep.server or {"colo": ep.host}
    try:
        pc = _conn(ep, timeout)
        idle = [_latency(pc, ep.latency_path) for _ in range(11)][1:]       # first pays TCP+TLS setup
        pc.close()
        res.latency_ms = round(statistics.median(idle), 1)
        res.jitter_ms = round(jitter(idle) or 0.0, 1)
        res.latency_samples = list(idle)
        cb(Progress("latency", 1, 1, samples=idle))
    except (OSError, http.client.HTTPException) as e:
        res.errors.append(f"latency: {e}")
        return res

    def phase(name: str) -> tuple[float | None, float | None, int, list[float]]:
        moved = [0] * flows
        stop = threading.Event()
        loaded: list[float] = []

        def down_flow(i):
            c = _conn(ep, timeout)
            try:
                while not stop.is_set():
                    c.request("GET", ep.download_path, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
                    r = c.getresponse()
                    while not stop.is_set() and (chunk := r.read(BLOCK)):
                        moved[i] += len(chunk)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                c.close()

        def up_flow(i):
            c = _conn(ep, timeout)
            block = b"0" * BLOCK
            try:
                while not stop.is_set():
                    c.putrequest("POST", ep.upload_path)
                    c.putheader("User-Agent", UA)
                    c.putheader("Content-Type", "application/octet-stream")
                    c.putheader("Content-Length", str(UP_REQUEST))
                    c.endheaders()
                    sent = 0
                    while sent < UP_REQUEST:
                        n = min(BLOCK, UP_REQUEST - sent)
                        c.send(block[:n])
                        sent += n
                        moved[i] += n
                        if stop.is_set():
                            raise OSError("stopped")
                    c.getresponse().read()
            except (OSError, http.client.HTTPException):
                pass
            finally:
                c.close()

        def probe():
            c = _conn(ep, timeout)
            try:
                _latency(c, ep.latency_path)
                while not stop.is_set():
                    loaded.append(_latency(c, ep.latency_path))
                    stop.wait(0.2)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                c.close()

        ths = [threading.Thread(target=down_flow if name == "download" else up_flow, args=(i,), daemon=True)
               for i in range(flows)] + [threading.Thread(target=probe, daemon=True)]
        for t in ths:
            t.start()
        t0 = last_t = time.perf_counter()
        half_t, half_b, last_b, samples = None, 0, 0, []
        while (el := time.perf_counter() - t0) < seconds:
            time.sleep(0.25)
            now_b, now_t = sum(moved), time.perf_counter()
            samples.append((now_b - last_b) * 8 / (now_t - last_t) / 1e6)
            last_b, last_t = now_b, now_t
            if half_t is None and el >= seconds / 2:
                half_t, half_b = now_t, now_b
            cb(Progress(name, int(el * 4), int(seconds * 4), samples[-1], list(samples), None, now_b))
        stop.set()
        end_b, end_t = sum(moved), time.perf_counter()
        for t in ths:
            t.join(timeout=2)
        rate = (end_b - half_b) * 8 / (end_t - half_t) / 1e6 if half_t and end_t > half_t else None
        return (round(rate, 1) if rate else None), (round(statistics.median(loaded), 1) if loaded else None), end_b, samples

    res.download_mbps, res.loaded_latency_down_ms, res.bytes_down, s = phase("download")
    res.download_samples = [round(x, 1) for x in s]
    if upload and res.download_mbps:
        res.upload_mbps, res.loaded_latency_up_ms, res.bytes_up, s = phase("upload")
        res.upload_samples = [round(x, 1) for x in s]
    res.rpm = {"down": round(60000 / res.loaded_latency_down_ms) if res.loaded_latency_down_ms else None,
               "up": round(60000 / res.loaded_latency_up_ms) if res.loaded_latency_up_ms else None}
    res.duration_s = round(time.perf_counter() - t_start, 1)
    cb(Progress("done"))
    return res
