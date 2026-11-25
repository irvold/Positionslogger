# Positionslogger på Google Nest Hub

Gør en Google Nest Hub (eller en anden skærm med Google Cast) til en fast
statusskærm for Positionslogger, og lader den læse beskeder op:

- **Kiosk:** kortet med ur, seneste position, dagens rute, batteri og "sidst set"
  vises på hubben. Siden er lavet til 1024×600 og bliver mørk om aftenen.
- **Oplæsning:** når en telefon kommer ind i eller forlader en zone, siger hubben
  fx *"Daniel er kommet hjem"*. Den advarer også, hvis telefonens batteri er lavt.

```
┌──────────┐  GET /api/latest   ┌────────────────────┐  Cast (DashCast + mp3)  ┌──────────┐
│ Server   │ ◀───────────────── │  nesthub.py (Pi)   │ ──────────────────────▶ │ Nest Hub │
│ (Pi/PC)  │                    │  geofence + TTS    │                         │          │
└──────────┘ ◀─────────────────────── GET /hub (kortet, poller hvert 5. sek) ──── └──────────┘
```

Hubben kan ikke køre egne apps, så alt foregår ved at **caste** til den. Det betyder:

- Hubben smider castet væk efter et stykke tid, eller når nogen rører skærmen.
  Tjenesten caster så igen, når hubben har været ledig i `recast_delay` sekunder.
- Er hubben i gang med noget andet (musik, Gemini, timer, video), bliver den ikke
  afbrudt, hverken af kortet eller af beskeder. Vil du have beskederne igennem
  alligevel, så sæt `interrupt = true`.
- Uden for `kiosk_hours` får hubben lov at være ur eller fotoramme igen.

## 1. Kiosk-siden

Siden findes allerede i begge servervarianter:

| Server | URL |
|--------|-----|
| `server/` (FastAPI) | `http://<pi>:8000/hub` |
| `raspberry-pi/` (Flask-blueprint) | `http://<pi>:5000/position/hub` |

Åbn den i en browser for at se den. Du kan bruge disse parametre (alle valgfrie):

| Parameter | Eksempel | Betydning |
|-----------|----------|-----------|
| `token` | `token=…` | `VIEW_TOKEN`, hvis serveren kræver det |
| `device` | `device=daniels-telefon` | Vis kun disse enheder (kommasepareret) |
| `names` | `names=daniels-telefon:Daniel` | Visningsnavne |
| `theme` | `auto` / `light` / `dark` | `auto` = mørk kl. 20–07 |
| `zoom` | `15` | Maks zoom |
| `track` | `today` / `6` / `0` | Rute for i dag, de sidste N timer, eller ingen |
| `stale` | `15` | Minutter før en position vises som gammel (grå) |

Tjenesten sætter selv `token` og `names` ud fra `config.toml`.

## 2. Installér tjenesten på Pi'en

```bash
cd ~/positionslogger          # samme sted som serveren - kopiér nest-hub/ hertil
cd nest-hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
nano config.toml              # hub-IP, zoner, navne
```

**Find hubben:** IP'en står på hubben under *Indstillinger → Enhedsoplysninger →
Tekniske oplysninger*. Du kan også køre:

```bash
.venv/bin/python nesthub.py --discover
```

Giv hubben en fast IP (DHCP-reservation i routeren), eller brug `name =` med
navnet fra Google Home-appen i stedet for `host =`.

**Zoner:** find koordinater ved at højreklikke i Google Maps (det første tal er
`lat`). 150–200 m i radius er passende for et hjem. `{navn}`, `{zone}` og `{tid}`
kan bruges i teksterne.

### Test trin for trin

```bash
.venv/bin/python nesthub.py --status            # kan vi nå hubben?
.venv/bin/python nesthub.py --cast              # vises kortet?
.venv/bin/python nesthub.py --say "Hej fra Pi'en"
.venv/bin/python nesthub.py --stop
.venv/bin/python nesthub.py --dry-run           # kør løkken uden hub - se i loggen hvad den ville gøre
```

### Kør som tjeneste

```bash
sudo cp nesthub.service /etc/systemd/system/     # ret User= og stier hvis du ikke er "pi"
sudo systemctl daemon-reload
sudo systemctl enable --now nesthub
journalctl -u nesthub -f
```

Tilstanden (hvem er hvor) gemmes i `nesthub_state.json`, så en genstart ikke giver
dobbelte beskeder. Første gang en telefon ses, bliver den bare registreret uden
besked.

## Netværk og fejlfinding

- **Pi og hub skal være på samme netværk.** Hubben skal kunne nå Pi'en på serverens
  port (8000 eller 5000) og på TTS-porten (`8765`). Bruger du `ufw`:
  `sudo ufw allow from 172.29.0.0/16 to any port 8000,8765 proto tcp`.
- **Kortet vises ikke / blank skærm:** DashCast-modtageren kører over HTTPS. Med
  `force = true` (standard) navigerer hubben direkte til siden, og så virker
  `http://`. Har du HTTPS (fx `tailscale serve` eller Caddy), så sæt `kiosk_url` til
  https-adressen og `force = false`.
- **Kortet blinker eller bliver castet igen og igen:** nogle firmwareversioner
  rapporterer ikke DashCast som kørende i `force`-mode. Prøv `force = false` med en
  https-`kiosk_url`, eller hæv `recast_delay`.
- **Beskeder spilles ikke:** test med `--say`. Hvis hubben ikke vil afspille fra
  `http://`, så sæt `base_url` under `[tts]` til en https-adresse, der peger på port
  8765. Stemmen kommer fra Google Translate via `gTTS`, så Pi'en skal være online.
  Faste beskeder caches i `tts_cache/`.
- **Ingen beskeder, selvom telefonen flytter sig:** positioner ældre end
  `max_age_min` eller med `acc` over `max_acc` meter bliver ikke læst op. Det
  forhindrer, at en gammel offline-kø får hubben til at melde ting, der skete for en
  time siden. Se i `journalctl -u nesthub`.
- **For meget snak om natten:** sæt `quiet_hours`.

## Udvikling

```bash
pip install pytest
python -m pytest nest-hub        # kører uden hub og uden netværk
```

`raspberry-pi/positionslogger_blueprint.py` indeholder en kopi af
`server/static/hub.html` (blueprinten er én fil). Efter ændringer i `hub.html`:

```bash
python nest-hub/sync_hub_page.py
```

Testen `test_hub_page_in_sync_with_blueprint` fejler, hvis de to kopier er forskellige.
