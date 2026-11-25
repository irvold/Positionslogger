import datetime
import json
import os
import sqlite3
import threading

from flask import Blueprint, Response, render_template_string, request, jsonify

# Opretter Blueprint i stedet for en selvstændig Flask-app
positionslogger_blueprint = Blueprint('positionslogger_dashboard', __name__)

# Cross-platform stier (virker på Windows og Raspberry Pi)
MAPPE_STI = os.path.dirname(os.path.abspath(__file__))
DB_FIL = os.environ.get("POSITIONSLOGGER_DB", os.path.join(MAPPE_STI, "positionslogger.db"))

# Token som Android-app'en skal sende i "Authorization: Bearer <token>".
# Kan overrides med miljøvariabel POSITIONSLOGGER_TOKEN - ellers bruges værdien her.
INGEST_TOKEN = os.environ.get("POSITIONSLOGGER_TOKEN", "skift-mig")

# Valgfrit token for at se kortet/hente historik. Tom = ingen beskyttelse
# (fint på lokalt netværk / Tailscale). Gives som ?token=... i URL'en.
VIEW_TOKEN = os.environ.get("POSITIONSLOGGER_VIEW_TOKEN", "").strip()

MAX_BATCH = 500

# SQLite skrives kun fra én tråd ad gangen
_skrive_laas = threading.Lock()


# ──────────────────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────────────────
def _forbind():
	con = sqlite3.connect(DB_FIL, timeout=5)
	con.row_factory = sqlite3.Row
	con.execute("PRAGMA journal_mode=WAL")
	con.execute("PRAGMA busy_timeout=3000")
	return con


def _init_db():
	with _forbind() as con:
		con.executescript(
			"""
			CREATE TABLE IF NOT EXISTS positions (
				id          INTEGER PRIMARY KEY AUTOINCREMENT,
				device_id   TEXT NOT NULL,
				lat         REAL NOT NULL,
				lon         REAL NOT NULL,
				acc         REAL,
				alt         REAL,
				spd         REAL,
				brg         REAL,
				bat         INTEGER,
				ts          TEXT NOT NULL,
				received_at TEXT NOT NULL
			);
			CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_device_ts
				ON positions(device_id, ts);
			CREATE INDEX IF NOT EXISTS ix_positions_device_ts
				ON positions(device_id, ts);
			"""
		)
	print(f"-> Positionslogger klar (database: {DB_FIL})")


_init_db()


# ──────────────────────────────────────────────────────────
#  HJÆLPEFUNKTIONER
# ──────────────────────────────────────────────────────────
def _nu_iso():
	return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(vaerdi):
	"""Normalisér en timestamp til ISO-8601 UTC. Accepterer ISO-streng eller epoch (sek/ms)."""
	if vaerdi is None or vaerdi == "":
		return _nu_iso()
	if isinstance(vaerdi, (int, float)):
		sekunder = vaerdi / 1000.0 if vaerdi > 1e12 else float(vaerdi)
		return datetime.datetime.fromtimestamp(sekunder, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
	tekst = str(vaerdi).strip().replace("Z", "+00:00")
	try:
		dt = datetime.datetime.fromisoformat(tekst)
	except ValueError:
		return _nu_iso()
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=datetime.timezone.utc)
	return dt.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tal(v):
	try:
		return None if v is None or v == "" else float(v)
	except (TypeError, ValueError):
		return None


def _normaliser(raw):
	"""Map et løst JSON-objekt til vores kolonner. Understøtter korte og lange feltnavne + OwnTracks."""
	if not isinstance(raw, dict):
		return None

	def vaelg(*navne):
		for n in navne:
			if n in raw and raw[n] is not None:
				return raw[n]
		return None

	lat = _tal(vaelg("lat", "latitude", "_lat"))
	lon = _tal(vaelg("lon", "lng", "longitude", "_lon"))
	if lat is None or lon is None:
		return None

	enhed = str(vaelg("device_id", "device", "tid", "topic") or "ukendt").strip() or "ukendt"

	bat = vaelg("bat", "batt", "battery")
	try:
		bat = int(round(float(bat))) if bat is not None and bat != "" else None
	except (TypeError, ValueError):
		bat = None

	return {
		"device_id": enhed,
		"lat": lat,
		"lon": lon,
		"acc": _tal(vaelg("acc", "accuracy")),
		"alt": _tal(vaelg("alt", "altitude")),
		"spd": _tal(vaelg("spd", "speed", "vel")),
		"brg": _tal(vaelg("brg", "bearing", "cog", "course")),
		"bat": bat,
		"ts": _parse_ts(vaelg("ts", "timestamp", "time", "tst")),
		"received_at": _nu_iso(),
	}


def _hent_token():
	auth = request.headers.get("Authorization", "")
	if auth.lower().startswith("bearer "):
		return auth[7:].strip()
	return request.args.get("token", "")


def _tjek_ingest():
	return _hent_token() == INGEST_TOKEN


def _tjek_view():
	if not VIEW_TOKEN:
		return True
	return _hent_token() == VIEW_TOKEN or request.headers.get("X-View-Token", "") == VIEW_TOKEN


