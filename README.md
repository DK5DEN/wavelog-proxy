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
3. Die vom Script angezeigten Schritte im Browser erledigen:
   - `http://localhost:8086/install` — Wavelog-Erstinstallation mit den angezeigten DB-Daten
   - Benutzer anlegen (gleiches Rufzeichen wie am Server), einloggen
   - Logbuch anlegen und als aktiv setzen
4. Im Usermenü **Offline-Sync** anklicken (direkt unter „Hardware-Schnittstellen") →
   **Einstellungen**: nur Server-URL und Server-API-Key (am **Server** unter
   *Account → API Keys*, read/write) eintragen → „Speichern & testen". Der lokale
   API-Key wird dabei **automatisch erstellt**. Danach **„Server-Bestand übernehmen (Seed)"**.
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

**Sync-Icon in der Menüleiste:** Sobald der Sync eingerichtet ist und einmal gelaufen
ist, erscheint oben in der Menüleiste ein Sync-Icon. Ein Klick startet den Sync direkt
(Icon dreht sich währenddessen); vorher wird die Server-Erreichbarkeit geprüft — ist der
Server nicht da, kommt eine Meldung statt eines Sync-Versuchs.

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

## Geänderte oder gelöschte QSOs

Bearbeitungen lassen sich über die Wavelog-API nicht übertragen. Das Panel zeigt
stattdessen unter **Abweichungen** alle Unterschiede zwischen lokal und Server an —
pro QSO die betroffenen Felder, beide Werte und auf welcher Seite geändert wurde.
Zeitkorrekturen werden erkannt („vermutlich Zeit-Änderung: lokal 18:25 / Server 18:10"),
einseitig vorhandene QSOs deuten auf Löschungen hin. Nachgezogen wird von Hand auf der
Seite, die den alten Stand hat.

## Gut zu wissen

- Gesynct wird das **aktive Logbuch** — Archiv-Stationen außerhalb bleiben nur am Server.
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
