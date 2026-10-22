"""Offline tests: grades, output contract, inventory/topology, issues, writes, capabilities, session, CLI errors."""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from fakes import MAIN, SAT_A, SAT_B, fake_router, mesh_fixture
from tplink_router import UnsupportedError, UsageError, quality
from tplink_router.cli import cli
from tplink_router.cli._errors import record_for
from tplink_router.output import line, render
from tplink_router.services.health import diff_states, inventory_issues, verdict
from tplink_router.services.mesh import assign_labels, is_randomized_mac
from tplink_router.session import SessionStore


# ---- grades -------------------------------------------------------------
@pytest.mark.parametrize("dbm,grade", [(-40, "excellent"), (-60, "good"), (-70, "fair"), (-80, "poor"), (-90, "bad"),
                                       (None, None), (-128, None)])
def test_signal_grade(dbm, grade):
    assert quality.signal(dbm) == grade


def test_backhaul_grade_takes_worse_of_level_and_rssi():
    assert quality.backhaul("wifi", -50, 2) == "poor"
    assert quality.backhaul("wired", None, None) == "excellent"
    assert quality.backhaul("wifi", -50, 5, online=False) == "bad"


def test_ping_grade():
    assert quality.ping(0, 2) == "excellent"
    assert quality.ping(5, 2) == "fair"
    assert quality.ping(100, None) == "bad"


# ---- output contract ----------------------------------------------------
def test_line_quoting_lists_and_kind_collision():
    s = line("device", label="Living room TV", band="5G", empty=None, tags=["a", "b"], kind="x")
    assert s == 'device label="Living room TV" band=5G tags=a,b kind=x'
    assert 'path="admin/x?form=y"' in line("endpoint", path="admin/x?form=y")


# ---- inventory / topology ----------------------------------------------
def test_inventory_topology_and_attribution():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    hall, base = inv.node("Hall"), inv.node("Basement")
    assert [n.name for n in inv.path(base)] == ["Basement", "Hall", "Main"]
    assert base.depth == 2 and hall.backhaul_dbm == -79 and hall.backhaul_grade == "poor"
    assert {c.ip for c in inv.devices_behind(hall)} == {"10.0.0.21", "10.0.0.22", "10.0.0.23"}
    assert inv.device("10.0.0.23").node == "Basement" and inv.device("10.0.0.23").signal_dbm == -88
    assert inv.nodes_matching("nope") == []
    with pytest.raises(Exception, match="no mesh node matches"):
        inv.node("nope")


def test_labels_disambiguate_and_randomized_mac():
    assert is_randomized_mac("02-AA-00-00-00-01") and not is_randomized_mac("00-11-22-33-44-55")
    r, _ = fake_router()
    labels = sorted(c.label for c in r.mesh.inventory().devices)
    assert labels == ["laptop", "phone#00-00-01", "phone#33-44-55", "tv"]


def test_no_easymesh_falls_back_to_single_node_and_generic_devices():
    resp = {k: v for k, v in mesh_fixture().items() if "easymesh" not in k[0] and "game_accelerator" not in k[0]}
    dev = SimpleNamespace(type="Connection.HOST_5G", macaddr="00-11-22-33-44-66", ipaddr="10.0.0.30", hostname="tablet",
                          signal=-58, ap_name="", tx_rate=None, rx_rate=None, online_time=30)
    r, _ = fake_router(resp, devices=[dev])
    inv = r.mesh.inventory()
    assert len(inv.nodes) == 1 and inv.nodes[0].is_main
    assert inv.devices[0].band == "5G" and inv.devices[0].node == inv.nodes[0].name
    with pytest.raises(UnsupportedError):
        r.mesh.reboot("x")


# ---- issues / verdict / changes ----------------------------------------
def test_issues_grouping_and_verdict():
    r, _ = fake_router()
    issues = inventory_issues(r.mesh.inventory())
    codes = [i.code for i in issues]
    assert "WEAK_BACKHAUL" in codes and codes.count("WEAK_DEVICES") >= 1 and "STRONG_ON_2G" in codes
    assert issues[0].level == "warn"                      # sorted most severe first
    v = verdict(issues)
    assert v["status"] == "warn" and "WEAK_BACKHAUL:Hall" in v["headline"]
    assert verdict(issues, [{"endpoint": "x"}])["status"] == "partial"


def test_diff_states_detects_moves_and_parent_changes():
    old = {"nodes": {"n1": {"name": "Hall", "parent": "Main", "status": "connected", "backhaul": "wifi", "grade": "good"}},
           "devices": {"c1": {"label": "tv", "node": "Hall", "band": "5G"}, "c2": {"label": "old", "node": "Hall", "band": "5G"}}}
    new = {"nodes": {"n1": {"name": "Hall", "parent": "Basement", "status": "connected", "backhaul": "wifi", "grade": "poor"}},
           "devices": {"c1": {"label": "tv", "node": "Main", "band": "2.4G"}, "c3": {"label": "new", "node": "Main", "band": "5G"}}}
    what = sorted(c["what"] for c in diff_states(old, new))
    assert what == ["device_joined", "device_left", "device_moved", "node_grade", "node_parent"]


# ---- writes: dry-run, no-op skip, verify -------------------------------
def test_apply_form_dry_run_skip_and_apply():
    r, lib = fake_router()
    dry = r.nat.set_alg(sip=False, dry_run=True)
    assert dry.dry_run and not dry.applied and lib.writes == []
    res = r.nat.set_alg(sip=False)
    assert res.applied and res.changes[0].old == "on" and res.changes[0].new == "off" and len(lib.writes) == 1
    again = r.nat.set_alg(sip=False)
    assert not again.applied and again.skipped_reason and len(lib.writes) == 1


def test_wifi_noop_does_not_touch_radios_and_band_validation():
    r, lib = fake_router()
    res = r.wifi.set_txpower("high")
    assert not res.applied and lib.writes == []
    assert r.caps.bands == ["2g", "5g"]                   # 5g_2 empty, 6g absent
    with pytest.raises(Exception, match="not on this router"):
        r.wifi.set_txpower("low", bands=["6g"])
    res = r.wifi.set_txpower("low", bands=["5g"])
    assert res.applied and res.changes[0].new == "low"


def test_partial_read_is_recorded():
    r, _ = fake_router()
    r.try_call("admin/missing?form=x")
    assert r.warnings and r.warnings[0]["endpoint"] == "admin/missing?form=x"
    r.try_call("admin/missing?form=y", quiet=True)
    assert len(r.warnings) == 1


# ---- session store ------------------------------------------------------
class _Lib:
    def __init__(self, host, pw):
        self.host = host


def test_session_store_roundtrip_binding_and_perms(tmp_path):
    s = SessionStore(tmp_path, ttl_s=60)
    c = _Lib("h", "pw")
    c._stok, c._aes_key, c._seq = "tok", "k", 7
    s.save(c, "h", "pw")
    assert stat.S_IMODE(os.stat(s.file).st_mode) == 0o600
    assert "pw" not in s.file.read_text()
    back = s.load("h", "pw")
    assert back is not None and back._stok == "tok" and back._seq == 7
    assert s.load("h", "other") is None and s.load("other", "pw") is None
    d = json.loads(s.file.read_text()); d["last_used"] -= 3600; s.file.write_text(json.dumps(d))
    assert s.load("h", "pw") is None                      # idle beyond TTL


# ---- CLI: offline commands + error records ------------------------------
def test_bare_command_prints_help_without_login():
    res = CliRunner().invoke(cli, [])
    assert res.exit_code == 0 and "Look:" in res.output and "overview" in res.output and "Tools:" in res.output


def test_commands_and_schema_need_no_login():
    res = CliRunner().invoke(cli, ["commands", "--json"])
    env = json.loads(res.output)
    assert env["schema_version"] == 1 and env["ok"] is True
    assert any(r["command"] == "tpctl wifi set txpower" and r["risk"] == "disruptive" and r["dry_run"]
               and r["needs_confirm_flag"] for r in env["data"])
    assert json.loads(CliRunner().invoke(cli, ["schema", "--json"]).output)["data"]["exit_codes"]["partial"] == 6


@pytest.mark.parametrize("argv,code", [(["hh"], "UNKNOWN_COMMAND"), (["mesh"], "MISSING_COMMAND"),
                                       (["mesh", "node"], "MISSING_ARGUMENT"),
                                       (["wifi", "set", "txpower", "ultra"], "BAD_VALUE"),
                                       (["devices", "list", "--bnd", "5g"], "UNKNOWN_OPTION")])
def test_usage_errors_are_structured(argv, code):
    with pytest.raises(click.UsageError) as ei:
        cli.main(argv, standalone_mode=False)
    rec, exit_code = record_for(ei.value)
    assert rec["code"] == code and exit_code == 2 and rec.get("hint")


def test_render_any_model():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    out = render([inv.node("Hall"), inv.devices[0]])
    assert out[0].startswith("node name=Hall") and out[1].startswith("device label=")


# ---- human rendering / format selection ----------------------------------
def test_human_table_aligns_cjk():
    from tplink_router import human
    rows = human.table(["NODE", "X"], [["中文节点", "1"], ["Hall", "2"]], indent=0)
    col = lambda row, ch: human.width(row[:row.index(ch)])
    assert col(rows[1], "1") == col(rows[2], "2")          # 2nd column starts at the same display column
    assert human.width("中文") == 4


def test_default_format_records_when_piped_human_when_forced(monkeypatch):
    from tplink_router.cli import _ctx
    monkeypatch.delenv("TPCTL_FORMAT", raising=False)
    assert _ctx.default_format() == "records"           # pytest's stdout is not a TTY
    monkeypatch.setenv("TPCTL_FORMAT", "human")
    assert _ctx.default_format() == "human"


def test_human_overview_and_write_render():
    from tplink_router import human
    r, _ = fake_router()
    inv = r.mesh.inventory()
    lines = human.mesh(inv.nodes)
    assert lines[0].split()[0] == "NODE" and any("└─ Basement" in l for l in lines)
    res = r.nat.set_alg(sip=False, dry_run=True)
    assert human.write(res)[0].startswith("DRY RUN")


def test_bad_input_suggests_instead_of_dumping():
    from tplink_router.errors import UsageError
    rec, code = record_for(UsageError("no device matches 'lapto'", "lapto", ["laptop", "phone", "tv"], "device"))
    assert code == 2 and rec["did_you_mean"] == ["laptop"] and rec["hint"] == "tpctl devices list"


