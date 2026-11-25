#!/usr/bin/env python3
"""
Nest Hub-tjeneste til Positionslogger.

Kører på Pi'en (eller PC'en) ved siden af serveren og gør to ting:

1. Kiosk: holder Positionslogger's /hub-side vist på en Google Nest Hub (eller
   anden skærm med Google Cast) via DashCast. Hubben smider castet væk efter et
   stykke tid eller når nogen rører skærmen - så caster vi igen, når hubben er
   ledig. Er hubben i gang med noget andet (musik, Gemini, video), lader vi den være.

2. Oplæsning: henter seneste position for hver enhed fra serveren, tjekker dem
   mod dine geofence-zoner og får hubben til at sige fx "Daniel er kommet hjem".
   Kan også advare om lavt batteri.

Brug:
    python nesthub.py --config config.toml              # kør som tjeneste
    python nesthub.py --config config.toml --discover   # find Cast-enheder på netværket
    python nesthub.py --config config.toml --status     # hvad viser hubben nu?
    python nesthub.py --config config.toml --cast       # cast kiosk-siden én gang
    python nesthub.py --config config.toml --say "Hej"  # test oplæsning
    python nesthub.py --config config.toml --stop       # stop det der kører på hubben
    python nesthub.py --config config.toml --dry-run    # kør uden hub - log kun hvad der ville ske

Kræver: pychromecast, gTTS (se requirements.txt). Python 3.9+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - ældre Raspberry Pi OS
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger("nesthub")

APP_DASHCAST = "84912283"
APP_MEDIA_RECEIVER = "CC1AD845"
APP_BACKDROP = "E8C28D3C"  # hubbens "hjemmeskærm"/fotoramme

DEFAULT_ENTER = "{navn} er ankommet til {zone}"
DEFAULT_LEAVE = "{navn} har forladt {zone}"
DEFAULT_BATTERY = "{navn}s telefon har kun {bat} procent strøm"


# =========================================================================== config
@dataclass
class Zone:
    name: str
    lat: float
    lon: float
    radius: float = 150.0
    enter: str | None = DEFAULT_ENTER
    leave: str | None = DEFAULT_LEAVE
    devices: list[str] | None = None  # None = alle enheder

    def applies_to(self, device_id: str) -> bool:
        return not self.devices or device_id in self.devices


@dataclass
class Config:
    # [server]
    server_url: str = "http://localhost:8000"
    view_token: str = ""
    # [hub]
    hub_host: str = ""
    hub_name: str = ""
    kiosk: bool = True
    kiosk_url: str = ""
    kiosk_params: dict[str, str] = field(default_factory=dict)
    kiosk_hours: str = ""
    force: bool = True
    recast_delay: float = 60.0
    check_interval: float = 15.0
    # [tts]
    tts: bool = True
    tts_lang: str = "da"
    tts_port: int = 8765
    tts_base_url: str = ""
    volume: float | None = None
    quiet_hours: str = ""
    interrupt: bool = False
    max_age_min: float = 20.0
    max_acc: float = 100.0
    hysteresis: float = 50.0
    battery_low: int = 0
    battery_text: str = DEFAULT_BATTERY
    # andet
    names: dict[str, str] = field(default_factory=dict)
    zones: list[Zone] = field(default_factory=list)
    state_file: Path = Path("nesthub_state.json")
    cache_dir: Path = Path("tts_cache")

    def name_of(self, device_id: str) -> str:
        return self.names.get(device_id, device_id)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    base = path.resolve().parent
    srv, hub, tts = raw.get("server", {}), raw.get("hub", {}), raw.get("tts", {})

    def rel(p: str | None, default: str) -> Path:
        p = Path(p or default).expanduser()
        return p if p.is_absolute() else base / p

    cfg = Config(
        server_url=str(srv.get("url", Config.server_url)).rstrip("/"),
        view_token=str(srv.get("view_token", "")),
        hub_host=str(hub.get("host", "")),
        hub_name=str(hub.get("name", "")),
        kiosk=bool(hub.get("kiosk", True)),
        kiosk_url=str(hub.get("kiosk_url", "")),
        kiosk_params={str(k): str(v) for k, v in hub.get("kiosk_params", {}).items()},
        kiosk_hours=str(hub.get("kiosk_hours", "")),
        force=bool(hub.get("force", True)),
        recast_delay=float(hub.get("recast_delay", 60)),
        check_interval=float(hub.get("check_interval", 15)),
        tts=bool(tts.get("enabled", True)),
        tts_lang=str(tts.get("lang", "da")),
        tts_port=int(tts.get("port", 8765)),
        tts_base_url=str(tts.get("base_url", "")).rstrip("/"),
        volume=float(tts["volume"]) if "volume" in tts else None,
        quiet_hours=str(tts.get("quiet_hours", "")),
        interrupt=bool(tts.get("interrupt", False)),
        max_age_min=float(tts.get("max_age_min", 20)),
        max_acc=float(tts.get("max_acc", 100)),
        hysteresis=float(tts.get("hysteresis", 50)),
        battery_low=int(tts.get("battery_low", 0)),
        battery_text=str(tts.get("battery_text", DEFAULT_BATTERY)),
        names={str(k): str(v) for k, v in raw.get("names", {}).items()},
        state_file=rel(raw.get("state_file"), "nesthub_state.json"),
        cache_dir=rel(raw.get("cache_dir"), "tts_cache"),
    )
    for z in raw.get("zone", []):
        cfg.zones.append(Zone(
            name=str(z["name"]), lat=float(z["lat"]), lon=float(z["lon"]),
            radius=float(z.get("radius", 150)),
            enter=z.get("enter", DEFAULT_ENTER) or None,   # "" = sig ingenting
            leave=z.get("leave", DEFAULT_LEAVE) or None,
            devices=[str(d) for d in z["devices"]] if z.get("devices") else None,
        ))
    if not cfg.hub_host and not cfg.hub_name:
        raise SystemExit("config: sæt [hub] host (IP) eller name (navnet i Google Home-appen)")
    for window in (cfg.kiosk_hours, cfg.quiet_hours):
        parse_window(window)  # fejl tidligt ved forkert format
    return cfg


# =========================================================================== hjælpere
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Afstand i meter mellem to punkter."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_window(text: str) -> tuple[int, int] | None:
    """"22:00-07:00" -> (1320, 420) i minutter efter midnat. Tom streng -> None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        a, b = text.split("-")
        ha, ma = (int(x) for x in a.strip().split(":"))
        hb, mb = (int(x) for x in b.strip().split(":"))
    except ValueError:
        raise ValueError(f"ugyldigt tidsrum {text!r} - brug fx \"22:00-07:00\"") from None
    if not (0 <= ha < 24 and 0 <= hb < 24 and 0 <= ma < 60 and 0 <= mb < 60):
        raise ValueError(f"ugyldigt tidsrum {text!r}")
    return ha * 60 + ma, hb * 60 + mb


