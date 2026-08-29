# Kør serveren på Raspberry Pi og gør den tilgængelig over internettet

## 1. Installér på Pi'en

```bash
sudo apt update && sudo apt install -y python3-venv
mkdir -p ~/positionslogger && cd ~/positionslogger
# kopiér server/-mappen hertil (scp, git, USB ...)

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Test:

```bash
INGEST_TOKEN=min-hemmelighed .venv/bin/python app.py
# åbn http://<pi-ip>:8000 fra en anden maskine på samme net
```

## 2. Kør som tjeneste (systemd)

Læg dine hemmeligheder i `/etc/positionslogger.env` (kun læsbar for root):

```ini
INGEST_TOKEN=en-lang-tilfældig-streng
VIEW_TOKEN=en-anden-streng-hvis-du-vil-beskytte-kortet
DB_PATH=/home/pi/positionslogger/positions.db
PORT=8000
```

```bash
sudo chmod 600 /etc/positionslogger.env
sudo cp positionslogger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now positionslogger
systemctl status positionslogger
journalctl -u positionslogger -f
```

`positionslogger.service` ligger i denne mappe — ret `User=` og stierne hvis din
bruger ikke er `pi`.

## 3. Adgang udefra — vælg én

### A) Tailscale (nemmest, privat, ingen åbne porte) — anbefalet

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Installér også Tailscale-app'en på telefonen og log ind med samme konto.
I Positionslogger-app'en sætter du så **Server-URL** til Pi'ens tailscale-navn,
fx `http://raspberrypi.tail1234.ts.net:8000` (eller aktivér Tailscale HTTPS og brug `https://`).
Kun dine egne enheder kan nå serveren — du behøver ikke `VIEW_TOKEN`.

### B) Cloudflare Tunnel (offentlig HTTPS-URL, ingen port-forwarding)

```bash
# installér cloudflared (se Cloudflares docs for din arkitektur)
cloudflared tunnel login
cloudflared tunnel create positionslogger
cloudflared tunnel route dns positionslogger pos.ditdomæne.dk
# kør (eller opsæt som tjeneste):
cloudflared tunnel run --url http://localhost:8000 positionslogger
```

App'ens **Server-URL** bliver `https://pos.ditdomæne.dk`. Sæt et stærkt
`VIEW_TOKEN`, da URL'en er offentlig, og åbn kortet som
`https://pos.ditdomæne.dk/?token=<VIEW_TOKEN>`.

### C) Port-forwarding + DDNS (hvis du insisterer)

Videresend port 443 på routeren til Pi'en, kør en reverse proxy (Caddy giver
gratis Let's Encrypt-cert automatisk) foran `localhost:8000`, og brug en
DDNS-tjeneste (DuckDNS) til et fast domæne. Sæt både `INGEST_TOKEN` og `VIEW_TOKEN`.

## 4. Vedligehold

- **Backup:** kopiér `positions.db` (stop tjenesten kortvarigt, eller brug
  `sqlite3 positions.db ".backup backup.db"`).
- **Ryd gammelt data:**
  `curl -X DELETE "http://localhost:8000/api/positions?before=2026-01-01" -H "Authorization: Bearer $INGEST_TOKEN"`
- **Opdater:** kopiér ny kode ind, `sudo systemctl restart positionslogger`.