# ──────────────────────────────────────────────────────────
#  API
# ──────────────────────────────────────────────────────────
@positionslogger_blueprint.route('/api/positions', methods=['POST'])
def modtag_positioner():
	if not _tjek_ingest():
		return jsonify({"detail": "ugyldigt ingest-token"}), 401

	krop = request.get_json(silent=True)
	if krop is None:
		return jsonify({"detail": "ugyldig JSON"}), 400

	elementer = krop if isinstance(krop, list) else [krop]
	if len(elementer) > MAX_BATCH:
		return jsonify({"detail": f"maks {MAX_BATCH} pr. kald"}), 413

	raekker = [r for r in (_normaliser(x) for x in elementer) if r]
	if not raekker:
		return jsonify({"detail": "ingen gyldige positioner (mangler lat/lon)"}), 422

	gemt = 0
	with _skrive_laas, _forbind() as con:
		for r in raekker:
			cur = con.execute(
				"""
				INSERT OR IGNORE INTO positions
					(device_id, lat, lon, acc, alt, spd, brg, bat, ts, received_at)
				VALUES (:device_id,:lat,:lon,:acc,:alt,:spd,:brg,:bat,:ts,:received_at)
				""",
				r,
			)
			gemt += cur.rowcount

	return jsonify({"received": len(elementer), "stored": gemt})


@positionslogger_blueprint.route('/api/positions', methods=['GET'])
def hent_positioner():
	if not _tjek_view():
		return jsonify({"detail": "ugyldigt view-token"}), 401

	enhed = request.args.get("device")
	since = request.args.get("since")
	until = request.args.get("until")
	order = "DESC" if request.args.get("order", "asc").lower() == "desc" else "ASC"
	try:
		limit = max(1, min(int(request.args.get("limit", 5000)), 100000))
	except ValueError:
		limit = 5000

	vilkaar, params = [], []
	if enhed:
		vilkaar.append("device_id = ?")
		params.append(enhed)
	if since:
		vilkaar.append("ts >= ?")
		params.append(_parse_ts(since))
	if until:
		vilkaar.append("ts <= ?")
		params.append(_parse_ts(until))

	hvor = ("WHERE " + " AND ".join(vilkaar)) if vilkaar else ""
	params.append(limit)
	with _forbind() as con:
		raekker = con.execute(
			f"SELECT * FROM positions {hvor} ORDER BY ts {order} LIMIT ?", params
		).fetchall()
	return jsonify([dict(r) for r in raekker])


@positionslogger_blueprint.route('/api/positions', methods=['DELETE'])
def slet_positioner():
	if not _tjek_ingest():
		return jsonify({"detail": "ugyldigt ingest-token"}), 401

	enhed = request.args.get("device")
	before = request.args.get("before")
	vilkaar, params = [], []
	if enhed:
		vilkaar.append("device_id = ?")
		params.append(enhed)
	if before:
		vilkaar.append("ts < ?")
		params.append(_parse_ts(before))
	hvor = ("WHERE " + " AND ".join(vilkaar)) if vilkaar else ""
	with _skrive_laas, _forbind() as con:
		cur = con.execute(f"DELETE FROM positions {hvor}", params)
	return jsonify({"deleted": cur.rowcount})


@positionslogger_blueprint.route('/api/devices')
def hent_enheder():
	if not _tjek_view():
		return jsonify({"detail": "ugyldigt view-token"}), 401
	with _forbind() as con:
		raekker = con.execute(
			"""
			SELECT device_id,
			       COUNT(*) AS points,
			       MIN(ts)  AS first_ts,
			       MAX(ts)  AS last_ts
			FROM positions GROUP BY device_id ORDER BY last_ts DESC
			"""
		).fetchall()
	return jsonify([dict(r) for r in raekker])


@positionslogger_blueprint.route('/api/latest')
def hent_seneste():
	if not _tjek_view():
		return jsonify({"detail": "ugyldigt view-token"}), 401
	enhed = request.args.get("device")
	with _forbind() as con:
		if enhed:
			r = con.execute(
				"SELECT * FROM positions WHERE device_id=? ORDER BY ts DESC LIMIT 1", (enhed,)
			).fetchone()
			return (jsonify(dict(r)) if r else (jsonify({"detail": "ingen data"}), 404))
		raekker = con.execute(
			"""
			SELECT p.* FROM positions p
			JOIN (SELECT device_id, MAX(ts) AS mts FROM positions GROUP BY device_id) m
			  ON m.device_id = p.device_id AND m.mts = p.ts
			"""
		).fetchall()
	return jsonify([dict(r) for r in raekker])


@positionslogger_blueprint.route('/api/health')
def health():
	with _forbind() as con:
		n = con.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
	return jsonify({"ok": True, "points": n, "time": _nu_iso()})


