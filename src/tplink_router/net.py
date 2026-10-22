"""Local reachability probes from this machine (no router session needed)."""
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .models import PingResult

_LOSS = re.compile(r"([\d.]+)% packet loss")
_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")


def ping(host: str, count: int = 4, timeout_s: int = 4) -> PingResult:
    v6 = ":" in host
    cmd = ["ping6", "-c", str(count), "-i", "0.3", host] if v6 else \
          ["ping", "-c", str(count), "-i", "0.2", "-t", str(timeout_s), host]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + count).stdout
    except subprocess.TimeoutExpired:
        return PingResult(host, 100.0)
    loss, rtt = _LOSS.search(out), _RTT.search(out)
    jit = re.search(r"= [\d.]+/[\d.]+/[\d.]+/([\d.]+)", out)
    return PingResult(host, float(loss.group(1)) if loss else 100.0,
                      float(rtt.group(2)) if rtt else None, float(rtt.group(3)) if rtt else None,
                      float(jit.group(1)) if jit else None)


def ping_many(hosts: list[str], count: int = 4) -> dict[str, PingResult]:
    hosts = [h for h in dict.fromkeys(hosts) if h and h != "0.0.0.0"]
    with ThreadPoolExecutor(max(1, min(12, len(hosts)))) as ex:
        return dict(zip(hosts, ex.map(lambda h: ping(h, count), hosts)))


INTERNET_V4 = ["1.1.1.1", "8.8.8.8"]
INTERNET_V6 = ["2606:4700:4700::1111"]


