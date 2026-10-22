"""Domain services, attached to Router as attributes (r.wifi, r.mesh, ...)."""
from __future__ import annotations


class Service:
    def __init__(self, router):
        self.r = router


def attach(router) -> None:
    from .capabilities import CapabilityService
    from .devices import DeviceService
    from .dhcp import DhcpService
    from .health import HealthService
    from .mesh import MeshService
    from .lan import LanService
    from .nat import NatService
    from .qos import QosService
    from .parental import ParentalService
    from .settings import SettingsService
    from .snapshots import SnapshotService
    from .status import StatusService
    from .system import SystemService
    from .wifi import WifiService
    router.caps = CapabilityService(router)
    router.status = StatusService(router)
    router.wifi = WifiService(router)
    router.mesh = MeshService(router)
    router.devices = DeviceService(router)
    router.clients = router.devices          # backwards-compatible name
    router.nat = NatService(router)
    router.lan = LanService(router)
    router.settings = SettingsService(router)
    router.dhcp = DhcpService(router)
    router.qos = QosService(router)
    router.parental = ParentalService(router)
    router.snapshots = SnapshotService(router)
    router.health = HealthService(router)
    router.system = SystemService(router)
