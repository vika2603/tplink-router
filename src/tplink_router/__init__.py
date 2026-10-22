"""TP-Link routers (current web UI) + EasyMesh: library + `tpctl` CLI.

    from tplink_router import connect
    with connect() as r:                      # one login for a whole batch
        inv = r.mesh.inventory(ping=True)     # Inventory(nodes, devices)
        for c in r.devices.list(band="2.4G", inventory=inv): ...
        r.wifi.set_txpower("middle", bands=["5g"])   # -> WriteResult (snapshot taken first)
        r.call("admin/nat?form=alg")          # raw endpoint access

Services: r.caps r.status r.wifi r.mesh r.devices r.nat r.dhcp r.snapshots r.health r.system
"""
from . import net
from .client import Router, connect, redact
from .config import Config
from .errors import AuthError, RouterError, TplinkError, UnreachableError, UnsupportedError, UsageError
from .models import Change, Device, Inventory, Issue, Node, PingResult, WriteResult, to_dict, Client
from .output import render, show

__all__ = ["Router", "connect", "Config", "redact", "net", "to_dict", "show", "render",
           "Change", "Device", "Client", "Inventory", "Issue", "Node", "PingResult", "WriteResult",
           "TplinkError", "UsageError", "AuthError", "RouterError", "UnsupportedError"]
__version__ = "0.4.0"