def in_window(now: datetime, text: str, default: bool) -> bool:
    """Er klokken inden for tidsrummet? Håndterer tidsrum hen over midnat. Tom -> default."""
    w = parse_window(text)
    if w is None:
        return default
    start, end = w
    m = now.hour * 60 + now.minute
    if start == end:
        return True
    return start <= m < end if start < end else (m >= start or m < end)


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, **values: Any) -> str:
    return template.format_map(_SafeDict(values))


def local_ip_towards(host: str) -> str:
    """Den lokale IP-adresse som `host` kan nå os på (ingen pakker sendes)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((host, 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def kiosk_url_for(cfg: Config, reach_host: str) -> str:
    """URL'en til /hub-siden som hubben skal åbne. `localhost` i server-URL'en erstattes
    med en adresse hubben faktisk kan nå."""
    if cfg.kiosk_url:
        url = cfg.kiosk_url
    else:
        u = urllib.parse.urlsplit(cfg.server_url)
        host = u.hostname or "localhost"
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            host = local_ip_towards(reach_host)
        netloc = host + (f":{u.port}" if u.port else "")
        url = urllib.parse.urlunsplit((u.scheme, netloc, u.path.rstrip("/") + "/hub", "", ""))
    params: dict[str, str] = {}
    if cfg.view_token:
        params["token"] = cfg.view_token
    if cfg.names:
        params["names"] = ",".join(f"{k}:{v}" for k, v in cfg.names.items())
    params.update(cfg.kiosk_params)
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe=",:")


# =========================================================================== geofence
class Watcher:
    """Holder styr på hvilke zoner hver enhed er i og finder ud af hvad der skal siges.

    Tilstanden gemmes i en JSON-fil, så en genstart ikke giver dobbelte beskeder.
    Første gang en enhed ses, registreres den bare (ingen besked)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            self.state = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError) as e:
            log.warning("kunne ikke læse %s (%s) - starter forfra", self.cfg.state_file, e)
            self.state = {}

    def save(self) -> None:
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cfg.state_file)

    def update(self, fix: dict[str, Any], now: datetime | None = None) -> list[str]:
        """Behandl én position. Returnerer de beskeder der skal læses op (evt. ingen)."""
        cfg = self.cfg
        now = now or datetime.now(timezone.utc)
        dev = str(fix.get("device_id", ""))
        try:
            lat, lon, ts = float(fix["lat"]), float(fix["lon"]), str(fix["ts"])
            when = parse_iso(ts)
        except (KeyError, TypeError, ValueError):
            return []
        st = self.state.setdefault(dev, {"zones": {}})
        if st.get("ts") and parse_iso(st["ts"]) >= when:
            return []  # set før
        st["ts"] = ts

        fresh = (now - when).total_seconds() <= cfg.max_age_min * 60
        navn = cfg.name_of(dev)
        tid = when.astimezone().strftime("%H:%M")
        out: list[str] = []

        acc = fix.get("acc")
        if acc is None or float(acc) <= cfg.max_acc:
            zones = st.setdefault("zones", {})
            for z in cfg.zones:
                if not z.applies_to(dev):
                    continue
                d = haversine_m(lat, lon, z.lat, z.lon)
                prev = zones.get(z.name)
                if d <= z.radius:
                    new = "in"
                elif d > z.radius + cfg.hysteresis:
                    new = "out"
                else:
                    new = prev  # i grænseområdet - behold hvad vi havde
                if new is None:
                    continue
                zones[z.name] = new
                if prev is None or new == prev:
                    continue  # første gang vi ser enheden, eller ingen ændring
                template = z.enter if new == "in" else z.leave
                log.info("%s: %s %s (%.0f m fra centrum)", dev, "ind i" if new == "in" else "ud af", z.name, d)
                if template and fresh:
                    out.append(render(template, navn=navn, zone=z.name, tid=tid))

        bat = fix.get("bat")
        if cfg.battery_low > 0 and bat is not None:
            bat = int(bat)
            if bat <= cfg.battery_low and not st.get("battery_low"):
                st["battery_low"] = True
                if fresh:
                    out.append(render(cfg.battery_text, navn=navn, bat=bat, tid=tid))
            elif bat >= cfg.battery_low + 10:
                st["battery_low"] = False
        return out


