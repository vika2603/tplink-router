"""Per-model feature detection. Probes each optional endpoint once (read-only, quiet), caches the result per
host+model+firmware in <state_dir>/capabilities.json, so services can adapt instead of hard-coding one model."""
from __future__ import annotations

import datetime as dt
import json

from ..errors import UnsupportedError
from . import Service

# feature -> (endpoint, op) whose successful read means "supported"
PROBES = {
    "band_2g": ("admin/wireless?form=wireless_2g", "read_spf"),
    "band_5g": ("admin/wireless?form=wireless_5g", "read_spf"),
    "band_5g_2": ("admin/wireless?form=wireless_5g_2", "read_spf"),
    "band_6g": ("admin/wireless?form=wireless_6g", "read_spf"),
    "smart_connect": ("admin/wireless?form=smart_connect", "read"),
    "ofdma_mimo": ("admin/wireless?form=ofdma_mimo", "read"),
    "easymesh": ("admin/easymesh_network?form=get_mesh_device_list_all", "read"),
    "mesh_detail": ("admin/easymesh_network?form=mesh_sclient_list_all", "read"),
    "client_rates": ("admin/smart_network?form=game_accelerator", "loadSpeed"),
    "device_names": ("admin/smart_network?form=device_priority", "load"),
    "client_v6": ("admin/nat?form=client_list_v6", "read"),
    "alg": ("admin/nat?form=alg", "read"),
    "firewall": ("admin/security_settings?form=new_enable", "read"),
    "ipv6_rules": ("admin/nat?form=fr6", "read"),
    "dual_nat_state": ("admin/status?form=wan_dual_nat_state", "read"),
    "iot_isolation": ("admin/iot_security?form=enable", "read"),
    "dhcp_reservations": ("admin/dhcps?form=reservation", "load"),
    "guest_network": ("admin/wireless?form=guest_2g", "read"),
    "ports": ("admin/status?form=router", "read"),
    "access_control": ("admin/access_control?form=mode", "read"),
    "syslog": ("admin/syslog?form=types", "load"),
    "firmware_check": ("admin/cloud_account?form=cloud_upgrade", "read"),
    "satellite_firmware": ("admin/firmware?form=slave_cmd", "slave_get_info"),
}


class CapabilityService(Service):
    _cache: dict | None = None

    @property
    def _file(self):
        return self.r.config.state_dir / "capabilities.json"

    def detect(self, refresh: bool = False) -> dict:
        """{"model", "firmware", "detected_at", "features": {name: bool}, "bands": [...]}; cached per model+fw."""
        if self._cache is not None and not refresh:
            return self._cache
        fw = self.r.lib_call("get_firmware")
        key = f"{self.r.config.host}|{fw.model}|{fw.firmware_version}"
        store = {}
        if self._file.exists():
            try:
                store = json.loads(self._file.read_text())
            except ValueError:
                store = {}
        if not refresh and key in store:
            self._cache = store[key]
            missing = [k for k in PROBES if k not in self._cache["features"]]   # cache from an older tpctl
            if not missing:
                return self._cache
            self._cache["features"] |= self._probe(missing)
            store[key] = self._cache
            self._save(store)
            return self._cache
        feats = self._probe(list(PROBES))
        caps = {"model": fw.model, "hardware": fw.hardware_version, "firmware": fw.firmware_version,
                "detected_at": dt.datetime.now().isoformat(timespec="seconds"), "features": feats,
                "bands": [b for b in ("2g", "5g", "5g_2", "6g") if feats.get(f"band_{b}")]}
        store[key] = caps
        self._save(store)
        self._cache = caps
        return caps

    def _save(self, store: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(store, ensure_ascii=False, indent=1))

    def _probe(self, names: list[str]) -> dict:
        feats = {}
        for name in names:
            ep, op = PROBES[name]
            res = self.r.try_call(ep, op, quiet=True, default={"__unsupported__": True})
            ok = not (isinstance(res, dict) and res.get("__unsupported__"))
            if ok and name.startswith("band_"):
                ok = isinstance(res, dict) and bool(res.get("ssid") or res.get("macaddr", "00-00-00-00-00-00") != "00-00-00-00-00-00")
            feats[name] = ok
        return feats

    def has(self, feature: str) -> bool:
        return bool(self.detect()["features"].get(feature))

    def require(self, feature: str, what: str) -> None:
        if not self.has(feature):
            caps = self.detect()
            raise UnsupportedError(f"{what} is not supported on {caps['model']} fw {caps['firmware']} "
                                   f"(feature '{feature}' not detected). `tpctl capabilities --refresh` re-probes.")

    @property
    def bands(self) -> list[str]:
        return self.detect()["bands"]
