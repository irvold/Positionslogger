# Positionslogger

Android-app der logger telefonens GPS-position og sender den til en lille server
på din PC eller Raspberry Pi. Serveren gemmer historikken i SQLite og viser
aktuel position + rute på et Leaflet-kort i browseren — med live-opdatering.

```
┌─────────────┐   HTTPS POST /api/positions    ┌──────────────────┐   WebSocket   ┌──────────┐
│  Android    │ ─────────────────────────────▶ │  FastAPI-server  │ ────────────▶ │ Browser  │
│  (Kotlin)   │   JSON, Bearer-token            │  + SQLite        │   /ws         │  kort    │
│  offline-kø │ ◀───────────────────────────    │  (Pi / PC)       │               │ (Leaflet)│
└─────────────┘        200 OK                   └──────────────────┘               └──────────┘
```

- **Offline-robust:** app'en gemmer positioner lokalt i en kø og sender dem, når
  der er forbindelse igen (både fra en foreground-service og et WorkManager-job).
- **Over internettet:** serveren lytter på `0.0.0.0`. Gør den tilgængelig udefra
  via Tailscale (nemmest/sikrest) eller en Cloudflare Tunnel (offentlig HTTPS-URL).
  Se [`server/DEPLOY.md`](server/DEPLOY.md).
- **Sikkerhed:** al indsendelse kræver et hemmeligt `INGEST_TOKEN`. Kortet kan
  valgfrit beskyttes med et `VIEW_TOKEN`.

## Mapper

| Sti | Indhold |
|-----|---------|
| `server/` | FastAPI-server (standalone), SQLite, Leaflet-kort med WebSocket-live. Kører på PC eller Pi. |
| `raspberry-pi/` | Samme server som en Flask-**blueprint** til at sætte ind i en eksisterende Flask-app på en Raspberry Pi (poller i stedet for WebSocket). Se [`raspberry-pi/README.md`](raspberry-pi/README.md). |
| `android/` | Android Studio-projekt (Kotlin + Jetpack Compose): baggrunds-GPS, offline-kø, accuracy-gate, forbindelses-/VPN-tjek. |

## Kom hurtigt i gang

### 1. Server (på PC til test, senere på Pi)

```bash
cd server
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate
pip install -r requirements.txt

# sæt en hemmelighed (skift værdien!)
# PowerShell:
$env:INGEST_TOKEN = "min-lange-hemmelighed"
# bash:
# export INGEST_TOKEN=min-lange-hemmelighed

python app.py
```

Åbn <http://localhost:8000> — kortet er tomt indtil app'en sender data.
Test uden telefon:

```bash
curl -X POST http://localhost:8000/api/positions \
  -H "Authorization: Bearer min-lange-hemmelighed" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"test","lat":55.6761,"lon":12.5683,"acc":8,"bat":90}'
```

### 2. App

1. Åbn mappen `android/` i **Android Studio** (Ladybug eller nyere). Lad den
   synkronisere Gradle — den henter selv wrapper og afhængigheder.
2. Sæt telefonen i USB-fejlfindingstilstand og tryk **Run**.
3. I appen:
   - **Server-URL:** `https://<din-tunnel-eller-tailscale-adresse>` (til test på
     samme WiFi: `http://<pc-ip>:8000`).
   - **Enheds-ID:** fx `daniels-telefon`.
   - **Ingest-token:** samme streng som `INGEST_TOKEN` på serveren.
   - Vælg interval, tryk **Start logning**, og giv **"Tillad hele tiden"** til
     placering + tillad notifikationer.
4. Positionen dukker op på kortet inden for et interval eller to.

> **Ren HTTP (uden TLS)** virker kun til test på lokalt netværk. `android/`-projektet
> tillader cleartext for at gøre den første test nem — se noten i
> `android/app/src/main/AndroidManifest.xml`-afsnittet i `DEPLOY.md` for at slå det
> fra i produktion.

## API (server)

| Metode | Sti | Auth | Beskrivelse |
|--------|-----|------|-------------|
| POST | `/api/positions` | `INGEST_TOKEN` | Modtag én position (objekt) eller mange (array). |
| GET | `/api/devices` | `VIEW_TOKEN`\* | Liste over enheder + antal punkter. |
| GET | `/api/positions?device=&since=&until=&limit=&order=` | `VIEW_TOKEN`\* | Historik. `since/until` = ISO-8601 eller epoch. |
| GET | `/api/latest?device=` | `VIEW_TOKEN`\* | Seneste position (pr. enhed hvis `device` udelades). |
| DELETE | `/api/positions?device=&before=` | `INGEST_TOKEN` | Ryd historik. |
| GET | `/api/health` | — | Status + antal punkter. |
| WS | `/ws` | `VIEW_TOKEN`\* | Live-strøm af nye positioner. |

\* Kun håndhævet hvis `VIEW_TOKEN` er sat. Token gives som `?token=…` i URL/WebSocket.

### Positions-JSON (fra app til server)

```json
{
  "device_id": "daniels-telefon",
  "lat": 55.6761,
  "lon": 12.5683,
  "acc": 4.2,          // nøjagtighed i meter
  "alt": 31.0,         // højde i meter
  "spd": 1.4,          // fart i m/s
  "brg": 187.0,        // retning i grader
  "bat": 87,           // batteri i procent
  "ts": "2026-08-27T10:00:00.123Z"
}
```

Serveren accepterer også OwnTracks-agtige felter (`tid`, `tst`, `lat/lon`, `batt`),
så du kan teste med andre klienter.

## Miljøvariabler (server)

| Variabel | Standard | Betydning |
|----------|----------|-----------|
| `INGEST_TOKEN` | `skift-mig-ingest` | Kræves for at sende positioner. **Sæt altid en rigtig værdi.** |
| `VIEW_TOKEN` | *(tom)* | Hvis sat: kræves for at se kort/historik. |
| `DB_PATH` | `server/positions.db` | Placering af SQLite-filen. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind-adresse. |
| `RELOAD` | *(tom)* | Sæt til `1` under udvikling for auto-reload. |
