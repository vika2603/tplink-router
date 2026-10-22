"""A fake router (no network): canned, anonymized responses keyed by endpoint+op, writes recorded."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

from tplink_router import Config, Router

MAIN, SAT_A, SAT_B = "02-00-00-00-00-01", "02-00-00-00-00-0A", "02-00-00-00-00-0B"


def mesh_fixture() -> dict:
    """main <- sat_a (weak, wifi) <- sat_b ; devices spread across them."""
    band = lambda ssid, ch: {"enable": "on", "ssid": ssid, "current_channel": ch, "channel": "auto", "htmode": "80",
                              "hwmode": "ax", "txpower": "high", "encryption": "psk_sae", "psk_version": "sae_transition",
                              "hidden": "off", "psk_key": "secret", "macaddr": "02-00-00-00-01-00"}
    return {
        ("admin/status?form=internet", "read"): {"internet_status": "connected"},
        ("admin/wireless?form=wireless_2g", "read_spf"): band("home", "6"),
        ("admin/wireless?form=wireless_5g", "read_spf"): band("home", "36"),
        ("admin/wireless?form=wireless_5g_2", "read_spf"): {"ssid": "", "macaddr": "00-00-00-00-00-00"},
        ("admin/wireless?form=smart_connect", "read"): {"smart_enable": "on"},
        ("admin/nat?form=alg", "read"): {"sip": "on", "ftp": "on", "h323": "on"},
        ("admin/dhcps?form=setting", "read"): {"enable": "on", "leasetime": "120", "ipaddr_start": "10.0.0.10",
                                               "ipaddr_end": "10.0.0.200", "gateway": "10.0.0.1"},
        ("admin/easymesh_network?form=get_mesh_device_list_all", "read"): [
            {"mac": MAIN, "name": "Main", "role": "main_router", "model": "Archer X", "ip": "10.0.0.1", "status": "connected"},
            {"mac": SAT_A, "name": "Hall", "role": "satellite_router", "model": "Sat", "ip": "10.0.0.2", "status": "connected",
             "parent_mac": MAIN, "connect_type": "wireless", "signal_strength": 2, "support_reboot": True},
            {"mac": SAT_B, "name": "Basement", "role": "satellite_router", "model": "Sat", "ip": "10.0.0.3", "status": "connected",
             "parent_mac": SAT_A, "connect_type": "wireless", "signal_strength": 4, "support_reboot": True}],
        ("admin/easymesh_network?form=mesh_sclient_list_all", "read"): [],
        ("admin/easymesh_network?form=mesh_sclient_detail", "read", SAT_A): {
            "parent_device_mac": MAIN, "signal_strength_5g": -79, "signal_level_5g": 2, "link_speed_5g": 600000,
            "mesh_nclient_list": [{"mac": "02-AA-00-00-00-01", "signal_strength": -85}],
            "name": "Hall", "location": "", "led": True, "preferred_wifi_uplink_type": "auto",
            "preferred_wifi_uplink_ap_available": []},
        ("admin/easymesh_network?form=mesh_sclient_detail", "read", MAIN): {
            "name": "Main", "location": "", "traffic_separation": False, "backhaul_steering": True},
        ("admin/easymesh_network?form=mesh_sclient_detail", "read", SAT_B): {
            "parent_device_mac": SAT_A, "signal_strength_5g": -55, "signal_level_5g": 4, "link_speed_5g": 1700000,
            "mesh_nclient_list": [{"mac": "02-AA-00-00-00-02", "signal_strength": -60},
                                  {"mac": "02-AA-00-00-00-03", "signal_strength": -88}]},
        ("admin/nat?form=vs", "load"): [],
        ("admin/access_control?form=mode", "read"): {"access_mode": "black"},
        ("admin/access_control?form=enable", "read"): {"enable": "off", "host_mac": "00-11-22-33-44-55"},
        ("admin/access_control?form=black_list", "load"): {},
        ("admin/access_control?form=black_devices", "load"): [
            {"mac": "02-AA-00-00-00-03", "type": "TV", "conn_type": "wireless", "host": "NON_HOST", "name": "tv", "ipaddr": "10.0.0.23"},
            {"mac": "00-11-22-33-44-55", "type": "Phone", "conn_type": "wireless", "host": "HOST", "name": "phone", "ipaddr": "10.0.0.20"}],
        ("admin/ledpm?form=setting", "read"): {"enable": "off", "time_start": "23:00", "time_end": "07:00", "ledpm_support": "yes"},
        ("admin/wireless?form=guest_2g&form=guest_5g&form=guest_2g5g", "read"): {
            "guest_2g5g_enable": "off", "guest_2g5g_ssid": "Guest", "guest_2g5g_encryption": "none", "guest_2g5g_psk_version": "rsn",
            "guest_2g5g_psk_key": ""},
        ("admin/wireless?form=guest_2g", "read"): {"guest_2g5g_enable": "off"},
        ("admin/smart_network?form=device_priority", "load"): [
            {"key": f"k{i}", "mac": m, "deviceName": n, "enablePriority": False}
            for i, (m, n) in enumerate([("00-11-22-33-44-55", "phone"), ("02-AA-00-00-00-01", "phone"),
                                        ("02-AA-00-00-00-02", "laptop"), ("02-AA-00-00-00-03", "tv")])],
        ("admin/smart_network?form=game_accelerator", "loadSpeed"): [
            {"mac": "00-11-22-33-44-55", "ip": "10.0.0.20", "deviceName": "phone", "deviceTag": "2.4G", "signal": -50,
             "isAgent": False, "txrate": 100000, "rxrate": 90000, "onlineTime": 600},
            {"mac": "02-AA-00-00-00-01", "ip": "10.0.0.21", "deviceName": "phone", "deviceTag": "5G", "isAgent": True,
             "txrate": 50000, "rxrate": 400000, "onlineTime": 60},
            {"mac": "02-AA-00-00-00-02", "ip": "10.0.0.22", "deviceName": "laptop", "deviceTag": "5G", "isAgent": True,
             "txrate": 50000, "rxrate": 800000, "onlineTime": 60},
            {"mac": "02-AA-00-00-00-03", "ip": "10.0.0.23", "deviceName": "tv", "deviceTag": "5G", "isAgent": True,
             "txrate": 10000, "rxrate": 50000, "onlineTime": 60}],
    }


class FakeLib:
    def __init__(self, responses: dict, model="Archer X", devices=None):
        self.responses = copy.deepcopy(responses)
        self.writes: list[tuple[str, dict]] = []
        self.model = model
        self.devices = devices or []

    def request(self, path, body):
        f = dict(parse_qsl(body, keep_blank_values=True))
        op = f.pop("operation")
        if op in ("insert", "remove"):                        # list add (new=<JSON>, index) / remove (key, index)
            self.writes.append((path, dict(f, operation=op)))
            rows = self.responses.setdefault((path, "load"), [])
            if isinstance(rows, dict):
                rows = rows.setdefault("list", [])
            if op == "insert":
                rows.insert(0, json.loads(f["new"]))
            else:
                del rows[int(f["index"])]
            return {}
        if op == "update":                                    # list row edit: new/old JSON + key
            self.writes.append((path, dict(f, operation=op)))
            for (ep, rop, *_), v in self.responses.items():
                if ep == path and isinstance(v, list):
                    for i, row in enumerate(v):
                        if row.get("key") == f.get("key"):
                            v[i] = json.loads(f["new"])
            return {}
        if op in ("write", "write_spf"):
            self.writes.append((path, f))
            for (ep, rop, *rest), v in self.responses.items():   # reflect writes into later reads
                if ep == path and isinstance(v, dict) and (not rest or rest[0] == f.get("mac")):
                    v.update({k: (json.loads(x) if k == "preferred_wifi_uplink_ap" else
                                  {"true": True, "false": False}.get(x, x)) for k, x in f.items() if k in v or k == "preferred_wifi_uplink_ap"}
                             if rest else {k: x for k, x in f.items() if k in v})
                if op == "write_spf" and isinstance(v, dict):
                    for k, x in f.items():
                        band = ep.split("form=wireless_")[-1] if "form=wireless_" in ep else None
                        if band and k.startswith(f"wireless_{band}_") and f"form=wireless_{band}" in path:
                            v[k[len(f'wireless_{band}_'):]] = x
            return {}
        key = (path, op, f["mac"]) if "mac" in f else (path, op)
        if key not in self.responses:
            raise Exception(f"fake: no response for {key}")
        return copy.deepcopy(self.responses[key])

    def get_firmware(self):
        return SimpleNamespace(model=self.model, hardware_version=f"{self.model} v1", firmware_version="1.0")

    def get_status(self):
        return SimpleNamespace(devices=self.devices, lan_macaddr=MAIN, lan_ipv4_addr="10.0.0.1")

    def get_ipv6_status(self):
        return SimpleNamespace()

    def logout(self):
        pass


def fake_router(responses: dict | None = None, **kw) -> tuple[Router, FakeLib]:
    tmp = Path(tempfile.mkdtemp())
    r = Router(Config(host="http://10.0.0.1", password="x", config_dir=tmp / "cfg", state_dir=tmp / "state"),
               persist_session=False)
    lib = FakeLib(mesh_fixture() if responses is None else responses, **kw)
    r._c, r._verified, r.session_source = lib, True, "fake"
    return r, lib
