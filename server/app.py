"""
Positionslogger server.

- Modtager positioner fra Android-app'en (POST /api/positions, Bearer-token).
- Gemmer i SQLite.
- Viser et Leaflet-kort med aktuel position + historik (GET /).
- Sender live-opdateringer til kortet via WebSocket (/ws).

Kør:
    pip install -r requirements.txt
    # sæt tokens (Linux/macOS):   export INGEST_TOKEN=hemmelig
    # sæt tokens (Windows PS):    $env:INGEST_TOKEN = "hemmelig"
    python app.py
        eller
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "positions.db"))
STATIC_DIR = BASE_DIR / "static"

# Token som app'en skal sende i "Authorization: Bearer <token>".
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "skift-mig-ingest")
# Valgfrit token for at se kortet/hente historik. Tom = ingen beskyttelse (kun brug på privat netværk/Tailscale).
VIEW_TOKEN = os.environ.get("VIEW_TOKEN", "").strip()

MAX_BATCH = 500

# Standard-grænse (meter) for hvor unøjagtig en position må være for at blive vist/broadcastet.
# Punkter gemmes altid råt i databasen - det er kun visning (GET) og live-broadcast (WS) der filtrerer støj væk.
# Sæt MAX_ACC_M=0 for at slå filtreringen fra som standard.
MAX_ACC_M = float(os.environ.get("MAX_ACC_M", "25"))


def _acc_ok(acc: float | None, max_acc: float) -> bool:
    """Er punktet præcist nok til at blive vist? Ukendt nøjagtighed (None) regnes som ok."""
    return max_acc <= 0 or acc is None or acc < max_acc


# --------------------------------------------------------------------------- db
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: Any) -> str:
    """Normalisér en indkommende timestamp til ISO-8601 UTC. Accepterer ISO-streng eller epoch (s/ms)."""
    if value is None or value == "":
        return now_iso()
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e12 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return now_iso()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def normalize(raw: dict) -> dict | None:
    """Map et løst JSON-objekt til vores kolonner. Understøtter både korte og lange feltnavne."""
    def pick(*names: str) -> Any:
        for n in names:
            if n in raw and raw[n] is not None:
                return raw[n]
        return None

    lat = _num(pick("lat", "latitude", "_lat"))
    lon = _num(pick("lon", "lng", "longitude", "_lon"))
    if lat is None or lon is None:
        return None
    device = str(pick("device_id", "device", "tid", "topic") or "ukendt").strip() or "ukendt"
    bat = pick("bat", "batt", "battery")
    try:
        bat = int(round(float(bat))) if bat is not None and bat != "" else None
    except (TypeError, ValueError):
        bat = None
    return {
        "device_id": device,
        "lat": lat,
        "lon": lon,
        "acc": _num(pick("acc", "accuracy")),
        "alt": _num(pick("alt", "altitude")),
        "spd": _num(pick("spd", "speed", "vel")),
        "brg": _num(pick("brg", "bearing", "cog", "course")),
        "bat": bat,
        "ts": parse_ts(pick("ts", "timestamp", "time", "tst")),
        "received_at": now_iso(),
    }


def insert_many(rows: Iterable[dict]) -> list[dict]:
    inserted: list[dict] = []
    with db() as conn:
        for r in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO positions
                    (device_id, lat, lon, acc, alt, spd, brg, bat, ts, received_at)
                VALUES (:device_id,:lat,:lon,:acc,:alt,:spd,:brg,:bat,:ts,:received_at)
                """,
                r,
            )
            if cur.rowcount:
                inserted.append({**r, "id": cur.lastrowid})
    return inserted


# ------------------------------------------------------------------------- auth
def require_ingest(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    if token != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="ugyldigt ingest-token")


def require_view(request: Request) -> None:
    if not VIEW_TOKEN:
        return
    token = request.query_params.get("token", "") or request.headers.get("x-view-token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = token or auth[7:].strip()
    if token != VIEW_TOKEN:
        raise HTTPException(status_code=401, detail="ugyldigt view-token")


# ------------------------------------------------------------------- websockets
class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload, default=str)
        async with self.lock:
            dead = []
            for ws in self.clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


hub = Hub()


