"""QoS (HomeCare > QoS): the line bandwidth QoS shapes to, per-device speed limits, Wi-Fi off-schedule rules.

Payloads follow the web UI (references/api.md): speed limits are Kbps with -1 = no limit; the schedule list lives
inside `wireless_schedule_v2?form=settings` (read -> {enable, max_rules, list}), rows get a router-assigned key.
"""
from __future__ import annotations

import json
import re

from ..errors import UsageError
from ..models import Change, WriteResult
from . import Service

QOS_EP = "admin/smart_network?form=qos"
LIMIT_EP = "admin/smart_network?form=client_speed_limit"
SCHED_EP = "admin/wireless_schedule_v2?form=settings"
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
DAY_SETS = {"daily": DAYS, "weekdays": DAYS[1:6], "weekends": ("sat", "sun")}


def _kbps(mbps: float | None) -> int:
    return -1 if mbps is None else max(1, round(float(mbps) * 1024))


def _mbps(kbps) -> float | None:
    try:
        k = int(kbps)
    except (TypeError, ValueError):
        return None
    if k < 0:
        return None
    v = round(k / 1024, 2)
    return int(v) if v == int(v) else v


class QosService(Service):
    # -- QoS bandwidth --
    def get(self) -> dict:
        d = self.r.try_call(QOS_EP)
        mult = {"mbps": 1, "kbps": 1 / 1024, "gbps": 1024}
        def band(v, unit):
            try:
                x = round(float(v) * mult.get(str(unit).lower(), 1), 2)
                return int(x) if x == int(x) else x
            except (TypeError, ValueError):
                return None
        return {"enable": d.get("enable"), "down_mbps": band(d.get("down_band"), d.get("down_unit")),
                "up_mbps": band(d.get("up_band"), d.get("up_unit")), "max_mbps": d.get("max_wan_speed")}

    def set(self, enable: bool | None = None, down_mbps: float | None = None, up_mbps: float | None = None,
            dry_run: bool = False) -> WriteResult:
        """QoS on/off and the bandwidth it shapes to (set ~90 % of the measured line speed: `tpctl net speed`).
        The web UI writes {enable, up_band, max_up_band, up_unit, down_band, max_down_band, down_unit}."""
        cur = self.r.call(QOS_EP)
        ch = {}
        if enable is not None:
            ch["enable"] = "on" if enable else "off"
        for k, v, mx in (("down_band", down_mbps, "max_down_band"), ("up_band", up_mbps, "max_up_band")):
            if v is None:
                continue
            if not 0 < float(v) <= float(cur.get(mx) or 10**6):
                raise UsageError(f"{k.split('_')[0]} must be 1-{cur.get(mx)} Mbps", str(v), None, "speed")
            ch[k] = str(int(round(float(v))))
            ch[k.replace("band", "unit")] = "mbps"
        if not ch:
            raise UsageError("nothing to change: give on/off, --down or --up")
        if ch.get("enable") == "on" and not (down_mbps or up_mbps) and cur.get("down_band") == str(cur.get("max_down_band")):
            raise UsageError("QoS on with the default (maximum) bandwidth does nothing: measure with `tpctl net speed` "
                             "and pass --down/--up at ~90 % of it", None, None, "speed")
        body = {k: cur.get(k) for k in ("enable", "up_band", "max_up_band", "up_unit", "down_band", "max_down_band",
                                        "down_unit")} | ch
        side = ("QoS prioritises interactive traffic within that bandwidth; NAT acceleration turns off, the router "
                "re-applies NAT (~2 min)") if ch.get("enable") == "on" else None
        plan = [Change("qos." + k, cur.get(k), v, v) for k, v in ch.items() if not k.endswith("_unit")]
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.r.snapshot("qos")
        self.r.call(QOS_EP, "write", **{k: str(v) for k, v in body.items()})
        after = self.r.call(QOS_EP)
        return WriteResult([Change(c.key, c.old, after.get(c.key[4:]), c.target) for c in plan], str(snap), side,
                           "tpctl qos show")

    # -- per-device speed limits --
    def limits(self) -> list[dict]:
        """Every known device with its limit (Mbps; None = no limit) from the clients list."""
        rows = self.r.call("admin/smart_network?form=game_accelerator", "loadSpeed") or []
        return [{"mac": x.get("mac"), "name": x.get("deviceName"), "enable": x.get("enableLimit"),
                 "down_mbps": _mbps(x.get("downloadLimit")), "up_mbps": _mbps(x.get("uploadLimit"))}
                for x in rows if isinstance(x, dict)]

    def set_limit(self, query: str, down_mbps: float | None = None, up_mbps: float | None = None, off: bool = False,
                  dry_run: bool = False) -> WriteResult:
        """Cap one device's download/upload (Mbps; None = unlimited in that direction), or remove its limit."""
        mac = query.upper().replace(":", "-")
        if not re.fullmatch(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}", mac):
            c = self.r.mesh.inventory().device(query)
            mac, label = c.mac, c.label
        else:
            label = mac
        if not off and down_mbps is None and up_mbps is None:
            raise UsageError("give --down and/or --up (Mbps), or `off`", query, None, "speed")
        mx = self.r.try_call(LIMIT_EP, "read_max", default={}) or {}
        for v, lim in ((down_mbps, mx.get("downloadLimitMax")), (up_mbps, mx.get("uploadLimitMax"))):
            if v is not None and (float(v) <= 0 or (lim and _kbps(v) > int(lim))):
                raise UsageError(f"limit must be > 0 and <= {_mbps(lim)} Mbps", str(v), None, "speed")
        cur = next((x for x in self.limits() if str(x["mac"]).upper() == mac), None)
        if cur is None:
            raise UsageError(f"{label} is not in the router's client list (it must have connected once)", query, None, "device")
        new = {"enable": "off"} if off else {"enable": "on", "down_mbps": _mbps(_kbps(down_mbps)), "up_mbps": _mbps(_kbps(up_mbps))}
        plan = [Change(f"limit[{label}].{k}", cur.get(k) if cur.get("enable") == "on" or k == "enable" else None, v, v)
                for k, v in new.items()]
        side = None if off else (f"{label} is capped at " + " / ".join(
            f"{d} {v:g} Mbps" for d, v in (("down", new["down_mbps"]), ("up", new["up_mbps"])) if v is not None))
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.r.snapshot("speed-limit")
        body = {"mac": mac, "enableLimit": new["enable"]}
        if not off:
            body |= {"downloadLimit": str(_kbps(down_mbps)), "uploadLimit": str(_kbps(up_mbps))}
        else:
            body |= {"downloadLimit": str(_kbps(cur["down_mbps"])), "uploadLimit": str(_kbps(cur["up_mbps"]))}
        self.r.call(LIMIT_EP, "write", **body)
        after = next((x for x in self.limits() if str(x["mac"]).upper() == mac), {})
        return WriteResult([Change(c.key, c.old, after.get(c.key.rsplit(".", 1)[1]), c.target) for c in plan], str(snap),
                           side, "tpctl qos limits")

    # -- Wi-Fi off-schedule rules --
    def schedules(self) -> dict:
        d = self.r.try_call(SCHED_EP)
        rows = d.get("list") if isinstance(d.get("list"), list) else []
        return {"enable": d.get("enable"), "max_rules": d.get("max_rules"),
                "rules": [{"index": i, "key": x.get("key"), "off_from": x.get("time_start"), "on_at": x.get("time_end"),
                           "days": x.get("repeat_days"), "bands": x.get("band_list"), "enable": x.get("enable")}
                          for i, x in enumerate(rows)]}

    def add_schedule(self, start: str, end: str, days: str = "daily", bands: str | None = None,
                     dry_run: bool = False) -> WriteResult:
        """Wi-Fi off from `start` to `end` (HH:MM, may cross midnight) on `days` (daily/weekdays/weekends or
        mon,tue,…) for `bands` (default: all). Rules only act while the schedule switch is on."""
        for t in (start, end):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise UsageError(f"time must be HH:MM (24 h), got {t}", t, None, "time")
        if start == end:
            raise UsageError("start and end are the same", start, None, "time")
        dl = DAY_SETS.get(days) or tuple(d.strip().lower()[:3] for d in days.split(","))
        if not dl or any(d not in DAYS for d in dl):
            raise UsageError(f"days: daily, weekdays, weekends or a list of {','.join(DAYS)}", days, None, "days")
        supported = list(self.r.caps.bands or [])
        bl = [b.strip().lower() for b in (bands.split(",") if bands else supported or ["2g", "5g"])]
        if supported and any(b not in supported for b in bl):
            raise UsageError(f"bands must be among {','.join(supported)}", bands, supported, "band")
        d = self.schedules()
        if d["max_rules"] and len(d["rules"]) >= int(d["max_rules"]):
            raise UsageError(f"the router allows at most {d['max_rules']} schedule rules")
        row = {"key": "", "enable": True, "repeat_days": ",".join(x for x in DAYS if x in dl), "time_start": start,
               "time_end": end, "band_list": ",".join(bl), "next_time": "off"}
        ident = f"{start}-{end} {row['repeat_days']} {row['band_list']}"
        dup = any((x["off_from"], x["on_at"], x["days"], x["bands"]) == (start, end, row["repeat_days"], row["band_list"])
                  for x in d["rules"])
        plan = [Change(f"wifi_schedule[{ident}]", "present" if dup else "absent", "present", "present")]
        side = ("Wi-Fi " + ",".join(bl) + f" turns off {start}-{end} ({days})" +
                ("" if d["enable"] else "; the schedule switch is OFF: run `tpctl wifi schedule on` to make rules act"))
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        if dup:
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.r.snapshot("wifi-schedule")
        self.r.call(SCHED_EP, "insert", new=json.dumps(row), index="0")
        now = any((x["off_from"], x["on_at"], x["days"]) == (start, end, row["repeat_days"]) for x in self.schedules()["rules"])
        return WriteResult([Change(plan[0].key, plan[0].old, "present" if now else "absent", "present")], str(snap), side,
                           "tpctl wifi schedule")

    def remove_schedule(self, number: int, dry_run: bool = False) -> WriteResult:
        """Remove rule `number` (1-based, as `tpctl wifi schedule` lists them)."""
        rules = self.schedules()["rules"]
        if not 1 <= number <= len(rules):
            raise UsageError(f"no schedule rule #{number} ({len(rules)} rules)", str(number), None, "rule")
        x = rules[number - 1]
        key = f"wifi_schedule[{x['off_from']}-{x['on_at']} {x['days']} {x['bands']}]"
        plan = [Change(key, "present", "absent", "absent")]
        if dry_run:
            return WriteResult(plan, None, None, None, dry_run=True, applied=False)
        snap = self.r.snapshot("wifi-schedule-rm")
        self.r.call(SCHED_EP, "remove", key=str(x["key"]), index=str(x["index"]))
        gone = not any(y["key"] == x["key"] for y in self.schedules()["rules"])
        return WriteResult([Change(key, "present", "absent" if gone else "present", "absent")], str(snap), None,
                           "tpctl wifi schedule")
