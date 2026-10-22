import click

from .. import human as h
from .. import output as o
from ..services.wifi import BANDS
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option
from ..services.wifi import SIDE_EFFECT

BAND = click.Choice(BANDS)


@click.group("wifi")
def wifi():
    """Wi-Fi networks and radios, guest Wi-Fi. Every change is planned first, snapshotted, then verified."""


@wifi.command("show")
@json_option
@pass_state
def show(st):
    """Smart connect, ofdma_mimo, region, guest/IoT nets, per-band SSID/channel/width/power/security."""
    w = st.r.wifi.get()

    def human():
        rows = [[b.replace("g", "G"), x["ssid"] or "", x["enable"], str(x["channel"]) + h.dim(f" ({x['channel_setting']})"),
                 x["width"] if x["width"] in (None, "auto") else f"{x['width']} MHz", x["txpower"] or "",
                 x["security"]] for b, x in w["bands"].items()]
        return [h.kvline("Settings", f"smart connect {w['smart_connect']} · OFDMA/MU-MIMO {w['ofdma_mimo']} · region {w['region']}"
                         f" · guest {w['guest_network']}"), "",
                *h.table(["BAND", "SSID", "ON", "CHANNEL", "WIDTH", "POWER", "SECURITY"], rows)]
    st.out(w, o.wifi_lines(w), human)


@wifi.group("set")
def set_():
    """Change a Wi-Fi setting. Disruptive: without --yes prints the plan and exits 4.

    \b
    Examples:
      tpctl wifi set txpower middle --band 5g --dry-run
      tpctl wifi set txpower middle --band 5g --yes
      tpctl wifi set smart-connect on --yes
    """


@set_.command("band")
@click.argument("band", type=BAND)
@click.option("--channel", help="Channel number or auto.")
@click.option("--width", type=click.Choice(["20", "40", "80", "160", "320", "auto"]))
@click.option("--txpower", type=click.Choice(["low", "middle", "high"]))
@dry_run_option
@yes_option
@json_option
@pass_state
def band_cmd(st, band, channel, width, txpower, dry_run, yes):
    """Several settings of ONE band in a single write (one radio restart, one confirmation).

    \b
    Example: tpctl wifi set band 5g --width 80 --channel 36
    """
    kinds = [k for k, v in (("channel", channel), ("width", width), ("txpower", txpower)) if v]

    def cav():
        return st.r.wifi.caveats("band", [band], {"channel": channel, "width": width, "txpower": txpower})
    run_disruptive(st, lambda dr: st.r.wifi.set_band(band, channel, width, txpower, dry_run=dr), yes, dry_run,
                   st.r.wifi.effect_for([band]), st.r.wifi.scope_for([band]), cav if kinds else None,
                   effect_fields=st.r.wifi.effect_counts([band]))


def _band_and_value(args: tuple, band_opt: str | None, what: str) -> tuple[str, str]:
    """`<value> --band B` (preferred) or legacy positional `<band> <value>`."""
    if len(args) == 2 and args[0] in BANDS:
        return args[0], args[1]
    if len(args) == 1 and band_opt:
        return band_opt, args[0]
    if len(args) == 1:
        raise click.UsageError(f"{what} applies to one band: add --band 2g|5g|6g (e.g. `tpctl wifi set {what} {args[0]} --band 5g`)")
    raise click.UsageError(f"expected `tpctl wifi set {what} <value> --band <band>`")


def _done(st, res):
    st.out(res, o.write_lines(res), lambda: h.write(res))


