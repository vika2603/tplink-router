"""Shared plumbing: kept-alive HTTPS connection, Cloudflare Server-Timing parsing, statistics."""
from __future__ import annotations

import http.client
import statistics
import time

UA = "tpctl-speedtest/1"
HOST = "speed.cloudflare.com"


def p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * 0.9
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def jitter(xs: list[float]) -> float | None:
    return statistics.fmean(abs(a - b) for a, b in zip(xs, xs[1:])) if len(xs) > 1 else None


def server_ms(resp: http.client.HTTPResponse) -> float:
    """Cloudflare's own processing time, subtracted like their client does. Server-Timing carries one or more
    `name;dur=X` entries (now cfSpeedEdge + cfSpeedWorker; formerly cfRequestDuration): sum them."""
    total = 0.0
    for part in ",".join(v for k, v in resp.getheaders() if k.lower() == "server-timing").split(","):
        if "dur=" in part:
            try:
                total += float(part.split("dur=")[1].split(";")[0])
            except ValueError:
                pass
    return total


def tcp_rtt_ms(resp: http.client.HTTPResponse) -> float | None:
    """Cloudflare's TCP stack reports the connection's smoothed RTT (Server-Timing `cfL4;desc="...rtt=<us>..."`).
    It excludes server processing (whose reported duration swings 15-200 ms) and includes queueing delay, so it is
    the latency figure used here, idle and under load alike."""
    import re
    m = re.search(r"[?&]rtt=(\d+)", ",".join(v for k, v in resp.getheaders() if k.lower() == "server-timing"))
    return int(m.group(1)) / 1000 if m else None


def latency_ms(ttfb_s: float, resp: http.client.HTTPResponse) -> float:
    rtt = tcp_rtt_ms(resp)
    return rtt if rtt is not None else max(0.0, ttfb_s * 1000 - server_ms(resp))


def http_latency_ms(ttfb_s: float, resp: http.client.HTTPResponse) -> float:
    """Per-request HTTP latency (time to first byte minus server time): noisier, used for jitter."""
    return max(0.0, ttfb_s * 1000 - server_ms(resp))




class Conn:
    """One kept-alive HTTPS connection (no TLS handshake per request, like a browser)."""

    def __init__(self, timeout: float, host: str = HOST):
        self.timeout = timeout
        self.host = host
        self.c: http.client.HTTPSConnection | None = None

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[float, float, int, http.client.HTTPResponse]:
        """-> (ttfb_s, transfer_s, bytes, response). Reconnects once if the server closed the connection."""
        for attempt in (1, 2):
            if self.c is None:
                self.c = http.client.HTTPSConnection(self.host, timeout=self.timeout)
            try:
                hdrs = {"User-Agent": UA, "Accept-Encoding": "identity", "Origin": f"https://{self.host}", "Referer": f"https://{self.host}/"}
                if body is not None:
                    hdrs["Content-Type"] = "application/octet-stream"
                t0 = time.perf_counter()
                self.c.request(method, path, body=body, headers=hdrs)
                resp = self.c.getresponse()
                t1 = time.perf_counter()
                n = 0
                while chunk := resp.read(256 * 1024):
                    n += len(chunk)
                t2 = time.perf_counter()
                if resp.status >= 400:
                    raise OSError(f"HTTP {resp.status}")
                return t1 - t0, t2 - t1, n, resp
            except (http.client.HTTPException, ConnectionError, OSError) as e:
                self.close()
                if attempt == 2 or "HTTP 4" in str(e):
                    raise
        raise OSError("unreachable")

    def close(self):
        if self.c is not None:
            self.c.close()
            self.c = None


