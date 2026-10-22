"""Result and progress types shared by every engine."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Progress:
    phase: str                     # meta | latency | download | upload | done
    done: int = 0                  # requests finished in this phase
    total: int = 0                 # planned requests in this phase
    current_mbps: float | None = None
    samples: list = field(default_factory=list)     # per-request Mbps (or latency ms for the latency phase)
    size: int | None = None        # bytes of the current request size
    bytes_total: int = 0


@dataclass
class SpeedResult:
    download_mbps: float | None = None
    upload_mbps: float | None = None
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loaded_latency_down_ms: float | None = None
    loaded_latency_up_ms: float | None = None
    server: dict = field(default_factory=dict)     # colo, city, country
    client: dict = field(default_factory=dict)     # ip, isp (asOrganization), asn
    bytes_down: int = 0
    bytes_up: int = 0
    duration_s: float = 0.0
    errors: list = field(default_factory=list)
    rate_limited: bool = False     # HTTP 429 mid-test: results are from the requests that completed
    engine: str = "cloudflare"     # cloudflare | apple | ookla | networkquality
    flows: dict = field(default_factory=dict)      # parallel connections used {"down": n, "up": n}
    rpm: dict = field(default_factory=dict)        # responsiveness under load, round trips per minute {"down", "up"}
    latency_samples: list = field(default_factory=list)
    download_samples: list = field(default_factory=list)
    upload_samples: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.download_mbps is not None


