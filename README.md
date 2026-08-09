# Wavelog Offline-Kit

Eine lokale Wavelog-Instanz für Laptop und Feldbetrieb — offline loggen (auch im
Contest-Modus) und später mit dem Wavelog-Server abgleichen. Läuft mit Docker Desktop
unter Windows, macOS und Linux.

Der Abgleich nutzt ausschließlich die offiziellen Wavelog-APIs. Es funktioniert daher
mit jeder Wavelog-Instanz, auch gehosteten (z. B. DARC) — auf dem Server ist nichts
einzurichten, es genügt ein eigener API-Key.

## Installation

1. Diesen Ordner auf den Laptop kopieren.
2. Setup starten: `./setup.sh` (Linux/Mac) bzw. **`setup.cmd` per Doppelklick** (Windows —
   umgeht die PowerShell-Blockade für heruntergeladene Scripts).
3. `http://localhost:8086` im Browser öffnen — die Seite fragt die Installationsart ab:
   - **Einfach** (empfohlen): nur das Rufzeichen und optional ein Wunsch-Passwort
     eingeben (leer = Passwort wird erzeugt und angezeigt). Alles Weitere —
     Wavelog-Erstinstallation, Benutzer, aktives Logbuch, Station — läuft
     automatisch; die Seite zeigt den Fortschritt und am Ende die Zugangsdaten.
     Profildaten wie Name/Locator/E-Mail werden mit Platzhaltern belegt und
     lassen sich später im Wavelog-Account ändern.
   - **Experte**: die Wavelog-Erstinstallation wie gehabt manuell durchklicken —
     die Seite zeigt die dafür nötigen DB-Daten an. Danach: Benutzer anlegen
     (gleiches Rufzeichen wie am Server), einloggen, Logbuch anlegen und als
     aktiv setzen.
