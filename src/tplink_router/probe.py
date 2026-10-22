"""Identify a LAN device from this machine: MAC vendor, mDNS/Bonjour name + model, UPnP description, open
service ports, web page title. Light and read-only: TCP connects only (no payload except an HTTP GET / and the
UPnP description fetch), short timeouts. Meant for "what is this device?" on the user's own network.
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import ipaddress
import re
import socket
import struct
import time
import urllib.request
from pathlib import Path

# port -> (service, what it suggests)
PORTS = {
    22: ("ssh", "computer / NAS / Linux box"), 53: ("dns", "router / Pi-hole / DNS server"),
    80: ("http", "has a web page"), 443: ("https", "has a web page"), 445: ("smb", "computer / NAS (file sharing)"),
    548: ("afp", "Mac / Time Machine NAS"), 554: ("rtsp", "IP camera / media device"), 631: ("ipp", "printer"),
    1883: ("mqtt", "IoT hub"), 3389: ("rdp", "Windows PC"), 3689: ("daap", "Apple media sharing"),
    5000: ("http-alt", "NAS (Synology DSM) / dev server"), 5001: ("https-alt", "NAS (Synology DSM)"),
    5900: ("vnc", "screen sharing (Mac/PC)"), 7000: ("airplay", "AirPlay receiver (Apple TV / Mac / speaker)"),
    8008: ("cast-http", "Google Cast device"), 8009: ("cast", "Google Cast device (Chromecast / Nest / TV)"),
    8080: ("http-alt", "web UI (camera / NAS / app)"), 8123: ("home-assistant", "Home Assistant"),
    8443: ("https-alt", "web UI"), 9100: ("jetdirect", "printer"), 32400: ("plex", "Plex media server"),
    49152: ("upnp", "UPnP device (TV / media / router)"), 62078: ("lockdown", "iPhone / iPad (Apple mobile device)"),
}
OUI_SOURCES = ("https://www.wireshark.org/download/automated/data/manuf",   # Wireshark's list (IEEE + names), ~3 MB
               "https://standards-oui.ieee.org/oui/oui.csv")


def is_randomized(mac: str) -> bool:
    try:
        return bool(int(mac.replace(":", "-").split("-")[0], 16) & 0x02)
    except ValueError:
        return False


def oui_vendor(mac: str, cache_dir: Path, download: bool = True) -> tuple[str | None, str]:
    """-> (vendor, source note). Randomized (private) MACs have no vendor. The public vendor list is downloaded
    once into cache_dir (nothing about the device is sent anywhere)."""
    if is_randomized(mac):
        return None, "private (randomized) MAC: no vendor; the device hides its hardware address"
    f = cache_dir / "manuf.txt"
    if not f.exists() and download:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for url in OUI_SOURCES:
            try:
                data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tpctl"}),
                                              timeout=20).read()
            except OSError:
                continue
            if url.endswith(".csv"):              # IEEE CSV -> manuf-style lines
                rows = csv.reader(data.decode("utf-8", "ignore").splitlines())
                data = "\n".join(f"{r[1][:2]}:{r[1][2:4]}:{r[1][4:6]}\t\t{r[2]}" for r in rows if len(r) > 2 and len(r[1]) == 6).encode()
            f.write_bytes(data)
            break
    if not f.exists():
        return None, "vendor list unavailable (offline?)"
    hexmac = mac.upper().replace(":", "").replace("-", "")
    best, best_bits = None, 0
    with open(f, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not line or line[0] == "#" or line[2] != ":":
                continue
            parts = line.rstrip("\n").split("\t")
            pre, _, bits = parts[0].strip().partition("/")
            bits = int(bits or 24)
            ph = pre.replace(":", "").upper()
            if bits > best_bits and int(hexmac, 16) >> (48 - bits) == int(ph.ljust(12, "0"), 16) >> (48 - bits):
                name = next((x.strip() for x in reversed(parts[1:]) if x.strip()), None)
                best, best_bits = name, bits
    return (best, "vendor list") if best else (None, "not in the vendor list")


def local_ips() -> set[str]:
    """This machine's LAN addresses (so a probe of itself says so)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))              # no packet is sent; just picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    return ips