# ──────────────────────────────────────────────────────────
#  KORTET
# ──────────────────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Positionslogger</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; font: 14px/1.4 Arial, sans-serif; background:#121212; color:#eee; }
  #app { display: flex; height: 100%; }
  #map { flex: 1; background:#1e1e1e; }
  #side { width: 300px; padding: 14px; overflow-y: auto; background:#1e1e1e; border-left: 1px solid #333; }
  h1 { font-size: 16px; margin: 0 0 12px; color:#b3e5fc; }
  label { display:block; margin: 10px 0 4px; font-weight: bold; color:#a1887f; }
  /* kun knapper i sidepanelet fylder hele bredden - rør ikke kiosk-knappen fra Main_app.py */
  #side select, #side button { width: 100%; padding: 7px; font: inherit; background:#252525; color:#eee; border:1px solid #444; border-radius:4px; }
  #side button { cursor: pointer; margin-top: 8px; }
  .row { display:flex; gap:8px; }
  .row > * { flex:1; }
  .stat { margin: 3px 0; }
  .stat b { display:inline-block; min-width: 84px; color:#a1887f; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  .on { background:#28a745; } .off { background:#888; }
  #side.collapsed { display:none; }
  #toggle { position:absolute; z-index:1000; top:10px; right:10px; width:auto; padding:6px 10px;
            font:inherit; cursor:pointer; background:#252525; color:#eee; border:1px solid #444; border-radius:4px; }
  #dayNav { display:none; align-items:center; gap:6px; margin-top:6px; }
  #dayNav button { width:auto; padding:6px 10px; margin-top:0; }
  #dayNav button:disabled { opacity:0.35; cursor:default; }
  #dayNav input[type=date] { flex:1; padding:6px; font:inherit; background:#252525; color:#eee;
            border:1px solid #444; border-radius:4px; color-scheme: dark; }
  @media (max-width: 820px) { #side { width: 230px; } }
  @media (max-width: 640px) { #app { flex-direction: column; } #side { width:100%; height:45%; } }
</style>
</head>
<body>
<div id="app">
  <div id="map"></div>
  <button id="toggle" title="Vis/skjul panel">&#9776;</button>
  <div id="side">
    <h1>Positionslogger</h1>
    <label for="device">Enhed</label>
    <select id="device"></select>
    <label for="range">Tidsrum</label>
    <select id="range">
      <option value="1">Sidste time</option>
      <option value="6">Sidste 6 timer</option>
      <option value="24" selected>Sidste d&oslash;gn</option>
      <option value="168">Sidste uge</option>
      <option value="0">Alt</option>
      <option value="day">Bestemt dag</option>
    </select>
    <div id="dayNav">
      <button id="dayPrev" title="Forrige dag">&#8592;</button>
      <input type="date" id="dayPick">
      <button id="dayNext" title="N&aelig;ste dag">&#8594;</button>
    </div>
    <div class="row">
      <label><input type="checkbox" id="follow" checked style="width:auto"> F&oslash;lg</label>
      <label><input type="checkbox" id="live" checked style="width:auto"> Live</label>
    </div>
    <label for="maxacc">Fjern st&oslash;j</label>
    <select id="maxacc">
      <option value="0">alle punkter</option>
      <option value="100">n&oslash;jagtighed &le; 100 m</option>
      <option value="50">n&oslash;jagtighed &le; 50 m</option>
      <option value="25" selected>n&oslash;jagtighed &le; 25 m</option>
    </select>
    <label style="display:inline; font-weight:normal; color:#eee">
      <input type="checkbox" id="smooth" checked style="width:auto"> Glat rute (ignor&eacute;r sm&aring; ryk)
    </label>
    <button id="fit">Zoom til rute</button>
    <label>Fart p&aring; ruten</label>
    <div style="display:flex; flex-wrap:wrap; gap:6px 12px; font-size:12px; color:#ccc">
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#888888;margin-right:4px"></span>stille</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#4caf50;margin-right:4px"></span>gang</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#29b6f6;margin-right:4px"></span>cykel</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#fb8c00;margin-right:4px"></span>bil (by)</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#e53935;margin-right:4px"></span>landevej</span>
    </div>
    <hr style="margin:14px 0; border:none; border-top:1px solid #333">
    <div id="status"><span class="dot off"></span><span>indl&aelig;ser&hellip;</span></div>
    <div class="stat"><b>Sidst set</b><span id="s-age">&ndash;</span></div>
    <div class="stat"><b>Position</b><span id="s-pos">&ndash;</span></div>
    <div class="stat"><b>N&oslash;jagtighed</b><span id="s-acc">&ndash;</span></div>
    <div class="stat"><b>Fart</b><span id="s-spd">&ndash;</span></div>
    <div class="stat"><b>H&oslash;jde</b><span id="s-alt">&ndash;</span></div>
    <div class="stat"><b>Batteri</b><span id="s-bat">&ndash;</span></div>
    <div class="stat"><b>Punkter</b><span id="s-cnt">&ndash;</span> <span style="color:#888">vist/r&aring;</span></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script src="https://unpkg.com/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js"></script>
<script>
// API-rod = denne sides sti uden afsluttende skråstreg (virker uanset url_prefix)
const API = location.pathname.replace(/\\/+$/, "");
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const q = (extra) => {
  const p = new URLSearchParams(extra || {});
  if (TOKEN) p.set("token", TOKEN);
  const s = p.toString();
  return s ? "?" + s : "";
};

const DEFAULT_ZOOM = 16;   // hvor tæt kortet zoomer på som standard
const map = L.map("map").setView([56.0, 10.5], 12);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "&copy; OpenStreetMap-bidragydere",
}).addTo(map);

// "track" driver kun pil-dekoratoren og zoom-til-rute (usynlig) - selve stregen
// tegnes som hastigheds-farvede segmenter i speedLayer herunder.
const track = L.polyline([], { opacity: 0 }).addTo(map);
const speedLayer = L.layerGroup().addTo(map);
// Retningspile langs ruten - viser hvilken vej man har kørt/gået.
const arrows = L.polylineDecorator(track, {
  patterns: [{
    offset: "5%", repeat: "80px",
    symbol: L.Symbol.arrowHead({
      pixelSize: 10, headAngle: 40, polygon: false,
      pathOptions: { stroke: true, color: "#eeeeee", weight: 2, opacity: 0.95 },
    }),
  }],
}).addTo(map);

// Fartfarver: stille / gang / cykel / by-bil / landevej.
const SPEED_BANDS = [
  { maxKmh: 2, color: "#888888", label: "stille" },
  { maxKmh: 8, color: "#4caf50", label: "gang" },
  { maxKmh: 20, color: "#29b6f6", label: "cykel" },
  { maxKmh: 60, color: "#fb8c00", label: "bil (by)" },
  { maxKmh: Infinity, color: "#e53935", label: "bil (landevej)" },
];
function speedColor(kmh) {
  for (const b of SPEED_BANDS) if (kmh < b.maxKmh) return b.color;
  return SPEED_BANDS[SPEED_BANDS.length - 1].color;
}
// Fart mellem to punkter i km/t - brug enhedens egen spd hvis den findes, ellers udregn ud fra afstand/tid.
function segSpeedKmh(a, b) {
  if (a.spd != null && b.spd != null) return ((a.spd + b.spd) / 2) * 3.6;
  if (b.spd != null) return b.spd * 3.6;
  if (a.spd != null) return a.spd * 3.6;
  const d = metersBetween(a, b);
  const dt = (new Date(b.ts) - new Date(a.ts)) / 1000;
  return dt > 0 ? (d / dt) * 3.6 : 0;
}
let curMarker = null, curCircle = null, startMarker = null;
let rawPoints = [];          // alt fra serveren
let viewLine = [];           // filtreret rute der faktisk tegnes
let currentDevice = null;
let lastTs = null;

const el = (id) => document.getElementById(id);

let dayMode = false;   // true når "Bestemt dag" er valgt i #range

const pad2 = (n) => String(n).padStart(2, "0");
const todayStr = () => {
  const d = new Date();
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
};
// Lokal kalenderdag (00:00 -> 00:00 dagen efter) omregnet til UTC-ISO til brug i since/until.
function dayRangeOf(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const start = new Date(y, m - 1, d, 0, 0, 0, 0);
  const end = new Date(y, m - 1, d + 1, 0, 0, 0, 0);
  return { since: start.toISOString(), until: end.toISOString() };
}
function currentRange() {
  if (dayMode) return dayRangeOf(el("dayPick").value || todayStr());
  const hours = Number(el("range").value);
  return hours ? { since: new Date(Date.now() - hours * 3600e3).toISOString() } : {};
}
function inRange(iso, rng) {
  const t = new Date(iso).getTime();
  if (rng.since && t < new Date(rng.since).getTime()) return false;
  if (rng.until && t >= new Date(rng.until).getTime()) return false;
  return true;
}

// afstand i meter mellem to punkter
function metersBetween(a, b) {
  const R = 6371000, toR = Math.PI / 180;
  const dLat = (b.lat - a.lat) * toR, dLon = (b.lon - a.lon) * toR;
  const s = Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * toR) * Math.cos(b.lat * toR) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

// Fjern GPS-støj: kassér dårlige fixes, teleport-spring og små ryk når man står stille.
function filterPoints(raw) {
  const maxAcc = Number(el("maxacc").value);      // 0 = ingen grænse
  const smooth = el("smooth").checked;
  const MAX_MPS = 250 / 3.6;                       // hurtigere end dette = spring
  const line = [];
  let last = null, marker = null;
  for (const p of raw) {
    if (maxAcc && p.acc != null && p.acc > maxAcc) continue;   // dårligt fix
    if (marker) {
      const d = metersBetween(marker, p);
      const dt = (new Date(p.ts) - new Date(marker.ts)) / 1000;
      if (dt > 0 && d / dt > MAX_MPS) continue;                // urealistisk spring
    }
    marker = p;                                                // seneste fornuftige måling
    if (!last) { line.push(p); last = p; continue; }
    const band = smooth ? Math.max(12, ((last.acc || 0) + (p.acc || 0)) * 0.6) : 0;
    if (metersBetween(last, p) >= band) { line.push(p); last = p; }
  }
  return { line, marker };
}
const fmtAge = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return Math.round(s) + " s siden";
  if (s < 3600) return Math.round(s / 60) + " min siden";
  if (s < 86400) return (s / 3600).toFixed(1) + " t siden";
  return (s / 86400).toFixed(1) + " dage siden";
};
const setStatus = (ok, text) =>
  el("status").innerHTML = '<span class="dot ' + (ok ? "on" : "off") + '"></span><span>' + text + "</span>";

function redraw(label) {
  const { line, marker } = filterPoints(rawPoints);
  viewLine = line;
  track.setLatLngs(line.map((p) => [p.lat, p.lon]));
  arrows.setPaths(track);
  speedLayer.clearLayers();
  for (let i = 1; i < line.length; i++) {
    const a = line[i - 1], b = line[i];
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
      color: speedColor(segSpeedKmh(a, b)), weight: 5, opacity: 0.9,
    }).addTo(speedLayer);
  }
  if (startMarker) { map.removeLayer(startMarker); startMarker = null; }
  if (line.length > 1) {
    startMarker = L.circleMarker([line[0].lat, line[0].lon], {
      radius: 6, color: "#28a745", fillColor: "#28a745", fillOpacity: 1,
    }).addTo(map).bindTooltip("Start");
  }
  const last = marker;
  el("s-cnt").textContent = line.length + " / " + rawPoints.length;
  if (!last) {
    if (curMarker) { map.removeLayer(curMarker); curMarker = null; }
    if (curCircle) { map.removeLayer(curCircle); curCircle = null; }
    el("s-age").textContent = "–"; el("s-pos").textContent = "–"; el("s-acc").textContent = "–";
    el("s-spd").textContent = "–"; el("s-alt").textContent = "–"; el("s-bat").textContent = "–";
    setStatus(false, rawPoints.length ? "alle punkter filtreret fra" : "ingen positioner i dette tidsrum");
    return;
  }
  const ll = [last.lat, last.lon];
  if (!curMarker) {
    curMarker = L.marker(ll).addTo(map);
    curCircle = L.circle(ll, { radius: last.acc || 0, color: "#1976d2", weight: 1, fillOpacity: 0.1 }).addTo(map);
  } else {
    curMarker.setLatLng(ll);
    curCircle.setLatLng(ll).setRadius(last.acc || 0);
  }
  curMarker.bindTooltip(last.device_id + "<br>" + new Date(last.ts).toLocaleString("da-DK"));
  el("s-age").textContent = fmtAge(last.ts);
  el("s-pos").textContent = last.lat.toFixed(5) + ", " + last.lon.toFixed(5);
  el("s-acc").textContent = last.acc != null ? "± " + last.acc.toFixed(0) + " m" : "–";
  el("s-spd").textContent = last.spd != null ? (last.spd * 3.6).toFixed(1) + " km/t" : "–";
  el("s-alt").textContent = last.alt != null ? last.alt.toFixed(0) + " m" : "–";
  el("s-bat").textContent = last.bat != null ? last.bat + " %" : "–";
  if (el("follow").checked) {
    // hold aktuel position centreret, men zoom mindst ind til DEFAULT_ZOOM
    map.setView(ll, Math.max(map.getZoom(), DEFAULT_ZOOM), { animate: true });
  }
  if (label) setStatus(true, label);
}

