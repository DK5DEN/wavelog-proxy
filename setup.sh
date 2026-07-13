#!/usr/bin/env bash
# Wavelog Offline-Kit — Setup/Update der lokalen Instanz (Linux/Mac).
#
# Kümmert sich nur um die Infrastruktur: Stack starten, lokales Wavelog-Image
# auf die Version des Servers pinnen (api/version), Erstinstallation anleiten.
# Konfiguration (Server-URL, API-Keys) und Datenübernahme (Seed) macht der User
# bequem im Wavelog-UI über den Sync-Button (Fallback-URL: /_sync/).
#
# Master aktualisiert? -> dieses Script erneut ausführen, lokal zieht nach.
set -euo pipefail
cd "$(dirname "$0")"

# .env ist rein intern (Script-verwaltet): Image-Version + DB-Passwort für docker compose
touch .env
set -a; . ./.env; set +a
PUBLIC="${LOCAL_PUBLIC_URL:-http://localhost:8086}"

set_env() { # set_env KEY VALUE — .env-Eintrag setzen/ersetzen
  if grep -q "^$1=" .env; then
    sed -i.bak "s#^$1=.*#$1=$2#" .env && rm -f .env.bak
  else
    echo "$1=$2" >> .env
  fi
}

# Server-Zugang: UI-Konfiguration (config.json) hat Vorrang vor .env
CFG=data/sync/config.json
if [ -f "$CFG" ]; then
  V=$(sed -n 's/.*"server_url": *"\([^"]*\)".*/\1/p' "$CFG" | head -1)
  [ -n "$V" ] && SERVER_URL="$V"
  V=$(sed -n 's/.*"server_api_key": *"\([^"]*\)".*/\1/p' "$CFG" | head -1)
  [ -n "$V" ] && SERVER_API_KEY="$V"
fi

if [ -z "${LOCAL_DB_PASSWORD:-}" ]; then
  set_env LOCAL_DB_PASSWORD "$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)"
  echo ">> Lokales DB-Passwort generiert"
  set -a; . ./.env; set +a
fi

mkdir -p data/config data/uploads data/userdata data/logs data/sync
chmod 777 data/config data/uploads data/userdata data/logs 2>/dev/null || true

# Versions-Kopplung an den Master (geht erst, wenn Server-Zugang konfiguriert ist)
if [ -n "${SERVER_URL:-}" ] && [ -n "${SERVER_API_KEY:-}" ]; then
  echo ">> Frage Server-Version ab ($SERVER_URL)…"
  VERSION=$(curl -sf -X POST "$SERVER_URL/api/version" -H 'Content-Type: application/json' \
    -d "{\"key\":\"$SERVER_API_KEY\"}" | sed -n 's/.*"version":"\{0,1\}\([0-9][0-9.]*\)"\{0,1\}.*/\1/p') || true
  if [ -n "${VERSION:-}" ]; then
    echo ">> Server läuft Wavelog $VERSION — pinne lokales Image darauf"
    set_env WAVELOG_VERSION "$VERSION"
    set -a; . ./.env; set +a
  else
    echo ">> WARNUNG: Server-Version nicht abrufbar — behalte Version ${WAVELOG_VERSION:-3.0.1}"
  fi
else
  echo ">> Noch kein Server-Zugang konfiguriert — Stack startet mit Version ${WAVELOG_VERSION:-3.0.1}."
  echo "   Konfiguration danach im Wavelog-UI: Usermenü -> "Offline-Sync" -> Einstellungen"
fi

echo ">> Starte/aktualisiere Stack (Wavelog ${WAVELOG_VERSION:-3.0.1})…"
if ! docker compose pull wavelog wavelog-db; then
  echo "FEHLER: Image ghcr.io/wavelog/wavelog:${WAVELOG_VERSION:-3.0.1} nicht verfügbar."
  echo "        Verfügbare Tags: https://github.com/wavelog/wavelog/pkgs/container/wavelog"
  echo "        Notfalls WAVELOG_VERSION in .env manuell auf den nächstliegenden Tag setzen."
  exit 1
fi
docker compose up -d --build

echo ">> Warte auf lokale Instanz…"
for i in $(seq 1 60); do
  curl -sf -o /dev/null "$PUBLIC/" && break
  sleep 2
done

if docker compose exec -T sync python app.py status >/dev/null 2>&1; then
  echo ">> Fertig. Lokales Wavelog: $PUBLIC"
  echo "   Sync, Seed und Einstellungen: Usermenü -> "Offline-Sync" im Wavelog-UI"
else
  cat <<EOF

>> Erstinstallation nötig — einmalig im Browser erledigen:
   1. $PUBLIC/install öffnen
      DB-Host:     wavelog-db
      DB-Name:     ${LOCAL_DB_NAME:-wavelog}
      DB-User:     ${LOCAL_DB_USER:-wavelog}
      DB-Passwort: $LOCAL_DB_PASSWORD
   2. Eigenen Benutzer anlegen (gleiches Rufzeichen wie am Server)
   3. Einloggen, Logbuch anlegen und als aktiv setzen
   4. Account -> API Keys -> Key mit read/write anlegen
   5. Usermenü -> "Offline-Sync" anklicken -> Einstellungen: Server-URL, Server-API-Key und den
      lokalen Key eintragen -> "Server-Bestand übernehmen (Seed)" klicken
   6. Dieses Script noch einmal ausführen, damit die lokale Wavelog-Version
      auf die des Servers gepinnt wird
EOF
fi