# -- mDNS (Bonjour) --
def _dns_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    for _ in range(64):
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", buf[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            off, jumped = ptr, True
            continue
        labels.append(buf[off + 1:off + 1 + n].decode(errors="replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def _mdns(qname: str, qtype: int, timeout: float = 1.2, target_ip: str | None = None) -> list[tuple[str, int, bytes, int]]:
    """One-shot mDNS query (QU bit: answers come back unicast). Returns [(name, type, packet, rdata_offset)]."""
    pkt = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _dns_name(qname) + struct.pack("!HH", qtype, 0x8001)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.3)
    out = []
    try:
        for dst in ([(target_ip, 5353)] if target_ip else []) + [("224.0.0.251", 5353)]:
            s.sendto(pkt, dst)
        end = time.time() + timeout
        while time.time() < end:
            try:
                buf, _ = s.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                _, _, qd, an, ns, ar = struct.unpack("!HHHHHH", buf[:12])
                off = 12
                for _ in range(qd):
                    _, off = _read_name(buf, off)
                    off += 4
                for _ in range(an + ns + ar):
                    name, off = _read_name(buf, off)
                    t, _, _, ln = struct.unpack("!HHIH", buf[off:off + 10])
                    off += 10
                    out.append((name, t, buf, off))
                    off += ln
            except (struct.error, IndexError):
                continue
    except OSError:
        pass
    finally:
        s.close()
    return out


SERVICES = ("_companion-link._tcp", "_airplay._tcp", "_raop._tcp", "_googlecast._tcp", "_ipp._tcp", "_printer._tcp",
            "_hap._tcp", "_smb._tcp", "_afpovertcp._tcp", "_device-info._tcp", "_spotify-connect._tcp", "_sonos._tcp",
            "_homekit._tcp", "_ssh._tcp", "_http._tcp", "_workstation._tcp", "_rfb._tcp", "_androidtvremote2._tcp",
            "_amzn-wplay._tcp", "_matter._tcp", "_meshcop._udp")
SERVICE_HINT = {"_companion-link._tcp": "Apple device (iPhone / iPad / Mac / Apple TV)", "_airplay._tcp": "AirPlay receiver",
                "_raop._tcp": "AirPlay speaker", "_googlecast._tcp": "Google Cast device", "_ipp._tcp": "printer",
                "_printer._tcp": "printer", "_hap._tcp": "HomeKit accessory", "_smb._tcp": "file sharing (PC / NAS)",
                "_afpovertcp._tcp": "Mac / NAS file sharing", "_spotify-connect._tcp": "Spotify Connect speaker",
                "_sonos._tcp": "Sonos speaker", "_androidtvremote2._tcp": "Android / Google TV",
                "_amzn-wplay._tcp": "Amazon Fire TV / Echo", "_matter._tcp": "Matter smart-home device",
                "_meshcop._udp": "Thread border router (HomePod / Nest hub)"}


def mdns_browse(timeout: float = 3.0) -> dict[str, dict]:
    """Ask the LAN which devices offer common Bonjour services; -> {ip: {"names", "services", "txt"}}.
    Joins 224.0.0.251:5353 (SO_REUSEPORT: coexists with the OS responder) and stitches PTR -> SRV -> A."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        s.bind(("", 5353))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton("224.0.0.251"),
                                                                               socket.inet_aton("0.0.0.0")))
    except OSError:
        s.close()
        return {}
    s.settimeout(0.3)
    q = b"".join(_dns_name(f"{t}.local") + struct.pack("!HH", 12, 1) for t in SERVICES)
    query = struct.pack("!HHHHHH", 0, 0, len(SERVICES), 0, 0, 0) + q
    ptr, srv, txt, addr = {}, {}, {}, {}
    t0 = time.time()
    end, resend = t0 + timeout, [t0, t0 + 1.0]          # ask twice: sleepy devices answer late
    while time.time() < end:
        if resend and time.time() >= resend[0]:
            resend.pop(0)
            try:
                s.sendto(query, ("224.0.0.251", 5353))
            except OSError:
                pass
        try:
            buf, src = s.recvfrom(9000)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", buf[:12])
            if not flags & 0x8000:                     # queries from others
                continue
            off = 12
            for _ in range(qd):
                _, off = _read_name(buf, off)
                off += 4
            for _ in range(an + ns + ar):
                name, off = _read_name(buf, off)
                t, _, _, ln = struct.unpack("!HHIH", buf[off:off + 10])
                off += 10
                rd = off
                if t == 12:
                    ptr.setdefault(name.lower(), set()).add(_read_name(buf, rd)[0])
                elif t == 33:
                    srv[name.lower()] = _read_name(buf, rd + 6)[0].lower()
                elif t == 16:
                    i, kv = rd, {}
                    while i < rd + ln:
                        n = buf[i]
                        item = buf[i + 1:i + 1 + n].decode(errors="replace")
                        if "=" in item:
                            k, v = item.split("=", 1)
                            kv[k] = v
                        i += 1 + n
                    txt[name.lower()] = kv
                elif t == 1 and ln == 4:
                    addr.setdefault(name.lower(), set()).add(socket.inet_ntoa(buf[rd:rd + 4]))
                off = rd + ln
            addr.setdefault(f"__src__{src[0]}", set()).add(src[0])
        except (struct.error, IndexError):
            continue
    s.close()
    out: dict[str, dict] = {}
    for stype, insts in ptr.items():
        svc = stype.removesuffix(".local")
        for inst in insts:
            host = srv.get(inst.lower())
            for ip in addr.get(host or "", set()):
                d = out.setdefault(ip, {"names": [], "services": [], "txt": {}})
                label = inst.split("._")[0]
                if label not in d["names"]:
                    d["names"].append(label)
                if svc not in d["services"]:
                    d["services"].append(svc)
                d["txt"].update({k: v for k, v in txt.get(inst.lower(), {}).items() if k in ("md", "model", "fn", "am", "ty", "rpMd", "manufacturer")})
                if host:
                    d.setdefault("host", host)
    return out


# -- UPnP / SSDP --
def upnp_info(ip: str, timeout: float = 2.0) -> dict:
    """M-SEARCH, keep the target's answer, fetch its description: friendlyName, manufacturer, modelName."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.3)
    loc = None
    try:
        s.sendto(msg, (ip, 1900))
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end and not loc:
            try:
                buf, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            if addr[0] == ip:
                m = re.search(rb"(?im)^location:\s*(\S+)", buf)
                loc = m.group(1).decode() if m else None
    except OSError:
        pass
    finally:
        s.close()
    if not loc or ipaddress.ip_address(re.sub(r"^https?://([^:/]+).*", r"\1", loc) or "0.0.0.0").is_global:
        return {}
    try:
        xml = urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "tpctl"}), timeout=3).read(200_000).decode(errors="replace")
    except (OSError, ValueError):
        return {"upnp": "yes"}
    tag = lambda t: (m.group(1).strip() if (m := re.search(rf"<{t}>([^<]{{1,120}})</{t}>", xml)) else None)
    return {k: v for k, v in {"upnp_name": tag("friendlyName"), "manufacturer": tag("manufacturer"),
                              "model": tag("modelName"), "model_description": tag("modelDescription")}.items() if v}