function fitTrack() {
  if (!viewLine.length) return;
  const b = track.getBounds();
  // ét punkt eller en meget lille rute (telefonen står stille) -> fast tæt zoom
  if (viewLine.length < 2 || !b.isValid() ||
      b.getNorthEast().distanceTo(b.getSouthWest()) < 300) {
    const p = viewLine[viewLine.length - 1];
    map.setView([p.lat, p.lon], DEFAULT_ZOOM);
  } else {
    map.fitBounds(b, { padding: [40, 40], maxZoom: DEFAULT_ZOOM });
  }
}

async function loadDevices() {
  try {
    const r = await fetch(API + "/api/devices" + q());
    if (!r.ok) { setStatus(false, "enheder: HTTP " + r.status); return; }
    const list = await r.json();
    const sel = el("device");
    const prev = sel.value;
    sel.innerHTML = "";
    list.forEach((d) => {
      const o = document.createElement("option");
      o.value = d.device_id;
      o.textContent = d.device_id + " (" + d.points + ")";
      sel.appendChild(o);
    });
    if (list.length) {
      sel.value = list.some((d) => d.device_id === prev) ? prev : list[0].device_id;
      currentDevice = sel.value;
    }
  } catch (e) { setStatus(false, "ingen forbindelse"); }
}