def test_unknown_option_alias_tip():
    with pytest.raises(click.UsageError) as ei:
        cli.main(["status", "--output", "json"], standalone_mode=False)
    rec, _ = record_for(ei.value)
    assert rec["code"] == "UNKNOWN_OPTION" and rec["did_you_mean"] == ["-o/--format"]



# ---- CLI against the fake router: confirmation, verify mismatch, truncation ---
@pytest.fixture
def cli_fake(monkeypatch):
    from tplink_router.cli import _ctx
    from tplink_router import output
    r, lib = fake_router()

    def fresh(*a, **k):                                       # each CLI run gets its own session on the same fake lib
        from tplink_router import Router
        r2 = Router(r.config, persist_session=False)
        r2._c, r2._verified, r2.session_source = lib, True, "fake"
        return r2
    monkeypatch.setattr(_ctx, "connect", fresh)
    monkeypatch.setattr(output, "DETAIL", False)
    monkeypatch.setattr(output, "FIELDS", None)

    def run(*argv):
        from tplink_router.cli import main
        import sys
        monkeypatch.setattr(sys, "argv", ["tpctl", *argv])
        _ctx.CURRENT = None
        with pytest.raises(SystemExit) as ei:
            main()
        return ei.value.code
    return run, r, lib


def test_disruptive_without_yes_plans_and_exits_4(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "txpower", "low", "--band", "5g") == 4
    out = capsys.readouterr().out
    assert "would_change key=5g.txpower" in out and 'run="tpctl wifi set txpower low --band 5g --yes"' in out
    assert lib.writes == []                                   # nothing written


def test_disruptive_noop_needs_no_confirmation(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "txpower", "high") == 0
    assert "write status=skipped" in capsys.readouterr().out and lib.writes == []


def test_disruptive_with_yes_applies(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "txpower", "low", "--band", "5g", "--yes") == 0
    assert "changed key=5g.txpower old=high new=low" in capsys.readouterr().out and len(lib.writes) == 1


def test_verify_mismatch_exits_7(cli_fake, capsys):
    run, r, lib = cli_fake
    lib.request_orig = lib.request
    def ignore_writes(path, body):                            # router acks but keeps the old value
        return {} if "operation=write" in body else lib.request_orig(path, body)
    lib.request = ignore_writes
    assert run("nat", "alg", "sip=off") == 7
    assert "mismatch key=sip target=off actual=on" in capsys.readouterr().out