# -------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if INGEST_TOKEN == "skift-mig-ingest":
        print("!! ADVARSEL: INGEST_TOKEN er ikke sat - brug en rigtig hemmelighed i produktion.", file=sys.stderr)
    yield


app = FastAPI(title="Positionslogger", lifespan=lifespan)


@app.post("/api/positions")
async def post_positions(request: Request, _: None = Depends(require_ingest)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="ugyldig JSON")
    items = body if isinstance(body, list) else [body]
    if len(items) > MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"maks {MAX_BATCH} pr. kald")
    rows = [n for n in (normalize(x) for x in items if isinstance(x, dict)) if n]
    if not rows:
        raise HTTPException(status_code=422, detail="ingen gyldige positioner (mangler lat/lon)")
    inserted = insert_many(rows)
    for r in inserted:
        # Gem altid punktet, men lad kun rimeligt præcise fixes gå live ud på kortet.
        if _acc_ok(r.get("acc"), MAX_ACC_M):
            await hub.broadcast({"type": "position", **r})
    return {"received": len(items), "stored": len(inserted)}


@app.get("/api/devices")
def get_devices(request: Request, _: None = Depends(require_view)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT device_id,
                   COUNT(*)      AS points,
                   MIN(ts)       AS first_ts,
                   MAX(ts)       AS last_ts
            FROM positions GROUP BY device_id ORDER BY last_ts DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/positions")
def get_positions(
    request: Request,
    _: None = Depends(require_view),
    device: str | None = None,
    since: str | None = Query(None, description="ISO-8601 eller epoch"),
    until: str | None = None,
    limit: int = Query(5000, ge=1, le=100_000),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    max_acc: float = Query(MAX_ACC_M, description="Skjul punkter med acc >= denne værdi (meter). 0 = vis alt."),
):
    clauses, params = [], []
    if device:
        clauses.append("device_id = ?")
        params.append(device)
    if since:
        clauses.append("ts >= ?")
        params.append(parse_ts(since))
    if until:
        clauses.append("ts <= ?")
        params.append(parse_ts(until))
    if max_acc > 0:
        clauses.append("(acc IS NULL OR acc < ?)")
        params.append(max_acc)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM positions {where} ORDER BY ts {order.upper()} LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return rows


@app.get("/api/latest")
def get_latest(
    request: Request,
    _: None = Depends(require_view),
    device: str | None = None,
    max_acc: float = Query(MAX_ACC_M, description="Ignorér den seneste fix hvis acc >= denne værdi (meter). 0 = ignorér ikke."),
):
    acc_clause = "AND (acc IS NULL OR acc < ?)" if max_acc > 0 else ""
    acc_param = [max_acc] if max_acc > 0 else []
    with db() as conn:
        if device:
            row = conn.execute(
                f"SELECT * FROM positions WHERE device_id=? {acc_clause} ORDER BY ts DESC LIMIT 1",
                [device, *acc_param],
            ).fetchone()
            return dict(row) if row else JSONResponse({"detail": "ingen data"}, status_code=404)
        rows = conn.execute(
            f"""
            SELECT p.* FROM positions p
            JOIN (SELECT device_id, MAX(ts) AS mts FROM positions WHERE 1=1 {acc_clause} GROUP BY device_id) m
              ON m.device_id = p.device_id AND m.mts = p.ts
            """,
            acc_param,
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/positions")
def delete_positions(request: Request, _: None = Depends(require_ingest), device: str | None = None, before: str | None = None):
    clauses, params = [], []
    if device:
        clauses.append("device_id = ?")
        params.append(device)
    if before:
        clauses.append("ts < ?")
        params.append(parse_ts(before))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db() as conn:
        cur = conn.execute(f"DELETE FROM positions {where}", params)
    return {"deleted": cur.rowcount}


@app.get("/api/health")
def health():
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    return {"ok": True, "points": n, "time": now_iso()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if VIEW_TOKEN:
        token = ws.query_params.get("token", "")
        if token != VIEW_TOKEN:
            await ws.close(code=4401)
            return
    await hub.join(ws)
    try:
        while True:
            await ws.receive_text()  # klienten sender ikke noget; hold forbindelsen åben
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(ws)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