async function loadHistory() {
  if (!currentDevice) return;
  try {
    const rng = currentRange();
    const r = await fetch(API + "/api/positions" + q({ device: currentDevice, limit: 50000, ...rng }));
    if (!r.ok) { setStatus(false, "historik: HTTP " + r.status); return; }
    rawPoints = await r.json();
    lastTs = rawPoints.length ? rawPoints[rawPoints.length - 1].ts : null;
    const label = dayMode ? "dag " + el("dayPick").value : "opdateret " + new Date().toLocaleTimeString("da-DK");
    redraw(label);
    fitTrack();
  } catch (e) { setStatus(false, "ingen forbindelse"); }
}

async function poll() {
  if (!el("live").checked || !currentDevice) return;
  try {
    const r = await fetch(API + "/api/latest" + q({ device: currentDevice }));
    if (!r.ok) return;
    const p = await r.json();
    if (p && p.ts && p.ts !== lastTs) {
      const rng = currentRange();
      if (!inRange(p.ts, rng)) return;   // fx en tidligere dag valgt - ignorér nye live-fixes
      if (!rawPoints.length || new Date(p.ts) > new Date(lastTs)) {
        rawPoints.push(p);
        lastTs = p.ts;
        if (rng.since) {
          const cutoff = new Date(rng.since).getTime();
          rawPoints = rawPoints.filter((x) => new Date(x.ts).getTime() >= cutoff);
        }
        redraw("live " + new Date().toLocaleTimeString("da-DK"));
      }
    }
  } catch (e) { setStatus(false, "ingen forbindelse"); }
}

