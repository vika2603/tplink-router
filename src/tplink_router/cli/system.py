import click

from .. import human as h
from .. import output as o
from ._ctx import dry_run_option, json_option, pass_state, run_disruptive, yes_option


@click.group("system")
def system():
    """Main router: Ethernet ports, system log, firmware/updates, time, config backup, LEDs, reboot."""


@system.command("reboot")
@click.option("--yes", "-y", is_flag=True, help="Confirm (without it: show what would happen, exit 4).")
@click.option("--dry-run", is_flag=True, help="Show the plan and the confirm command (exit 0); nothing happens.")
@json_option
@pass_state
def reboot(st, yes, dry_run):
    """Reboot the MAIN router (whole network down ~2-3 min). For a satellite use `tpctl mesh reboot`."""
    if not yes or dry_run:
        from ._ctx import confirm_command
        effect = "the whole network (internet, Wi-Fi, every satellite) goes down ~2-3 min"
        cmd = confirm_command()
        st.exit_override = None if dry_run else 4
        return st.out({"confirmation_required": True, "effect": effect, "confirm_command": cmd},
                      [o.line("confirm", required="yes", risk="disruptive", effect=effect, run=cmd)],
                      lambda: [h.yellow(h.bold("Needs confirmation: ") + effect), f"  run:  {h.bold(cmd)}"])
    res = st.r.system.reboot()
    st.out(res, [o.line("reboot", target="main router", sent="yes"), *o.write_lines(res)])


@system.command("ports")
@json_option
@pass_state
def ports(st):
    """Ethernet ports: link up/down and negotiated speed. A linked port at <= 100 Mbps = bad cable / old NIC."""
    from ..services.health import port_issues
    rows = st.r.system.ports()
    iss = port_issues(rows)
    st.out({"ports": rows, "issues": iss},
           [*(o.line("port", port=p["port"], link=p["link"], speed=o.unit(p["speed_mbps"], "Mbps"), duplex=p["duplex"],
                     wan=p["wan"] or None) for p in rows), *o.issue_lines(iss)],
           lambda: [*h.table(["PORT", "LINK", "SPEED", "DUPLEX"],
                             [[p["port"] + (" (WAN)" if p["wan"] else ""), p["link"],
                               f"{p['speed_mbps']} Mbps" if p["speed_mbps"] else "—", p["duplex"] or "—"] for p in rows]),
                    *(h.issues(iss) if iss else [])])


@system.command("log")
@click.option("--type", "type_", metavar="T", help="Log type substring, e.g. ppp, network, dhcp, nat (`--types` lists them).")
@click.option("--level", type=click.Choice(["debug", "info", "notice", "warning", "error"]), help="Minimum level.")
@click.option("--since", help="Window, e.g. 30m, 6h, 2d.")
@click.option("--grep", "grep", metavar="TEXT", help="Only entries whose text contains TEXT (case-insensitive).")
@click.option("--limit", type=int, default=50, show_default=True, help="Show the newest N entries, oldest first (0 = all).")
@click.option("--types", "list_types", is_flag=True, help="List log types with counts instead.")
@json_option
@pass_state
def log(st, type_, level, since, grep, limit, list_types):
    """Router system log, oldest first (newest at the bottom, like `tail`). WAN drops: --grep "Disconnect interface wan"; PPPoE: --type ppp.

    \b
    The router keeps ~1200 entries; EasyMesh/backhaul events are not logged (use `tpctl events`).
      tpctl system log --since 24h --type network
      tpctl system log --types
    """
    from collections import Counter
    from .events import _minutes
    rows = st.r.system.log(type_, level, _minutes(since), grep)
    if list_types:
        c = Counter(x.get("type") for x in rows)
        return st.out(dict(c.most_common()), [o.line("log_type", type=t, entries=n) for t, n in c.most_common()],
                      lambda: h.table(["TYPE", "ENTRIES"], [[t, str(n)] for t, n in c.most_common()], right={1}))
    shown = rows if not limit else rows[-limit:]
    span = (rows[0].get("time"), rows[-1].get("time")) if rows else (None, None)
    head = o.line("log", entries=len(rows), shown=len(shown), oldest=span[0], newest=span[1],
                  hint="--limit 0 for all, or narrow with --since/--type/--grep" if len(shown) < len(rows) else None)
    st.out({"entries": shown, "total": len(rows)},
           [head, *(o.line("entry", time=x.get("time"), type=x.get("type"), level=x.get("level"), text=x.get("content"))
                    for x in shown)],
           lambda: [h.dim(f"{len(rows)} entries" + (f", newest {len(shown)} shown" if len(shown) < len(rows) else "")),
                    *h.table(["TIME", "TYPE", "LEVEL", "MESSAGE"],
                             [[x.get("time", ""), x.get("type", ""), x.get("level", ""), x.get("content", "")] for x in shown])])