@set_.command("smart-connect")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--ssid-2g", help="With off: name for 2.4G (same plan).")
@click.option("--ssid-5g", help="With off: name for 5G (same plan).")
@click.option("--ssid-6g", help="With off: name for 6G (same plan).")
@dry_run_option
@yes_option
@json_option
@pass_state
def smart_connect(st, state, ssid_2g, ssid_5g, ssid_6g, dry_run, yes):
    """Band steering: one SSID for all bands, router picks the band. `off --ssid-5g NAME` splits bands in one plan.

    \b
    Example: tpctl wifi set smart-connect off --ssid-5g Home-5G
    """
    ssids = {"2g": ssid_2g, "5g": ssid_5g, "6g": ssid_6g}

    def cav():
        out = st.r.wifi.caveats("smart-connect", None, state)  # effect text below carries the live count
        if any(ssids.values()):
            out = [c for c in out if not c.startswith("all bands currently share SSID")]
            out += st.r.wifi.caveats("ssid", [b for b, n in ssids.items() if n], "x")
        return out
    from collections import Counter
    bands = Counter(c.band for c in st.r.mesh.inventory().devices if c.band in ("2.4G", "5G", "6G", "MLO"))
    wl = sum(bands.values())
    per = ", ".join(f"{n} on {b}" for b, n in sorted(bands.items()))
    run_disruptive(st, lambda dr: st.r.wifi.set_smart_connect(state == "on", dry_run=dr, ssids=ssids), yes, dry_run,
                   f"{wl} Wi-Fi devices ({per}) drop for the radio restart (30-60 s); auto channels re-pick; EasyMesh satellites "
                   "re-onboard (~1-3 min, up to ~5 min; topology may change)", st.r.wifi.SCOPE_SSID, cav,
                   effect_fields=_all_band_effect(st, wl, {"2g": "2.4G", "5g": "5G", "6g": "6G"}, ssids))


def _all_band_effect(st, wl: int, names: dict | None = None, ssids: dict | None = None) -> dict:
    """Same effect fields as a single-band plan, for a change that restarts every radio."""
    inv = st.r.mesh.inventory()
    wifi = [c for c in inv.devices if c.band != "wired"]
    sat = sum(1 for c in wifi if c.node != inv.main.name)
    renamed = {names[b] for b, n in (ssids or {}).items() if n and names and b in names}
    lose = sum(1 for c in wifi if c.band in renamed) if renamed else None   # devices whose band gets a new name
    return {"drop": wl, "drop_main": wl - sat, "drop_satellites": sat,
            "lose_ssid": lose or 0,
            "may_blip": 0, "wired_unaffected": sum(1 for c in inv.devices if c.band == "wired"), "worst_case": wl,
            "outage_main": "30-60s", "outage_satellites": "~1-3 min" if sat else None}


@set_.command("ofdma-mimo")
@click.argument("mode", type=click.Choice(["all", "ofdma_only", "disable"]))
@dry_run_option
@yes_option
@json_option
@pass_state
def ofdma_mimo(st, mode, dry_run, yes):
    """Combined OFDMA/MU-MIMO switch (Wi-Fi 7 models; legacy per-band toggles are ignored by their UI)."""
    run_disruptive(st, lambda dr: st.r.wifi.set_ofdma_mimo(mode, dry_run=dr), yes, dry_run, SIDE_EFFECT, st.r.wifi.SCOPE)


@set_.command("ssid")
@click.argument("ssid")
@click.option("--band", "bands", multiple=True, type=BAND,
              help="Repeatable; default all bands. Per-band names need smart connect off: use `wifi set smart-connect off --ssid-5g NAME`.")
@dry_run_option
@yes_option
@json_option
@pass_state
def ssid(st, ssid, bands, dry_run, yes):
    """Rename network(s). Password unchanged; devices that only know the old name must rejoin."""
    run_disruptive(st, lambda dr: st.r.wifi.set_ssid(ssid, list(bands) or None, dry_run=dr), yes, dry_run, SIDE_EFFECT,
                   st.r.wifi.SCOPE_SSID, lambda: st.r.wifi.caveats("ssid", list(bands) or None, ssid))


@set_.command("txpower")
@click.argument("level", type=click.Choice(["low", "middle", "high"]))
@click.option("--band", "bands", multiple=True, type=BAND, help="Repeatable; default all.")
@dry_run_option
@yes_option
@json_option
@pass_state
def txpower(st, level, bands, dry_run, yes):
    """Transmit power per band."""
    run_disruptive(st, lambda dr: st.r.wifi.set_txpower(level, list(bands) or None, dry_run=dr), yes, dry_run,
                   st.r.wifi.effect_for(list(bands) or None),
                   st.r.wifi.scope_for(list(bands) or None) + "; whether satellites change their own power is unverified", lambda: st.r.wifi.caveats("txpower", list(bands) or None, level))