function setDayMode(on) {
  dayMode = on;
  el("dayNav").style.display = on ? "flex" : "none";
  if (on && !el("dayPick").value) el("dayPick").value = todayStr();
  updateDayNextDisabled();
}
function updateDayNextDisabled() {
  el("dayNext").disabled = (el("dayPick").value || todayStr()) >= todayStr();
}
function shiftDay(delta) {
  const [y, m, d] = (el("dayPick").value || todayStr()).split("-").map(Number);
  const dt = new Date(y, m - 1, d + delta);
  el("dayPick").value = dt.getFullYear() + "-" + pad2(dt.getMonth() + 1) + "-" + pad2(dt.getDate());
  updateDayNextDisabled();
  loadHistory();
}

el("device").addEventListener("change", (e) => { currentDevice = e.target.value; loadHistory(); });
el("range").addEventListener("change", () => { setDayMode(el("range").value === "day"); loadHistory(); });
el("dayPick").addEventListener("change", () => { updateDayNextDisabled(); loadHistory(); });
el("dayPrev").addEventListener("click", () => shiftDay(-1));
el("dayNext").addEventListener("click", () => shiftDay(1));
el("maxacc").addEventListener("change", () => { redraw(); fitTrack(); });
el("smooth").addEventListener("change", () => { redraw(); fitTrack(); });
el("fit").addEventListener("click", fitTrack);
el("live").addEventListener("change", (e) => { if (e.target.checked) loadHistory(); });
el("toggle").addEventListener("click", () => {
  el("side").classList.toggle("collapsed");
  setTimeout(() => map.invalidateSize(), 200);
});

(async function start() {
  await loadDevices();
  await loadHistory();
  setInterval(poll, 4000);               // live-markør + tal
  setInterval(async () => {              // fuld historik + enhedsliste ind imellem
    await loadDevices();
    if (!el("live").checked) await loadHistory();
  }, 30000);
})();
</script>
</body>
</html>
"""


@positionslogger_blueprint.route('/')
def index():
	return render_template_string(HTML_TEMPLATE)


# ──────────────────────────────────────────────────────────
#  HUB-VISNING (kiosk til Nest Hub / faste skærme)
# ──────────────────────────────────────────────────────────
# Kopi af server/static/hub.html - hold dem ens (nest-hub/test_nesthub.py tjekker det).
# Sendes som rå tekst (ikke Jinja), og r-strengen bevarer backslashes i JavaScript'en.
HUB_TEMPLATE = r"""<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Positionslogger &ndash; hub</title>
<!--
  Kiosk-visning til Google Nest Hub (1024x600) og andre faste skærme.
  Ingen knapper - siden skal kunne læses på afstand og passer sig selv.

  URL-parametre (alle valgfrie):
    token=<VIEW_TOKEN>               hvis serveren kræver det
    device=id1,id2                   vis kun disse enheder (standard: alle)
    names=id1:Daniel,id2:Anna        visningsnavne
    theme=auto|light|dark            auto = mørk kl. 20-07
    zoom=15                          maks zoom
    track=today|<timer>|0            hvor meget rute der tegnes (standard: i dag)
    stale=15                         minutter før en position vises som gammel

  Bruges både af server/ (FastAPI) og raspberry-pi/ (Flask-blueprint): API'et
  findes relativt til siden, så /hub -> /api/... og /position/hub -> /position/api/...
