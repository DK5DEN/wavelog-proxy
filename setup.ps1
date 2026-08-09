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


# Server-Zugang: UI-Konfiguration (config.json) hat Vorrang vor .env.
# Struktur: { "users": { "RUFZEICHEN": { server_url, server_api_key, ... } } }
$serverV1Key = $cfg['SERVER_V1_KEY']
if (Test-Path data/sync/config.json) {
    $ui = Get-Content data/sync/config.json -Raw | ConvertFrom-Json
    $profiles = if ($ui.users) { $ui.users.PSObject.Properties | ForEach-Object { $_.Value } } else { @($ui) }
    foreach ($p in $profiles) {
        if ($p.server_url) { $serverUrl = $p.server_url }
        if ($p.server_api_key) { $serverKey = $p.server_api_key }
        if ($p.server_v1_key) { $serverV1Key = $p.server_v1_key }
    }
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
    $newVersion = $null
    $v1key = $serverV1Key
    if ($serverKey -like 'wl2_*') {
        # APIv2: Version steht in der system-Statistik (braucht Admin-Rechte
        # und statistic:read — sonst greift unten der Legacy-Fallback)
        try {
            $resp = Invoke-RestMethod -Uri "$serverUrl/index.php/api/v2/statistic?profile=system" `
                -Headers @{ Authorization = "Bearer $serverKey" }
            if ($resp.data.system.wavelog) { $newVersion = $resp.data.system.wavelog }
        } catch {}
    } elseif (-not $v1key) {
        $v1key = $serverKey
    }
    if (-not $newVersion -and $v1key) {
        # einziger verbliebener v1-Einsatz: Versionsabgleich per Legacy-Key
        try {
            $resp = Invoke-RestMethod -Method Post -Uri "$serverUrl/api/version" `
                -ContentType 'application/json' -Body (@{key = $v1key} | ConvertTo-Json)
            if ($resp.version) { $newVersion = $resp.version }
        } catch {}
    }
    if ($newVersion) {
        $version = $newVersion
        Write-Host ">> Server läuft Wavelog $version — pinne lokales Image darauf"
        Set-DotEnv 'WAVELOG_VERSION' $version
    } else {
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
    Write-Host ""
    Write-Host ">> Erstinstallation: $publicUrl im Browser öffnen — die Seite fragt die Installationsart ab:"
    Write-Host "   - Einfach:  nur Rufzeichen (und optional Wunsch-Passwort) eingeben, Rest automatisch"
    Write-Host "   - Experte:  Wavelog-Installer manuell durchklicken (die Seite zeigt die DB-Daten an)"
    Write-Host "   Danach im Usermenü 'Offline-Sync' -> Einstellungen: Server-URL und Server-API-Key"
    Write-Host "   eintragen -> 'Server-Bestand übernehmen (Seed)'. Anschließend dieses Script erneut"
    Write-Host "   ausführen (pinnt die lokale Wavelog-Version auf die des Servers)."
}