# -- ports + web --
def open_ports(ip: str, ports=PORTS, timeout: float = 0.6) -> list[int]:
    def tryp(p):
        try:
            with socket.create_connection((ip, p), timeout=timeout):
                return p
        except OSError:
            return None
    with cf.ThreadPoolExecutor(32) as ex:
        return sorted(p for p in ex.map(tryp, ports) if p)


def web_info(ip: str, ports: list[int]) -> dict:
    for p, scheme in ((80, "http"), (8080, "http"), (5000, "http"), (443, "https"), (8443, "https"), (5001, "https")):
        if p not in ports:
            continue
        try:
            import ssl
            ctx = ssl._create_unverified_context()            # LAN devices use self-signed certs
            r = urllib.request.urlopen(urllib.request.Request(f"{scheme}://{ip}:{p}/", headers={"User-Agent": "tpctl"}),
                                       timeout=2, context=ctx if scheme == "https" else None)
            body = r.read(65536).decode(errors="replace")
            title = re.search(r"(?is)<title[^>]*>\s*([^<]{1,100})", body)
            return {k: v for k, v in {"web_port": p, "web_title": title.group(1).strip() if title else None,
                                      "web_server": r.headers.get("Server")}.items() if v}
        except Exception:                                     # noqa: BLE001 - any failure = no web info
            continue
    return {}


