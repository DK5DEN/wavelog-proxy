# Wavelog Offline-Kit — Setup/Update der lokalen Instanz (Windows PowerShell).
#
# Kümmert sich nur um die Infrastruktur: Stack starten, lokales Wavelog-Image
# auf die Version des Servers pinnen (api/version), Erstinstallation anleiten.
# Konfiguration (Server-URL, API-Keys) und Datenübernahme (Seed) macht der User
# bequem im Wavelog-UI über den Sync-Button (Fallback-URL: /_sync/).
#
# Master aktualisiert? -> dieses Script erneut ausführen, lokal zieht nach.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# .env ist rein intern (Script-verwaltet): Image-Version + DB-Passwort für docker compose
if (-not (Test-Path .env)) { New-Item -ItemType File .env | Out-Null }

function Get-DotEnv {
    $h = @{}
    Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        $h[$k.Trim()] = $v.Trim()
    }
    return $h
}
function Set-DotEnv([string]$key, [string]$value) {
    $lines = Get-Content .env
    if ($lines -match "^$key=") {
        $lines = $lines -replace "^$key=.*", "$key=$value"
    } else {
        $lines += "$key=$value"
    }
    $lines | Set-Content .env
}

$cfg = Get-DotEnv
$publicUrl = if ($cfg['LOCAL_PUBLIC_URL']) { $cfg['LOCAL_PUBLIC_URL'] } else { 'http://localhost:8086' }
$serverUrl = $cfg['SERVER_URL']; $serverKey = $cfg['SERVER_API_KEY']

# Server-Zugang: UI-Konfiguration (config.json) hat Vorrang vor .env
if (Test-Path data/sync/config.json) {
    $ui = Get-Content data/sync/config.json -Raw | ConvertFrom-Json
    if ($ui.server_url) { $serverUrl = $ui.server_url }
    if ($ui.server_api_key) { $serverKey = $ui.server_api_key }
}

if (-not $cfg['LOCAL_DB_PASSWORD']) {
    $pw = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object {[char]$_})
    Set-DotEnv 'LOCAL_DB_PASSWORD' $pw
    Write-Host ">> Lokales DB-Passwort generiert"
    $cfg = Get-DotEnv
}

New-Item -ItemType Directory -Force data/config, data/uploads, data/userdata, data/logs, data/sync | Out-Null

# Versions-Kopplung an den Master (geht erst, wenn Server-Zugang konfiguriert ist)
$version = $cfg['WAVELOG_VERSION']; if (-not $version) { $version = '3.0.1' }
if ($serverUrl -and $serverKey) {
    Write-Host ">> Frage Server-Version ab ($serverUrl)..."
    try {
        $resp = Invoke-RestMethod -Method Post -Uri "$serverUrl/api/version" `
            -ContentType 'application/json' -Body (@{key = $serverKey} | ConvertTo-Json)
        if ($resp.version) {
            $version = $resp.version
            Write-Host ">> Server läuft Wavelog $version — pinne lokales Image darauf"
            Set-DotEnv 'WAVELOG_VERSION' $version
        }
    } catch {
        Write-Host ">> WARNUNG: Server-Version nicht abrufbar — behalte Version $version"
    }
} else {
    Write-Host ">> Noch kein Server-Zugang konfiguriert — Stack startet mit Version $version."
    Write-Host "   Konfiguration danach im Wavelog-UI: Usermenü -> 'Offline-Sync' -> Einstellungen"
}

Write-Host ">> Starte/aktualisiere Stack (Wavelog $version)..."
docker compose pull wavelog wavelog-db
if ($LASTEXITCODE -ne 0) {
    Write-Host "FEHLER: Image ghcr.io/wavelog/wavelog:$version nicht verfügbar."
    Write-Host "        Verfügbare Tags: https://github.com/wavelog/wavelog/pkgs/container/wavelog"
    Write-Host "        Notfalls WAVELOG_VERSION in .env manuell auf den nächstliegenden Tag setzen."
    exit 1
}
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose up fehlgeschlagen" }

Write-Host ">> Warte auf lokale Instanz..."
foreach ($i in 1..60) {
    try { Invoke-WebRequest -UseBasicParsing -Uri "$publicUrl/" | Out-Null; break } catch { Start-Sleep 2 }
}

docker compose exec -T sync python app.py status | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host ">> Fertig. Lokales Wavelog: $publicUrl"
    Write-Host "   Sync, Seed und Einstellungen: Usermenü -> 'Offline-Sync' im Wavelog-UI"
} else {
    $cfg = Get-DotEnv
    Write-Host ""
    Write-Host ">> Erstinstallation nötig — einmalig im Browser erledigen:"
    Write-Host "   1. $publicUrl/install öffnen"
    Write-Host "      DB-Host:     wavelog-db"
    $dbName = if ($cfg['LOCAL_DB_NAME']) { $cfg['LOCAL_DB_NAME'] } else { 'wavelog' }
    $dbUser = if ($cfg['LOCAL_DB_USER']) { $cfg['LOCAL_DB_USER'] } else { 'wavelog' }
    Write-Host "      DB-Name:     $dbName"
    Write-Host "      DB-User:     $dbUser"
    Write-Host "      DB-Passwort: $($cfg['LOCAL_DB_PASSWORD'])"
    Write-Host "   2. Eigenen Benutzer anlegen (gleiches Rufzeichen wie am Server)"
    Write-Host "   3. Einloggen, Logbuch anlegen und als aktiv setzen"
    Write-Host "   4. Account -> API Keys -> Key mit read/write anlegen"
    Write-Host "   5. Usermenü -> 'Offline-Sync' anklicken -> Einstellungen: Server-URL, Server-API-Key und den"
    Write-Host "      lokalen Key eintragen -> 'Server-Bestand übernehmen (Seed)' klicken"
    Write-Host "   6. Dieses Script noch einmal ausführen (pinnt die lokale Version auf die des Servers)"
}