# =========================================================================== server
def fetch_latest(cfg: Config, timeout: float = 10) -> list[dict[str, Any]]:
    """Seneste position pr. enhed fra Positionslogger-serveren (begge varianter)."""
    url = cfg.server_url + "/api/latest"
    if cfg.view_token:
        url += "?" + urllib.parse.urlencode({"token": cfg.view_token})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    rows = data if isinstance(data, list) else [data]
    return sorted((r for r in rows if isinstance(r, dict) and "lat" in r), key=lambda r: str(r.get("ts", "")))


# =========================================================================== tts
class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("tts-http: " + fmt, *args)


class TTS:
    """Laver mp3 med Google Translate-stemmen (gTTS) og serverer dem over HTTP, så
    hubben kan hente dem. Filer caches på disk, så faste beskeder kun hentes én gang."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self.server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        if self.server:
            return
        handler = partial(_QuietHandler, directory=str(self.cfg.cache_dir))
        self.server = ThreadingHTTPServer(("0.0.0.0", self.cfg.tts_port), handler)
        threading.Thread(target=self.server.serve_forever, name="tts-http", daemon=True).start()
        log.info("TTS-filer serveres på port %d fra %s", self.cfg.tts_port, self.cfg.cache_dir)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server = None

    def make(self, text: str) -> str:
        """Returnerer filnavnet på en mp3 med `text` læst op."""
        name = hashlib.sha1(f"{self.cfg.tts_lang}|{text}".encode()).hexdigest()[:20] + ".mp3"
        path = self.cfg.cache_dir / name
        if not path.exists():
            from gtts import gTTS  # importeres først når det skal bruges

            tmp = path.with_suffix(".part")
            gTTS(text, lang=self.cfg.tts_lang).save(str(tmp))
            os.replace(tmp, path)
        self.cleanup()
        return name

    def url(self, name: str, reach_host: str) -> str:
        base = self.cfg.tts_base_url or f"http://{local_ip_towards(reach_host)}:{self.cfg.tts_port}"
        return f"{base}/{name}"

    def cleanup(self, max_files: int = 300) -> None:
        files = sorted(self.cfg.cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        for p in files[:-max_files]:
            p.unlink(missing_ok=True)


# =========================================================================== hub
class Hub:
    """Tynd indpakning om pychromecast med automatisk genforbindelse."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cast: Any = None
        self.browser: Any = None
        self.dash: Any = None

    # ---- forbindelse
    def connect(self) -> Any:
        if self.cast is not None and self.cast.socket_client.is_connected:
            return self.cast
        self.close()
        import pychromecast
        from pychromecast.controllers.dashcast import DashCastController

        if self.cfg.hub_host:
            log.info("forbinder til hub på %s", self.cfg.hub_host)
            cast = pychromecast.get_chromecast_from_host(
                (self.cfg.hub_host, 8009, None, None, None), tries=3, retry_wait=5, timeout=15)
        else:
            log.info("leder efter hub med navnet %r", self.cfg.hub_name)
            found, self.browser = pychromecast.get_listed_chromecasts(friendly_names=[self.cfg.hub_name])
            if not found:
                raise ConnectionError(f"fandt ingen Cast-enhed med navnet {self.cfg.hub_name!r} (prøv --discover)")
            cast = found[0]
        cast.wait(timeout=20)
        self.dash = DashCastController()
        cast.register_handler(self.dash)
        self.cast = cast
        log.info("forbundet: %s (%s)", cast.cast_info.friendly_name or "?", cast.cast_info.model_name or "?")
        return cast

    def close(self) -> None:
        try:
            if self.cast is not None:
                self.cast.disconnect(timeout=5)
            if self.browser is not None:
                self.browser.stop_discovery()
        except Exception:  # noqa: BLE001 - oprydning må aldrig vælte løkken
            pass
        self.cast = self.browser = self.dash = None

    @property
    def host(self) -> str:
        if self.cast is not None and self.cast.cast_info.host:
            return str(self.cast.cast_info.host)
        return self.cfg.hub_host or "8.8.8.8"

    # ---- status
    def app_id(self) -> str | None:
        st = self.connect().status
        return st.app_id if st else None

    def app_name(self) -> str:
        st = self.connect().status
        return (st.display_name if st else None) or "-"

    def is_free(self) -> bool:
        """Viser hubben ingenting (hjemmeskærm/fotoramme)?"""
        return self.app_id() in (None, APP_BACKDROP)

    def showing_kiosk(self) -> bool:
        return self.app_id() == APP_DASHCAST

    # ---- handlinger
    def cast_kiosk(self) -> None:
        url = kiosk_url_for(self.cfg, self.host)
        self.connect()
        log.info("caster kiosk: %s", url)
        self.dash.load_url(url, force=self.cfg.force)

    def stop(self) -> None:
        self.connect().quit_app()

    def say(self, url: str, max_seconds: float = 60) -> None:
        """Afspil en lydfil og vent til den er færdig. Stopper medieafspilleren bagefter."""
        cast = self.connect()
        old_volume = cast.status.volume_level if cast.status else None
        if self.cfg.volume is not None:
            cast.set_volume(self.cfg.volume)
        mc = cast.media_controller
        mc.play_media(url, "audio/mpeg", title="Positionslogger")
        try:
            mc.block_until_active(timeout=15)
            deadline = time.monotonic() + max_seconds
            started = False
            while time.monotonic() < deadline:
                state = mc.status.player_state if mc.status else None
                if state in ("PLAYING", "BUFFERING"):
                    started = True
                elif started or (state == "IDLE" and mc.status.idle_reason):
                    break
                time.sleep(0.4)
        finally:
            if self.app_id() == APP_MEDIA_RECEIVER:
                cast.quit_app()
            if self.cfg.volume is not None and old_volume is not None:
                cast.set_volume(old_volume)