4. Im Usermenü **Offline-Sync** anklicken (direkt unter „Hardware-Schnittstellen") →
   **Einstellungen**: nur Server-URL und Server-API-Token eintragen → „Speichern &
   testen". Das lokale Token wird dabei **automatisch erstellt**. Danach
   **„Server-Bestand übernehmen (Seed)"**.

   Der Sync läuft komplett über die **APIv2** (der Server muss Wavelog ≥ 3.1
   laufen, z. B. DARC). Als Server-Token ein **APIv2-Token** am Server anlegen
   (`wl2_…`) mit den Scopes `qso:read`, `qso:write`, `qso:delete` (für den
   Lösch-Sync), `station:read`, `station:write`, `confirmation:read` (für den
   QSL-Abgleich) und optional `statistic:read`. Die Server-*Version* gibt die
   APIv2 nur an Admins heraus — für den automatischen Versionsabgleich
   (Image-Pinning, ⚠-Badge) kann deshalb optional zusätzlich ein Legacy-v1-Key
   hinterlegt werden; das ist der einzige verbliebene v1-Einsatz. Hinweis:
   über die APIv2 am Server angelegte Stationen bekommen dort eine neue UUID
   (der Sync merkt sich die Zuordnung) und werden am Server nicht automatisch
   ins Logbuch verknüpft.
5. Setup-Script noch einmal ausführen — es stellt die lokale Wavelog-Version passend
   zum Server ein.

Danach ist nichts mehr zu konfigurieren — alle Einstellungen liegen in der UI.
(Die vom Script erzeugte `.env` ist rein intern: Image-Version und DB-Passwort.)

## Benutzung

Alles läuft über den Menüpunkt **Offline-Sync** im Usermenü (direkt unter
„Hardware-Schnittstellen", erscheint erst nach dem Login). Er öffnet die Seite
`/offline-sync`, die sich Header, Menü, Footer und Theme mit Wavelog teilt und wie eine
native Wavelog-Seite aussieht (Status, Sync/Push/Pull, Abweichungen, Einstellungen, Seed,
Reset — in Wavelog-Cards). Das Badge am Menüpunkt zeigt auf allen Seiten, ob etwas ansteht.
Solange der Sync noch nicht eingerichtet ist, weist ein Banner unter der Menüleiste darauf hin.

**Sync-Status je QSO:** In den QSO-Übersichten (Dashboard „Letzte QSOs", Logbook) zeigt ein
kleines Icon vor jedem QSO seinen Sync-Stand: ✓ synchronisiert, ↑ noch nicht synchronisiert,
⚠ lokal/Server unterschiedlich, 🕐 Uhrzeit weicht ab, 🗑 am Server gelöscht (Details im Tooltip).
Der Stand stammt vom letzten Sync/Abweichungs-Check.

**Code-Updates:** Die Sync-Dateien werden aus dem Ordner gemountet, nicht ins Image
gebacken. `sync/inject.js` und `sync/status.html` ändern → Datei speichern, Browser
neu laden (kein Rebuild). `sync/app.py` ändern → `docker compose restart sync`. Nur
bei neuen Python-Paketen (`sync/requirements.txt`) ist `docker compose up -d --build sync` nötig.

**Mehrere Benutzer:** Die lokale Instanz kann von mehreren Personen genutzt werden.
Einstellungen (Server, API-Keys) und Sync-Stand gelten pro Profil — das Profil ist das
Rufzeichen des lokal eingeloggten Wavelog-Users und wird automatisch erkannt. Jeder
Benutzer legt einmalig im Panel seine eigenen Keys an (Schritt 3 und 4 der Installation)
und synct damit nur seine eigenen Stationen und QSOs.

| Situation | Was tun |
|---|---|
| Wieder online | Sync ausführen — neue QSOs und Stationen wandern in beide Richtungen |
| Vor einem Contest | einmal syncen, damit die Dupe-Basis aktuell ist |
| Nach einem Contest | syncen, dann am Server einmal **Contesting → Import Historical Contests** klicken — daraus entsteht die Contest-Session (das Panel erinnert daran) |
| Server wurde aktualisiert | Setup-Script erneut ausführen (das Badge zeigt vorher ⚠) |
| Neue Station unterwegs anlegen | erlaubt — beim Anlegen ins aktive Logbuch verknüpfen, sonst wird sie nicht gesynct |

## Geänderte oder gelöschte QSOs, QSL-Status

Der **Abweichungs-Check** (läuft beim Sync automatisch mit, solange das Log klein
genug ist) erledigt inzwischen das meiste selbst:

- **Feld-Änderungen** (RST, Name, Kommentar, Locator, Contest-Nummern, SOTA/POTA/…)
  mit eindeutiger Richtung werden automatisch auf die andere Seite übertragen.
  Konflikte (beide Seiten geändert) und nicht per API änderbare Felder
  (z. B. `contest_id`, Submode, Operator, QSL-Nachricht) bleiben im Panel zur
  manuellen Entscheidung; Zeitkorrekturen werden weiterhin nur angezeigt.
- **Löschungen**: fehlt ein beidseitig bekanntes QSO auf einer Seite, gilt es
  dort als gelöscht. Das Panel sammelt diese Fälle und löscht erst nach **einer
  Sicherheitsabfrage mit der kompletten Liste** („Löschungen übernehmen") —
  lokal wie am Server.
- **QSL-Bestätigungen** (LoTW, eQSL, QSL-Karte, QRZ, Clublog) werden bei jedem
  Sync vom Server geholt und in die lokalen QSOs geschrieben — das Logbuch
  zeigt offline denselben Bestätigungsstand. Der Server bleibt dafür Master.

## Gut zu wissen

- Gesynct werden standardmäßig **alle Stationen des Server-Accounts** (die APIv2
  kennt keine Logbuch-Grenze). Unter **Einstellungen → Stationen im Sync** lassen
  sich einzelne Server-Stationen (z. B. Archiv-/Ausbildungsrufzeichen) abwählen —
  sie werden dann ignoriert und beim nächsten Sync lokal samt QSOs entfernt;
  der Server bleibt unberührt. Neue Server-Stationen sind automatisch angewählt.
- QSL-Abgleich (eQSL/LoTW/…) macht weiterhin nur der Server.
- Die lokale Instanz ist ein Arbeits-Spiegel, kein Backup.
- Nach Sync-Problemen: im Panel unter **Reparatur** → „Lokalen Bestand zurücksetzen" —
  löscht alle lokal gespeicherten QSOs, Contest-Sessions und Stationen des Profils und
  lädt sie frisch vom Server (ungesyncte lokale QSOs gehen dabei verloren).
- Kompletter Neuanfang der ganzen Instanz: `docker compose down`, `data/`-Ordner löschen,
  Installation neu durchlaufen.

## Wenn etwas klemmt

| Symptom | Lösung |
|---|---|
| Fehler 401/403 beim Sync | API-Key falsch oder ohne Schreibrecht — im Panel unter Einstellungen prüfen |
| ⚠ am Menüpunkt | Wavelog-Versionen unterscheiden sich → Setup-Script ausführen |
| Neue Station wird nicht gesynct | Station ist lokal nicht im aktiven Logbuch verknüpft |
| Menüpunkt fehlt | Panel direkt unter `http://localhost:8086/_sync/` öffnen |
| Sync sehr langsam | Rate-Limit des Server-Betreibers — wird automatisch abgewartet |
| Sync-Zustand reparieren | `docker compose exec sync python app.py baseline` (markiert den aktuellen Bestand beider Seiten als gesynct) |
| Windows: „nicht digital signiert" | `setup.cmd` statt `setup.ps1` starten (oder einmalig `Unblock-File .\setup.ps1`) |
