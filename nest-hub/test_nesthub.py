"""Tests for nesthub.py - kører uden hub og uden netværk:  python -m pytest nest-hub"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import nesthub
from nesthub import Config, Watcher, Zone, haversine_m, in_window, kiosk_url_for, load_config, parse_window

HERE = Path(__file__).parent
HOME = (55.6761, 12.5683)
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def offset(lat: float, lon: float, north_m: float) -> tuple[float, float]:
    return lat + north_m / 111_195.0, lon


def fix(dev: str, pos: tuple[float, float], minutes_ago: float = 0, **extra) -> dict:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"device_id": dev, "lat": pos[0], "lon": pos[1], "ts": ts, "acc": 10, **extra}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        hub_host="10.0.0.5",
        names={"tlf": "Daniel"},
        zones=[Zone("hjemme", *HOME, radius=150, enter="{navn} er kommet hjem", leave="{navn} er taget hjemmefra")],
        battery_low=15,
        state_file=tmp_path / "state.json",
        cache_dir=tmp_path / "cache",
    )


class Clock:
    """Giver stigende tidsstempler, så hver fix er nyere end den forrige."""

    def __init__(self) -> None:
        self.i = 0

    def __call__(self, dev: str, pos, **extra) -> dict:
        self.i += 1
        return fix(dev, pos, minutes_ago=10 - self.i * 0.1, **extra)


# ------------------------------------------------------------------ hjælpere
def test_haversine():
    assert haversine_m(*HOME, *HOME) == 0
    assert haversine_m(*HOME, *offset(*HOME, 1000)) == pytest.approx(1000, rel=1e-3)


@pytest.mark.parametrize("text,hh,mm,expected", [
    ("22:00-07:00", 23, 0, True),
    ("22:00-07:00", 6, 59, True),
    ("22:00-07:00", 7, 0, False),
    ("22:00-07:00", 12, 0, False),
    ("07:00-22:00", 12, 0, True),
    ("07:00-22:00", 22, 0, False),
])
def test_in_window(text, hh, mm, expected):
    assert in_window(datetime(2026, 1, 1, hh, mm), text, default=False) is expected


def test_in_window_empty_uses_default():
    assert in_window(datetime(2026, 1, 1, 3, 0), "", default=True) is True
    assert in_window(datetime(2026, 1, 1, 3, 0), "", default=False) is False


def test_parse_window_rejects_garbage():
    with pytest.raises(ValueError):
        parse_window("kl 10")
    with pytest.raises(ValueError):
        parse_window("25:00-07:00")


def test_kiosk_url_replaces_localhost(cfg, monkeypatch):
    monkeypatch.setattr(nesthub, "local_ip_towards", lambda host: "192.168.1.20")
    cfg.server_url = "http://localhost:5000/position"
    cfg.view_token = "hemmelig"
    cfg.kiosk_params = {"theme": "dark"}
    url = kiosk_url_for(cfg, "10.0.0.5")
    assert url.startswith("http://192.168.1.20:5000/position/hub?")
    assert "token=hemmelig" in url and "names=tlf:Daniel" in url and "theme=dark" in url


def test_kiosk_url_override(cfg):
    cfg.kiosk_url = "https://pi.example.ts.net/hub"
    cfg.names = {}
    assert kiosk_url_for(cfg, "10.0.0.5") == "https://pi.example.ts.net/hub"


# ------------------------------------------------------------------ geofence
def test_first_sighting_is_silent_then_leave_and_enter(cfg):
    w, t = Watcher(cfg), Clock()
    assert w.update(t("tlf", HOME), NOW) == []                                # første gang: ingen besked
    assert w.update(t("tlf", offset(*HOME, 100)), NOW) == []                  # stadig inde
    assert w.update(t("tlf", offset(*HOME, 500)), NOW) == ["Daniel er taget hjemmefra"]
    assert w.update(t("tlf", offset(*HOME, 600)), NOW) == []
    assert w.update(t("tlf", offset(*HOME, 20)), NOW) == ["Daniel er kommet hjem"]


def test_hysteresis_prevents_flapping(cfg):
    w, t = Watcher(cfg), Clock()
    w.update(t("tlf", HOME), NOW)
    # 170 m er uden for radius (150) men inden for radius + hysterese (200) -> stadig "inde"
    for _ in range(3):
        assert w.update(t("tlf", offset(*HOME, 170)), NOW) == []
        assert w.update(t("tlf", offset(*HOME, 140)), NOW) == []
    assert w.update(t("tlf", offset(*HOME, 210)), NOW) == ["Daniel er taget hjemmefra"]


def test_same_or_older_fix_ignored(cfg):
    w = Watcher(cfg)
    w.update(fix("tlf", HOME, minutes_ago=5), NOW)
    assert w.update(fix("tlf", offset(*HOME, 900), minutes_ago=5), NOW) == []
    assert w.update(fix("tlf", offset(*HOME, 900), minutes_ago=6), NOW) == []


def test_stale_fix_updates_state_but_is_silent(cfg):
    w = Watcher(cfg)
    w.update(fix("tlf", HOME, minutes_ago=120), NOW)
    assert w.update(fix("tlf", offset(*HOME, 900), minutes_ago=60), NOW) == []  # for gammel til at sige noget
    assert w.update(fix("tlf", HOME, minutes_ago=1), NOW) == ["Daniel er kommet hjem"]  # men tilstanden er fulgt med


def test_inaccurate_fix_ignored_for_zones(cfg):
    w, t = Watcher(cfg), Clock()
    w.update(t("tlf", HOME), NOW)
    assert w.update(t("tlf", offset(*HOME, 900), acc=800), NOW) == []
    assert w.update(t("tlf", HOME), NOW) == []  # vi har aldrig regnet ham for ude


def test_zone_device_filter_and_empty_template(cfg):
    cfg.zones.append(Zone("arbejde", *offset(*HOME, 5000), radius=200, enter="", leave=None, devices=["tlf"]))
    cfg.zones.append(Zone("kun-anna", *HOME, radius=150, devices=["anna"]))
    w, t = Watcher(cfg), Clock()
    w.update(t("tlf", HOME), NOW)
    msgs = w.update(t("tlf", offset(*HOME, 5000)), NOW)
    assert msgs == ["Daniel er taget hjemmefra"]  # arbejde har tom tekst, kun-anna gælder ikke
    assert w.state["tlf"]["zones"]["arbejde"] == "in"
    assert "kun-anna" not in w.state["tlf"]["zones"]


def test_unknown_name_falls_back_to_device_id(cfg):
    w, t = Watcher(cfg), Clock()
    w.update(t("ukendt-tlf", HOME), NOW)
    assert w.update(t("ukendt-tlf", offset(*HOME, 900)), NOW) == ["ukendt-tlf er taget hjemmefra"]


def test_battery_warning_once_until_recharged(cfg):
    w, t = Watcher(cfg), Clock()
    assert w.update(t("tlf", HOME, bat=50), NOW) == []
    assert w.update(t("tlf", HOME, bat=15), NOW) == ["Daniels telefon har kun 15 procent strøm"]
    assert w.update(t("tlf", HOME, bat=10), NOW) == []
    assert w.update(t("tlf", HOME, bat=20), NOW) == []          # under low+10: stadig "lav"
    assert w.update(t("tlf", HOME, bat=80), NOW) == []          # opladet - nulstil
    assert w.update(t("tlf", HOME, bat=14), NOW) == ["Daniels telefon har kun 14 procent strøm"]


def test_state_survives_restart(cfg):
    w, t = Watcher(cfg), Clock()
    w.update(t("tlf", HOME), NOW)
    w.save()
    w2 = Watcher(cfg)
    assert w2.update(t("tlf", offset(*HOME, 900)), NOW) == ["Daniel er taget hjemmefra"]
    assert json.loads(cfg.state_file.read_text())["tlf"]["zones"]["hjemme"] == "in"  # w2 har ikke gemt endnu


def test_corrupt_state_file_is_ignored(cfg):
    cfg.state_file.write_text("{ikke json")
    assert Watcher(cfg).state == {}


# ------------------------------------------------------------------ runner med falsk hub
class FakeHub(nesthub.DryHub):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.app = None
        self.calls: list[str] = []

    def is_free(self):
        return self.app is None

    def showing_kiosk(self):
        return self.app == "dash"

    def app_name(self):
        return self.app or "-"

    def cast_kiosk(self):
        self.calls.append("cast")
        self.app = "dash"

    def stop(self):
        self.calls.append("stop")
        self.app = None

    def say(self, url, max_seconds=60):
        self.calls.append("say")


def test_runner_recasts_only_when_free_and_in_hours(cfg, monkeypatch):
    cfg.recast_delay = 0
    cfg.kiosk_hours = "07:00-22:00"
    hub = FakeHub(cfg)
    r = nesthub.Runner(cfg, hub, None, dry=True)
    day, night = datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 23)

    r.keep_kiosk(day)
    assert hub.calls == ["cast"]
    hub.app = "spotify"                    # nogen bruger hubben
    r.keep_kiosk(day)
    assert hub.calls == ["cast"]
    hub.app = None                         # ledig igen
    r.keep_kiosk(day)
    assert hub.calls == ["cast", "cast"]
    r.keep_kiosk(night)                    # uden for kiosk-tid
    assert hub.calls == ["cast", "cast", "stop"]
    r.keep_kiosk(night)
    assert hub.calls == ["cast", "cast", "stop"]


def test_runner_announce_respects_quiet_hours_and_busy(cfg):
    cfg.quiet_hours = "22:00-07:00"
    hub = FakeHub(cfg)
    r = nesthub.Runner(cfg, hub, None, dry=True)
    r.tts = object()  # type: ignore[assignment]  # bare ikke None
    r.announce("hej", datetime(2026, 1, 1, 23))
    assert hub.calls == []
    hub.app = "spotify"
    r.announce("hej", datetime(2026, 1, 1, 12))
    assert hub.calls == []
    hub.app = None
    r.announce("hej", datetime(2026, 1, 1, 12))
    assert hub.calls == ["say"]


# ------------------------------------------------------------------ config + filer
def test_example_config_loads(tmp_path):
    cfg = load_config(HERE / "config.example.toml")
    assert cfg.hub_host and cfg.zones and cfg.names
    assert cfg.zones[0].enter and "{navn}" in cfg.zones[0].enter
    assert cfg.state_file.parent == HERE


def test_config_requires_hub(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[server]\nurl = "http://x"\n')
    with pytest.raises(SystemExit):
        load_config(p)


def test_hub_page_in_sync_with_blueprint():
    """raspberry-pi-blueprinten har en kopi af hub.html - de skal være ens."""
    html = (HERE.parent / "server" / "static" / "hub.html").read_text(encoding="utf-8")
    tree = ast.parse((HERE.parent / "raspberry-pi" / "positionslogger_blueprint.py").read_text(encoding="utf-8"))
    copies = [n.value.value for n in tree.body
              if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "HUB_TEMPLATE"]
    assert copies == [html], "kør nest-hub/sync_hub_page.py efter at have rettet server/static/hub.html"