-->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<style>
  :root {
    --bg: #f4f4f2; --card: rgba(255,255,255,.92); --text: #1c1c1e; --muted: #6b6b70;
    --line: rgba(0,0,0,.08); --bad: #c62828; --tiles: none;
  }
  body.dark {
    --bg: #101113; --card: rgba(24,25,28,.9); --text: #f2f2f3; --muted: #9a9aa2;
    --line: rgba(255,255,255,.1); --bad: #ff6b6b;
    --tiles: invert(1) hue-rotate(180deg) brightness(.85) contrast(.9) saturate(.6);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; background: var(--bg); color: var(--text);
               font: 16px/1.25 system-ui, "Google Sans", Roboto, sans-serif; cursor: none; }
  #map { position: absolute; inset: 0; background: var(--bg); }
  .leaflet-tile-pane { filter: var(--tiles); }
  .leaflet-control-attribution { font-size: 9px; opacity: .6; background: transparent !important; color: var(--muted); }
  .leaflet-control-attribution a { color: inherit; }

  #clock {
    position: absolute; z-index: 1000; top: 2.2vh; left: 2vw; padding: 1vh 1.6vw;
    background: var(--card); border-radius: 18px; box-shadow: 0 2px 12px rgba(0,0,0,.18);
  }
  #clock .t { font-size: clamp(34px, 7.5vh, 64px); font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
  #clock .d { font-size: clamp(13px, 2.7vh, 22px); color: var(--muted); }

  #net {
    position: absolute; z-index: 1000; top: 2.2vh; right: 2vw; display: none;
    padding: .8vh 1.4vw; border-radius: 999px; background: var(--card); color: var(--bad);
    font-size: clamp(13px, 2.6vh, 20px); font-weight: 600;
  }

  #cards {
    position: absolute; z-index: 1000; left: 2vw; right: 2vw; bottom: 2.4vh;
    display: flex; gap: 1.4vw; justify-content: center;
  }
  .card {
    flex: 1 1 0; max-width: 46vw; min-width: 0; display: flex; align-items: center; gap: 1.4vw;
    padding: 1.6vh 1.8vw; background: var(--card); border-radius: 20px;
    box-shadow: 0 2px 14px rgba(0,0,0,.2);
  }
  .card .dot { flex: none; width: 2.6vh; height: 2.6vh; min-width: 14px; min-height: 14px; border-radius: 50%; }
  .card .main { flex: 1; min-width: 0; }
  .card .name { font-size: clamp(20px, 4.6vh, 38px); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card .age  { font-size: clamp(14px, 3.2vh, 26px); color: var(--muted); white-space: nowrap; }
  .card .side { text-align: right; font-size: clamp(14px, 3.1vh, 25px); color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .card .side b { color: var(--text); font-weight: 600; }
  .card.stale { opacity: .62; }
  .card.stale .age { color: var(--bad); }
  .card .low { color: var(--bad); }

  #empty {
    position: absolute; z-index: 1000; inset: 0; display: none; align-items: center; justify-content: center;
    font-size: clamp(18px, 4vh, 32px); color: var(--muted);
  }
  .pin { border-radius: 50%; border: 3px solid #fff; box-shadow: 0 0 0 2px rgba(0,0,0,.25), 0 2px 8px rgba(0,0,0,.35); }
</style>
</head>
<body>
<div id="map"></div>
<div id="clock"><div class="t" id="time">--:--</div><div class="d" id="date"></div></div>
<div id="net">Ingen forbindelse til serveren</div>
<div id="cards"></div>
<div id="empty">Venter p&aring; positioner&hellip;</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
"use strict";
const P = new URLSearchParams(location.search);
const TOKEN = P.get("token") || "";
const ONLY = (P.get("device") || "").split(",").map((s) => s.trim()).filter(Boolean);
const NAMES = Object.fromEntries((P.get("names") || "").split(",").map((pair) => {
  const i = pair.indexOf(":");
  return i > 0 ? [pair.slice(0, i).trim(), pair.slice(i + 1).trim()] : null;
}).filter(Boolean));
const THEME = P.get("theme") || "auto";
const MAX_ZOOM = Number(P.get("zoom")) || 15;
const TRACK = P.get("track") || "today";
const STALE_MIN = Number(P.get("stale")) || 15;
const POLL_MS = 5000;
const COLORS = ["#2563eb", "#e11d48", "#16a34a", "#d97706", "#7c3aed", "#0891b2"];

// /hub -> "" og /position/hub -> "/position"
const BASE = location.pathname.replace(/\/hub\/?$/, "");
const api = (path, extra) => {
  const p = new URLSearchParams(extra || {});
  if (TOKEN) p.set("token", TOKEN);
  const s = p.toString();
  return BASE + path + (s ? "?" + s : "");
};
const el = (id) => document.getElementById(id);
const pad2 = (n) => String(n).padStart(2, "0");
const nameOf = (id) => NAMES[id] || id;

// ------------------------------------------------------------------- kort
const map = L.map("map", {
  zoomControl: false, attributionControl: true, dragging: false, scrollWheelZoom: false,
  doubleClickZoom: false, boxZoom: false, keyboard: false, touchZoom: false, zoomSnap: 0.25,
}).setView([56.0, 10.5], 7);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "&copy; OpenStreetMap",
}).addTo(map);

const devices = new Map();   // id -> {color, marker, circle, track, last, card}
let colorIdx = 0;

function trackSince() {
  if (TRACK === "0") return null;
  if (TRACK === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return d; }
  const h = Number(TRACK);
  return h > 0 ? new Date(Date.now() - h * 3600e3) : null;
}

function ensureDevice(id) {
  let d = devices.get(id);
  if (d) return d;
  const color = COLORS[colorIdx++ % COLORS.length];
  const icon = L.divIcon({ className: "", html: `<div class="pin" style="width:22px;height:22px;background:${color}"></div>`, iconSize: [22, 22], iconAnchor: [11, 11] });
  d = {
    color,
    track: L.polyline([], { color, weight: 5, opacity: 0.75 }).addTo(map),
    circle: L.circle([0, 0], { radius: 0, color, weight: 1, fillOpacity: 0.08, opacity: 0.4 }).addTo(map),
    marker: L.marker([0, 0], { icon, keyboard: false, interactive: false }).addTo(map),
    last: null,
    card: document.createElement("div"),
  };
  d.card.className = "card";
  d.card.innerHTML = `<div class="dot" style="background:${color}"></div>
    <div class="main"><div class="name"></div><div class="age"></div></div>
    <div class="side"><div class="spd"></div><div class="bat"></div></div>`;
  d.card.querySelector(".name").textContent = nameOf(id);
  el("cards").appendChild(d.card);
  devices.set(id, d);
  return d;
}