class DryHub:
    """Stand-in når der køres med --dry-run: logger i stedet for at caste."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.showing = False
        self.host = cfg.hub_host or "8.8.8.8"

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def is_free(self) -> bool: return not self.showing
    def showing_kiosk(self) -> bool: return self.showing
    def app_name(self) -> str: return "DashCast" if self.showing else "-"

    def cast_kiosk(self) -> None:
        log.info("[dry-run] ville caste %s", kiosk_url_for(self.cfg, self.host))
        self.showing = True

    def stop(self) -> None:
        log.info("[dry-run] ville stoppe app på hubben")
        self.showing = False

    def say(self, url: str, max_seconds: float = 60) -> None:
        log.info("[dry-run] ville afspille %s", url)


# =========================================================================== løkken
class Runner:
    def __init__(self, cfg: Config, hub: Hub | DryHub, tts: TTS | None, dry: bool = False) -> None:
        self.cfg, self.hub, self.tts, self.dry = cfg, hub, tts, dry
        self.watcher = Watcher(cfg)
        self.free_since: float | None = None
        self.stop_event = threading.Event()

    def announce(self, text: str, now: datetime) -> None:
        if in_window(now, self.cfg.quiet_hours, default=False):
            log.info("stille tid - siger ikke: %s", text)
            return
        if not self.cfg.tts or self.tts is None:
            log.info("(oplæsning slået fra) %s", text)
            return
        was_kiosk = self.hub.showing_kiosk()
        if not (was_kiosk or self.hub.is_free() or self.cfg.interrupt):
            log.info("hubben er optaget (%s) - siger ikke: %s", self.hub.app_name(), text)
            return
        log.info("siger: %s", text)
        if self.dry:
            self.hub.say(f"<tts: {text}>")
            return
        self.hub.say(self.tts.url(self.tts.make(text), self.hub.host))
        if was_kiosk and self.cfg.kiosk:
            self.hub.cast_kiosk()

    def keep_kiosk(self, now: datetime) -> None:
        if not self.cfg.kiosk:
            return
        wanted = in_window(now, self.cfg.kiosk_hours, default=True)
        showing = self.hub.showing_kiosk()
        if not wanted:
            self.free_since = None
            if showing:
                log.info("uden for kiosk-tid - stopper visningen")
                self.hub.stop()
            return
        if showing:
            self.free_since = None
            return
        if not self.hub.is_free():
            self.free_since = None  # nogen bruger hubben - lad den være
            return
        mono = time.monotonic()
        if self.free_since is None:
            self.free_since = mono
        if mono - self.free_since >= self.cfg.recast_delay:
            self.hub.cast_kiosk()
            self.free_since = None

    def step(self) -> None:
        now = datetime.now().astimezone()
        messages: list[str] = []
        try:
            for fix in fetch_latest(self.cfg):
                messages += self.watcher.update(fix)
            self.watcher.save()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("kan ikke hente positioner fra %s: %s", self.cfg.server_url, e)
        for text in messages:
            try:
                self.announce(text, now)
            except Exception as e:  # noqa: BLE001 - fx ingen forbindelse til Googles TTS
                log.warning("kunne ikke læse op %r: %s", text, e)
        self.keep_kiosk(now)

    def run(self) -> None:
        if self.tts and self.cfg.tts and not self.dry:
            self.tts.start()
        # ved start caster vi med det samme, hvis hubben er ledig
        self.free_since = time.monotonic() - self.cfg.recast_delay
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - tjenesten skal overleve netværksbøvl
                log.warning("fejl: %s - prøver igen", e, exc_info=log.isEnabledFor(logging.DEBUG))
                self.hub.close()
            self.stop_event.wait(self.cfg.check_interval)
        self.hub.close()
        if self.tts:
            self.tts.stop()


# =========================================================================== cli
def discover() -> None:
    import pychromecast

    casts, browser = pychromecast.get_chromecasts(timeout=10)
    browser.stop_discovery()
    if not casts:
        print("Ingen Cast-enheder fundet (er du på samme netværk/VLAN som hubben?)")
    for c in casts:
        i = c.cast_info
        print(f"{i.friendly_name!r:32} {i.model_name or '?':24} {i.host}:{i.port}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Positionslogger på Google Nest Hub")
    ap.add_argument("--config", "-c", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--discover", action="store_true", help="find Cast-enheder på netværket")
    ap.add_argument("--status", action="store_true", help="vis hvad hubben kører nu")
    ap.add_argument("--cast", action="store_true", help="cast kiosk-siden én gang og afslut")
    ap.add_argument("--stop", action="store_true", help="stop det der kører på hubben")
    ap.add_argument("--say", metavar="TEKST", help="læs en tekst op og afslut")
    ap.add_argument("--dry-run", action="store_true", help="kør uden hub - log hvad der ville ske")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("pychromecast").setLevel(logging.WARNING)

    if args.discover:
        discover()
        return 0

    cfg = load_config(args.config)
    hub: Hub | DryHub = DryHub(cfg) if args.dry_run else Hub(cfg)

    if args.status:
        hub.connect()
        print(f"app: {hub.app_name()}  kiosk: {'ja' if hub.showing_kiosk() else 'nej'}  "
              f"ledig: {'ja' if hub.is_free() else 'nej'}")
        print(f"kiosk-URL: {kiosk_url_for(cfg, hub.host)}")
        hub.close()
        return 0
    if args.cast:
        hub.cast_kiosk()
        time.sleep(3)
        hub.close()
        return 0
    if args.stop:
        hub.stop()
        hub.close()
        return 0
    if args.say:
        tts = TTS(cfg)
        tts.start()
        name = tts.make(args.say)
        url = tts.url(name, hub.host)
        log.info("afspiller %s", url)
        hub.say(url)
        tts.stop()
        hub.close()
        return 0

    runner = Runner(cfg, hub, TTS(cfg), dry=args.dry_run)
    signal.signal(signal.SIGTERM, lambda *_: runner.stop_event.set())
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.stop_event.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