def guess(d: dict) -> tuple[str | None, list[str]]:
    """Best guess of what the device is, with the evidence used."""
    ev, kind = [], None
    if d.get("this_machine"):
        return "this computer (the one running tpctl)", ["its own IP address"]
    if d.get("mesh_role"):
        return ("the main router" if d["mesh_role"] == "main_router" else "mesh satellite (router)"), ["listed by the router as a mesh node"]
    host = (d.get("bonjour_host") or d.get("dns_name") or "").lower()
    svcs = set(d.get("bonjour_services") or [])
    m = d.get("model") or ""
    for pat, k in ((r"^ps[345]-", "PlayStation"), (r"xbox", "Xbox"), (r"nintendo|switch", "Nintendo Switch")):
        if re.search(pat, host):
            return k, [f"Bonjour/DNS host name {host}"]
    apple_models = (("iPhone", "iPhone"), ("iPad", "iPad"), ("MacBookPro", "MacBook Pro"), ("MacBookAir", "MacBook Air"),
                    ("Macmini", "Mac mini"), ("iMac", "iMac"), ("Mac", "Mac"), ("AppleTV", "Apple TV"),
                    ("AudioAccessory", "HomePod"), ("Watch", "Apple Watch"))
    names = " ".join(d.get("bonjour_names") or [])
    for code, k in apple_models:
        if m.startswith(code):
            # new Macs report MacNN,N codes: the device's own name usually says which kind it is
            k = next((x for x in ("MacBook Pro", "MacBook Air", "Mac mini", "Mac Studio", "iMac") if x in names), k)
            first = (d.get("bonjour_names") or [""])[0]
            return f"{k} ({m})", [f"Bonjour model {m}"] + ([f"device name '{first}'"] if k in first else [])
    # names the device gives itself (Bonjour) or its DHCP host name as the router saw it
    for src, nm in (("device name", (d.get("bonjour_names") or [""])[0]), ("host name", d.get("router_name") or "")):
        for pat, k in ((r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"MacBook ?Pro", "MacBook Pro"), (r"MacBook ?Air|Air$", "MacBook Air"),
                       (r"Mac ?mini", "Mac mini"), (r"Mac ?Studio", "Mac Studio"), (r"iMac", "iMac"), (r"Apple ?Watch|^Watch$", "Apple Watch"),
                       (r"Apple ?TV", "Apple TV"), (r"HomePod", "HomePod"), (r"^Mac$", "Mac"), (r"(?i)^android|pixel|galaxy|oneplus|redmi|xiaomi|oppo|vivo|honor",
                                                                                 "Android phone"),
                       (r"(?i)book|laptop|thinkpad|surface", "laptop"), (r"(?i)desktop|-pc$|^pc-", "desktop PC")):
            if nm and re.search(pat, nm) and not re.fullmatch(r"(?i)iphone|ipad", nm.strip()) or (nm and re.fullmatch(pat, nm.strip())):
                return k, [f"{src} '{nm}'"]
    for svc in ("_googlecast._tcp", "_androidtvremote2._tcp", "_amzn-wplay._tcp", "_sonos._tcp", "_hap._tcp",
                "_matter._tcp", "_ipp._tcp", "_printer._tcp", "_raop._tcp", "_companion-link._tcp"):
        if svc in svcs:
            kind = SERVICE_HINT[svc] + (f" — {d['cast_name']}" if d.get("cast_name") else "")
            ev.append(f"announces {svc}")
            break
    ports = set(d.get("open_ports") or [])
    model = (d.get("model") or "") + " " + (d.get("upnp_name") or "") + " " + (d.get("manufacturer") or "")
    vendor = (d.get("vendor") or "").lower()
    rules = [
        (62078 in ports, "iPhone / iPad", "port 62078 (Apple mobile lockdown service)"),
        (bool(re.search(r"iPhone", d.get("model") or "")), "iPhone", f"Bonjour model {d.get('model')}"),
        (bool(re.search(r"iPad", d.get("model") or "")), "iPad", f"Bonjour model {d.get('model')}"),
        (bool(re.search(r"Mac|iMac", d.get("model") or "")), "Mac", f"Bonjour model {d.get('model')}"),
        (bool(re.search(r"AppleTV|AudioAccessory", d.get("model") or "")), "Apple TV / HomePod", f"Bonjour model {d.get('model')}"),
        (8009 in ports, "Google Cast device (Chromecast / Nest / TV)", "port 8009 (Cast)"),
        (bool({631, 9100} & ports), "printer", "printer ports (IPP / 9100)"),
        (bool({5000, 5001} & ports) and "synology" in (str(d.get("web_title")) + vendor).lower(), "Synology NAS", "DSM web UI"),
        (32400 in ports, "Plex media server", "port 32400"),
        (8123 in ports, "Home Assistant", "port 8123"),
        (554 in ports, "IP camera / media device", "RTSP port 554"),
        (3389 in ports, "Windows PC", "RDP port 3389"),
        (bool(re.search(r"(?i)tv|bravia|roku|fire ?tv", model)), "TV / streaming box", f"UPnP {model.strip()}"),
        (bool(re.search(r"(?i)sonos", model + vendor)), "Sonos speaker", "Sonos"),
        (bool(re.search(r"(?i)playstation|xbox|nintendo", model + vendor)), "game console", model.strip() or vendor),
        ("espressif" in vendor or "tuya" in vendor, "smart-home / IoT gadget", f"vendor {d.get('vendor')}"),
        ("raspberry" in vendor, "Raspberry Pi", f"vendor {d.get('vendor')}"),
        (7000 in ports, "AirPlay receiver (Apple TV / Mac / speaker)", "AirPlay port 7000"),
        (bool({22, 445} & ports), "computer / NAS", "ssh/smb ports"),
    ]
    for ok, k, why in rules:
        if ok:
            kind = kind or k
            ev.append(why)
    if not kind and d.get("vendor") and "Xerox" not in d["vendor"]:
        kind = f"{d['vendor']} device"
        ev.append("hardware vendor (MAC)")
    if not kind and d.get("router_type"):
        kind = {"mobile": "phone / tablet", "phone": "phone / tablet", "computer": "computer", "pc": "computer"}.get(
            d["router_type"].lower(), d["router_type"].lower())
        ev.append(f"router classifies it as {d['router_type']}")
    return kind, ev