@system.command("firmware")
@click.option("--no-online", is_flag=True, help="Skip the router's online update check.")
@json_option
@pass_state
def firmware(st, no_online):
    """Firmware of the main router and every satellite, latest available version, auto-update setting."""
    d = st.r.system.firmware(online=not no_online)
    rows = [d["main"], *d["satellites"]]
    behind = [x["name"] for x in rows if x.get("up_to_date") is False]
    st.out(d, [*(o.line("firmware", device=x["name"], model=x.get("model"), version=x.get("version"), latest=x.get("latest"),
                        up_to_date=x.get("up_to_date")) for x in rows),
               o.line("auto_update", **(d["auto_update"] or {"enable": None})),
               o.line("updates", available=len(behind), nodes=behind or None,
                      note="satellites report no version string on some firmware; up_to_date comes from the router")],
           lambda: [*h.table(["DEVICE", "MODEL", "VERSION", "LATEST", "UP TO DATE"],
                             [[x["name"], x.get("model") or "", x.get("version") or "—", x.get("latest") or "—",
                               {True: "yes", False: "NO", None: "?"}[x.get("up_to_date")]] for x in rows]),
                    h.dim(f"  auto-update: {(d['auto_update'] or {}).get('enable', '?')} at {(d['auto_update'] or {}).get('time', '?')}")])


@system.command("upgrade")
@dry_run_option
@yes_option
@json_option
@pass_state
def upgrade(st, dry_run, yes):
    """Install the latest firmware on the main router (online). Planned first; --yes to apply. Whole network
    down ~5 min. Satellites: the web UI's EasyMesh firmware page."""
    run_disruptive(st, lambda dr: st.r.system.upgrade(dry_run=dr), yes, dry_run,
                   "downloads and flashes the firmware, then reboots: whole network down ~5 min",
                   "main router (satellites lose the mesh while it is down)",
                   lambda: ["don't power the router off during the upgrade",
                            "ok: settings are kept (take `tpctl system backup` first to be safe)"])


