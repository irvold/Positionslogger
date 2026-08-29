# Android-app

Åbn **denne mappe** (`android/`) i Android Studio og tryk **Run ▶**.

## Toolchain (verificeret byg 2026-08-27)

| Komponent | Version |
|-----------|---------|
| Android Gradle Plugin | 8.13.0 |
| Kotlin | 2.2.20 |
| Gradle | 9.5.0 (wrapper med i repoet) |
| Compose BOM | 2025.09.01 |
| compileSdk / targetSdk | 36 |
| minSdk | 26 (Android 8.0) |
| JDK | Android Studios medfølgende JBR (25) |

Projektet er bygget på denne PC med Android Studio-installationen i
`C:\Program Files\Android\Android Studio` og SDK'et i
`%LOCALAPPDATA%\Android\Sdk`. `local.properties` (peger på SDK'et) genereres
lokalt og er ikke i repoet.

## Byg fra kommandolinjen

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
cd C:\Arbejdsmappen\Positionslogger\android
.\gradlew.bat assembleDebug
```

Resultat: `app/build/outputs/apk/debug/app-debug.apk` (~19 MB).

Installér på en tilsluttet telefon (USB-fejlfinding slået til):

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" install -r app\build\outputs\apk\debug\app-debug.apk
```

## Hvad appen gør

- `LocationService` — foreground-service, henter GPS via FusedLocationProvider,
  lægger hver position i en lokal SQLite-kø (`QueueDb`) og forsøger straks upload.
- `Uploader` — sender køen som JSON-array til `POST /api/positions` med Bearer-token.
  Sletter kun rækker serveren har kvitteret for.
- `UploadWorker` — WorkManager-job hvert 15. min (ved netværk) som tømmer køen,
  også hvis service'en er blevet dræbt.
- `BootReceiver` — starter logningen igen efter genstart (hvis slået til).
- `MainActivity` — Compose-UI til opsætning + start/stop + kø-status.
- `Net` — tjekker om VPN er aktiv (`TRANSPORT_VPN`), pinger `…/api/health` for at se om
  serveren kan nås, og kan åbne Tailscale-appen (`com.tailscale.ipn`). Status vises i
  kortet øverst; ved **Start** laves et forbindelsestjek — kan serveren ikke nås, vises
  en dialog med **"Åbn Tailscale" / "Prøv igen" / "Start alligevel"**. Android tillader
  ikke at tænde en anden apps VPN automatisk, så "Åbn Tailscale" sender brugeren ind i
  Tailscale-appen (ét tryk på Connect).

## Indstillinger (gemmes i DataStore)

| Felt | Betydning |
|------|-----------|
| Server-URL | Basis-URL, fx `https://pos.ditdomæne.dk`. `/api/positions` tilføjes selv. |
| Enheds-ID | Frit navn; vises som enhed på kortet. |
| Ingest-token | Skal matche `INGEST_TOKEN` på serveren. |
| Interval | Sekunder mellem målinger (5–300). |
| Mindste afstand | Spring målinger over hvis du ikke har bevæget dig X meter. |
| Maks. unøjagtighed | Accuracy-gate: fixes hvor GPS'ens egen `accuracy` er værre end dette (m) bliver aldrig lagt i køen/sendt. 0 = fra. Standard 100 m. Notifikationen viser `sprang fix over (±Xm) · droppet N`. |
| Høj nøjagtighed | GPS (præcist, mere strøm) vs. balanceret. |
| Start efter genstart | Genoptag logning efter reboot. |

## Tilladelser der skal gives på telefonen

- Placering: **Tillad hele tiden** (ellers stopper baggrundslogning).
- Notifikationer (Android 13+): til den vedvarende service-notifikation.
- Fjern batterioptimering for appen (Indstillinger → Apps → Positionslogger → Batteri).
