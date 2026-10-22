"""Apple's responsiveness test servers (what macOS `networkQuality` uses), driven directly: works on any OS.
Config: https://mensura.cdn-apple.com/api/v1/gm/config -> small (latency), large (endless download), slurp (upload)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from . import saturate
from ._http import UA
from .models import SpeedResult

CONFIG = "https://mensura.cdn-apple.com/api/v1/gm/config"


def endpoints(timeout: float = 10.0) -> saturate.Endpoints:
    cfg = json.loads(urllib.request.urlopen(urllib.request.Request(CONFIG, headers={"User-Agent": UA}), timeout=timeout).read())
    u = cfg["urls"]
    path = lambda x: (lambda p: p.path + (f"?{p.query}" if p.query else ""))(urllib.parse.urlsplit(x))
    host = urllib.parse.urlsplit(u["large_https_download_url"]).hostname
    return saturate.Endpoints(host=host, latency_path=path(u["small_https_download_url"]),
                              download_path=path(u["large_https_download_url"]), upload_path=path(u["https_upload_url"]),
                              server={"colo": cfg.get("test_endpoint"), "city": None, "country": None})


def run(progress=None, upload: bool = True, connections: int = 8, timeout: float = 10.0) -> SpeedResult:
    res = SpeedResult(engine="apple")
    try:
        ep = endpoints(timeout)
    except (OSError, ValueError, KeyError) as e:
        res.errors.append(f"apple config: {e}")
        return res
    return saturate.run(ep, res, progress, upload, flows=max(2, connections), timeout=timeout)