@set_.command("channel")
@click.argument("args", nargs=-1, required=True, metavar="CHANNEL")
@click.option("--band", "band_opt", type=BAND, help="Required (2g/5g/6g). Legacy form `channel 5g 36` also accepted.")
@dry_run_option
@yes_option
@json_option
@pass_state
def channel(st, args, band_opt, dry_run, yes):
    """Fixed channel number or `auto` for one band: `tpctl wifi set channel 36 --band 5g`."""
    band, value = _band_and_value(args, band_opt, "channel")
    run_disruptive(st, lambda dr: st.r.wifi.set_channel(band, value, dry_run=dr), yes, dry_run,
                   st.r.wifi.effect_for([band]), st.r.wifi.scope_for([band]), lambda: st.r.wifi.caveats("channel", [band], value), effect_fields=st.r.wifi.effect_counts([band]))


@set_.command("width")
@click.argument("args", nargs=-1, required=True, metavar="{20|40|80|160|320|auto}")
@click.option("--band", "band_opt", type=BAND, help="Required (2g/5g/6g). Legacy form `width 5g 80` also accepted.")
@dry_run_option
@yes_option
@json_option
@pass_state
def width(st, args, band_opt, dry_run, yes):
    """Channel width in MHz for one band: `tpctl wifi set width 80 --band 5g`."""
    band, value = _band_and_value(args, band_opt, "width")
    if value not in ("20", "40", "80", "160", "320", "auto"):
        from ..errors import UsageError
        raise UsageError(f"invalid width '{value}' (MHz)", value, ["20", "40", "80", "160", "320", "auto"], "width")
    run_disruptive(st, lambda dr: st.r.wifi.set_width(band, value, dry_run=dr), yes, dry_run,
                   st.r.wifi.effect_for([band]), st.r.wifi.scope_for([band]), lambda: st.r.wifi.caveats("width", [band], value), effect_fields=st.r.wifi.effect_counts([band]))


@wifi.command("iot")
@click.argument("state", required=False, type=click.Choice(["on", "off"]))
@dry_run_option
@yes_option
@json_option
@pass_state
def iot(st, state, dry_run, yes):
    """IoT network (separate SSID for smart-home gadgets): show it (no args) or turn it on/off. On/off restarts radios."""
    if state is None:
        d = st.r.wifi.iot()
        return st.out(d, [o.line("iot", enable=d["enable"], isolation=d["isolation"]),
                          *(o.line("iot_band", band=b, enable=x["enable"], ssid=x["ssid"], encryption=x["encryption"])
                            for b, x in d["bands"].items())],
                      lambda: [h.kvline("IoT Wi-Fi", h.green("on") if d["enable"] == "on" else h.dim("off"), 11),
                               *h.table(["BAND", "ON", "NAME"], [[b, x["enable"] or "", x["ssid"] or ""] for b, x in d["bands"].items()])])
    run_disruptive(st, lambda dr: st.r.wifi.set_iot(state == "on", dry_run=dr), yes, dry_run, SIDE_EFFECT,
                   "IoT network on every band (main router + satellites)",
                   lambda: ["ok: the main and guest networks keep their names and passwords; they only drop for the restart"])


