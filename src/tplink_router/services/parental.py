"""Parental controls (HomeCare > Parental Controls): profiles of devices, internet pause, bedtime.

Payloads follow the web UI (references/api.md, avira_pactrl): one request endpoint with operations getOwnerTotalData,
addOwnerInList, delOwnerInList, internetBlock. Bedtime times are minutes after midnight, as strings.
Live-verified 2026-10-22 (BE805 fw 1.5.1): addOwnerInList, internetBlock=true, delOwnerInList. Not yet:
internetBlock=false. Plans say so; all writes need --yes.
"""
from __future__ import annotations

import json
import re

from ..errors import UsageError
from ..models import Change, WriteResult
from . import Service

EP = "admin/avira_parental_control?form=avira_pactrl"
UNVERIFIED_NOTE = ("parental controls: add, pause-on and rm were verified on a live router; pause-off follows the web "
                   "UI's payload but is not live-tested yet")


def _mins(t: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise UsageError(f"time must be HH:MM (24 h), got {t}", t, None, "time")
    h, m = t.split(":")
    return str(int(h) * 60 + int(m))


def _hhmm(v) -> str | None:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return f"{v // 60:02d}:{v % 60:02d}"


def _rows(v) -> list:
    return v if isinstance(v, list) else []          # the router answers {} for an empty list


class ParentalService(Service):
    def _data(self) -> dict:
        return self.r.call(EP, "getOwnerTotalData") or {}

    def profiles(self) -> list[dict]:
        out = []
        for p in _rows(self._data().get("ownerList")):
            bt = p.get("bedtime") or {}
            every = bt.get("everyday") or {}
            out.append({"id": str(p.get("ownerId")), "name": p.get("name"),
                        "devices": [c.get("name") or c.get("mac") for c in _rows(p.get("clientList"))],
                        "internet_blocked": bool(p.get("internetBlocked")),
                        "bedtime": f"{_hhmm(every.get('bedtimeBegin'))}-{_hhmm(every.get('bedtimeEnd'))}"
                        if bt.get("enable") else None,
                        "filters": len(_rows(p.get("filterCategoriesList"))) + len(_rows(p.get("filterWebsiteList")))})
        return out

    def limits(self) -> dict:
        d = self._data()
        return {k: d.get(k) for k in ("totalOwnerMax", "availableOwnerMax", "clientPerOwnerMax")}

    def _find(self, name: str) -> dict:
        ps = self.profiles()
        hit = [p for p in ps if p["name"] == name] or [p for p in ps if name.lower() in str(p["name"]).lower()]
        if len(hit) != 1:
            raise UsageError(f"profile '{name}' matched {len(hit)}", name, [p["name"] for p in ps], "profile")
        return hit[0]

    def add(self, name: str, devices: tuple[str, ...], bedtime: str | None = None, dry_run: bool = False) -> WriteResult:
        """New profile: these devices, optional bedtime 'HH:MM-HH:MM' every day (internet off in that window)."""
        if not name or len(name) > 32:
            raise UsageError("profile name must be 1-32 characters", name, None, "name")
        if any(p["name"] == name for p in self.profiles()):
            raise UsageError(f"profile '{name}' exists", name, None, "name")
        inv = self.r.mesh.inventory()
        devs = [inv.device(q) for q in devices]
        bt = {"enable": False, "everyday": {"bedtimeBegin": _mins("21:00"), "bedtimeEnd": _mins("07:00")}}
        if bedtime:
            a, _, b = bedtime.partition("-")
            bt = {"enable": True, "everyday": {"bedtimeBegin": _mins(a), "bedtimeEnd": _mins(b)}}
        body = {"ownerId": "-1", "name": name, "age": "0", "internetBlocked": "false",
                "allDeviceMac": json.dumps([c.mac for c in devs]), "filterCategoriesList": "[]", "filterWebsiteList": "[]",
                "bedtime": json.dumps(bt), "filterFreeWebsiteList": "[]"}
        plan = [Change(f"parental[{name}]", "absent", "present", "present"),
                Change(f"parental[{name}].devices", None, ",".join(c.label for c in devs) or "(none)", None)]
        if bedtime:
            plan.append(Change(f"parental[{name}].bedtime", None, bedtime, bedtime))
        side = (f"{', '.join(c.label for c in devs) or 'no device'} get no internet {bedtime} every day" if bedtime
                else "a profile without schedule: use `tpctl parental pause` to cut its devices off")
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        snap = self.r.snapshot("parental-add")
        self.r.call(EP, "addOwnerInList", **body)
        now = any(p["name"] == name for p in self.profiles())
        return WriteResult([Change(plan[0].key, "absent", "present" if now else "absent", "present"), *plan[1:]], str(snap),
                           side, "tpctl parental")

    def pause(self, name: str, on: bool, dry_run: bool = False) -> WriteResult:
        """Internet off (on=True) / back (on=False) for every device of a profile, right now."""
        p = self._find(name)
        plan = [Change(f"parental[{p['name']}].internet_blocked", p["internet_blocked"], on, on)]
        side = (f"{', '.join(p['devices']) or 'its devices'} lose internet now (LAN stays) until `parental pause "
                f"{p['name']} off`" if on else None)
        if dry_run:
            return WriteResult(plan, None, side, None, dry_run=True, applied=False)
        if p["internet_blocked"] == on:
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.r.snapshot("parental-pause")
        self.r.call(EP, "internetBlock", ownerId=p["id"], internetBlocked="true" if on else "false")
        after = self._find(p["name"])["internet_blocked"]
        return WriteResult([Change(plan[0].key, p["internet_blocked"], after, on)], str(snap), side, "tpctl parental")

    def remove(self, name: str, dry_run: bool = False) -> WriteResult:
        p = self._find(name)
        plan = [Change(f"parental[{p['name']}]", "present", "absent", "absent")]
        if dry_run:
            return WriteResult(plan, None, "its devices are no longer filtered or scheduled", None, dry_run=True, applied=False)
        snap = self.r.snapshot("parental-rm")
        self.r.call(EP, "delOwnerInList", ownerList=json.dumps([p["id"]]))
        gone = not any(x["id"] == p["id"] for x in self.profiles())
        return WriteResult([Change(plan[0].key, "present", "absent" if gone else "present", "absent")], str(snap), None,
                           "tpctl parental")