def test_devices_limit_emits_truncated(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("devices", "list", "--limit", "2") == 0
    out = capsys.readouterr().out
    assert out.count("\ndevice ") + out.startswith("device ") == 2 and "truncated shown=2 total=4" in out


def test_json_envelope_and_fields(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("devices", "list", "--json") == 0
    env = json.loads(capsys.readouterr().out)
    assert env["schema_version"] == 1 and env["ok"] and env["data"]["total"] == 4
    assert run("devices", "list", "--fields", "ip") == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("device label=") and " ip=" in first and " band=" not in first



def test_unreachable_fails_fast():
    import time
    from tplink_router import Config, Router
    from tplink_router.errors import UnreachableError
    r = Router(Config(host="http://127.0.0.1:9", password="x"), persist_session=False)   # discard port: refused
    t = time.time()
    with pytest.raises(UnreachableError):
        r.open()
    assert time.time() - t < 5



def test_weak_definition_is_single_source():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    by_node = {i.subject: i.data["count"] for i in inventory_issues(inv) if i.code == "WEAK_DEVICES"}
    for n in inv.nodes:
        assert n.devices_weak == by_node.get(n.name, 0)
    assert {c.ip for c in r.devices.list(weak_below=quality.WEAK_BELOW_DBM, inventory=inv)} == \
           {c.ip for c in inv.devices if quality.is_weak(c.signal_dbm)}


def test_confirm_plan_reports_backhaul_caveat(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "txpower", "low", "--band", "5g") == 4
    out = capsys.readouterr().out
    assert "write status=needs_confirmation" in out and "would_side_effect" not in out
    assert "caveat detail=" in out and "Hall uses a poor 5G wireless backhaul" in out and "confirm required=yes" in out



def test_snapshot_diff_ignores_one_sided_keys_and_shortens():
    from tplink_router.services.snapshots import diff_dicts
    a = {"admin/nat?form=alg": {"sip": "on"}, "admin/dhcps?form=setting": {"leasetime": "120"}}
    b = {"admin/nat?form=alg": {"sip": "off", "new_field": "x"}, "admin/dhcps?form=setting": {"leasetime": "1440"},
         "admin/wireless?form=twt": {"enable": "off"}}
    ch = {c.key: (c.old, c.new) for c in diff_dicts(a, b)}
    assert ch == {"alg.sip": ("on", "off"), "dhcp.lease": ("120", "1440")}
    assert len(diff_dicts(a, b, include_noisy=True)) == 4


def test_events_log_records_changes(tmp_path):
    r, lib = fake_router()
    inv = r.mesh.inventory()
    r.health.record(inv)                                      # baseline
    lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")].pop()   # tv leaves
    r.health.record(r.mesh.inventory())
    ev = r.health.events()
    assert ev["observations"]["count"] == 2 and [e["what"] for e in ev["events"]] == ["device_left"]
    assert ev["events"][0]["subject"] == "tv"



def test_config_changes_become_events():
    from tplink_router.services.health import config_brief
    r, lib = fake_router()
    inv = r.mesh.inventory()
    r.health.changes_since_last(inv, config=config_brief(None, r.wifi.get()))
    lib.responses[("admin/wireless?form=wireless_5g", "read_spf")]["txpower"] = "low"     # changed in the web UI
    r.health.changes_since_last(r.mesh.inventory(), config=config_brief(None, r.wifi.get()))
    ev = [e for e in r.health.events()["events"] if e["what"] == "config_changed"]
    assert ev == [{"at": ev[0]["at"], "what": "config_changed", "subject": "wifi.5g.txpower", "old": "high", "new": "low"}]


@pytest.mark.parametrize("cgnat,fwd,spi,rules,v4,v6", [
    (True, 0, "on", 0, "blocked (CGNAT", "blocked (firewall on"),
    (False, 2, "on", 1, "exposed via port_forwards", "only 1 allowed"),
    (False, 0, "off", 0, "blocked (NAT", "OPEN"),
])
def test_inbound_reachability(cgnat, fwd, spi, rules, v4, v6):
    r, _ = fake_router()
    res = r.status.inbound({"cgnat": cgnat}, {"port_forwards": fwd, "spi_firewall": spi, "ipv6_allow_rules": rules})
    assert res["ipv4"].startswith(v4) and res["ipv6"].startswith(v6)


def test_records_render_bools_as_yes_no():
    assert line("x", a=True, b=False) == "x a=yes b=no"



def test_unknown_satellite_never_becomes_a_move():
    old = {"nodes": {}, "devices": {"c": {"label": "tv", "node": "Hall", "band": "5G"}}}
    new = {"nodes": {}, "devices": {"c": {"label": "tv", "node": "unknown-satellite", "band": "5G"}}}
    assert diff_states(old, new) == [] and diff_states(new, old) == []


def test_schema_library_fields_generated():
    env = json.loads(CliRunner().invoke(cli, ["schema", "--json"]).output)
    lib = env["data"]["library"]
    assert "backhaul_dbm" in lib["Node"] and "signal_dbm" in lib["Device"]
    assert lib["record_key_differences"]["node"]["uplink"].startswith("parent_name")



def test_path_grade_points_at_weak_upstream_hop():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    c = inv.device("laptop")                                   # on Basement, behind the poor Hall backhaul
    assert c.node == "Basement" and c.path_grade == "poor" and c.weakest_hop == "Hall"
    assert "weakest_hop=Hall" in render(c)[0]


def test_smart_connect_caveat_is_direction_aware():
    r, _ = fake_router()
    assert any("band steering stops" in c for c in r.wifi.caveats("smart-connect", None, "off"))
    assert "rejoin" in r.wifi.caveats("smart-connect", None, "on")[0]


def test_channel_caveats_dfs_and_160():
    r, lib = fake_router()
    lib.responses[("admin/wireless?form=wireless_5g", "read_spf")]["htmode"] = "160"
    assert any("DFS" in c for c in r.wifi.caveats("channel", ["5g"], "100"))
    assert any("UNII-4" in c for c in r.wifi.caveats("channel", ["5g"], "149"))



def test_fields_validated_and_drawn_from_full_set(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("devices", "list", "--fields", "randomized_mac") == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert "randomized_mac=" in first                          # detail-only field, explicit yes/no
    assert run("devices", "list", "--fields", "nope") == 2
    assert "unknown field" in capsys.readouterr().err


def test_backhaul_band_and_unaffected_note(cli_fake, capsys):
    run, r, lib = cli_fake
    inv = r.mesh.inventory()
    assert inv.node("Hall").backhaul_band == "5G"
    lib.responses[("admin/wireless?form=wireless_6g", "read_spf")] = dict(lib.responses[("admin/wireless?form=wireless_5g", "read_spf")])
    r.caps.detect(refresh=True)
    assert run("wifi", "set", "txpower", "low", "--band", "6g") == 4
    out = capsys.readouterr().out
    confirm = next(l for l in out.splitlines() if l.startswith("confirm "))
    assert 'note detail="mesh backhaul unaffected' in out and "caveats=" not in confirm   # reassurance isn't a risk


def test_timeline_windows(tmp_path):
    r, lib = fake_router()
    r.snapshots.save("manual")                       # not a tpctl write: the change happened somewhere in a window
    import time; time.sleep(1.1)
    lib.responses[("admin/dhcps?form=setting", "read")]["leasetime"] = "1440"
    rows = r.snapshots.timeline(key="dhcp")
    assert rows and rows[-1]["new"] == "1440" and rows[-1]["before"] == "now" and rows[-1]["by"].startswith("unknown")
    r2, lib2 = fake_router()
    r2.snapshots.save("dhcp")                        # taken right before a tpctl write: exact time, no window
    time.sleep(1.1)
    lib2.responses[("admin/dhcps?form=setting", "read")]["leasetime"] = "1440"
    row = r2.snapshots.timeline(key="dhcp")[-1]
    assert row["by"] == "tpctl" and row["likely"] and row["after"] is None and row["before"] is None



# ---- anti-drift: docs/schema must match what the code emits ---------------------
def test_issue_data_keys_match_declared():
    from tplink_router.services.health import ISSUE_DATA_KEYS
    r, _ = fake_router()
    for i in inventory_issues(r.mesh.inventory()):
        assert set(i.data) <= set(ISSUE_DATA_KEYS[i.code]), (i.code, set(i.data) - set(ISSUE_DATA_KEYS[i.code]))


def test_schema_lists_every_record_key():
    from tplink_router import output
    rec = json.loads(CliRunner().invoke(cli, ["schema", "--json"]).output)["data"]["records"]
    assert all(k in rec["node"] for k in output.NODE_KEYS) and all(k in rec["device"] for k in output.DEVICE_KEYS)


def test_node_and_device_lines_only_use_declared_keys():
    from tplink_router import output
    r, _ = fake_router()
    inv = r.mesh.inventory()
    import re as _re
    for detail in (False, True):
        for n in inv.nodes:
            keys = _re.findall(r" (\w+)=", output.node_line(n, detail=detail))
            assert set(keys) <= set(output.NODE_KEYS), set(keys) - set(output.NODE_KEYS)
        for c in inv.devices:
            keys = _re.findall(r" (\w+)=", output.device_line(c, detail=detail))
            assert set(keys) <= set(output.DEVICE_KEYS), set(keys) - set(output.DEVICE_KEYS)


def test_skill_md_mentions_no_stale_keys():
    """Backticked issue examples (`CODE key= ...`) must use that code's declared keys; renamed keys must not linger."""
    import pathlib, re as _re
    from tplink_router.services.health import ISSUE_DATA_KEYS
    skill = (pathlib.Path(__file__).parents[1] / "skills/tplink-router/SKILL.md").read_text()
    for code, body in _re.findall(r"`([A-Z_]{5,}) ([^`]*)`", skill):
        if code in ISSUE_DATA_KEYS:
            keys = set(_re.findall(r"\b([a-z_]+)=", body))
            assert keys <= set(ISSUE_DATA_KEYS[code]), (code, keys - set(ISSUE_DATA_KEYS[code]))
    stale = ["devices_poor", "signal_level=", "would_side_effect", "status=dry_run"]   # renamed/removed outputs
    assert not [x for x in stale if x in skill]


def test_devices_behind_attribute_equals_method():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    for n in inv.nodes:
        assert n.devices_behind == len(inv.devices_behind(n))


def test_set_band_is_one_write(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "band", "5g", "--width", "40", "--txpower", "low", "--yes") == 0
    assert len(lib.writes) == 1 and {"wireless_5g_htmode", "wireless_5g_txpower"} <= set(lib.writes[0][1])



def test_smart_connect_off_with_band_names_is_one_plan(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "smart-connect", "off", "--ssid-5g", "Home-5G") == 4
    out = capsys.readouterr().out
    assert "would_change key=smart_connect" in out and "would_change key=5g.ssid" in out and lib.writes == []
    assert run("wifi", "set", "smart-connect", "off", "--ssid-5g", "Home-5G", "--yes") == 0
    assert any("smart_connect" in w[0] for w in lib.writes) and any("wireless_5g_ssid" in w[1] for w in lib.writes)


def test_weakest_sort_and_reverse(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("devices", "list", "--weak", "--limit", "1") == 0
    first = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("device "))
    assert "label=tv" in first and "rank=1" in first                    # weakest first by default with --weak
    assert run("devices", "list", "--sort", "signal", "--reverse", "--limit", "1") == 0
    assert "label=tv" in capsys.readouterr().out.splitlines()[0]


def test_band_plan_has_no_duplicate_notes():
    r, lib = fake_router()
    lib.responses[("admin/wireless?form=wireless_5g", "read_spf")]["htmode"] = "160"
    cav = r.wifi.caveats("band", ["5g"], {"width": "80", "channel": "44", "txpower": "middle"})
    assert len(cav) == len(set(cav)) and sum("Hall uses" in c for c in cav) == 1   # one poor-backhaul note (capacity note is separate)



def test_split_plan_keeps_band_steering_risk(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("wifi", "set", "smart-connect", "off", "--ssid-5g", "Home-5G") == 4
    out = capsys.readouterr().out
    assert "band steering stops" in out and "share SSID" not in out


def test_reboot_plan_names_targets_and_uses_resolved_name(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("mesh", "reboot", "Hal") == 4
    out = capsys.readouterr().out
    assert "the 1 node(s) behind it (Basement)" in out and "laptop" in out and "run=\"tpctl mesh reboot Hall --yes\"" in out


def test_no_track_env_writes_nothing(monkeypatch):
    r, _ = fake_router()
    monkeypatch.setenv("TPCTL_NO_TRACK", "1")
    r.health.record(r.mesh.inventory())
    assert not (r.config.state_dir / "observations.json").exists()


def test_ssid_caveat_names_devices():
    r, _ = fake_router()
    cav = " ".join(r.wifi.caveats("ssid", None, "X"))
    assert "4 wireless devices" in cav and "laptop" in cav and "password and security stay the same" in cav


def test_confirm_command_strips_output_flags(monkeypatch):
    import sys
    from tplink_router.cli._ctx import confirm_command
    monkeypatch.setattr(sys, "argv", ["tpctl", "wifi", "set", "ssid", "Casa", "--json", "-o", "human", "--dry-run", "--fields", "ip"])
    assert confirm_command() == "tpctl wifi set ssid Casa --yes"


def test_zero_dbm_is_missing_not_strong():
    r, lib = fake_router()
    lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")][0]["signal"] = 0
    inv = r.mesh.inventory()
    assert inv.device("10.0.0.20").signal_dbm is None
    assert not [i for i in inventory_issues(inv) if i.code == "STRONG_ON_2G"]


def test_events_what_filter():
    r, lib = fake_router()
    r.health.record(r.mesh.inventory())
    lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")].pop()
    r.health.record(r.mesh.inventory())
    assert [e["what"] for e in r.health.events(what=["left"])["events"]] == ["device_left"]
    assert r.health.events(what=["joined"])["events"] == []


def test_quality_grade_helpers_and_jitter():
    assert quality.worse("poor", "good") and not quality.worse("good", "poor") and quality.rank("excellent") == 0
    assert quality.ping(0, 3, jitter_ms=30) == "fair" and quality.ping(0, 3, jitter_ms=50) == "poor"


def test_node_path_grade_and_sort():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    assert inv.node("Basement").path_grade == "poor" and inv.node("Basement").backhaul_grade == "excellent"


def test_ping_diagnosis_attribution():
    from tplink_router.cli.net import _diagnose
    from types import SimpleNamespace as NS
    rows = [{"name": "device:x", "avg_ms": 90, "jitter_ms": 4, "loss_pct": 0}, {"name": "node:n", "avg_ms": 3, "loss_pct": 0},
            {"name": "internet-v4", "avg_ms": 6, "loss_pct": 0}]
    weak = NS(signal_grade="bad", signal_dbm=-85, path_grade=None, weakest_hop=None)
    good = NS(signal_grade="good", signal_dbm=-60, path_grade=None, weakest_hop=None)
    assert _diagnose(rows, weak)["verdict"] == "last_hop_signal"
    assert _diagnose(rows, good)["verdict"] == "likely_power_save"
    rows[0]["avg_ms"] = 5
    assert _diagnose(rows, NS(signal_grade="good", signal_dbm=-60, path_grade="poor", weakest_hop="Hall"))["verdict"] == "upstream_backhaul"


def test_split_band_caveat_says_what_stays():
    r, _ = fake_router()
    cav = " ".join(r.wifi.caveats("ssid", ["5g"], "x"))
    assert "2.4G keep the name 'home'" in cav and "reconnect to 'home'" in cav and "stay offline" in cav


def test_diagnosis_falls_back_to_router_data_and_roaming():
    from tplink_router.cli.net import _diagnose, VERDICTS
    from types import SimpleNamespace as NS
    rows = [{"name": "device:x", "loss_pct": 100}, {"name": "node:n", "avg_ms": 3, "loss_pct": 0}]
    ok = NS(signal_grade="good", signal_dbm=-60, path_grade=None, weakest_hop=None, link_rx_mbps=400, link_tx_mbps=200)
    d = _diagnose(rows, ok)
    assert d["verdict"] == "router_data_only" and d["basis"] == "router_data"
    assert _diagnose(rows, ok, flaps=4)["verdict"] == "roaming_flaps"
    assert all(v in VERDICTS for v in ("roaming_flaps", "router_data_only"))


def test_plan_changes_carry_target():
    r, _ = fake_router()
    plan = r.wifi.set_txpower("low", ["5g"], dry_run=True)
    assert plan.changes[0].target == "low" == plan.changes[0].new


def test_path_bottleneck_and_unattributed_not_grouped():
    r, lib = fake_router()
    inv = r.mesh.inventory()
    assert inv.node("Basement").path_link_mbps == 600          # Hall's 600 Mbps hop is the bottleneck
    assert not [i for i in inventory_issues(inv) if i.code == "WEAK_DEVICES" and i.subject == "unknown-satellite"]


def test_roaming_summary_separates_node_changes_from_band_switches(tmp_path):
    import json as _j, datetime as _dt
    r, _ = fake_router()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    ev = [{"at": now, "what": "device_moved", "subject": "tv", "old": "Hall/5G", "new": "Main/5G"},
          {"at": now, "what": "device_moved", "subject": "tv", "old": "Main/5G", "new": "Hall/5G"},
          {"at": now, "what": "device_moved", "subject": "tv", "old": "Hall/5G", "new": "Main/5G"},
          {"at": now, "what": "device_moved", "subject": "pad", "old": "Hall/5G", "new": "Hall/2.4G"},
          {"at": now, "what": "device_moved", "subject": "pad", "old": "Hall/2.4G", "new": "Hall/5G"},
          {"at": now, "what": "device_moved", "subject": "pad", "old": "Hall/5G", "new": "Hall/2.4G"}]
    r.config.state_dir.mkdir(parents=True, exist_ok=True)
    (r.config.state_dir / "events.jsonl").write_text("\n".join(_j.dumps(e) for e in ev) + "\n")
    rows = {d["label"]: d for d in r.health.roaming(60)}
    assert rows["tv"]["node_changes"] == 3 and rows["tv"]["assessment"].startswith("flapping")
    assert rows["pad"]["band_switches"] == 3 and rows["pad"]["assessment"].startswith("band steering")


def test_plans_never_repeat_a_line(cli_fake, capsys):
    run, r, lib = cli_fake
    run("wifi", "set", "smart-connect", "off", "--ssid-5g", "N")
    out = [l for l in capsys.readouterr().out.splitlines() if l.startswith(("caveat", "note"))]
    assert len(out) == len(set(out))


def test_library_ranking_matches_cli_and_helpers(cli_fake, capsys):
    run, r, lib = cli_fake
    inv = r.mesh.inventory()
    ranked = [n.name for _, n in inv.ranked("path")]
    assert ranked[0] == "Hall" and inv.weakest_device("Basement").label == "tv"
    assert inv.devices_on(inv.node("Basement")) == inv.devices_on("Basement")
    assert run("mesh", "show", "--sort", "path", "--no-ping") == 0
    cli_order = [l.split(" name=")[1].split(" ")[0] for l in capsys.readouterr().out.splitlines() if l.startswith("node rank=")]
    assert cli_order == ranked


def test_scope_is_band_aware():
    r, _ = fake_router()
    assert "no satellite backhauls over 2.4G" in r.wifi.scope_for(["2g"])
    assert "satellites backhaul over 5G" in r.wifi.scope_for(["5g"])
    assert "other main-router bands may blip too" in r.wifi.effect_for(["2g"])


def test_near_boundary_flag():
    assert quality.near_boundary(-74) and quality.near_boundary(-76) and not quality.near_boundary(-71)
    from types import SimpleNamespace as NS
    from tplink_router.output import device_line
    r, _ = fake_router()
    c = r.mesh.inventory().device("10.0.0.21")        # -85 dBm: not near a boundary (-82 is 3 dB away)
    assert "near_boundary" not in device_line(c)


def test_events_counts_and_summary():
    r, lib = fake_router()
    r.health.record(r.mesh.inventory())
    lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")].pop()
    r.health.record(r.mesh.inventory())
    ev = r.health.events()
    assert ev["counts"]["device_left"] == 1 and ev["counts"]["config_changed"] == 0 and ev["top_subjects"] == ["tv:1"]


def test_transient_read_failure_is_retried_once():
    r, lib = fake_router()
    orig, calls = lib.request, {"n": 0}
    def flaky(path, body):
        if "dhcps?form=setting" in path and calls["n"] == 0:
            calls["n"] += 1
            raise Exception("busy")
        return orig(path, body)
    lib.request = flaky
    assert r.call("admin/dhcps?form=setting")["leasetime"] == "120" and r.transient_retries == 1


def test_permanently_rejected_endpoint_not_retried_forever():
    import time as _t
    r, _ = fake_router()
    t = _t.time()
    for _ in range(3):
        r.try_call("admin/missing?form=x", quiet=True)
    assert _t.time() - t < 1.5          # only the first failure paid the retry pause


def test_snapshot_keys_use_cli_names():
    from tplink_router.services.snapshots import _short
    assert _short("admin/wireless?form=wireless_5g.ssid") == "5g.ssid"
    assert _short("admin/wireless?form=wireless_5g.htmode") == "5g.width"
    assert _short("admin/wireless?form=smart_connect.smart_enable") == "smart_connect"
    assert _short("admin/dhcps?form=setting.leasetime") == "dhcp.lease"
    assert _short("admin/nat?form=alg.sip") == "alg.sip"


def test_timeline_marks_reverted_changes():
    import time as _t
    r, lib = fake_router()
    r.snapshots.save("alg"); _t.sleep(1.1)
    lib.responses[("admin/nat?form=alg", "read")]["sip"] = "off"
    r.snapshots.save("alg"); _t.sleep(1.1)
    lib.responses[("admin/nat?form=alg", "read")]["sip"] = "on"          # back to the start
    rows = r.snapshots.timeline(key="alg.sip")
    assert len(rows) == 2 and all(x["reverted"] == "yes" for x in rows)


def test_radio_plan_counts_and_names_affected_devices():
    r, _ = fake_router()
    eff = r.wifi.effect_for(["5g"])
    assert eff.startswith("3 devices mesh-wide (3 on satellites) drop") and "laptop" in eff



def test_5g_restart_counts_satellite_devices_on_other_bands():
    r, lib = fake_router()
    lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")].append(
        {"mac": "02-AA-00-00-00-09", "ip": "10.0.0.29", "deviceName": "plug", "deviceTag": "2.4G", "isAgent": True,
         "txrate": 1000, "rxrate": 1000, "onlineTime": 60})
    lib.responses[("admin/easymesh_network?form=mesh_sclient_detail", "read", SAT_B)]["mesh_nclient_list"].append(
        {"mac": "02-AA-00-00-00-09", "signal_strength": -60})
    eff = r.wifi.effect_for(["5g"])
    assert eff.startswith("4 devices mesh-wide (4 on satellites) drop") and "plus 1 on other bands" in eff and "plug" in eff


def test_jitter_already_upstream_is_not_blamed_on_last_hop():
    from tplink_router.cli.net import _diagnose
    from types import SimpleNamespace as NS
    rows = [{"name": "device:x", "avg_ms": 26, "jitter_ms": 38, "loss_pct": 0},
            {"name": "node:n", "avg_ms": 18, "jitter_ms": 31, "loss_pct": 0}]
    c = NS(signal_grade="bad", signal_dbm=-90, path_grade="poor", weakest_hop="Hall", link_rx_mbps=0, link_tx_mbps=0)
    assert _diagnose(rows, c)["verdict"] == "upstream_backhaul"


def test_since_today():
    from tplink_router.cli.events import _minutes
    import datetime as dt
    now = dt.datetime.now()
    assert 1 <= _minutes("today") <= now.hour * 60 + now.minute + 1



@pytest.mark.parametrize("ctype,band,guest", [("Connection.HOST_MLO", "MLO", False), ("Connection.GUEST_2G", "2.4G", True),
                                              ("Connection.IOT_6G", "6G", False), ("Connection.WIRED", "wired", False)])
def test_generic_device_fallback_bands(ctype, band, guest):
    from types import SimpleNamespace as NS
    resp = {k: v for k, v in mesh_fixture().items() if "easymesh" not in k[0] and "game_accelerator" not in k[0]}
    dev = NS(type=NS(value=ctype.split(".")[1].lower()), macaddr="00-11-22-33-44-77", ipaddr="10.0.0.40",
             hostname="x", signal=-60, ap_name="", tx_rate=None, rx_rate=None, online_time=1)
    r, _ = fake_router(resp, devices=[dev])
    c = r.mesh.inventory().devices[0]
    assert c.band == band and c.guest == guest



@pytest.mark.parametrize("argv", [["wifi", "set", "txpower", "low", "--band", "5g", "--dry-run"],
                                  ["mesh", "reboot", "Hall", "--dry-run"], ["system", "reboot", "--dry-run"]])
def test_dry_run_gives_plan_and_confirm_command_exit_0(cli_fake, capsys, argv):
    run, r, lib = cli_fake
    assert run(*argv) == 0
    out = capsys.readouterr().out
    assert "confirm required=yes" in out and "--yes" in out.split("run=")[1] and lib.writes == []


def test_migrates_pre_rename_observation_and_events():
    import json as _j, datetime as _dt
    r, _ = fake_router()
    inv = r.mesh.inventory()
    old = {"time": _dt.datetime.now().isoformat(timespec="seconds"), "config": {}, "nodes": {},
           "clients": {c.mac: {"label": c.label, "node": c.node, "band": c.band} for c in inv.devices}}
    r.config.state_dir.mkdir(parents=True, exist_ok=True)
    (r.config.state_dir / "last_observation.json").write_text(_j.dumps(old))
    (r.config.state_dir / "events.jsonl").write_text(_j.dumps({"at": old["time"], "what": "client_left", "subject": "x"}) + "\n")
    ch = r.health.changes_since_last(inv)
    assert not [c for c in ch["changes"] if c["what"] == "device_joined"]      # no fake "everyone joined"
    assert r.health.events()["events"][0]["what"] == "device_left"


def test_old_names_still_work(cli_fake, capsys):
    run, r, lib = cli_fake
    assert run("clients", "find", "laptop") == 0 and "device label=laptop" in capsys.readouterr().out
    from tplink_router import Client, Device
    assert Client is Device and r.clients is r.devices and r.mesh.inventory().clients == r.mesh.inventory().devices


# -- router-side device names + node settings --

def test_rename_device_updates_router_row_and_verifies():
    r, lib = fake_router()
    res = r.devices.rename("laptop", "Work laptop")
    assert res.applied and not res.mismatches and res.changes[0].new == "Work laptop"
    path, f = lib.writes[-1]
    assert path.endswith("form=device_priority") and f["operation"] == "update" and f["key"] == "k2"
    new, old = json.loads(f["new"]), json.loads(f["old"])
    assert new == dict(old, deviceName="Work laptop")            # whole row, only the name changed


def test_rename_dry_run_and_noop_write_nothing():
    r, lib = fake_router()
    assert r.devices.rename("tv", "Lounge TV", dry_run=True).dry_run
    assert not r.devices.rename("tv", "tv").applied
    assert not lib.writes


def test_mesh_set_satellite_sends_ui_payload():
    r, lib = fake_router()
    res = r.mesh.configure("Hall", name="Hallway", location="hallway", led=False)
    assert not res.mismatches and {c.key for c in res.changes} == {"name", "location", "led"}
    path, f = lib.writes[-1]
    assert path.endswith("form=mesh_sclient_detail")
    assert f == {"mac": SAT_A, "name": "Hallway", "location": "hallway", "led": "false", "preferred_wifi_uplink_type": "auto"}


def test_mesh_set_main_router_payload_and_rules():
    r, lib = fake_router()
    r.mesh.configure("Main", name="Core")
    assert lib.writes[-1][1] == {"mac": MAIN, "name": "Core", "location": "", "traffic_separation": "false",
                                 "backhaul_steering": "true"}
    with pytest.raises(UsageError):
        r.mesh.configure("Main", led=False)
    with pytest.raises(UsageError):
        r.mesh.configure("Hall", location="garage")               # not a router location enum value


def test_mesh_set_uplink_needs_router_candidate():
    r, lib = fake_router()
    with pytest.raises(UnsupportedError):                         # candidate list empty -> can't pin
        r.mesh.configure("Hall", uplink="Basement")
    resp = mesh_fixture()
    resp[("admin/easymesh_network?form=mesh_sclient_detail", "read", SAT_B)] |= {
        "name": "Basement", "location": "", "led": True, "preferred_wifi_uplink_type": "auto",
        "preferred_wifi_uplink_ap_available": [{"device_name": "Main", "mac": MAIN, "signal_strength": -60}]}
    r, lib = fake_router(resp)
    plan = r.mesh.configure("Basement", uplink="Main", dry_run=True)
    assert plan.dry_run and plan.changes[0].key == "uplink" and plan.side_effect
    res = r.mesh.configure("Basement", uplink="Main")
    f = lib.writes[-1][1]
    assert f["preferred_wifi_uplink_type"] == "manual" and json.loads(f["preferred_wifi_uplink_ap"]) == {"device_name": "Main", "mac": MAIN}
    assert not res.mismatches
    with pytest.raises(UsageError):
        r.mesh.configure("Basement", uplink="Hall")               # exists, but the router doesn't offer it


def test_backhaul_limited_and_weakest_device_fields():
    r, _ = fake_router()
    inv = r.mesh.inventory()
    sat_b = inv.node("Basement")
    assert sat_b.weakest_device == inv.weakest_device(sat_b).label == "tv"
    laptop = inv.device("laptop")                          # -60 dBm (good) behind a poor hop -> backhaul is the limit
    assert laptop.path_grade == "poor" and laptop.backhaul_limited
    assert not inv.device("tv").backhaul_limited             # its own -88 dBm is worse than the path
    assert [c.label for c in r.devices.list(inventory=inv, backhaul_limited=True)] == ["laptop"]


def test_port_rows_and_slow_port_issue():
    from tplink_router.services.system import port_rows
    from tplink_router.services.health import port_issues
    rows = port_rows([{"name": "wan", "status": "connected", "speed": 2500, "duplex": "FULL", "is_wan": True},
                      {"name": "lan1", "status": "unconnected", "speed": "", "duplex": ""},
                      {"name": "lan3", "status": "connected", "speed": 100, "duplex": "FULL"}])
    assert rows[1] == {"port": "lan1", "link": "down", "speed_mbps": None, "duplex": None, "wan": False}
    iss = port_issues(rows)
    assert [(i.code, i.subject, i.data["speed"]) for i in iss] == [("SLOW_PORT", "lan3", "100Mbps")]


def test_weakest_device_skips_stale_readings_and_ties_are_reported(cli_fake, capsys):
    run, r, lib = cli_fake
    inv = r.mesh.inventory()
    tv = inv.device("tv")
    tv.link_rx_mbps = tv.link_tx_mbps = 0                    # idle: its -88 dBm may be an old reading
    assert tv.signal_stale and inv.weakest_device("Basement").label == "laptop"
    assert inv.weakest_device("Basement", include_stale=True).label == "tv"
    assert run("mesh", "show", "--sort", "devices", "--no-ping") == 0
    out = capsys.readouterr().out
    assert "tiebreak=" in out and "rank=1 name=Hall" in out and "devices_behind=" in out


# -- web-UI parity writes: lists, access control, guest, LEDs, router diagnostics --

def test_port_forward_add_remove_round_trip():
    r, lib = fake_router()
    res = r.nat.add_forward("nas", "10.0.0.50", "8443", "443", "tcp")
    assert res.applied and res.changes[0].new == "present" and not res.mismatches
    path, f = lib.writes[-1]
    assert f["operation"] == "insert" and f["index"] == "0"
    assert json.loads(f["new"]) == {"name": "nas", "ipaddr": "10.0.0.50", "external_port": "8443", "internal_port": "443",
                                    "protocol": "TCP", "enable": "on"}
    assert not r.nat.add_forward("nas", "10.0.0.50", "8443").applied           # already there: no-op
    assert r.nat.remove_forward("nas").changes[0].new == "absent"
    assert lib.writes[-1][1] | {} == {"operation": "remove", "key": "nas", "index": "0"}
    with pytest.raises(UsageError):
        r.nat.add_forward("x", "10.0.0.5", "80;rm", None)


def test_block_turns_access_control_on_and_unblock_restores_it():
    r, lib = fake_router()
    with pytest.raises(UsageError):
        r.devices.block("phone#33-44-55")                    # the machine running tpctl (host_mac / HOST)
    plan = r.devices.block("tv", dry_run=True)
    assert [c.key for c in plan.changes] == ["access_control", "blocked[02-AA-00-00-00-03]"] and not lib.writes
    res = r.devices.block("tv")
    assert ("admin/access_control?form=enable", {"enable": "on"}) in lib.writes
    row = json.loads(next(f["new"] for p, f in lib.writes if f.get("operation") == "insert"))
    assert row["mac"] == "02-AA-00-00-00-03" and row["host"] == "NON_HOST" and row["deviceType"] == "TV" and row["key"]
    assert [x["mac"] for x in r.devices.blocked()] == ["02-AA-00-00-00-03"] and res.applied
    lib.responses[("admin/access_control?form=enable", "read")]["enable"] = "on"
    un = r.devices.unblock("02-AA-00-00-00-03")
    assert un.changes[-1].key == "access_control" and lib.writes[-1] == ("admin/access_control?form=enable", {"enable": "off"})


def test_guest_network_payload_non_6e_model():
    r, lib = fake_router()
    plan = r.wifi.set_guest(enable=True, password="hunter2hunter2", dry_run=True)
    keys = {c.key: c.target for c in plan.changes}
    assert keys == {"guest.2g5g.enable": "on", "guest.2g5g.encryption": "psk_sae",
                    "guest.2g5g.psk_version": "sae_transition", "guest.2g5g.psk_key": "***"}
    with pytest.raises(UsageError):
        r.wifi.set_guest(password="short")


def test_night_mode_and_change_key_prefix():
    r, lib = fake_router()
    res = r.system.set_night_mode(True, "23:30", "06:30")
    assert [c.key for c in res.changes] == ["night_mode.enable", "night_mode.time_start", "night_mode.time_end"]
    assert lib.writes[-1][1] == {"enable": "on", "time_start": "23:30", "time_end": "06:30", "ledpm_support": "yes"}
    with pytest.raises(UsageError):
        r.system.set_night_mode(True, "25:00")


def test_router_diag_parses_ping_output():
    r, lib = fake_router()
    out = ('PING 1.1.1.1 (1.1.1.1): 64 data bytes\nReply from 1.1.1.1: bytes=64 ttl=55 seq=1 time=5.6 ms\n\n'
           '--- Ping Statistic "1.1.1.1" ---\nPackets: Sent=3, Received=3, Lost=0 (0.00% loss)\n'
           'Round-trip min/avg/max = 3.708/4.831/5.657 ms')
    lib.responses[("admin/diag?form=diag", "start")] = {"finish": "0", "result": ""}
    lib.responses[("admin/diag?form=diag", "continue")] = {"finish": "1", "result": out}
    d = r.system.diagnose("1.1.1.1", count=3)
    assert d["finished"] and d["loss_pct"] == 0.0 and d["avg_ms"] == 4.831 and d["received"] == 3
    with pytest.raises(UsageError):
        r.system.diagnose("1.1.1.1; reboot")


def test_settings_area_reads_and_redacts_only_secrets():
    r, lib = fake_router()
    lib.responses[("admin/wireless?form=guest", "read")] = {"access": "off"}
    lib.responses[("admin/wireless?form=guestnetwork_bandwidth_ctrl", "read")] = {"enable_2g": "off"}
    d = r.settings.get("guest")
    assert d["values"]["bands.guest_2g5g_enable"] == "off" and d["values"]["access.access"] == "off"
    from tplink_router.services.settings import redact
    assert redact({"psk_key": "x", "psk_version": "rsn", "wps_pin": "1234"}) == {"psk_key": "***", "psk_version": "rsn", "wps_pin": "***"}


def test_speedtest_helpers_follow_cloudflare_headers():
    from tplink_router.speedtest import _http as s
    from types import SimpleNamespace as NS
    resp = NS(getheaders=lambda: [("Server-Timing", "cfSpeedEdge;dur=4, cfSpeedWorker;dur=22"),
                                   ("server-timing", 'cfL4;desc="?proto=TCP&rtt=11829&min_rtt=11167&rtt_var=1768"')])
    assert s.server_ms(resp) == 26.0 and s.tcp_rtt_ms(resp) == 11.829
    assert s.latency_ms(0.0426, resp) == 11.829                   # TCP RTT wins over the noisy HTTP figure
    assert round(s.p90([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]), 1) == 910.0
    assert s.jitter([10, 12, 11]) == 1.5


def test_speed_summary_order_and_thin_bars():
    from tplink_router.speedtest import view as v
    from tplink_router.speedtest import SpeedResult
    r = SpeedResult(download_mbps=900, upload_mbps=300, latency_ms=5.0, jitter_ms=0.5, loaded_latency_down_ms=12,
                    loaded_latency_up_ms=9, server={"city": "Kuala Lumpur", "colo": "KUL"}, client={"isp": "ISP"})
    text = "\n".join(v.summary(r, "wired"))
    assert text.index("Latency") < text.index("Download") < text.index("Upload") < text.index("Bloat")
    assert "█" not in text and "━" in text


def test_probe_guess_uses_bonjour_names_vendor_and_router_context():
    from tplink_router.probe import guess, is_randomized, _bonjour_fields
    assert guess({"mesh_role": "satellite_router", "open_ports": [22, 80]})[0] == "mesh satellite (router)"
    assert guess({"this_machine": True})[0].startswith("this computer")
    assert guess({"bonjour_host": "ps5-89c531.local"})[0] == "PlayStation"
    d = _bonjour_fields({"names": ["KK的MacBook Pro"], "services": ["_companion-link._tcp"], "txt": {"model": "Mac15,6"}})
    assert guess(d)[0] == "MacBook Pro (Mac15,6)"
    assert guess({"router_name": "AlicesAir", "vendor": "Apple, Inc."})[0] == "MacBook Air"
    assert guess({"bonjour_services": ["_googlecast._tcp"], "cast_name": "Living Room TV"})[0].endswith("Living Room TV")
    assert guess({"open_ports": [62078]})[0] == "iPhone / iPad"
    assert guess({"router_type": "Mobile"})[0] == "phone / tablet"
    assert is_randomized("02-00-00-00-00-01") and not is_randomized("00-11-22-33-44-56")


def test_oui_vendor_lookup_from_cached_manuf(tmp_path):
    from tplink_router.probe import oui_vendor
    (tmp_path / "manuf.txt").write_text("# comment\nD0:11:E5\tApple\tApple, Inc.\n8C:90:2D\tTPLink\tTP-Link Systems Inc.\n"
                                        "00:1B:C5:00:00:00/36\tConverg\tConverging Systems Inc.\n")
    assert oui_vendor("d0:11:e5:8d:b1:6b", tmp_path, download=False) == ("Apple, Inc.", "vendor list")
    assert oui_vendor("00-1B-C5-00-00-01", tmp_path, download=False)[0] == "Converging Systems Inc."
    assert oui_vendor("02-AA-00-00-00-01", tmp_path, download=False)[0] is None      # private MAC


def test_empty_device_list_is_retried_then_flagged():
    resp = mesh_fixture()
    resp[("admin/easymesh_network?form=get_mesh_device_list_all", "read")][0]["client_num"] = 3
    rows = resp[("admin/smart_network?form=game_accelerator", "loadSpeed")]
    resp[("admin/smart_network?form=game_accelerator", "loadSpeed")] = []
    r, lib = fake_router(resp)
    inv = r.mesh.inventory()
    assert inv.devices == [] and any("empty device list" in w["detail"] for w in r.warnings)
    from tplink_router.services.health import verdict
    assert verdict([], r.warnings)["status"] == "partial"


def test_cloudflare_engine_runs_end_to_end_on_a_stub(monkeypatch):
    """Exercises every code path of the default engine (single + parallel) without network."""
    from types import SimpleNamespace as NS
    from tplink_router.speedtest import cloudflare as cfl
    import http.client

    hdrs = [("Server-Timing", "cfSpeedEdge;dur=2, cfSpeedWorker;dur=5"), ("server-timing", 'cfL4;desc="?rtt=6000&min_rtt=5000"')]

    class FakeConn:
        def __init__(self, timeout, host=None):
            pass

        def request(self, method, path, body=None):
            size = int(path.split("bytes=")[1]) if "bytes=" in path else len(body or b"")
            resp = NS(getheaders=lambda: hdrs, getheader=lambda k: None)
            if method == "POST":                     # the body goes up before the first response byte
                return 0.006 + max(size / 100e6, 0.012), 0.0, 0, resp
            return 0.008, max(size / 100e6, 0.012), size, resp

        def close(self):
            pass

    monkeypatch.setattr(cfl, "Conn", FakeConn)
    monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    for n in (1, 3):
        seen = []
        res = cfl.run(seen.append, upload=True, connections=n)
        assert res.ok and res.upload_mbps and res.latency_ms == 6.0 and res.jitter_ms == 0.0
        assert {p.phase for p in seen} >= {"latency", "download", "upload", "done"}


def test_speed_cli_records_line_with_stub_engine(cli_fake, capsys, monkeypatch):
    from tplink_router import speedtest
    from tplink_router.speedtest import SpeedResult
    monkeypatch.setattr(speedtest, "run", lambda *a, **k: SpeedResult(download_mbps=900.0, upload_mbps=400.0, latency_ms=5.0,
                                                                       jitter_ms=0.5, loaded_latency_down_ms=9.0))
    run, r, lib = cli_fake
    assert run("net", "speed") == 0
    out = capsys.readouterr().out
    assert "speed download=900.0Mbps upload=400.0Mbps" in out and "bufferbloat=4.0ms" in out


def test_sort_devices_tiebreak_counts_downstream_nodes(cli_fake, capsys):
    run, r, lib = cli_fake
    resp = lib.responses
    # Hall (1 device, Basement behind it) vs Basement (2 devices): make devices_behind tie at 2 for a clean check
    resp[("admin/smart_network?form=game_accelerator", "loadSpeed")] = [
        x for x in resp[("admin/smart_network?form=game_accelerator", "loadSpeed")] if x["mac"] != "02-AA-00-00-00-01"]
    assert run("mesh", "show", "--sort", "devices", "--no-ping") == 0
    out = capsys.readouterr().out
    assert out.index("name=Hall") < out.index("name=Basement")      # tie on devices_behind; Hall has a node behind it


def test_diagnosis_does_not_say_ok_for_a_bad_signal_with_clean_ping():
    from tplink_router.cli.net import _diagnose
    from types import SimpleNamespace as NS
    rows = [{"name": "device:x", "loss_pct": 0, "avg_ms": 5.8, "jitter_ms": 0.9}, {"name": "node:n", "avg_ms": 2.8, "jitter_ms": 0.5},
            {"name": "internet:1.1.1.1", "avg_ms": 6.0}]
    bad = NS(signal_grade="bad", signal_dbm=-85, path_grade=None, weakest_hop=None, link_rx_mbps=288, link_tx_mbps=18)
    d = _diagnose(rows, bad)
    assert d["verdict"] == "last_hop_signal" and d["basis"] == "ping+signal" and "18 Mbps" in d["why"]


def test_every_record_kind_is_documented_in_schema():
    import glob
    import re
    from tplink_router.cli.meta import _records
    kinds = set()
    for f in glob.glob(str(Path(__file__).resolve().parents[1] / "src/tplink_router/**/*.py"), recursive=True):
        kinds |= set(re.findall(r'\bline\(\s*"([a-z_]+)"', open(f).read()))
    documented = {k for key in _records() for k in key.split("/")}
    assert not sorted(kinds - documented), "undocumented record kinds: add them to cli/meta.py RECORDS"


def test_upnp_and_dmz_payloads():
    r, lib = fake_router()
    lib.responses[("admin/upnp?form=enable", "read")] = {"enable": "on"}
    lib.responses[("admin/nat?form=dmz", "read")] = {"enable": "off", "ipaddr": ""}
    res = r.nat.set_upnp(False)
    assert lib.writes[-1] == ("admin/upnp?form=enable", {"enable": "off"}) and res.changes[0].key == "upnp.enable"
    r.nat.set_dmz(True, "10.0.0.50")
    assert lib.writes[-1] == ("admin/nat?form=dmz", {"enable": "on", "ipaddr": "10.0.0.50"})
    with pytest.raises(UsageError):
        r.nat.set_dmz(True, None)
    r.nat.set_dmz(False)
    assert lib.writes[-1] == ("admin/nat?form=dmz", {"enable": "off"})


def test_dhcp_set_dns_payload():
    r, lib = fake_router()
    res = r.dhcp.set_dns("1.1.1.1", "8.8.8.8")
    assert [c.key for c in res.changes] == ["dhcp.pri_dns", "dhcp.snd_dns"]
    body = lib.writes[-1][1]
    assert body["pri_dns"] == "1.1.1.1" and body["snd_dns"] == "8.8.8.8" and body["leasetime"] == "120"   # whole form
    with pytest.raises(UsageError):
        r.dhcp.set_dns("1.1.1")


def test_snapshot_diff_lists_per_row():
    from tplink_router.services.snapshots import diff_dicts
    a = {"admin/dhcps?form=reservation": {"list": [{"mac": "AA", "ip": "10.0.0.5", "comment": "Alpha"}]}}
    b = {"admin/dhcps?form=reservation": {"list": [{"mac": "AA", "ip": "10.0.0.5", "comment": "Alpha dev"},
                                                   {"mac": "BB", "ip": "10.0.0.6", "comment": "nas"}]}}
    ch = {c.key: (c.old, c.new) for c in diff_dicts(a, b)}
    assert any(k.endswith("[AA].comment") and v == ("Alpha", "Alpha dev") for k, v in ch.items())
    assert any(k.endswith("[BB]") and v[0] == "absent" for k, v in ch.items()) and len(ch) == 2


def test_snapshot_diff_groups_rename_fields():
    from tplink_router.services.snapshots import diff_dicts
    a = {"admin/dhcps?form=reservation": {"list": [{"mac": "AA", "comment": "Alpha", "hostname": "Alpha"}]}}
    b = {"admin/dhcps?form=reservation": {"list": [{"mac": "AA", "comment": "Beta", "hostname": "Beta"}]}}
    ch = diff_dicts(a, b)
    assert len(ch) == 1 and ch[0].key.endswith("[AA].name") and (ch[0].old, ch[0].new) == ("Alpha", "Beta")


def test_labels_are_sticky_when_other_devices_leave():
    from tplink_router.models import Device
    mk = lambda mac: Device(label="", name="iPhone", ip="", mac=mac, band="5G", node="n")
    mem = {}
    a, b = mk("02-00-00-00-00-AA"), mk("02-00-00-00-00-BB")
    assign_labels([a, b], mem)
    assert a.label == "iPhone#00-00-AA" and b.label == "iPhone#00-00-BB"
    a2 = mk("02-00-00-00-00-AA")
    assign_labels([a2], mem)                       # b left: a keeps its suffixed label
    assert a2.label == "iPhone#00-00-AA"
    c = mk("02-00-00-00-00-CC")
    assign_labels([c], mem)                        # a newcomer with a name that collided before gets a suffix too
    assert c.label == "iPhone#00-00-CC"
    solo = Device(label="", name="PS5", ip="", mac="02-66-77-88-99-AA", band="5G", node="n")
    assign_labels([solo], mem)
    assert solo.label == "PS5"


def test_mass_vanish_is_not_recorded_as_departures():
    r, lib = fake_router()
    inv = r.mesh.inventory()
    r.health.changes_since_last(inv)                         # baseline with 4 devices -> need >= 5: add one
    resp = lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")]
    resp.append(dict(resp[0], mac="00-11-22-33-44-66", deviceName="extra"))
    r.health.changes_since_last(r.mesh.inventory())
    resp.clear()
    lib.responses[("admin/dhcps?form=client", "load")] = [{"macaddr": "00-11-22-33-44-55"}]
    empty = r.mesh.inventory()
    ch = r.health.changes_since_last(empty)
    assert empty.devices == [] and not any(c["what"] == "device_left" for c in ch.get("changes", []))
    assert any("glitch" in w["detail"] for w in r.warnings)


def test_no_undefined_names_pyflakes():
    """Catches renamed helpers / typos that only crash at runtime (round-3 `_httplatency_ms`, round-8 `unresolved`)."""
    import shutil
    import subprocess
    if not shutil.which("uvx"):
        pytest.skip("uvx not available")
    src = str(Path(__file__).resolve().parents[1] / "src" / "tplink_router")
    out = subprocess.run(["uvx", "pyflakes", src], capture_output=True, text=True, timeout=120).stdout
    bad = [ln for ln in out.splitlines() if "undefined name" in ln or "syntax" in ln.lower()]
    assert not bad, "\n".join(bad)


def test_settings_set_switch_uses_native_values_and_whitelist():
    r, lib = fake_router()
    lib.responses[("admin/security_settings?form=new_enable", "read")] = {"enable": "on", "lan_ping": "on", "wan_ping": "off"}
    lib.responses[("admin/wireless_schedule_v2?form=settings", "read")] = {"enable": "no", "max_rules": "20"}
    res = r.settings.set_switch("firewall.wan_ping", True)
    assert lib.writes[-1] == ("admin/security_settings?form=new_enable", {"enable": "on", "lan_ping": "on", "wan_ping": "on"})
    assert res.changes[0].key == "firewall.wan_ping" and not res.mismatches
    r.settings.set_switch("schedules.wifi_schedule", True)
    assert lib.writes[-1] == ("admin/wireless_schedule_v2?form=settings", {"enable": "yes"})       # yes/no vocabulary
    with pytest.raises(UsageError):
        r.settings.set_switch("wan.password", True)


def test_switch_vocabularies_compare_equal_in_verification():
    from tplink_router.models import Change
    assert Change("k", "no", True, "true").verified and Change("k", True, False, "no").verified
    assert not Change("k", "off", "on", "off").verified and not Change("k", False, "no", None).changed


def test_iot_network_toggle_payload():
    r, lib = fake_router()
    lib.responses[("admin/wireless?form=iot_2g&form=iot_5g", "read_spf")] = {"iot_2g_enable": "off", "iot_2g_ssid": "IoT",
                                                                               "iot_5g_enable": "off", "iot_5g_ssid": "IoT-5G"}
    plan = r.wifi.set_iot(True, dry_run=True)
    assert [c.key for c in plan.changes] == ["iot.2g.enable", "iot.5g.enable"] and plan.dry_run
    res = r.wifi.set_iot(True)
    path, f = lib.writes[-1]
    assert path == "admin/wireless?form=iot_2g&form=iot_5g" and f == {"iot_2g_enable": "on", "iot_5g_enable": "on"}


def test_port_trigger_uses_json_insert(monkeypatch):
    r, lib = fake_router()
    lib.responses[("admin/nat?form=pt", "load")] = {"list": []}
    sent = []

    def fake_json(ep, op, **f):
        sent.append((ep, op, f))
        lib.responses[("admin/nat?form=pt", "load")]["list"].insert(0, f["new"])
    monkeypatch.setattr(r, "call_json", fake_json)
    res = r.nat.add_trigger("game", "6112", "6112-6119", "tcp", "udp")
    assert res.changes[0].new == "present"
    ep, op, f = sent[0]
    assert op == "insert" and f["index"] == 0 and f["new"] == {"name": "game", "trigger_port": "6112", "trigger_protocol": "TCP",
                                                                   "external_port": "6112-6119", "external_protocol": "UDP", "enable": "on"}
    with pytest.raises(UsageError):
        r.nat.add_trigger("x", "61;2", "1")


def test_static_route_and_binding_payloads(monkeypatch):
    r, lib = fake_router()
    lib.responses[("admin/network?form=routes_static", "load")] = {"list": []}
    lib.responses[("admin/imb?form=bind_list", "load")] = {"list": []}
    sent = []

    def fake_json(ep, op, **f):
        sent.append((ep, f))
        row = f["new"][0] if isinstance(f["new"], list) else f["new"]
        lib.responses[(ep, "load")]["list"].insert(0, row)
    monkeypatch.setattr(r, "call_json", fake_json)
    r.lan.add_route("lab", "10.20.0.0/24", "10.0.0.2")
    ep, f = sent[-1]
    assert ep.endswith("routes_static") and isinstance(f["new"], dict)          # object, like the web UI
    assert {k: f["new"][k] for k in ("target", "gateway", "interface", "enable")} == \
        {"target": "10.20.0.0/24", "gateway": "10.0.0.2", "interface": "LAN", "enable": "on"}
    r.lan.bind("00-11-22-33-44-55", ip="10.0.0.20")
    ep, f = sent[-1]
    assert ep.endswith("bind_list") and f["new"] == [{"name": "00-11-22-33-44-55", "ipaddr": "10.0.0.20",
                                                       "mac": "00-11-22-33-44-55", "key": "10.0.0.20", "enable": "on"}]
    with pytest.raises(UsageError):
        r.lan.add_route("x", "10.20.0.0/33", "10.0.0.2")


def _dhcp_router(monkeypatch):
    r, lib = fake_router()
    lib.responses[("admin/network?form=lan_ipv4", "read")] = {"ipaddr": "10.0.0.1", "mask_type": "255.255.255.0"}
    lib.responses[("admin/dhcps?form=client", "load")] = [{"macaddr": "02-AA-00-00-00-01", "ipaddr": "10.0.0.21", "name": "phone"},
                                                          {"macaddr": "02-AA-00-00-00-09", "ipaddr": "10.0.0.250", "name": "old"}]
    lib.responses[("admin/dhcps?form=reservation", "load")] = {"list": [
        {"mac": "02-AA-00-00-00-01", "ip": "10.0.0.21", "comment": "phone", "hostname": "phone", "enable": "on"},
        {"mac": "02-AA-00-00-00-02", "ip": "10.0.0.50", "comment": "laptop", "hostname": "laptop", "enable": "on"}]}
    sent = []

    def fake_json(ep, op, **f):
        sent.append((ep, op, f))
        rows = lib.responses[(ep, "load")]["list"]
        if op == "update":
            i = next(i for i, x in enumerate(rows) if x["mac"] == f["old"]["mac"])
            rows[i] = {k: v for k, v in f["new"].items() if k != "key"}
    monkeypatch.setattr(r, "call_json", fake_json)
    return r, lib, sent


def test_dhcp_range_and_gateway_validation(monkeypatch):
    r, lib, _ = _dhcp_router(monkeypatch)
    for a, b in (("10.0.0.1", "10.0.0.50"), ("10.0.0.90", "10.0.0.20"), ("10.0.1.5", "10.0.1.9"), ("10.0.0.0", "10.0.0.9")):
        with pytest.raises(UsageError):
            r.dhcp.set_range(a, b, dry_run=True)
    plan = r.dhcp.set_range("10.0.0.100", "10.0.0.199", dry_run=True)
    assert [(c.key, c.new) for c in plan.changes] == [("dhcp.ipaddr_start", "10.0.0.100"), ("dhcp.ipaddr_end", "10.0.0.199")]
    cav = r.dhcp.range_impact("10.0.0.100", "10.0.0.199")
    assert any("offline" in c for c in cav) and any("would be disabled" in c and "phone" in c for c in cav)
    assert not any("get a new address" in c and "phone" in c for c in cav)            # reserved: doesn't move
    res = r.dhcp.set_range("10.0.0.100", "10.0.0.199")
    assert lib.writes[-1][1]["ipaddr_start"] == "10.0.0.100" and lib.writes[-1][1]["leasetime"] == "120"   # whole form
    assert r.dhcp.get()["pool_size"] == 100 and not res.mismatches
    assert r.dhcp.set_gateway("router", dry_run=True).changes[0].new == "10.0.0.1"
    r.dhcp.set_server(False)
    assert lib.writes[-1] == ("admin/dhcps?form=setting", {"enable": "off"})          # partial, like the web UI


def test_dhcp_reservation_edit_and_overview(monkeypatch):
    r, lib, sent = _dhcp_router(monkeypatch)
    with pytest.raises(UsageError):
        r.dhcp.edit("02-AA-00-00-00-02", ip="10.0.0.21")                              # taken by phone
    with pytest.raises(UsageError):
        r.dhcp.edit("02-AA-00-00-00-02")                                              # nothing to change
    res = r.dhcp.edit("02-aa-00-00-00-02", enable=False)
    ep, op, f = sent[-1]
    assert op == "update" and isinstance(f["new"], dict) and f["new"]["enable"] == "off" and f["old"]["enable"] == "on"
    assert f["new"]["key"] == f["old"]["key"] == f["key"] and res.changes[0].new == "off"
    res = r.dhcp.reserve("02-AA-00-00-00-02", ip="10.0.0.60")                        # existing: edited in place
    assert sent[-1][1] == "update" and sent[-1][2]["new"]["ip"] == "10.0.0.60" and sent[-1][2]["new"]["enable"] == "on"
    with pytest.raises(UsageError, match="outside the DHCP pool"):
        r.dhcp.edit("02-AA-00-00-00-02", ip="10.0.0.230")
    lib.responses[("admin/dhcps?form=client", "load")].append({"macaddr": "02-AA-00-00-00-09", "ipaddr": "10.0.0.199", "name": "old"})
    with pytest.raises(UsageError, match="still leased to old"):                     # router wouldn't apply it
        r.dhcp.edit("02-AA-00-00-00-02", ip="10.0.0.199")
    st = {x["mac"]: x["state"] for x in r.dhcp.overview()}
    assert st["02-AA-00-00-00-01"] == "in_use" and st["02-AA-00-00-00-02"] == "pending_renewal"


def test_system_log_oldest_first():
    r, lib = fake_router()
    r.caps.has = lambda *_: True
    r.caps.require = lambda *_: None
    lib.responses[("admin/syslog?form=log", "load")] = [{"time": "2026-01-02 10:00:00", "content": "b"},
                                                        {"time": "2026-01-01 10:00:00", "content": "a"}]
    assert [x["content"] for x in r.system.log()] == ["a", "b"]


def test_overview_recent_writes():
    r, lib = fake_router()
    r.snapshots.save("dhcp-edit")
    r.snapshots.save("auto-daily")
    assert [x["tag"] for x in r.health.recent_writes(None)] == ["dhcp-edit"]


def test_dhcp_unreserve_reenables_collateral(monkeypatch):
    r, lib, sent = _dhcp_router(monkeypatch)
    orig = lib.request

    def request(path, body):                          # router quirk: removing one row switches another off
        out = orig(path, body)
        if "operation=remove" in body:
            for x in lib.responses[(path, "load")]["list"]:
                x["enable"] = "off"
        return out
    lib.request = request
    res = r.dhcp.unreserve("02-AA-00-00-00-01")
    rows = lib.responses[("admin/dhcps?form=reservation", "load")]["list"]
    assert [x["mac"] for x in rows] == ["02-AA-00-00-00-02"] and rows[0]["enable"] == "on"
    assert "re-enabled" in res.side_effect and sent[-1][1] == "update"


def test_time_set_payload_and_zones():
    from tplink_router.services.system import resolve_timezone
    r, lib = fake_router()
    lib.responses[("admin/time?form=settings", "read")] = {"type": "auto", "timezone": "109", "ntp_svr1": "a.pool",
                                                           "ntp_svr2": "b.pool", "date": "10/22/2026", "time": "20:00:00"}
    assert resolve_timezone("singapore") == "109" and resolve_timezone("utc") == "47" and resolve_timezone("107") == "107"
    with pytest.raises(UsageError):
        resolve_timezone("standard")
    res = r.system.set_time(("asia.pool.ntp.org",), "china")
    assert lib.writes[-1] == ("admin/time?form=settings", {"type": "auto", "timezone": "107", "ntp_svr1": "asia.pool.ntp.org",
                                                            "ntp_svr2": ""})        # the UI's payload, no date/time
    assert not res.mismatches
    with pytest.raises(UsageError):
        r.system.set_time()


def test_ipv6_allow_payload(monkeypatch):
    r, lib = fake_router()
    lib.responses[("admin/nat?form=fr6", "load")] = {"list": []}
    sent = []

    def fake_json(ep, op, **f):
        sent.append((ep, op, f))
        lib.responses[(ep, "load")]["list"].insert(0, f["new"])
    monkeypatch.setattr(r, "call_json", fake_json)
    with pytest.raises(UsageError):
        r.nat.add_ipv6_allow("x", "fe80::1", "443")                                 # not global
    r.nat.add_ipv6_allow("nas", "2a00:1450:4001:80b::200e", "443", "tcp")
    ep, op, f = sent[-1]
    assert op == "insert" and isinstance(f["new"], dict) and {k: f["new"][k] for k in ("name", "ip", "port", "protocol", "type",
                                                                                       "ip_type", "enable")} == \
        {"name": "nas", "ip": "2a00:1450:4001:80b::200e", "port": "443", "protocol": "TCP", "type": "CUSTOM",
         "ip_type": "custom", "enable": "on"}


def test_upgrade_skips_when_latest():
    r, lib = fake_router()
    lib.responses[("admin/cloud_account?form=cloud_upgrade", "read")] = {"latest_flag": True, "latest_version": "1.5 rel.1"}
    lib.responses[("admin/firmware?form=upgrade", "read")] = {"firmware_version": "1.5 rel.1(55)"}
    res = r.system.upgrade(dry_run=True)
    assert not res.any_changed and not res.applied and not lib.writes


def test_qos_bandwidth_limit_and_schedule(monkeypatch):
    r, lib = fake_router()
    from tplink_router.services.capabilities import CapabilityService
    monkeypatch.setattr(CapabilityService, "bands", property(lambda self: ["2g", "5g"]))
    lib.responses[("admin/smart_network?form=qos", "read")] = {
        "enable": "off", "up_band": "1000", "max_up_band": 1000, "up_unit": "mbps", "down_band": "1000",
        "max_down_band": 1000, "down_unit": "mbps", "max_wan_speed": 1000, "high": "90"}
    with pytest.raises(UsageError):
        r.qos.set(True)                                               # on at max bandwidth shapes nothing
    with pytest.raises(UsageError):
        r.qos.set(down_mbps=5000)
    r.qos.set(True, 900, 45)
    path, f = lib.writes[-1]
    assert path == "admin/smart_network?form=qos" and set(f) == {"enable", "up_band", "max_up_band", "up_unit", "down_band",
                                                                 "max_down_band", "down_unit"}   # the UI's fields only
    assert (f["enable"], f["down_band"], f["up_band"]) == ("on", "900", "45")
    lib.responses[("admin/smart_network?form=client_speed_limit", "read_max")] = {"downloadLimitMax": 10240000, "uploadLimitMax": 10240000}
    for x in lib.responses[("admin/smart_network?form=game_accelerator", "loadSpeed")]:
        x |= {"enableLimit": "off", "downloadLimit": 1024, "uploadLimit": 200}
    plan = r.qos.set_limit("02-AA-00-00-00-02", down_mbps=10, dry_run=True)
    assert plan.dry_run and not lib.writes[-1][0].endswith("client_speed_limit")
    r.qos.set_limit("02-AA-00-00-00-02", down_mbps=10, up_mbps=2)
    assert lib.writes[-1] == ("admin/smart_network?form=client_speed_limit",
                              {"mac": "02-AA-00-00-00-02", "enableLimit": "on", "downloadLimit": "10240", "uploadLimit": "2048"})
    lib.responses[("admin/wireless_schedule_v2?form=settings", "read")] = {"enable": False, "max_rules": 20, "list": {}}
    with pytest.raises(UsageError):
        r.qos.add_schedule("23:00", "07:00", "funday")
    res = r.qos.add_schedule("23:00", "07:00", "weekdays", dry_run=True)
    assert "switch is OFF" in res.side_effect and res.changes[0].key == "wifi_schedule[23:00-07:00 mon,tue,wed,thu,fri 2g,5g]"


def test_weakest_device_prefers_a_weak_just_roamed_over_a_strong_settled():
    from tplink_router.models import Device, Inventory, Node
    mk = lambda label, dbm, roamed=False: Device(label=label, name=label, ip="", mac=label, band="5G", node="N",
                                                 signal_dbm=dbm, signal_grade="poor" if dbm < -75 else "excellent",
                                                 link_tx_mbps=100, link_rx_mbps=100, just_roamed=roamed)
    n = Node(name="N", role="satellite_router", model="", ip="", mac="N", status="connected")
    inv = Inventory([n], [mk("strong", -49), mk("roamer", -77, True)])
    assert inv.weakest_device(n).label == "roamer"
    inv2 = Inventory([n], [mk("weak", -78), mk("roamer", -85, True)])
    assert inv2.weakest_device(n).label == "weak"                  # a settled weak device still wins


def test_parental_payloads():
    r, lib = fake_router()
    lib.responses[("admin/avira_parental_control?form=avira_pactrl", "getOwnerTotalData")] = {
        "ownerList": [{"ownerId": 0, "name": "Kids", "internetBlocked": False, "clientList": [{"mac": "02-AA-00-00-00-02", "name": "laptop"}],
                       "bedtime": {"enable": True, "everyday": {"bedtimeBegin": 1290, "bedtimeEnd": 420}}}], "totalOwnerMax": 16}
    p = r.parental.profiles()[0]
    assert p["bedtime"] == "21:30-07:00" and p["devices"] == ["laptop"] and not p["internet_blocked"]
    plan = r.parental.add("Teen", ("02-AA-00-00-00-01",), "22:00-06:30", dry_run=True)
    assert plan.dry_run and [c.key for c in plan.changes][0] == "parental[Teen]"
    with pytest.raises(UsageError):
        r.parental.add("Kids", ())                                    # exists
    with pytest.raises(UsageError):
        r.parental.add("X", (), "25:00-06:00", dry_run=True)
    assert r.parental.pause("kid", True, dry_run=True).changes[0].new is True   # partial name match


def test_node_flapping_and_rejoining_states():
    from tplink_router.services.health import inventory_issues
    r, lib = fake_router()
    inv = r.mesh.inventory()
    sat = next(n for n in inv.nodes if not n.is_main)
    sat.status_flaps_30m = 4
    assert any(i.code == "NODE_FLAPPING" and i.data["status_changes_1h"] == 4 for i in inventory_issues(inv))
    sat.ip = "0.0.0.0"                                         # associated but no IP: offline, and still flapping
    off = next(i for i in inventory_issues(inv) if i.code == "NODE_OFFLINE")
    assert off.data["state"] == "flapping" and "--yes" not in off.hint
    sat.status_flaps_30m = 0
    off = next(i for i in inventory_issues(inv) if i.code == "NODE_OFFLINE")
    assert off.data["state"] == "rejoining" and "mesh wait" in off.hint


def test_grade_flap_is_not_merged_across_an_outage():
    from tplink_router.cli.events import _collapse_flaps
    ev = [{"at": "2026-01-01T10:00:00", "what": "node_status", "subject": "N", "old": "connected", "new": "disconnected"},
          {"at": "2026-01-01T10:00:00", "what": "node_grade", "subject": "N", "old": "good", "new": "bad"},
          {"at": "2026-01-01T10:02:00", "what": "node_grade", "subject": "N", "old": "bad", "new": "good"}]
    assert not any(e["what"] == "node_grade_flap" for e in _collapse_flaps(ev))
    noise = [{"at": "2026-01-01T10:00:00", "what": "node_grade", "subject": "N", "old": "good", "new": "fair"},
             {"at": "2026-01-01T10:03:00", "what": "node_grade", "subject": "N", "old": "fair", "new": "good"}]
    out = _collapse_flaps(noise)
    assert out[0]["what"] == "node_grade_flap" and out[0]["near_boundary"] == "yes"


def test_implausible_signal_is_maybe_stale():
    from tplink_router.models import Device
    d = Device(label="mac", name="mac", ip="", mac="m", band="6G", node="N", signal_dbm=-89, signal_grade="bad",
               link_rx_mbps=1087, link_tx_mbps=1441)
    assert d.signal_stale                                   # -89 dBm can't carry 1.4 Gbps
    d2 = Device(label="ph", name="ph", ip="", mac="p", band="5G", node="N", signal_dbm=-86, signal_grade="bad",
                link_rx_mbps=150, link_tx_mbps=20)
    assert not d2.signal_stale
