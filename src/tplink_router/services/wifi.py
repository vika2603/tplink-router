from __future__ import annotations

from ..errors import UsageError
from ..models import Change, WriteResult  # noqa: F401
from . import Service

BANDS = ("2g", "5g", "5g_2", "6g")
NAMES = {"2g": "2.4G", "5g": "5G", "5g_2": "5G", "6g": "6G"}   # superset; the connected model's bands come from r.caps.bands
SIDE_EFFECT = ("radios restart: all Wi-Fi devices drop 30-60s, auto channels re-pick, EasyMesh satellites "
               "re-onboard (~1-3 min, up to ~5 min; topology may change)")
NEXT = "tpctl mesh wait && tpctl check"


class WifiService(Service):
    def band(self, b: str) -> dict:
        w = self.r.call(f"admin/wireless?form=wireless_{b}", "read_spf")
        sec = {"sae_transition": "WPA2/WPA3", "sae_only": "WPA3", "rsn": "WPA2", "psk": "WPA2", "wpa": "WPA"}.get(
            str(w.get("psk_version")), None)
        if w.get("encryption") in ("none", "", None) and not sec:
            sec = "open"
        return {"security": sec or f"{w.get('encryption')}/{w.get('psk_version')}",
                "enable": w.get("enable"), "ssid": w.get("ssid"), "channel": w.get("current_channel"),
                "channel_setting": w.get("channel"), "width": w.get("htmode"), "mode": w.get("hwmode"),
                "txpower": w.get("txpower"), "encryption": w.get("encryption"), "psk_version": w.get("psk_version"),
                "hidden": w.get("hidden"), "psc": w.get("psc"), "afc": w.get("afc_enable"),
                "unii4": w.get("auto_channel_unii_4")}

    def bands_brief(self) -> dict:
        """Only what the concise overview prints: per-band ssid/channel/width/enable + smart connect (4 calls)."""
        r = self.r
        return {"smart_connect": r.try_call("admin/wireless?form=smart_connect").get("smart_enable") if r.caps.has("smart_connect") else "unsupported",
                "bands": {b: self.band(b) for b in r.caps.bands}}

    def get(self) -> dict:
        r, has = self.r, self.r.caps.has
        add = r.try_call("admin/wireless?form=wireless_addition_setting", quiet=True)
        return {"smart_connect": r.try_call("admin/wireless?form=smart_connect").get("smart_enable") if has("smart_connect") else "unsupported",
                "ofdma_mimo": r.try_call("admin/wireless?form=ofdma_mimo").get("setting") if has("ofdma_mimo") else "unsupported",
                "region": r.try_call("admin/wireless?form=region", quiet=True).get("country"),
                "zero_wait_dfs": add.get("zerowait_dfs"), "beacon_ms": add.get("beacon_int"),
                "dtim": add.get("dtim_period"),
                "guest_network": r.try_call("admin/wireless?form=guest_2g").get("enable") if has("guest_network") else "unsupported",
                "iot_network": r.try_call("admin/wireless?form=iot_2g", quiet=True).get("enable"),
                "bands": {b: self.band(b) for b in r.caps.bands}}

    # -- IoT network (Advanced > Wireless > IoT Network): a separate SSID for smart-home gadgets --
    def _iot_ep(self) -> tuple[str, list[str]]:
        bands = [b for b in ("2g", "5g", "5g_2") if b in self.r.caps.bands]   # IoT has no 6G form (rejected)
        return "admin/wireless?" + "&".join(f"form=iot_{b}" for b in bands), bands

    def iot(self) -> dict:
        ep, bands = self._iot_ep()
        d = self.r.call(ep, "read_spf") or {}            # the combined form carries iot_<band>_* keys (one form: unprefixed)
        out = {b: {"enable": d.get(f"iot_{b}_enable"), "ssid": d.get(f"iot_{b}_ssid"), "encryption": d.get(f"iot_{b}_encryption")}
               for b in bands if f"iot_{b}_enable" in d}
        iso = (self.r.try_call("admin/iot_security?form=enable", quiet=True) or {}).get("enable")
        return {"enable": "on" if any(x["enable"] == "on" for x in out.values()) else "off", "bands": out, "isolation": iso}

    def set_iot(self, enable: bool, dry_run: bool = False) -> WriteResult:
        """Turn the IoT network on/off on every band (its SSID/password are kept; set them in the web UI)."""
        ep, bands = self._iot_ep()
        cur = self.r.call(ep, "read_spf") or {}
        ch = {f"iot_{b}_enable": "on" if enable else "off" for b in bands if f"iot_{b}_enable" in cur}
        plan = [Change(f"iot.{k[4:].removesuffix('_enable')}.enable", cur.get(k), v, v) for k, v in ch.items()]
        if dry_run:
            return WriteResult(plan, None, SIDE_EFFECT, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False, skipped_reason="already at target; nothing written")
        snap = self.r.snapshot("iot")
        self.r.call(ep, "write_spf", **ch)
        after = self.r.call(ep, "read_spf") or {}
        return WriteResult([Change(c.key, c.old, after.get(k), c.target) for c, k in zip(plan, ch)], str(snap), SIDE_EFFECT,
                           "tpctl mesh wait && tpctl wifi iot")

    # -- guest network (Advanced > Wireless > Guest Network) --
    def _guest_ep(self) -> tuple[str, list[str]]:
        bands = self.r.caps.bands
        forms = ["guest_2g", "guest_5g", "guest_2g5g"] + (["guest_5g_2"] if "5g_2" in bands else []) + \
                (["guest_6g"] if "6g" in bands else [])
        per_band = [b for b in ("2g", "5g", "5g_2", "6g") if b in bands] if "6g" in bands else ["2g5g"]
        return "admin/wireless?" + "&".join(f"form={f}" for f in forms), per_band

    def guest(self) -> dict:
        """{"enable", "ssid", "security", "bands": {band: {enable, ssid, security}}, "lan_access", "isolate"}."""
        self.r.caps.require("guest_network", "guest network")
        ep, per_band = self._guest_ep()
        d = self.r.call(ep, "read") or {}
        acc = self.r.try_call("admin/wireless?form=guest", quiet=True) or {}
        bands = {b: {"enable": d.get(f"guest_{b}_enable"), "ssid": d.get(f"guest_{b}_ssid"),
                     "security": {"none": "open", "owe": "OWE (open, encrypted)", "psk": "WPA2", "psk_sae": "WPA2/WPA3"
                                  if d.get(f"guest_{b}_psk_version") != "sae_only" else "WPA3"}.get(d.get(f"guest_{b}_encryption"),
                                                                                                    d.get(f"guest_{b}_encryption"))}
                 for b in per_band}
        on = [b for b, x in bands.items() if x["enable"] == "on"]
        return {"enable": "on" if on else "off", "bands": bands, "lan_access": acc.get("access"), "isolate": acc.get("isolate")}

    def set_guest(self, enable: bool | None = None, ssid: str | None = None, password: str | None = None,
                  open_network: bool = False, dry_run: bool = False) -> WriteResult:
        """Turn the guest Wi-Fi on/off, rename it, set a password (WPA2/WPA3; WPA3 on 6G) or make it open."""
        self.r.caps.require("guest_network", "guest network")
        if password is not None and not 8 <= len(password) <= 63:
            raise UsageError("guest password must be 8-63 characters", None, None, "password")
        if ssid is not None and not 1 <= len(ssid.encode()) <= 32:
            raise UsageError("SSID must be 1-32 bytes", ssid, None, "ssid")
        ep, per_band = self._guest_ep()
        ch: dict[str, str] = {}
        for b in per_band:
            if enable is not None:
                ch[f"guest_{b}_enable"] = "on" if enable else "off"
            if ssid is not None:
                ch[f"guest_{b}_ssid"] = ssid
            if password is not None:
                ch |= {f"guest_{b}_encryption": "psk_sae", f"guest_{b}_psk_version": "sae_only" if b == "6g" else "sae_transition",
                       f"guest_{b}_psk_key": password}
            elif open_network:
                ch |= ({f"guest_{b}_encryption": "owe", f"guest_{b}_psk_version": "owe_only"} if b == "6g"
                       else {f"guest_{b}_encryption": "none"})
        if not ch:
            raise UsageError("nothing to change: give on/off, --ssid, --password or --open")
        res = self.r.apply_form(ep, ch, tag="guest", partial=True, dry_run=dry_run, side_effect=SIDE_EFFECT,
                                next_cmd="tpctl mesh wait && tpctl wifi guest")
        import re as _re
        for c in res.changes:
            if c.key.endswith("_psk_key"):        # never echo the password back
                c.old, c.new, c.target = ("***" if c.old else None), ("***" if c.new else None), "***"
            m = _re.match(r"guest_(2g5g|5g_2|2g|5g|6g)_(.+)", c.key)
            if m:
                c.key = f"guest.{m.group(1)}.{m.group(2)}"
        return res

    # -- writes: plan -> dry-run | no-op skip (no radio restart) -> snapshot -> write -> verify --
    def _write(self, tag: str, targets: list[tuple[str, callable, str]], do, dry_run: bool) -> WriteResult:
        """targets: (key, getter(wifi_dict) -> current value, target value)."""
        before = self.get()
        plan = [Change(k, get(before), want, want) for k, get, want in targets]
        if dry_run:
            return WriteResult(plan, None, SIDE_EFFECT, None, dry_run=True, applied=False)
        if not any(c.changed for c in plan):
            return WriteResult(plan, None, None, None, applied=False,
                               skipped_reason="already at target; nothing written, radios untouched")
        snap = self.r.snapshot(tag)
        do()
        after = self.get()
        return WriteResult([Change(k, get(before), get(after), want) for k, get, want in targets], str(snap), SIDE_EFFECT, NEXT)

    def set_smart_connect(self, on: bool, dry_run: bool = False, ssids: dict | None = None) -> WriteResult:
        """Toggle band steering. With on=False, `ssids={"5g": "Home-5G"}` also gives bands their own names in the
        same plan (smart connect must be off for per-band names)."""
        self.r.caps.require("smart_connect", "smart connect")
        v = "on" if on else "off"
        ssids = {b: n for b, n in (ssids or {}).items() if n}
        if ssids and on:
            raise UsageError("per-band SSIDs need smart connect OFF")
        if bad := set(ssids) - set(self.r.caps.bands):
            raise UsageError(f"band(s) {sorted(bad)} not on this router; it has {self.r.caps.bands}")
        targets = [("smart_connect", lambda w: w["smart_connect"], v)]
        targets += [(f"{b}.ssid", lambda w, b=b: w["bands"][b]["ssid"], n) for b, n in ssids.items()]

        def do():
            self.r.call("admin/wireless?form=smart_connect", "write", smart_enable=v)
            for b, n in ssids.items():
                self.r.write_wifi([b], ssid=n)
        return self._write("wifi-smart-connect", targets, do, dry_run)

    def set_ofdma_mimo(self, mode: str, dry_run: bool = False) -> WriteResult:
        if mode not in ("all", "ofdma_only", "disable"):
            raise UsageError("ofdma_mimo mode: all | ofdma_only | disable")
        self.r.caps.require("ofdma_mimo", "the combined OFDMA/MU-MIMO switch")
        return self._write("wifi-ofdma-mimo", [("ofdma_mimo", lambda w: w["ofdma_mimo"], mode)],
                           lambda: self.r.call("admin/wireless?form=ofdma_mimo", "write", setting=mode), dry_run)

    def _band_field(self, tag, field, key, value, bands, dry_run) -> WriteResult:
        have = self.r.caps.bands
        bands = list(bands or have)
        if bad := set(bands) - set(have):
            raise UsageError(f"band(s) {sorted(bad)} not on this router; it has {have}")
        return self._write(tag, [(f"{b}.{key}", lambda w, b=b: w["bands"][b][key], value) for b in bands],
                           lambda: self.r.write_wifi(bands, **{field: value}), dry_run)

    def set_ssid(self, ssid: str, bands=None, dry_run: bool = False) -> WriteResult:
        if self.get()["smart_connect"] == "on" and bands and set(bands) != set(self.r.caps.bands):
            flags = " ".join(f"--ssid-{b} {ssid}" for b in bands)
            raise UsageError("smart connect is on, so all bands share one name: omit --band to rename all, or turn smart "
                             f"connect off and name bands in one plan: `tpctl wifi set smart-connect off {flags}`")
        if not 1 <= len(ssid.encode()) <= 32:
            raise UsageError("SSID must be 1-32 bytes")
        return self._band_field("wifi-ssid", "ssid", "ssid", ssid, bands, dry_run)

    def set_txpower(self, level: str, bands=None, dry_run: bool = False) -> WriteResult:
        if level not in ("low", "middle", "high"):
            raise UsageError("txpower: low | middle | high")
        return self._band_field("wifi-txpower", "txpower", "txpower", level, bands, dry_run)

    def set_band(self, band: str, channel: str | None = None, width: str | None = None, txpower: str | None = None,
                 dry_run: bool = False) -> WriteResult:
        """Several settings of one band in ONE write (one radio restart, one confirmation)."""
        have = self.r.caps.bands
        if band not in have:
            raise UsageError(f"band {band} not on this router; it has {have}")
        if width is not None and str(width) not in ("20", "40", "80", "160", "320", "auto"):
            raise UsageError("width: 20 | 40 | 80 | 160 | 320 | auto")
        if txpower is not None and txpower not in ("low", "middle", "high"):
            raise UsageError("txpower: low | middle | high")
        fields, targets = {}, []
        for field, key, val in (("channel", "channel_setting", channel), ("htmode", "width", width), ("txpower", "txpower", txpower)):
            if val is not None:
                fields[field] = str(val)
                targets.append((f"{band}.{key}", lambda w, k=key: w["bands"][band][k], str(val)))
        if not fields:
            raise UsageError("nothing to change: give --channel, --width and/or --txpower")
        return self._write("wifi-band", targets, lambda: self.r.write_wifi([band], **fields), dry_run)

    def set_channel(self, band: str, channel: str, dry_run: bool = False) -> WriteResult:
        res = self._band_field("wifi-channel", "channel", "channel_setting", str(channel), [band], dry_run)
        if res.applied and not res.dry_run:                  # surface a width the router changed on its own
            w = self.band(band).get("width")
            res.changes.append(Change(f"{band}.width (resulting)", None, w))
        return res

    def set_width(self, band: str, width: str, dry_run: bool = False) -> WriteResult:
        if str(width) not in ("20", "40", "80", "160", "320", "auto"):
            raise UsageError("width: 20 | 40 | 80 | 160 | 320 | auto")
        return self._band_field("wifi-width", "htmode", "width", str(width), [band], dry_run)


    # -- impact analysis for plans (used before asking the user to confirm) --
    SCOPE = ("main router radios. EasyMesh pushes SSID/security to satellites; satellites on wireless 5G backhaul necessarily "
             "follow channel/width; whether per-satellite transmit power changes is not verified")
    SCOPE_SSID = "SSID/security apply on the main router and are pushed to every satellite by EasyMesh"

    def scope_for(self, bands: list[str] | None) -> str:
        """Scope text for radio changes on these bands, honest about what is and isn't known."""
        bands = list(bands or self.r.caps.bands)
        bh_bands = {n.backhaul_band for n in self.r.mesh.inventory().nodes if n.backhaul == "wifi"}
        touched = sorted({NAMES[b] for b in bands})
        bh = sorted(bh_bands & set(touched))
        part = (f"satellites backhaul over {', '.join(bh)}: they must follow channel/width changes on it"
                if bh else f"no satellite backhauls over {', '.join(touched)}: mesh links don't depend on this band")
        return f"main router {', '.join(touched)} radio(s); {part}"

    def effect_counts(self, bands: list[str] | None) -> dict:
        """The effect as numbers: drop (on these bands, or behind a satellite whose backhaul restarts), may_blip (other
        bands on the main router), wired_unaffected, worst_case (= drop + may_blip)."""
        bands = list(bands or self.r.caps.bands)
        names = {NAMES[b] for b in bands}
        inv = self.r.mesh.inventory()
        on = {c.mac for c in inv.devices if c.band in names}
        bh = [n for n in inv.nodes if n.backhaul == "wifi" and n.backhaul_band in names]
        drop = on | {c.mac for n in bh for c in inv.devices_on(n) if c.band != "wired"}
        blip = {c.mac for c in inv.devices if c.node == inv.main.name and c.band not in names and c.band != "wired"} - drop
        wired = sum(1 for c in inv.devices if c.band == "wired")
        by_mac = {c.mac: c for c in inv.devices}
        sat = sum(1 for m in drop if by_mac[m].node != inv.main.name)
        main_all = len(inv.devices_on(inv.main.name))
        main_wifi = sum(1 for c in inv.devices_on(inv.main.name) if c.band != "wired")
        return {"drop": len(drop), "drop_main": len(drop) - sat, "drop_satellites": sat,
                "main_devices": main_all, "main_wifi": main_wifi,
                "drop_on_band": len(on), "drop_other_band": len(drop - on), "may_blip": len(blip),
                "lose_ssid": 0,
                "wired_unaffected": wired, "worst_case": len(drop) + len(blip),
                "outage_main": "30-60s", "outage_satellites": "~1-3 min" if bh else None}

    def effect_for(self, bands: list[str] | None) -> str:
        bands = list(bands or self.r.caps.bands)
        names = {NAMES[b] for b in bands}
        inv = self.r.mesh.inventory()
        on = {c.mac: c for c in inv.devices if c.band in names}
        bh_sats = [n for n in inv.nodes if n.backhaul == "wifi" and n.backhaul_band in names]
        behind = {c.mac: c for n in bh_sats for c in inv.devices_on(n) if c.band != "wired"}
        hit = {**on, **behind}
        extra = len(set(behind) - set(on))
        why = (f" — {len(on)} on {', '.join(sorted(names))}, plus {extra} on other bands of the {len(bh_sats)} satellite(s) whose "
               f"{', '.join(sorted(names))} backhaul restarts with it") if extra else ""
        sat = sum(1 for c in hit.values() if c.node != inv.main.name)
        main_total = len(inv.devices_on(inv.main.name))
        main_total = sum(1 for c in inv.devices_on(inv.main.name) if c.band != "wired")
        all_main = len(inv.devices_on(inv.main.name))
        parts = [f"{n} of the main router's {all_main} devices ({main_total} Wi-Fi)" if w == "the main router" else f"{n} on {w}"
                 for n, w in ((len(hit) - sat, "the main router"), (sat, "satellites")) if n]
        where = f" mesh-wide ({', '.join(parts)})" if parts else ""
        dur = ("for 30-60s (main router) / ~1-3 min (satellite devices, until their node re-onboards)"
               if sat and bh_sats else "for 30-60s")
        return (f"{len(hit)} device{'s' * (len(hit) != 1)}{where} {'drop' if len(hit) != 1 else 'drops'} {dur}{why} "
                f"({', '.join(c.label for c in sorted(hit.values(), key=lambda c: c.label.lower()))}); other main-router bands "
                "may blip too; satellites re-onboard (~1-3 min, up to ~5 min)")

    def caveats(self, kind: str, bands: list[str] | None, value: str | None = None) -> list[str]:
        """Direction- and topology-aware risks, shown in plans before the user confirms."""
        """Concrete risks of a radio change given the current mesh: weak wireless backhauls and edge devices."""
        from .. import quality
        inv = self.r.mesh.inventory()
        bands = list(bands or self.r.caps.bands)
        out = []
        if kind == "band":                                   # combined change: value = {"channel":…, "width":…, "txpower":…}
            ch = dict(value or {})
            res = []
            if ch.get("width") or ch.get("channel"):
                res += self.caveats("width" if ch.get("width") else "channel", bands, ch.get("width") or ch.get("channel"))
            if ch.get("channel") and ch.get("width"):
                res += [c for c in self.caveats("channel", bands, ch["channel"]) if c not in res and "backhaul" not in c]
            if ch.get("txpower"):
                res += [c for c in self.caveats("txpower", bands, ch["txpower"]) if c not in res and "backhaul" not in c]
                if not (ch.get("width") or ch.get("channel")):
                    res = self.caveats("txpower", bands, ch["txpower"])
            seen, out_ = False, []                  # one "other bands may blip" note per plan (sub-plans each add one)
            for c in res:
                if "on the main router's other bands" in c:
                    if seen:
                        continue
                    seen = True
                out_.append(c)
            return out_
        weakening = (kind == "txpower" and value in ("low", "middle")) or kind in ("width", "channel")
        if weakening and any(b.startswith("5g") for b in bands):
            # txpower only changes the main router's links; channel/width move every 5G backhaul (EasyMesh follows the main)
            whole_mesh = kind in ("width", "channel")
            direct = [n for n in inv.nodes if n.backhaul == "wifi" and n.backhaul_band == "5G"
                      and (whole_mesh or n.parent_mac == inv.main.mac)]
            for n in direct:
                if n.backhaul_grade in ("fair", "poor", "bad"):
                    out.append(f"{n.name} uses a {n.backhaul_grade} 5G wireless backhaul to the main router "
                               f"({n.backhaul_dbm} dBm); this change can weaken or drop it, affecting "
                               f"{n.devices_behind} device{'s' * (n.devices_behind != 1)} behind it (devices_behind at read_at)")
            good = [n for n in direct if n.backhaul_grade in ("excellent", "good")]
            if good:
                out.append("ok: " + ", ".join(f"{n.name} ({n.backhaul_grade}, {n.backhaul_dbm} dBm)" for n in good)
                           + (" also use 5G backhaul; channel/width changes propagate through the whole mesh, so they "
                              "re-onboard too (they have margin)" if whole_mesh else
                              " also backhaul over the main router's 5G; they have margin but re-onboard after the restart"))
        if kind == "ssid" and "6g" in bands:
            sats = [n for n in inv.nodes if not n.is_main]
            if sats and not any(any("6g" in k for k in (n.raw.get("detail") or {})) for n in sats):
                out.append("ok: satellites report only 2.4G/5G radios, so the 6G network is served by the main router alone")
        if kind == "ssid":
            bh = sorted({n.backhaul_band for n in inv.nodes if n.backhaul == "wifi" and n.backhaul_band})
            if bh:
                out.append(f"ok: the mesh backhaul ({', '.join(bh)}) uses its own hidden link, not this network name: renaming "
                           "doesn't cut satellites off (they still re-onboard after the Wi-Fi restart)")
            on = {NAMES[b] for b in bands} if bands and set(bands) != set(self.r.caps.bands) else None
            wl = [c for c in inv.devices if c.band != "wired" and (on is None or c.band in on)]
            names_ = ", ".join(c.label for c in sorted(wl, key=lambda c: c.label.lower()))     # complete list, never truncated
            if on:                                   # only some bands renamed: the others keep the old name
                keep = sorted(NAMES[b] for b in self.r.caps.bands if NAMES[b] not in on)
                old = sorted({x["ssid"] for b, x in self.get()["bands"].items() if NAMES[b] not in on and x.get("ssid")})
                out.append("ok: password and security stay the same on every band; only the name changes")
                join = lambda xs, w: xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + f" {w} " + xs[-1]
                out.append(f"{len(wl)} device{'s' * (len(wl) != 1)} on {join(sorted(on), 'and')} most likely reconnect to "
                           f"'{join(old, 'or')}' on {join(keep, 'or')} by themselves (online, but without {join(sorted(on), 'or')}) "
                           f"until someone joins them to the new name; any that only know {join(sorted(on), 'or')} stay "
                           f"offline until then. Affected: {names_}. {join(keep, 'and')} keep the name '{join(old, 'and')}'; "
                           "wired devices are unaffected; satellites still re-onboard after the restart")
            else:
                out.append("ok: password and security stay the same; only the name changes")
                out.append(f"{len(wl)} wireless device{'s' * (len(wl) != 1)} will disconnect and must rejoin the new name "
                           f"with the same password (saved networks don't follow a rename): {names_}; wired devices keep "
                           "working; satellites receive the new name via EasyMesh and re-onboard after the restart")
        if kind == "txpower" and value in ("low", "middle"):
            edge = [c for c in inv.devices if c.node == inv.main.name and c.band in {NAMES[b] for b in bands}
                    and c.signal_grade in ("fair",) + quality.WEAK_GRADES]
            if edge:
                out.append(f"{len(edge)} device{'s' * (len(edge) != 1)} on the main router {'are' if len(edge) != 1 else 'is'} already at fair/weak signal on these bands "
                           f"and may drop or roam: " + ", ".join(f"{c.label} ({c.signal_dbm} dBm)" for c in edge[:6]))
        affected = {NAMES[b] for b in bands}
        bh = [n for n in inv.nodes if n.backhaul == "wifi"]
        if bh and kind in ("txpower", "channel", "width"):
            on_band = [n for n in bh if n.backhaul_band in affected]
            if not on_band:
                used = sorted({n.backhaul_band or "?" for n in bh})
                out.append(f"ok: mesh backhaul unaffected (satellite backhauls use {', '.join(used)}, not {', '.join(sorted(affected))})")
        cur = self.get()
        secs = {NAMES.get(b, b): x.get("security") for b, x in cur["bands"].items()}
        late = []                                        # risks that go after the change-specific ones
        for n in inv.nodes:                              # every radio-restarting change
            if not n.is_main and not n.online:
                late.append(f"{n.name} is offline right now: it may or may not re-onboard after the restart")
            elif n.status_flaps_30m >= 2:
                late.append(f"{n.name} has been dropping off the mesh ({n.status_flaps_30m} status changes in the last hour): "
                            "a radio restart can knock it off again")
        if kind in ("smart-connect", "ssid"):           # any radio restart: weak wireless backhauls are the fragile part
            for n in inv.nodes:
                if not n.online:
                    continue
                if n.backhaul == "wifi" and n.backhaul_grade in ("poor", "bad"):
                    late.append(f"{n.name} has a {n.backhaul_grade} wireless backhaul ({n.backhaul_dbm} dBm): after the "
                               f"radio restart it may take longer to re-onboard or attach worse; {n.devices_behind} "
                               f"device{'s' * (n.devices_behind != 1)} behind it")
        if kind in ("smart-connect", "ssid") and len(set(secs.values())) > 1:
            from collections import Counter as _C
            common = _C(secs.values()).most_common(1)[0][0]
            odd = [f"{b} {s}" for b, s in secs.items() if s != common]
            out.append(f"ok: {', '.join(odd)} differs from the other bands ({common}): devices that can't do "
                       f"{'/'.join(sorted({secs[b.split()[0]] for b in odd}))} won't use {'that band' if len(odd) == 1 else 'those bands'}, whatever the names")
        if kind == "smart-connect":
            ssids = sorted({b["ssid"] for b in cur["bands"].values() if b.get("ssid")})
            if value == "on":
                out.append("devices that only saved a per-band SSID must rejoin with the unified one")
            else:
                out.append("band steering stops: devices choose bands themselves (many prefer 2.4G's reach; 6G-capable "
                           "ones may stay on 6G)")
                out.append("ok: devices on bands that keep their name reconnect by themselves after the restart")
                out.append(f"all bands currently share SSID {', '.join(ssids)}: to choose bands manually, give them "
                           "separate names in the same plan (`tpctl wifi set smart-connect off --ssid-5g NAME`)")
        if kind == "channel" and value and value.isdigit():
            ch = int(value)
            for b in bands:
                w = cur["bands"].get(b, {}).get("width")
                if str(cur["bands"].get(b, {}).get("channel")) == str(ch):
                    out.append(f"ok: {b} already runs on channel {ch} (auto picked it); this only pins it so auto "
                               "selection stops moving it")
                if b.startswith("5g") and 52 <= ch <= 144:
                    out.append(f"channel {ch} is DFS: the radio must listen for radar first (often ~1 min, up to 10 min, "
                               "no 5G meanwhile) and radar later forces a channel change")
                if b.startswith("5g") and ch >= 149 and w == "160":
                    unii4 = cur["bands"].get(b, {}).get("unii4")
                    out.append(f"{b} is set to 160 MHz: around channel {ch} that needs UNII-4 (169-177, currently "
                               f"unii4={unii4}) or the router narrows the width (likely 80 MHz); check width in the result")
        if kind == "width" and value in ("160", "320"):
            out.append(f"{value} MHz uses more spectrum: more interference/DFS exposure and shorter range at the edges")
        if kind == "width" and str(value).isdigit():
            for b in bands:
                curw = str(self.get()["bands"].get(b, {}).get("width") or "")
                if curw.isdigit() and int(value) < int(curw):
                    out.append(f"{NAMES[b]} {curw} -> {value} MHz cuts the peak PHY rate to about 1/{int(curw) // int(value)} "
                               "(fast devices near the router lose top speed; edge devices often gain stability)")
                    bh = [n for n in inv.nodes if n.backhaul == "wifi" and n.backhaul_band == NAMES[b] and n.backhaul_mbps]
                    if bh:
                        f = int(curw) // int(value)
                        out.append(f"the {NAMES[b]} backhaul of {len(bh)} satellite(s) narrows too, so their capacity drops to "
                                   "about 1/" + str(f) + " (rates as read now; PHY rates swing between reads): " + ", ".join(f"{n.name} {n.backhaul_mbps}->~{n.backhaul_mbps // f} Mbps"
                                                                     for n in bh))
        if kind in ("width", "channel", "txpower", "band"):
            names = {NAMES[b] for b in bands}
            others = [c for c in inv.devices if c.node == inv.main.name and c.band not in names and c.band != "wired"]
            if others:
                ob = ", ".join(sorted({c.band for c in others}))
                out.append(f"ok: {len(others)} device{'s' * (len(others) != 1)} on the main router's other bands ({ob}) may "
                           "blip briefly during the radio restart; they keep their connection settings")
        return out + late