def lan_blocked(ip: str) -> bool:
    """macOS Local Network privacy: unsigned/third-party programs get EHOSTUNREACH instantly for LAN hosts
    (the gateway and this machine are exempt) unless the terminal app was allowed. Detect it."""
    import errno
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((ip, 9))
    except OSError as e:
        return e.errno == errno.EHOSTUNREACH
    finally:
        s.close()
    return False


SYSTEM_PYTHON = "/usr/bin/python3"          # Apple-signed: exempt from Local Network privacy


def _helper(args: list[str]):
    """Run this module in the Apple-signed system Python (exempt from macOS Local Network privacy)."""
    import json
    import subprocess
    r = subprocess.run([SYSTEM_PYTHON, "-I", __file__, *args], capture_output=True, text=True, timeout=90)
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise OSError("macOS blocked local-network access for this program; allow your terminal app in System "
                      "Settings > Privacy & Security > Local Network" + (f" ({r.stderr.strip()[-160:]})" if r.stderr else ""))


def _needs_helper(ip: str) -> bool:
    import os
    import sys
    return (sys.platform == "darwin" and os.path.exists(SYSTEM_PYTHON)
            and os.path.realpath(sys.executable) != os.path.realpath(SYSTEM_PYTHON) and lan_blocked(ip))