// ------------------------------------------------------------------- visning
function fmtAge(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "lige nu";
  if (s < 3600) return Math.round(s / 60) + " min siden";
  const t = new Date(iso);
  const hm = pad2(t.getHours()) + ":" + pad2(t.getMinutes());
  if (s < 86400 && t.getDate() === new Date().getDate()) return "kl. " + hm;
  return t.toLocaleDateString("da-DK", { day: "numeric", month: "short" }) + " kl. " + hm;
}

function renderCard(d) {
  const p = d.last;
  const ageS = (Date.now() - new Date(p.ts).getTime()) / 1000;
  const stale = ageS > STALE_MIN * 60;
  d.card.classList.toggle("stale", stale);
  d.card.querySelector(".age").textContent = (stale ? "Sidst set " : "") + fmtAge(p.ts);
  const kmh = p.spd != null && !stale ? p.spd * 3.6 : null;
  d.card.querySelector(".spd").innerHTML = kmh != null && kmh >= 3 ? `<b>${kmh.toFixed(0)}</b> km/t` : "";
  const bat = d.card.querySelector(".bat");
  bat.innerHTML = p.bat != null ? `&#128267; <b>${p.bat}</b> %` : "";
  bat.classList.toggle("low", p.bat != null && p.bat <= 15);
}

function frame() {
  const pts = [...devices.values()].filter((d) => d.last).map((d) => [d.last.lat, d.last.lon]);
  el("empty").style.display = pts.length ? "none" : "flex";
  if (!pts.length) return;
  // plads til ur foroven og kort forneden
  const bottom = el("cards").offsetHeight + 40, top = el("clock").offsetHeight + 30;
  map.fitBounds(L.latLngBounds(pts), {
    paddingTopLeft: [60, top], paddingBottomRight: [60, bottom], maxZoom: MAX_ZOOM, animate: true,
  });
}

function applyFix(p, { refit = true } = {}) {
  const d = ensureDevice(p.device_id);
  if (d.last && p.ts <= d.last.ts) return false;
  d.last = p;
  const ll = [p.lat, p.lon];
  d.marker.setLatLng(ll);
  d.circle.setLatLng(ll).setRadius(p.acc || 0);
  const since = trackSince();
  if (since && new Date(p.ts) >= since) d.track.addLatLng(ll);
  renderCard(d);
  if (refit) frame();
  return true;
}

// ------------------------------------------------------------------- data
async function getJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function loadTracks() {
  const since = trackSince();
  for (const [id, d] of devices) {
    if (!since) { d.track.setLatLngs([]); continue; }
    try {
      const rows = await getJson(api("/api/positions", { device: id, since: since.toISOString(), limit: 20000 }));
      d.track.setLatLngs(rows.map((r) => [r.lat, r.lon]));
    } catch (e) { /* næste poll prøver igen */ }
  }
}

async function poll() {
  try {
    let rows = await getJson(api("/api/latest"));
    if (!Array.isArray(rows)) rows = [rows];
    if (ONLY.length) rows = rows.filter((r) => ONLY.includes(r.device_id));
    const isNew = rows.some((r) => !devices.has(r.device_id));
    let changed = false;
    for (const r of rows.sort((a, b) => a.ts.localeCompare(b.ts))) changed = applyFix(r, { refit: false }) || changed;
    if (isNew) await loadTracks();
    if (changed) frame();
    for (const d of devices.values()) if (d.last) renderCard(d);
    el("net").style.display = "none";
  } catch (e) {
    el("net").style.display = "block";
  }
}

// ------------------------------------------------------------------- ur + tema
function tick() {
  const n = new Date();
  el("time").textContent = pad2(n.getHours()) + ":" + pad2(n.getMinutes());
  const dateTxt = n.toLocaleDateString("da-DK", { weekday: "long", day: "numeric", month: "long" });
  el("date").textContent = dateTxt.charAt(0).toUpperCase() + dateTxt.slice(1);
  const h = n.getHours();
  const dark = THEME === "dark" || (THEME === "auto" && (h >= 20 || h < 7));
  document.body.classList.toggle("dark", dark);
}

let lastDay = new Date().getDate();
setInterval(() => {
  tick();
  if (new Date().getDate() !== lastDay) { lastDay = new Date().getDate(); loadTracks(); }
}, 1000);
tick();

(async function start() {
  await poll();
  setInterval(poll, POLL_MS);
  setInterval(loadTracks, 10 * 60e3);
  // en fuld genindlæsning engang imellem holder hukommelsen nede på hubben
  setTimeout(() => location.reload(), 6 * 3600e3);
})();
</script>
</body>
</html>
"""


@positionslogger_blueprint.route('/hub')
def hub():
	return Response(HUB_TEMPLATE, mimetype="text/html")
