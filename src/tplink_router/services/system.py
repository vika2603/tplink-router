from __future__ import annotations

import datetime as dt
import re

from ..errors import UsageError
from ..models import WriteResult
from . import Service

SLOW_LINK_MBPS = 100     # a connected wired port at or below this is almost always a cable/NIC problem


def port_rows(raw) -> list[dict]:
    """admin/status?form=router -> [{port, link, speed_mbps, duplex, wan}] (speed None when unlinked)."""
    out = []
    for x in raw if isinstance(raw, list) else []:
        up = x.get("status") == "connected"
        sp = x.get("speed")
        out.append({"port": x.get("name"), "link": "up" if up else "down",
                    "speed_mbps": int(sp) if up and str(sp).isdigit() else None,
                    "duplex": ((x.get("duplex") or "").lower() or None) if up else None, "wan": bool(x.get("is_wan"))})
    return out


class SystemService(Service):
    def reboot(self) -> WriteResult:
        """Reboot the main router: whole network down ~2-3 min, satellites re-onboard after."""
        self.r.call("admin/system?form=reboot", "write")
        return WriteResult([], None, "whole network down ~2-3min; satellites re-onboard afterwards",
                           "sleep 180; tpctl mesh wait && tpctl check")

    def ports(self) -> list[dict]:
        """Physical Ethernet ports of the main router: link up/down and negotiated speed."""
        self.r.caps.require("ports", "port status")
        return port_rows(self.r.call("admin/status?form=router"))

    def log(self, type_: str | None = None, level: str | None = None, since_minutes: int | None = None,
            grep: str | None = None) -> list[dict]:
        """Router system log, oldest first (like a log file): {time, type, level, content}. Filters are case-insensitive."""
        self.r.caps.require("syslog", "system log")
        rows = [x for x in (self.r.call("admin/syslog?form=log", "load") or []) if isinstance(x, dict)]
        if type_:
            rows = [x for x in rows if type_.lower().replace("-", "_") in str(x.get("type", "")).lower()]
        if level:
            order = ["DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"]
            lv = order.index(level.upper()) if level.upper() in order else 0
            rows = [x for x in rows if (order.index(x.get("level")) if x.get("level") in order else 0) >= lv]
        if since_minutes:
            cut = (dt.datetime.now() - dt.timedelta(minutes=since_minutes)).strftime("%Y-%m-%d %H:%M:%S")
            rows = [x for x in rows if str(x.get("time", "")) >= cut]
        if grep:
            rows = [x for x in rows if grep.lower() in str(x.get("content", "")).lower()]
        return sorted(rows, key=lambda x: str(x.get("time", "")))

    def firmware(self, online: bool = True) -> dict:
        """Main router + satellite firmware, the router's online update check and auto-update setting."""
        up = self.r.try_call("admin/firmware?form=upgrade") or {}
        fw = self.r.lib_call("get_firmware")
        main = {"name": "main router", "model": up.get("model") or fw.model, "hardware": up.get("hardware_version") or fw.hardware_version,
                "version": up.get("firmware_version") or fw.firmware_version}
        if online and self.r.caps.has("firmware_check"):
            c = self.r.try_call("admin/cloud_account?form=cloud_upgrade") or {}
            latest = c.get("latest_version") or None
            main |= {"latest": latest, "up_to_date": bool(c.get("latest_flag")) if "latest_flag" in c else None}
        auto = self.r.try_call("admin/firmware?form=auto_upgrade", quiet=True) or {}
        sats = []
        if self.r.caps.has("satellite_firmware"):
            for x in self.r.try_call("admin/firmware?form=slave_cmd", "slave_get_info", default=[]) or []:
                if isinstance(x, dict):
                    sats.append({"name": x.get("name"), "model": x.get("model"), "ip": x.get("ip"),
                                 "version": x.get("version") or None, "latest": x.get("latest_version") or None,
                                 "up_to_date": x.get("is_latest")})
        return {"main": main, "satellites": sats,
                "auto_update": {"enable": auto.get("enable"), "time": auto.get("time")} if auto else None}

    # -- small, non-disruptive writes (web UI payloads; see references/api.md) --
    def set_led(self, on: bool, dry_run: bool = False) -> WriteResult:
        """Main router LEDs (TP-Link satellites follow it)."""
        return self.r.apply_form("admin/ledgeneral?form=setting", {"enable": "on" if on else "off"}, tag="led",
                                 partial=True, dry_run=dry_run, key_prefix="led.")

    def set_night_mode(self, enable: bool, start: str | None = None, end: str | None = None,
                       dry_run: bool = False) -> WriteResult:
        """LEDs off between start and end (HH:MM) every night."""
        ch = {"enable": "on" if enable else "off"}
        for k, v in (("time_start", start), ("time_end", end)):
            if v is not None:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                    raise UsageError("times must be HH:MM (24h)", v, None, "time")
                ch[k] = v
        return self.r.apply_form("admin/ledpm?form=setting", ch, tag="led-night", dry_run=dry_run, key_prefix="night_mode.")

    def set_reboot_schedule(self, enable: bool, time: str | None = None, cycle: str | None = None,
                            day: str | None = None, dry_run: bool = False) -> WriteResult:
        """Automatic reboot of the main router: cycle day|week|month? (as the router lists), at HH:MM."""
        ch = {"enable": "on" if enable else "off"}
        if time is not None:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
                raise UsageError("time must be HH:MM (24h)", time, None, "time")
            ch["time"] = time
        if cycle is not None:
            ch["cycle"] = cycle
        if day is not None:
            ch["day"] = day
        return self.r.apply_form("admin/reboot?form=set", ch, tag="reboot-schedule", dry_run=dry_run, key_prefix="reboot_schedule.",
                                 side_effect="the whole network goes down ~2-3 min at each scheduled reboot" if enable else None)

    def diagnose(self, target: str, kind: str = "ping", count: int = 4, size: int = 64, max_hops: int = 20,
                 timeout: float = 60.0) -> dict:
        """Ping or traceroute FROM THE ROUTER (Advanced > System > Diagnostics): separates 'this machine/Wi-Fi'
        from 'the router's uplink'. Returns {"kind", "target", "finished", "output"}."""
        import time as _t
        if kind not in ("ping", "traceroute"):
            raise UsageError("kind must be ping or traceroute", kind, ["ping", "traceroute"], "kind")
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", target):
            raise UsageError("target must be an IP address or host name", target, None, "target")
        ep, t = "admin/diag?form=diag", "0" if kind == "ping" else "1"
        base = {"type": t, "ipaddr": target}
        first = (base | {"ipaddr_ping": target, "count": str(max(1, min(count, 50))), "pktsize": str(max(4, min(size, 1472)))}
                 if kind == "ping" else base | {"ipaddr_trace": target, "ttl": str(max(1, min(max_hops, 30)))})
        res = self.r.call(ep, "start", **first) or {}
        start = _t.time()
        while str(res.get("finish")) != "1" and _t.time() - start < timeout:
            _t.sleep(1)
            res = self.r.call(ep, "continue", **base) or {}
        done = str(res.get("finish")) == "1"
        if not done:
            res = self.r.try_call(ep, "stop", **base) or res
        out = str(res.get("result") or "").strip()
        d = {"kind": kind, "target": target, "finished": done, "output": out}
        if kind == "ping":
            m = re.search(r"Sent=(\d+), Received=(\d+).*?\(([\d.]+)% loss\)", out, re.S)
            rt = re.search(r"min/avg/max = ([\d.]+)/([\d.]+)/([\d.]+)", out)
            d |= {"sent": int(m.group(1)) if m else None, "received": int(m.group(2)) if m else None,
                  "loss_pct": float(m.group(3)) if m else None, "avg_ms": float(rt.group(2)) if rt else None,
                  "max_ms": float(rt.group(3)) if rt else None}
        else:
            d["hops"] = [ln.strip() for ln in out.splitlines() if re.match(r"\s*\d+\s", ln)]
        return d

    def reboot_schedule(self) -> dict:
        d = self.r.try_call("admin/reboot?form=set", quiet=True) or {}
        return {k: d.get(k) for k in ("enable", "cycle", "day", "time")} if d else {}

    def time(self) -> dict:
        d = self.r.try_call("admin/time?form=settings")
        if d.get("timezone") is not None:
            d = {**d, "timezone_name": timezones().get(str(d["timezone"]))}
        return d

    def set_time(self, ntp: tuple[str, ...] = (), timezone: str | None = None, hour24: bool | None = None,
                 dry_run: bool = False) -> WriteResult:
        """NTP servers (1-2), time zone (router id or name, e.g. 109 / singapore), 24-hour clock. The web UI writes
        {type, timezone, ntp_svr1, ntp_svr2} for automatic time; 24-hour is its own form."""
        ep = "admin/time?form=settings"
        cur = self.r.call(ep)
        changes = {}
        if timezone is not None:
            changes["timezone"] = resolve_timezone(timezone)
        if ntp:
            if len(ntp) > 2:
                raise UsageError("at most two NTP servers")
            for x in ntp:
                if not re.fullmatch(r"[A-Za-z0-9.-]+|[0-9a-fA-F:]+", x):
                    raise UsageError(f"invalid NTP server {x}", x, None, "server")
            changes |= {"ntp_svr1": ntp[0], "ntp_svr2": ntp[1] if len(ntp) > 1 else ""}
        if hour24 is not None and not changes:
            return self.r.apply_form("admin/time?form=hour24", {"hour24_enable": "on" if hour24 else "off"}, tag="time",
                                     partial=True, dry_run=dry_run, key_prefix="time.")
        if not changes:
            raise UsageError("nothing to change: give --ntp, --timezone or --24h/--12h")
        if cur.get("type") != "auto" and ntp:
            changes["type"] = "auto"                  # NTP servers only apply to automatic time
        body = {"type": changes.get("type", cur.get("type")), "timezone": changes.get("timezone", cur.get("timezone")),
                "ntp_svr1": changes.get("ntp_svr1", cur.get("ntp_svr1")), "ntp_svr2": changes.get("ntp_svr2", cur.get("ntp_svr2") or "")}
        res = self.r.apply_form(ep, changes, tag="time", dry_run=dry_run, key_prefix="time.",
                                side_effect="the router re-syncs its clock; schedules (Wi-Fi, reboot, LED night mode, "
                                            "parental) follow the new time zone") if dry_run else None
        if dry_run:
            return res
        if not any(cur.get(k) != v for k, v in changes.items()):
            return self.r.apply_form(ep, changes, tag="time", dry_run=False, key_prefix="time.")      # no-op skip
        snap = self.r.snapshot("time")
        self.r.call(ep, "write", **body)               # the UI's exact payload (not the whole form: date/time are live)
        after = self.r.call(ep)
        if hour24 is not None:
            self.r.call("admin/time?form=hour24", "write", hour24_enable="on" if hour24 else "off")
        from ..models import Change
        return WriteResult([Change("time." + k, cur.get(k), after.get(k), v) for k, v in changes.items()], str(snap),
                           "the router re-syncs its clock; schedules follow the new time zone", "tpctl system info")

    def set_auto_update(self, on: bool, hour: int | None = None, dry_run: bool = False) -> WriteResult:
        """Automatic firmware updates (installed at `hour`, 0-23; the router reboots then)."""
        ch = {"enable": "on" if on else "off"}
        if hour is not None:
            if not 0 <= int(hour) <= 23:
                raise UsageError("--at must be an hour 0-23")
            ch["time"] = str(int(hour))
        return self.r.apply_form("admin/firmware?form=auto_upgrade", ch, tag="auto-update", dry_run=dry_run,
                                 key_prefix="auto_update.", side_effect="when a new firmware is out the router installs it "
                                 "at that hour and reboots (whole network down ~3-5 min)" if on else None)

    def upgrade(self, dry_run: bool = False) -> WriteResult:
        """Online firmware upgrade of the main router (download + flash + reboot)."""
        from ..models import Change
        c = self.r.call("admin/cloud_account?form=cloud_upgrade")
        cur = (self.r.try_call("admin/firmware?form=upgrade") or {}).get("firmware_version")
        latest = c.get("latest_version")
        if c.get("latest_flag") or not latest:
            return WriteResult([Change("firmware.version", cur, cur, cur)], None, None, None, applied=False,
                               skipped_reason=f"already on the latest firmware ({latest or cur})")
        plan = [Change("firmware.version", cur, latest, latest)]
        if dry_run:
            return WriteResult(plan, None, "downloads and flashes the firmware, then reboots: whole network down ~5 min",
                               None, dry_run=True, applied=False)
        snap = self.r.snapshot("firmware-upgrade")
        self.r.call("admin/cloud_account?form=cloud_upgrade", "upgrade")
        return WriteResult(plan, str(snap), "upgrading: don't power off; the router reboots by itself (~5 min)",
                           "sleep 300; tpctl mesh wait && tpctl system firmware")

    def backup(self, out: str | None = None) -> dict:
        """Download the router's own configuration backup (.bin, encrypted by the router; restore it in the web UI
        under System > Backup & Restore). Saved 0600: it contains every setting incl. Wi-Fi and admin credentials."""
        import os
        from pathlib import Path
        name, data = self.r.download_backup()
        if out and Path(out).expanduser().is_dir():
            p = Path(out).expanduser() / name
        elif out:
            p = Path(out).expanduser()
        else:
            p = self.r.config.state_dir / "backups" / name.replace(" ", "_")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return {"file": str(p), "bytes": len(data), "router_name": name}

    def led(self) -> dict:
        return {"led": self.r.try_call("admin/ledgeneral?form=setting"), "night_mode": self.r.try_call("admin/ledpm?form=setting")}


def timezones() -> dict[str, str]:
    """Router time-zone id -> name (from the web UI's list, e.g. 109 -> singaporeStandardTime)."""
    import json
    from importlib.resources import files
    return json.loads(files("tplink_router").joinpath("data/timezones.json").read_text())


def resolve_timezone(q: str) -> str:
    tz = timezones()
    if str(q) in tz:
        return str(q)
    key = re.sub(r"[^a-z0-9+-]", "", str(q).lower())
    exact = [i for i, n in tz.items() if n.lower() in (key, key + "standardtime")]
    if len(exact) == 1:
        return exact[0]
    hits = [i for i, n in tz.items() if key and key in n.lower()]
    if len(hits) == 1:
        return hits[0]
    raise UsageError(f"time zone '{q}' is {'ambiguous' if hits else 'unknown'}", q,
                     [f"{i}={tz[i]}" for i in hits[:12]] or None, "timezone")