@system.command("auto-update")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--at", "hour", type=int, metavar="HOUR", help="Hour of day 0-23 for installing updates.")
@dry_run_option
@json_option
@pass_state
def auto_update(st, state, hour, dry_run):
    """Automatic firmware updates: `tpctl system auto-update on --at 3` (installs + reboots at that hour)."""
    res = st.r.system.set_auto_update(state == "on", hour, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@system.command("time")
@click.option("--ntp", multiple=True, metavar="SERVER", help="NTP server (give once or twice).")
@click.option("--timezone", metavar="ID|NAME", help="Router time-zone id or name part (109, singapore, china, utc+08).")
@click.option("--24h/--12h", "hour24", default=None, help="Clock format in the web UI.")
@click.option("--zones", is_flag=True, help="List the router's time zones.")
@dry_run_option
@json_option
@pass_state
def time_(st, ntp, timezone, hour24, zones, dry_run):
    """Show or set the clock: NTP servers, time zone, 12/24 h.

    
      tpctl system time
      tpctl system time --ntp asia.pool.ntp.org --ntp time.cloudflare.com --dry-run
      tpctl system time --timezone singapore
    """
    from ..services.system import timezones
    if zones:
        tz = timezones()
        return st.out(tz, [o.line("timezone", id=k, name=v) for k, v in tz.items()],
                      lambda: h.table(["ID", "NAME"], [[k, v] for k, v in tz.items()]))
    if not (ntp or timezone or hour24 is not None):
        d = st.r.system.time()
        return st.out(d, [o.line("time", date=d.get("date"), time=d.get("time"), source=d.get("type"),
                                 timezone=d.get("timezone"), timezone_name=d.get("timezone_name"),
                                 ntp=",".join(x for x in (d.get("ntp_svr1"), d.get("ntp_svr2")) if x) or None,
                                 hour24=d.get("hour24_enable"))],
                      lambda: [h.kvline("Clock", f"{d.get('date')} {d.get('time')} " + h.dim(f"({d.get('type')})")),
                               h.kvline("Time zone", f"{d.get('timezone')} " + h.dim(str(d.get("timezone_name")))),
                               h.kvline("NTP", ", ".join(x for x in (d.get("ntp_svr1"), d.get("ntp_svr2")) if x))])
    res = st.r.system.set_time(ntp, timezone, hour24, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@system.command("backup")
@click.option("--out", metavar="PATH", help="File or directory (default: the tpctl state dir, backups/).")
@json_option
@pass_state
def backup(st, out):
    """Download the router's configuration backup (.bin, as the web UI's Backup button). Restore it in the web UI.
    Saved with mode 0600: it holds every setting, including Wi-Fi and admin passwords (encrypted by the router)."""
    d = st.r.system.backup(out)
    st.out(d, [o.line("backup", file=d["file"], bytes=d["bytes"], restore="web UI: System > Backup & Restore > Restore",
                      note="router-encrypted; contains credentials; keep it private")],
           lambda: [h.green("✓ ") + f"saved {d['file']} " + h.dim(f"({d['bytes']:,} bytes, mode 0600)"),
                    h.dim("  restore: web UI → System → Backup & Restore")])


@system.command("led")
@click.argument("state", type=click.Choice(["on", "off"]))
@dry_run_option
@json_option
@pass_state
def led(st, state, dry_run):
    """Main router LEDs on/off (TP-Link satellites follow it)."""
    res = st.r.system.set_led(state == "on", dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@system.command("night-mode")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--from", "start", metavar="HH:MM", help="LEDs off from (24h).")
@click.option("--to", "end", metavar="HH:MM", help="LEDs back on at (24h).")
@dry_run_option
@json_option
@pass_state
def night_mode(st, state, start, end, dry_run):
    """LEDs off every night: `tpctl system night-mode on --from 23:00 --to 07:00`."""
    res = st.r.system.set_night_mode(state == "on", start, end, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@system.command("reboot-schedule")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--at", "time", metavar="HH:MM", help="Time of day (24h).")
@click.option("--every", "cycle", type=click.Choice(["day", "week", "month"]), help="Repeat.")
@click.option("--day", help="With --every week: Mon..Sun; with month: 1-31.")
@dry_run_option
@json_option
@pass_state
def reboot_schedule(st, state, time, cycle, day, dry_run):
    """Automatic main-router reboot: `tpctl system reboot-schedule on --at 04:00 --every week --day Mon`."""
    res = st.r.system.set_reboot_schedule(state == "on", time, cycle, day, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@system.command("info")
@json_option
@pass_state
def info(st):
    """Firmware, time/NTP, LED settings, scheduled reboot."""
    d = {"firmware": st.r.status.firmware(), "time": st.r.system.time(), **st.r.system.led(),
         "reboot_schedule": st.r.system.reboot_schedule()}
    lines = []
    for k, v in d.items():
        lines += o.dict_lines(k, v)
    st.out(d, lines, lambda: h.system_info(d))


COMMANDS = [system]