def browse(sample_ip: str, timeout: float = 3.0) -> dict[str, dict]:
    """mdns_browse(), through the system-Python helper when macOS blocks this process."""
    return _helper(["browse", str(timeout)]) if _needs_helper(sample_ip) else mdns_browse(timeout)


def _bonjour_fields(bj: dict) -> dict:
    d: dict = {}
    if not bj:
        return d
    txt = bj.get("txt") or {}
    d |= {"bonjour_names": bj.get("names"), "bonjour_host": bj.get("host"), "bonjour_services": bj.get("services")}
    model = txt.get("model") or txt.get("am") or (txt.get("md") if txt.get("md", "").strip(" ,0123456789") else None)
    if model:
        d["model"] = model
    if txt.get("fn"):
        d["cast_name"] = txt["fn"]
    return d


def probe(ip: str, mac: str, cache_dir: Path, ports: bool = True, bonjour: dict | None = None,
          allow_helper: bool = True) -> dict:
    """Everything this machine can learn about one device (~1-3 s). `bonjour` = a shared browse() result."""
    if allow_helper and _needs_helper(ip):
        bonjour = bonjour if bonjour is not None else browse(ip)
        d = _helper(["probe", ip, mac, str(cache_dir), "1" if ports else "0", "nobrowse"])
        d["via"] = "system python (macOS Local Network privacy blocks this one)"
        return _bonjour_fields(bonjour.get(ip) or {}) | d
    d: dict = _bonjour_fields((bonjour if bonjour is not None else mdns_browse()).get(ip) or {})
    with cf.ThreadPoolExecutor(3) as ex:
        f_u = ex.submit(upnp_info, ip)
        f_p = ex.submit(open_ports, ip) if ports else None
        f_v = ex.submit(oui_vendor, mac, cache_dir)
        up = f_u.result()
        d |= {k: v for k, v in up.items() if k != "model" or not d.get("model")}
        if up.get("model") and d.get("model") and up["model"] != d["model"]:
            d["upnp_model"] = up["model"]
        vendor, note = f_v.result()
        d |= {"vendor": vendor, "vendor_note": note}
        if f_p:
            d["open_ports"] = f_p.result()
    if d.get("open_ports"):
        d["services"] = [f"{p}/{PORTS[p][0]}" for p in d["open_ports"]]
        d |= web_info(ip, d["open_ports"])
    with cf.ThreadPoolExecutor(1) as ex:                    # reverse DNS can hang for seconds: cap it
        try:
            d["dns_name"] = ex.submit(socket.gethostbyaddr, ip).result(timeout=1.5)[0]
        except (OSError, cf.TimeoutError):
            pass
    if ip in local_ips():
        d["this_machine"] = True
    return d


if __name__ == "__main__":                       # helper: probe IP MAC CACHE_DIR [PORTS] | browse [TIMEOUT]
    import json
    import sys
    a = sys.argv[1:]
    if a[0] == "browse":
        print(json.dumps(mdns_browse(float((a[1:] or ["2.5"])[0]))))
    else:
        print(json.dumps(probe(a[1], a[2], Path(a[3]), (a[4:] or ["1"])[0] == "1",
                               bonjour={} if "nobrowse" in a else None, allow_helper=False)))
