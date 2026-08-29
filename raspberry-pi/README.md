# Positionslogger på Raspberry Pi (Flask-blueprint)

`positionslogger_blueprint.py` er en selvstændig Flask-**blueprint** med samme
funktion som `server/` (FastAPI-versionen), men beregnet til at leve i en
eksisterende Flask-app på en Raspberry Pi. Ingen ekstra pakker — kun Flask +
`sqlite3` (stdlib). Ingen WebSocket (Flask-dev-serveren kan det ikke) — kortet
poller i stedet hvert 4. sekund.

## Sæt den ind i din Flask-app

Læg `positionslogger_blueprint.py` ved siden af din `app.py` / `main.py` og
tilføj tre linjer:

```python
from positionslogger_blueprint import positionslogger_blueprint
# ...
app.register_blueprint(positionslogger_blueprint, url_prefix='/position')
```

Kør appen som du plejer. `positionslogger.db` (SQLite, WAL) oprettes automatisk
ved siden af blueprinten.

## Konfiguration (miljøvariabler)

| Variabel | Standard | Betydning |
|----------|----------|-----------|
| `POSITIONSLOGGER_TOKEN` | `skift-mig` (hardcodet i toppen af filen) | Kræves i `Authorization: Bearer <token>` for at sende positioner. **Sæt en rigtig værdi** — enten i filen eller som miljøvariabel. |
| `POSITIONSLOGGER_VIEW_TOKEN` | *(tom)* | Hvis sat: kræves som `?token=…` for at se kort/historik. Tom = fri adgang (fint på lokalt netværk / VPN). |
| `POSITIONSLOGGER_DB` | `<mappe>/positionslogger.db` | Sti til SQLite-filen. |

## Endpoints (alle under `/position`)

| Metode | Sti | Auth | |
|--------|-----|------|--|
| GET | `/position/` | view | Leaflet-kort (aktuel position + rute + historik, poller hvert 4. sek) |
| POST | `/position/api/positions` | **ingest** | Én position (objekt) eller mange (array). Accepterer også OwnTracks-felter (`tid`, `tst`, `lat`, `lon`, `batt`, …). |
| GET | `/position/api/devices` | view | Enheder + antal punkter |
| GET | `/position/api/positions?device=&since=&until=&limit=&order=` | view | Historik |
| GET | `/position/api/latest?device=` | view | Seneste position (pr. enhed hvis `device` udelades) |
| DELETE | `/position/api/positions?device=&before=` | ingest | Ryd historik |
| GET | `/position/api/health` | — | Status + antal punkter |

## Android-appen

Sæt **Server-URL** til Pi'ens adresse **med `/position` bagpå**, fx
`http://raspberrypi:5000/position` (appen tilføjer selv `/api/positions`).
**Token** skal matche `POSITIONSLOGGER_TOKEN`.

## Bemærkninger

- SQLite åbnes pr. kald med WAL + `busy_timeout`; skrivninger serialiseres med en
  lås. Fint til nogle få klienter. Skal der mange samtidige skrivere til, så flyt
  til Postgres.
- Leaflet + kortfliser hentes fra internettet, så Pi'en skal være online for at
  kortet virker.
- Kør bag Tailscale eller en Cloudflare Tunnel hvis telefonen skal kunne nå
  Pi'en når du er ude — se `../server/DEPLOY.md`.