@wifi.command("guest")
@click.argument("state", required=False, type=click.Choice(["on", "off"]))
@click.option("--ssid", help="Guest network name (all guest bands).")
@click.option("--password", help="Set a password (8-63 chars; WPA2/WPA3, WPA3 on 6G). Visible in shell history: prefer --password-stdin.")
@click.option("--password-stdin", is_flag=True, help="Read the password from stdin.")
@click.option("--open", "open_network", is_flag=True, help="No password (6G uses OWE: open but encrypted).")
@dry_run_option
@yes_option
@json_option
@pass_state
def guest(st, state, ssid, password, password_stdin, open_network, dry_run, yes):
    """Guest Wi-Fi: show it (no args), or turn it on/off, rename it, set its password.

    \b
      tpctl wifi guest
      printf %s "$PW" | tpctl wifi guest on --ssid Visitors --password-stdin --dry-run
      tpctl wifi guest off --dry-run
    Changing it restarts the radios (like any Wi-Fi write): plan with --dry-run, apply with --yes.
    """
    import sys
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    if password and open_network:
        raise click.UsageError("--password and --open are exclusive")
    if state is None and not any((ssid, password, open_network)):
        d = st.r.wifi.guest()
        return st.out(d, [o.line("guest", enable=d["enable"], lan_access=d["lan_access"], isolate=d["isolate"]),
                          *(o.line("guest_band", band=b, enable=x["enable"], ssid=x["ssid"], security=x["security"])
                            for b, x in d["bands"].items())],
                      lambda: [h.kvline("Guest Wi-Fi", h.green("on") if d["enable"] == "on" else h.dim("off"), 13),
                               h.kvline("LAN access", str(d["lan_access"]), 13),
                               *h.table(["BAND", "ON", "NAME", "SECURITY"],
                                        [[b, x["enable"] or "", x["ssid"] or "", x["security"] or ""] for b, x in d["bands"].items()])])
    en = None if state is None else state == "on"
    run_disruptive(st, lambda dr: st.r.wifi.set_guest(en, ssid, password, open_network, dry_run=dr), yes, dry_run, SIDE_EFFECT,
                   "guest network on every band (main router + satellites via EasyMesh)",
                   lambda: ["ok: the main network, its password and connected devices' settings are not changed; "
                            "they only drop for the radio restart"])


@wifi.command("schedule")
@click.argument("action", type=click.Choice(["on", "off", "add", "rm"]), required=False)
@click.argument("number", type=int, required=False)
@click.option("--from", "start", metavar="HH:MM", help="add: Wi-Fi off from (24 h).")
@click.option("--to", "end", metavar="HH:MM", help="add: Wi-Fi back on at (may be the next day).")
@click.option("--days", default="daily", show_default=True, help="add: daily | weekdays | weekends | mon,tue,…")
@click.option("--bands", metavar="2g,5g,…", help="add: bands to turn off (default: all).")
@dry_run_option
@json_option
@pass_state
def schedule(st, action, number, start, end, days, bands, dry_run):
    """Wi-Fi off-schedule: show (no args), on/off (the switch), add a rule, rm rule N.

    
      tpctl wifi schedule
      tpctl wifi schedule add --from 23:30 --to 07:00 --days weekdays --bands 2g,5g --dry-run
      tpctl wifi schedule on
      tpctl wifi schedule rm 1
    While a rule is active the radios of those bands are off on the main router and satellites (wired devices
    unaffected). Rules only act while the switch is on.
    """
    if action is None:
        d = st.r.qos.schedules()
        return st.out(d, [o.line("wifi_schedule", enable=d["enable"], rules=len(d["rules"]), max_rules=d["max_rules"]),
                          *(o.line("schedule_rule", n=i, off_from=x["off_from"], on_at=x["on_at"], days=x["days"],
                                   bands=x["bands"], enable=x["enable"]) for i, x in enumerate(d["rules"], 1))],
                      lambda: [h.kvline("Wi-Fi schedule", h.green("on") if d["enable"] else h.dim("off"), 15),
                               *(h.table(["#", "OFF FROM", "ON AT", "DAYS", "BANDS", "ON"],
                                         [[str(i), x["off_from"], x["on_at"], x["days"], x["bands"], str(x["enable"])]
                                          for i, x in enumerate(d["rules"], 1)]) or [h.dim("  no rules")])])
    if action in ("on", "off"):
        res = st.r.settings.set_switch("schedules.wifi_schedule", action == "on", dry_run=dry_run)
    elif action == "add":
        if not start or not end:
            raise click.UsageError("add needs --from and --to")
        res = st.r.qos.add_schedule(start, end, days, bands, dry_run=dry_run)
    else:
        if number is None:
            raise click.UsageError("rm needs the rule number (from `tpctl wifi schedule`)")
        res = st.r.qos.remove_schedule(number, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


COMMANDS = [wifi]
