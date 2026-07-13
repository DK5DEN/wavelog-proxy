#!/usr/bin/env python3
"""Wavelog Offline-Kit Sync-Sidecar.

Synct QSOs und Stationen zwischen lokaler Wavelog-Instanz und dem Server —
ausschliesslich über die offiziellen Wavelog-APIs (api/qso, api/get_contacts_adif,
api/station_info, api/create_station). Server = Master.

Mehrbenutzer-faehig: Einstellungen (Server, API-Keys) und Sync-Stand liegen pro
Profil (= Rufzeichen des lokal eingeloggten Wavelog-Users). Jeder User synct
damit nur seine eigenen Stationen/QSOs mit seinem eigenen Server-Zugang.

Zusaetzlich: Injection-Reverse-Proxy vor der lokalen Instanz, der den Menuepunkt
"Offline-Sync" ins Wavelog-UI einblendet (Wavelog selbst bleibt unveraendert).
"""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, request

LOCAL_URL = os.environ.get("LOCAL_URL", "http://wavelog:80").rstrip("/")
STATE_FILE = os.environ.get("STATE_FILE", "/state/state.json")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/state/config.json")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8086"))
PULL_LIMIT = int(os.environ.get("PULL_LIMIT", "2000"))
# Bis zu dieser Log-Groesse laeuft der Abweichungs-Check bei jedem Sync mit;
# darueber nur manuell (Button/CLI), um grosse Instanzen nicht voll zu ziehen
DIFF_AUTO_LIMIT = int(os.environ.get("DIFF_AUTO_LIMIT", "5000"))

_lock = threading.Lock()


class ApiError(RuntimeError):
    pass


# ------------------------------------------------------- config & profiles ---

def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    return _load_json(CONFIG_FILE)


def save_config(cfg):
    _save_json(CONFIG_FILE, cfg)


def list_users():
    return sorted((load_config().get("users") or {}).keys())


def resolve_user(explicit=None):
    """Profil bestimmen: explizit angegeben, sonst das einzige vorhandene."""
    users = load_config().get("users") or {}
    if explicit:
        return explicit.strip().upper()
    if len(users) == 1:
        return next(iter(users))
    if not users:
        raise ApiError("Kein Profil konfiguriert — Einstellungen öffnen und speichern")
    raise ApiError("Mehrere Profile vorhanden (" + ", ".join(sorted(users))
                   + ") — Profil angeben")


def user_cfg(user):
    """Einstellungen eines Profils (Server-URL, Keys)."""
    u = (load_config().get("users") or {}).get(user, {})
    server_key = u.get("server_api_key", "")
    return {
        "server_url": (u.get("server_url") or "").rstrip("/"),
        "server_api_key": server_key,
        "local_api_key": u.get("local_api_key") or server_key,
        "diff_auto_limit": int(u.get("diff_auto_limit") or DIFF_AUTO_LIMIT),
    }


# ---------------------------------------------------------------- state ----

def load_state():
    return _load_json(STATE_FILE)


def save_state(state):
    _save_json(STATE_FILE, state)


def user_state(state, user):
    return state.setdefault("users", {}).setdefault(user, {
        "station_map": {},          # lokale station_id -> Server station_id
        "push_marks": {},           # lokale station_id -> lastfetchedid (lokal)
        "pull_marks": {},           # Server station_id -> lastfetchedid (Server)
        "contest_import_pending": [],
        "last_sync": None,
        "last_result": None,
    })


# ------------------------------------------------------------ api client ---

def _request(method, url, retries=5, **kw):
    """HTTP mit Retry: 429 gemaess Retry-After abwarten (fremde Instanzen wie
    die DARC-Wavelog koennen Rate-Limits gesetzt haben), transiente
    Verbindungsfehler (wackliges Feld-Internet) kurz erneut versuchen."""
    r = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kw)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt >= retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 429:
            return r
        try:
            wait = int(r.json().get("retry_after", 30))
        except ValueError:
            wait = 30
        time.sleep(min(max(wait, 1), 300))
    return r


def _check(resp):
    if resp.status_code >= 400:
        raise ApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def get_version(base, key):
    r = _request("POST", f"{base}/api/version", json={"key": key}, timeout=30)
    data = _check(r)
    if data.get("status") != "ok":
        raise ApiError(f"version failed: {data}")
    return str(data.get("version"))


def get_stations(base, key):
    r = _request("GET", f"{base}/api/station_info/{key}", timeout=60)
    data = _check(r)
    if isinstance(data, dict):  # Fehlerobjekt
        raise ApiError(str(data))
    return data


def get_contacts(base, key, station_id, fetchfromid, limit=PULL_LIMIT, fmt="adif"):
    payload = {
        "key": key,
        "station_id": [int(station_id)],
        "fetchfromid": int(fetchfromid),
        "limit": limit,
    }
    if fmt != "adif":
        payload["output_format"] = fmt
    r = _request("POST", f"{base}/api/get_contacts_adif", json=payload, timeout=300)
    return _check(r)


DUPE_MARKERS = ("Duplicate for", "Duplikat")


def push_adif(base, key, station_profile_id, adif):
    payload = {
        "key": key,
        "station_profile_id": str(station_profile_id),
        "type": "adif",
        "string": adif,
    }
    r = _request("POST", f"{base}/api/qso", json=payload, timeout=300)
    try:
        data = r.json()
    except ValueError:
        raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}")
    if data.get("status") == "created":
        data["dupes"] = 0
        return data
    # import_bulk speichert Nicht-Duplikate trotzdem und meldet Dupes als Fehler:
    # 400/abort mit ausschliesslich Duplicate-Zeilen == Erfolg (Dupes geskippt)
    if data.get("status") == "abort":
        lines = [
            ln.strip()
            for msg in data.get("messages", [])
            for ln in str(msg).split("<br>")
            if ln.strip()
        ]
        if lines and all(any(m in ln for m in DUPE_MARKERS) for ln in lines):
            data["dupes"] = len(lines)
            return data
    raise ApiError(f"qso push failed: HTTP {r.status_code}: {data}")


def create_station(base, key, st):
    # Felder aus station_info -> Felder, die api/create_station versteht
    payload = {
        "station_profile_name": st.get("station_profile_name") or "",
        "station_callsign": st.get("station_callsign") or "",
        "station_gridsquare": st.get("station_gridsquare") or "",
        "station_city": st.get("station_city") or "",
        "station_iota": st.get("station_iota") or "",
        "station_sota": st.get("station_sota") or "",
        "station_wwff": st.get("station_wwff") or "",
        "station_pota": st.get("station_pota") or "",
        "station_sig": st.get("station_sig") or "",
        "station_sig_info": st.get("station_sig_info") or "",
        "station_dxcc": st.get("station_dxcc") or "",
        "station_cnty": st.get("station_cnty") or "",
        "station_cq": st.get("station_cq") or "",
        "station_itu": st.get("station_itu") or "",
        "state": st.get("station_state") or "",
        "station_uuid": st.get("station_uuid") or "",
        "link_active_logbook": True,
    }
    r = _request("POST", f"{base}/api/create_station/{key}", json=payload, timeout=60)
    return _check(r)


# ------------------------------------------------------------- adif diff ---

# Edits sind über die offizielle API nicht übertragbar (kein Update-Endpoint,
# Dupe-Check skippt geänderte Records, Delta-Pull sieht Edits nicht). Daher:
# Vollvergleich beider Seiten + Anzeige für manuelles Nachziehen. Die Richtung
# ("wer hat geändert") kommt aus Snapshots des Sidecars je Prüfung, weil die
# API keine Änderungs-Timestamps liefert.

ADIF_TOKEN_RE = re.compile(r"<(\w+)(?::(\d+)(?::[^>]*)?)?>", re.IGNORECASE)

# Verglichene Felder: bewusst nur direkt editierbare (keine berechneten wie
# DXCC/Distance/Country — die erzeugen nur Lookup-Rauschen)
DIFF_FIELDS = [
    "rst_sent", "rst_rcvd", "name", "comment", "notes", "qslmsg", "gridsquare",
    "qth", "submode", "stx", "srx", "stx_string", "srx_string", "contest_id",
    "tx_pwr", "operator", "sota_ref", "pota_ref", "wwff_ref", "iota",
    "sat_name", "prop_mode",
]


def parse_adif(text):
    recs, cur, pos = [], {}, 0
    while True:
        m = ADIF_TOKEN_RE.search(text, pos)
        if not m:
            break
        name, ln = m.group(1).lower(), m.group(2)
        pos = m.end()
        if ln is not None:
            n = int(ln)
            cur[name] = text[pos:pos + n].strip()
            pos += n
        elif name == "eor":
            if cur:
                recs.append(cur)
            cur = {}
        elif name == "eoh":
            cur = {}  # Header-Felder verwerfen
    return recs


def qso_key(rec):
    """Instanzunabhängiger Schlüssel — identisch zum Wavelog-Dupe-Check
    (Call + Zeit minutengenau + Band + Mode; Station kommt über das Mapping)."""
    return "|".join([
        (rec.get("call") or "").upper(),
        rec.get("qso_date") or "",
        (rec.get("time_on") or "")[:4],
        (rec.get("band") or "").lower(),
        (rec.get("mode") or "").upper(),
    ])


def _norm(v):
    v = (v or "").strip()
    if v.isdigit():
        # numerische 0 == nicht gesetzt (Wavelogs Import macht aus 0 ein NULL)
        return str(int(v)) if int(v) else ""
    return v


def rec_hash(rec):
    return json.dumps([_norm(rec.get(f)) for f in DIFF_FIELDS])


def fetch_all_records(base, key, station_id):
    recs, mark = [], 0
    while True:
        r = get_contacts(base, key, station_id, mark)
        if not r.get("exported_qsos") or not r.get("adif"):
            return recs
        recs.extend(parse_adif(r["adif"]))
        mark = int(r["lastfetchedid"])


def _describe(rec, station):
    return {
        "station": station,
        "call": rec.get("call"),
        "date": rec.get("qso_date"),
        "time": (rec.get("time_on") or "")[:4],
        "band": rec.get("band"),
        "mode": rec.get("mode"),
    }


def _pair_time_edits(only_local, only_server):
    """Einseitige Eintraege paaren, die sich nur in der Uhrzeit unterscheiden:
    gleiche Station + Call + Band + Mode am selben Tag = vermutlich Zeit-Edit.
    Bei mehreren Kandidaten gewinnt die naechstliegende Uhrzeit."""
    def ident(q):
        return (q["station"], (q["call"] or "").upper(), q["date"] or "",
                (q["band"] or "").lower(), (q["mode"] or "").upper())

    def minutes(q):
        t = q.get("time") or ""
        return int(t[:2]) * 60 + int(t[2:4]) if t[:4].isdigit() else 0

    pairs, rest_local, remaining = [], [], list(only_server)
    for l in only_local:
        best_i = best_d = None
        for i, s in enumerate(remaining):
            if ident(l) == ident(s):
                d = abs(minutes(l) - minutes(s))
                if best_d is None or d < best_d:
                    best_i, best_d = i, d
        if best_i is None:
            rest_local.append(l)
        else:
            s = remaining.pop(best_i)
            pairs.append({**l, "zeit_lokal": l.get("time"), "zeit_server": s.get("time")})
    return pairs, rest_local, remaining


def do_diff(cfg, ust, state, log):
    """Beide Seiten voll vergleichen; Feld-Abweichungen + Nur-einseitig-Fälle
    sammeln und Änderungsrichtung aus Snapshots ableiten."""
    sync_stations(cfg, ust, log)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snaps = ust.setdefault("diff_snapshots", {})
    journal = ust.setdefault("diff_journal", [])
    names = {str(s["station_id"]): s["station_profile_name"]
             for s in get_stations(LOCAL_URL, cfg["local_api_key"])}
    diffs, only_local, only_server, total = [], [], [], 0

    for lid, sid in ust["station_map"].items():
        station = names.get(str(lid), str(lid))
        lrecs = {qso_key(r): r
                 for r in fetch_all_records(LOCAL_URL, cfg["local_api_key"], lid)}
        srecs = {qso_key(r): r
                 for r in fetch_all_records(cfg["server_url"], cfg["server_api_key"], sid)}
        total += len(srecs)

        for k, lr in lrecs.items():
            if k not in srecs:
                only_local.append(_describe(lr, station))
                continue
            sr = srecs[k]
            lh, sh = rec_hash(lr), rec_hash(sr)
            snap = snaps.get(k, {})
            l_edit = bool(snap) and snap.get("local") != lh
            s_edit = bool(snap) and snap.get("server") != sh
            changed = {
                f: {"lokal": _norm(lr.get(f)), "server": _norm(sr.get(f))}
                for f in DIFF_FIELDS if _norm(lr.get(f)) != _norm(sr.get(f))
            }
            if changed:
                if l_edit and s_edit:
                    side = "beide Seiten geändert (Konflikt!)"
                elif l_edit:
                    side = "lokal geändert"
                elif s_edit:
                    side = "am Server geändert"
                else:
                    side = "unbekannt (vor erstem Vergleich)"
                diffs.append({**_describe(lr, station), "fields": changed, "edited": side})
            if l_edit or s_edit:
                journal.append({"ts": now, "qso": _describe(lr, station),
                                "lokal_geaendert": l_edit, "server_geaendert": s_edit})
            snaps[k] = {"local": lh, "server": sh}

        for k, sr in srecs.items():
            if k not in lrecs:
                only_server.append(_describe(sr, station))

    time_edits, only_local, only_server = _pair_time_edits(only_local, only_server)
    del journal[:-200]
    ust["last_diff"] = {
        "ts": now, "total_qsos": total, "diffs": diffs,
        "time_edits": time_edits,
        "only_local": only_local, "only_server": only_server,
    }
    save_state(state)
    if diffs or time_edits or only_local or only_server:
        log.append(f"Abweichungen: {len(diffs)} Feld-Diff(s), "
                   f"{len(time_edits)} Zeit-Änderung(en), "
                   f"{len(only_local)} nur lokal, {len(only_server)} nur Server")
    else:
        log.append("Abweichungs-Check: beide Seiten identisch")
    return len(diffs) + len(time_edits)


# ------------------------------------------------------------- sync core ---

def sync_stations(cfg, ust, log):
    """Gleicht Stationen in beide Richtungen ab (Matching via station_uuid)."""
    local = get_stations(LOCAL_URL, cfg["local_api_key"])
    server = get_stations(cfg["server_url"], cfg["server_api_key"])
    l_by_uuid = {s["station_uuid"]: s for s in local if s.get("station_uuid")}
    s_by_uuid = {s["station_uuid"]: s for s in server if s.get("station_uuid")}

    created_up = created_down = 0
    for uuid, st in l_by_uuid.items():
        if uuid not in s_by_uuid:
            create_station(cfg["server_url"], cfg["server_api_key"], st)
            created_up += 1
            log.append(f"Station '{st['station_profile_name']}' -> Server angelegt")
    for uuid, st in s_by_uuid.items():
        if uuid not in l_by_uuid:
            create_station(LOCAL_URL, cfg["local_api_key"], st)
            created_down += 1
            log.append(f"Station '{st['station_profile_name']}' -> lokal angelegt")

    if created_up or created_down:
        local = get_stations(LOCAL_URL, cfg["local_api_key"])
        server = get_stations(cfg["server_url"], cfg["server_api_key"])
        l_by_uuid = {s["station_uuid"]: s for s in local if s.get("station_uuid")}
        s_by_uuid = {s["station_uuid"]: s for s in server if s.get("station_uuid")}

    ust["station_map"] = {
        str(l_by_uuid[u]["station_id"]): int(s_by_uuid[u]["station_id"])
        for u in l_by_uuid
        if u in s_by_uuid
    }
    return created_up, created_down


CONTEST_RE = re.compile(r"<CONTEST_ID:\d+(?::[A-Za-z])?>([^<]+)", re.IGNORECASE)


def do_push(cfg, ust, state, log):
    """Neue lokale QSOs (inkl. Contest-Feldern) zum Server pushen."""
    pushed = 0
    for lid, sid in ust["station_map"].items():
        mark = int(ust["push_marks"].get(lid, 0))
        while True:
            r = get_contacts(LOCAL_URL, cfg["local_api_key"], lid, mark)
            if not r.get("exported_qsos") or not r.get("adif"):
                break
            adif = r["adif"]
            contests = sorted(set(c.strip() for c in CONTEST_RE.findall(adif) if c.strip()))
            res = push_adif(cfg["server_url"], cfg["server_api_key"], sid, adif)
            dupes = res["dupes"]
            if dupes:
                log.append(f"{dupes} Dupe(s) vom Server geskippt")
            mark = int(r["lastfetchedid"])
            ust["push_marks"][lid] = mark
            pushed += max(0, int(r["exported_qsos"]) - dupes)
            for c in contests:
                if c not in ust["contest_import_pending"]:
                    ust["contest_import_pending"].append(c)
            save_state(state)
    if pushed:
        log.append(f"{pushed} QSO(s) zum Server gepusht (Server-Dupe-Check aktiv)")
    return pushed


def do_pull(cfg, ust, state, log):
    """Server-Delta holen und in die lokale Instanz importieren."""
    pulled = 0
    rev = {str(v): k for k, v in ust["station_map"].items()}
    for sid, lid in rev.items():
        mark = int(ust["pull_marks"].get(sid, 0))
        while True:
            r = get_contacts(cfg["server_url"], cfg["server_api_key"], sid, mark)
            if not r.get("exported_qsos") or not r.get("adif"):
                break
            res = push_adif(LOCAL_URL, cfg["local_api_key"], lid, r["adif"])
            if res["dupes"]:
                log.append(f"{res['dupes']} Dupe(s) beim lokalen Import geskippt")
            mark = int(r["lastfetchedid"])
            ust["pull_marks"][sid] = mark
            pulled += max(0, int(r["exported_qsos"]) - res["dupes"])
            save_state(state)
    if pulled:
        log.append(f"{pulled} QSO(s) vom Server geholt (lokaler Dupe-Check aktiv)")
    return pulled


def drain_mark(base, key, station_id):
    """Aktuelle Hochwassermarke (max COL_PRIMARY_KEY) einer Station ermitteln."""
    mark = 0
    while True:
        r = get_contacts(base, key, station_id, mark, fmt="json")
        new = int(r.get("lastfetchedid", mark))
        if new <= mark:
            return mark
        mark = new


def do_baseline(cfg, ust, state, log):
    """Alles Bestehende (beidseitig) als 'bereits gesynct' markieren."""
    sync_stations(cfg, ust, log)
    for lid in ust["station_map"]:
        ust["push_marks"][lid] = drain_mark(LOCAL_URL, cfg["local_api_key"], lid)
    for sid in set(ust["station_map"].values()):
        ust["pull_marks"][str(sid)] = drain_mark(cfg["server_url"],
                                                 cfg["server_api_key"], str(sid))
    ust["contest_import_pending"] = []
    save_state(state)
    log.append("Baseline gesetzt: bestehender Bestand gilt als gesynct")


def do_seed(cfg, ust, state, log, force=False):
    """Seed/Reseed rein über die API: kompletten Server-Bestand (Stationen +
    alle QSOs ab id 0) in die lokale Instanz ziehen — das API-Pendant zum
    Wavelog-Backup (backup/adif). Dupe-Check macht Wiederholungen gefahrlos."""
    if not force:
        info = pending_info_user(cfg, ust)
        if info["pending_qsos"] or info["stations_unsynced"]:
            raise ApiError(
                f"{info['pending_qsos']} ungepushte QSO(s)/"
                f"{info['stations_unsynced']} Station(en) vorhanden — erst syncen "
                "oder seed mit --force"
            )
    up, down = sync_stations(cfg, ust, log)
    ust["pull_marks"] = {}  # ab 0 ziehen = kompletter Bestand
    pulled = do_pull(cfg, ust, state, log)
    for lid in ust["station_map"]:
        ust["push_marks"][lid] = drain_mark(LOCAL_URL, cfg["local_api_key"], lid)
    save_state(state)
    log.append(f"Seed fertig: {pulled} QSO(s) übernommen, lokaler Bestand = Server-Stand")
    return pulled, up, down


def _local_db():
    """Verbindung zur LOKALEN Wavelog-DB (nur lokal genutzt: Reset-Knopf und
    Auto-Erzeugung des lokalen API-Keys). Der Server wird nie per DB angefasst."""
    import pymysql
    return pymysql.connect(
        host=os.environ.get("LOCAL_DB_HOST", "wavelog-db"),
        user=os.environ.get("LOCAL_DB_USER", "wavelog"),
        password=os.environ.get("LOCAL_DB_PASSWORD", ""),
        database=os.environ.get("LOCAL_DB_NAME", "wavelog"),
    )


def ensure_local_api_key(callsign):
    """Legt bei Bedarf einen lokalen read/write-API-Key fuer den User (Rufzeichen)
    in der lokalen Wavelog-DB an und gibt ihn zurueck — damit der User ihn nicht
    von Hand erzeugen/eintragen muss. Rein lokal; Format wie Wavelog (wl + hex)."""
    import hashlib
    conn = _local_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM users WHERE UPPER(user_callsign)=UPPER(%s) LIMIT 1",
                (callsign,))
            row = cur.fetchone()
            if not row:
                raise ApiError(f"Lokaler Benutzer '{callsign}' nicht gefunden — "
                               "bitte lokalen API-Key manuell eintragen")
            uid = row[0]
            cur.execute(
                "SELECT `key` FROM api WHERE user_id=%s AND rights='rw' "
                "AND status='active' AND description=%s LIMIT 1",
                (uid, LOCAL_KEY_DESC))
            r = cur.fetchone()
            if r:
                return r[0]
            key = "wl" + hashlib.md5(os.urandom(16)).hexdigest()[19:]
            cur.execute(
                "INSERT INTO api (`key`,rights,status,description,user_id,created_by) "
                "VALUES (%s,'rw','active',%s,%s,%s)", (key, LOCAL_KEY_DESC, uid, uid))
        conn.commit()
        return key
    finally:
        conn.close()


LOCAL_KEY_DESC = "Offline-Kit (auto)"


def reset_local_db(station_ids):
    """NUR fuer den Reset-Knopf: synchronisierte Inhalte (QSOs, Contest-Sessions,
    Stationen) des Profils aus der LOKALEN DB loeschen. Die lokale Instanz ist
    ein Wegwerf-Spiegel — der Server wird hiervon nie beruehrt (rein API)."""
    counts = {"qsos": 0, "sessions": 0, "stations": len(station_ids)}
    if not station_ids:
        return counts
    conn = _local_db()
    qso_table = os.environ.get("QSO_TABLE", "TABLE_HRD_CONTACTS_V01")
    ph = ",".join(["%s"] * len(station_ids))
    try:
        with conn.cursor() as cur:
            counts["sessions"] = cur.execute(
                f"DELETE FROM contest_session WHERE station_id IN ({ph})", station_ids)
            cur.execute(
                f"DELETE cq FROM contest_qsos cq JOIN {qso_table} q "
                f"ON q.COL_PRIMARY_KEY = cq.qso_id WHERE q.station_id IN ({ph})",
                station_ids)
            counts["qsos"] = cur.execute(
                f"DELETE FROM {qso_table} WHERE station_id IN ({ph})", station_ids)
            cur.execute(
                f"DELETE FROM station_logbooks_relationship "
                f"WHERE station_location_id IN ({ph})", station_ids)
            cur.execute(
                f"DELETE FROM station_profile WHERE station_id IN ({ph})", station_ids)
        conn.commit()
    finally:
        conn.close()
    return counts


def do_reset(cfg, ust, state, log):
    """Lokalen Bestand des Profils verwerfen und komplett neu vom Master laden
    (Reparatur nach Sync-Problemen)."""
    stations = get_stations(LOCAL_URL, cfg["local_api_key"])
    ids = [int(s["station_id"]) for s in stations]
    counts = reset_local_db(ids)
    log.append(f"Lokal gelöscht: {counts['qsos']} QSO(s), "
               f"{counts['sessions']} Contest-Session(s), {counts['stations']} Station(en)")
    ust["station_map"] = {}
    ust["push_marks"] = {}
    ust["pull_marks"] = {}
    ust["contest_import_pending"] = []
    for k in ("diff_snapshots", "diff_journal", "last_diff"):
        ust.pop(k, None)
    save_state(state)
    pulled, up, down = do_seed(cfg, ust, state, log, force=True)
    log.append("Hinweis: lokale Contest-Sessions bei Bedarf über "
               "'Import Historical Contests' neu erzeugen")
    return pulled, up, down


def pending_info_user(cfg, ust):
    """Ungesyncte lokale QSOs eines Profils zaehlen/auflisten (Status/Badge)."""
    out = {"pending_qsos": 0, "qsos": [], "stations_unsynced": 0,
           "configured": bool(cfg["server_url"] and cfg["server_api_key"]),
           "local_ok": False}
    try:
        local = get_stations(LOCAL_URL, cfg["local_api_key"])
        out["local_ok"] = True
    except (requests.RequestException, ApiError):
        local = []
    try:
        if not out["configured"]:
            raise ApiError("nicht konfiguriert")
        server_uuids = {s["station_uuid"]
                        for s in get_stations(cfg["server_url"], cfg["server_api_key"])}
        out["stations_unsynced"] = sum(
            1 for s in local if s.get("station_uuid") not in server_uuids
        )
        out["server_reachable"] = True
    except (requests.RequestException, ApiError):
        out["server_reachable"] = False

    for st in local:
        lid = str(st["station_id"])
        mark = int(ust["push_marks"].get(lid, 0))
        try:
            r = get_contacts(LOCAL_URL, cfg["local_api_key"], lid, mark,
                             limit=500, fmt="json")
        except ApiError:
            continue
        for q in r.get("qsos") or []:
            out["qsos"].append({
                "station": st["station_profile_name"],
                "call": q.get("CALL"),
                "date": q.get("QSO_DATE"),
                "time": q.get("TIME_ON"),
                "band": q.get("BAND"),
                "mode": q.get("MODE"),
                "contest": q.get("CONTEST_ID"),
            })
    out["pending_qsos"] = len(out["qsos"])
    out["contest_import_pending"] = ust.get("contest_import_pending", [])
    out["last_sync"] = ust.get("last_sync")
    out["last_result"] = ust.get("last_result")
    out["server_url"] = cfg["server_url"]
    ld = ust.get("last_diff") or {}
    out["diff_count"] = len(ld.get("diffs", [])) + len(ld.get("time_edits", []))
    out["diff_ts"] = ld.get("ts")
    try:
        out["local_version"] = get_version(LOCAL_URL, cfg["local_api_key"])
        if out["server_reachable"]:
            out["server_version"] = get_version(cfg["server_url"], cfg["server_api_key"])
            out["version_mismatch"] = out["local_version"] != out["server_version"]
    except (requests.RequestException, ApiError):
        pass
    return out


def pending_info(user_param):
    try:
        user = resolve_user(user_param)
    except ApiError as e:
        return {"pending_qsos": 0, "qsos": [], "stations_unsynced": 0,
                "configured": False, "local_ok": False, "server_reachable": False,
                "contest_import_pending": [], "diff_count": 0,
                "user": None, "users": list_users(), "error": str(e)}
    cfg = user_cfg(user)
    ust = user_state(load_state(), user)
    out = pending_info_user(cfg, ust)
    out["user"] = user
    out["users"] = list_users()
    return out


def run_sync(user_param=None, mode="sync", force=False):
    with _lock:
        result = {"pushed": 0, "pulled": 0, "stations_up": 0, "stations_down": 0,
                  "mode": mode}
        try:
            user = resolve_user(user_param)
        except ApiError as e:
            return {**result, "ok": False, "error": str(e), "log": []}
        result["user"] = user
        cfg = user_cfg(user)
        state = load_state()
        ust = user_state(state, user)
        log = []
        if not (cfg["server_url"] and cfg["server_api_key"]):
            result.update(ok=False, error=f"Profil {user} nicht konfiguriert — "
                          "Server-URL und API-Key in den Einstellungen eintragen")
            return {**result, "log": []}
        try:
            if mode == "baseline":
                do_baseline(cfg, ust, state, log)
            elif mode == "seed":
                result["pulled"], result["stations_up"], result["stations_down"] = \
                    do_seed(cfg, ust, state, log, force=force)
            elif mode == "reset":
                if not force:
                    raise ApiError("Reset erfordert Bestätigung (force)")
                result["pulled"], result["stations_up"], result["stations_down"] = \
                    do_reset(cfg, ust, state, log)
            else:
                up, down = sync_stations(cfg, ust, log)
                result["stations_up"], result["stations_down"] = up, down
                if mode in ("push", "sync"):
                    result["pushed"] = do_push(cfg, ust, state, log)
                if mode in ("pull", "sync"):
                    result["pulled"] = do_pull(cfg, ust, state, log)
                if mode == "diff":
                    result["diffs"] = do_diff(cfg, ust, state, log)
                elif mode == "sync":
                    # Abweichungs-Check automatisch, solange der Log klein genug
                    # ist (Vollvergleich = kompletter Pull beider Seiten)
                    known = int((ust.get("last_diff") or {}).get("total_qsos", 0))
                    if known <= cfg["diff_auto_limit"]:
                        result["diffs"] = do_diff(cfg, ust, state, log)
            result["ok"] = True
        except (requests.RequestException, ApiError) as e:
            result["ok"] = False
            result["error"] = str(e)
            log.append(f"FEHLER: {e}")
        ust["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ust["last_result"] = {**result, "log": log}
        save_state(state)
        return ust["last_result"]


# ----------------------------------------------------------------- proxy ---

app = Flask(__name__)
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length", "host", "accept-encoding",
}
INJECT_TAG = b'<script src="/_sync/inject.js" defer></script>'


def _req_user():
    return (request.args.get("user") or "").strip().upper() or None


@app.get("/_sync/")
def status_page():
    with open(os.path.join(os.path.dirname(__file__), "status.html"), "rb") as f:
        return Response(f.read(), mimetype="text/html")


@app.get("/_sync/inject.js")
def inject_js():
    with open(os.path.join(os.path.dirname(__file__), "inject.js"), "rb") as f:
        return Response(f.read(), mimetype="application/javascript")


@app.get("/_sync/api/users")
def api_users():
    return jsonify({"users": list_users()})


@app.get("/_sync/api/pending")
def api_pending():
    return jsonify(pending_info(_req_user()))


@app.post("/_sync/api/sync")
def api_sync():
    mode = request.args.get("mode", "sync")
    if mode not in ("sync", "push", "pull", "baseline", "diff", "seed", "reset"):
        return jsonify({"error": "invalid mode"}), 400
    return jsonify(run_sync(_req_user(), mode, force=request.args.get("force") == "1"))


def _mask(key):
    return (key[:4] + "…") if key else ""


@app.get("/_sync/api/config")
def api_config_get():
    try:
        user = resolve_user(_req_user())
        cfg = user_cfg(user)
    except ApiError:
        user, cfg = None, {"server_url": "", "server_api_key": "",
                           "local_api_key": "", "diff_auto_limit": DIFF_AUTO_LIMIT}
    return jsonify({
        "user": user,
        "users": list_users(),
        "server_url": cfg["server_url"],
        "server_api_key_masked": _mask(cfg["server_api_key"]),
        "local_api_key_masked": _mask(cfg["local_api_key"]),
        "diff_auto_limit": cfg["diff_auto_limit"],
        "configured": bool(cfg["server_url"] and cfg["server_api_key"]),
    })


@app.post("/_sync/api/config")
def api_config_set():
    """Profil-Einstellungen speichern; leere Felder lassen den Wert unverändert.
    Nach dem Speichern werden beide Seiten testweise angefragt."""
    body = request.get_json(silent=True) or {}
    user = (_req_user() or str(body.get("user") or "").strip().upper())
    if not user:
        return jsonify({"saved": False, "error": "Profil (Rufzeichen) fehlt"}), 400
    full = load_config()
    profile = full.setdefault("users", {}).setdefault(user, {})
    for field in ("server_url", "server_api_key", "local_api_key", "diff_auto_limit"):
        val = (str(body.get(field)).strip() if body.get(field) is not None else "")
        if val:
            profile[field] = val.rstrip("/") if field == "server_url" else val
    # Lokalen API-Key automatisch anlegen (falls nicht manuell gesetzt) — der
    # User muss ihn nicht selbst erzeugen/eintragen.
    key_note = None
    if not profile.get("local_api_key"):
        try:
            profile["local_api_key"] = ensure_local_api_key(user)
            key_note = "lokaler API-Key automatisch erstellt"
        except (ApiError, Exception) as e:
            key_note = f"lokaler API-Key nicht automatisch erstellbar: {str(e)[:120]}"
    save_config(full)
    cfg = user_cfg(user)

    checks = {"key_note": key_note}
    try:
        checks["server"] = {"ok": True,
                            "version": get_version(cfg["server_url"],
                                                   cfg["server_api_key"])}
    except (requests.RequestException, ApiError) as e:
        checks["server"] = {"ok": False, "error": str(e)[:200]}
    try:
        n = len(get_stations(LOCAL_URL, cfg["local_api_key"]))
        checks["local"] = {"ok": True, "stations": n,
                           "version": get_version(LOCAL_URL, cfg["local_api_key"])}
    except (requests.RequestException, ApiError) as e:
        checks["local"] = {"ok": False, "error": str(e)[:200]}
    return jsonify({"saved": True, "user": user, "checks": checks})


def _nk(call, date, time4, band, mode):
    """Natuerlicher QSO-Schluessel — identisch zu qso_key(), aus Einzelfeldern."""
    return "|".join([(call or "").upper(), date or "", (time4 or "")[:4],
                     (band or "").lower(), (mode or "").upper()])


@app.get("/_sync/api/ping")
def api_ping():
    """Leichte Erreichbarkeitspruefung des Servers (nur ein version-Call) — fuer
    den Sync-Button oben, damit vor dem Sync klar ist, ob der Server da ist."""
    try:
        user = resolve_user(_req_user())
        cfg = user_cfg(user)
    except ApiError:
        return jsonify({"configured": False, "reachable": False})
    if not (cfg["server_url"] and cfg["server_api_key"]):
        return jsonify({"configured": False, "reachable": False})
    # bewusst OHNE die _request-Retry-Logik: kurzer Timeout, ein Versuch, damit ein
    # nicht erreichbarer Server schnell (statt nach ~50s Retries) gemeldet wird
    try:
        r = requests.post(f"{cfg['server_url']}/api/version",
                          json={"key": cfg["server_api_key"]}, timeout=5)
        ok = r.status_code == 200 and (r.json() or {}).get("status") == "ok"
        return jsonify({"configured": True, "reachable": bool(ok)})
    except (requests.RequestException, ValueError):
        return jsonify({"configured": True, "reachable": False})


@app.get("/_sync/api/qso_status")
def api_qso_status():
    """Sync-Status je lokaler QSO-ID (fuer die Einblendung in den QSO-Listen):
    synced | pending | changed | time_changed | deleted_remote. Basis: Sync-Marken
    (gepusht?) + letzter Abweichungs-Check (last_diff)."""
    try:
        user = resolve_user(_req_user())
    except ApiError:
        return jsonify({})
    ids = [int(x) for x in (request.args.get("ids") or "").split(",")
           if x.strip().isdigit()]
    if not ids:
        return jsonify({})
    ust = user_state(load_state(), user)
    push_marks = ust.get("push_marks", {})
    ld = ust.get("last_diff") or {}
    changed = {_nk(d["call"], d["date"], d["time"], d["band"], d["mode"])
               for d in ld.get("diffs", [])}
    time_ch = {_nk(d["call"], d["date"], d["time"], d["band"], d["mode"])
               for d in ld.get("time_edits", [])}
    only_local = {_nk(d["call"], d["date"], d["time"], d["band"], d["mode"])
                  for d in ld.get("only_local", [])}
    have_diff = bool(ld)

    out = {}
    try:
        conn = _local_db()
    except Exception:
        return jsonify({})
    qso_table = os.environ.get("QSO_TABLE", "TABLE_HRD_CONTACTS_V01")
    try:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT COL_PRIMARY_KEY, COL_CALL, "
                f"DATE_FORMAT(COL_TIME_ON,'%%Y%%m%%d'), DATE_FORMAT(COL_TIME_ON,'%%H%%i'), "
                f"COL_BAND, COL_MODE, station_id FROM {qso_table} "
                f"WHERE COL_PRIMARY_KEY IN ({ph})", ids)
            for pk, call, d, t, band, mode, sid in cur.fetchall():
                k = _nk(call, d, t, band, mode)
                pushed = int(pk) <= int(push_marks.get(str(sid), 0))
                if not pushed:
                    st = "pending"
                elif not have_diff:
                    st = "unknown"
                elif k in changed:
                    st = "changed"
                elif k in time_ch:
                    st = "time_changed"
                elif k in only_local:
                    st = "deleted_remote"
                else:
                    st = "synced"
                out[str(pk)] = st
    finally:
        conn.close()
    return jsonify(out)


@app.get("/_sync/api/diff")
def api_diff():
    try:
        user = resolve_user(_req_user())
    except ApiError:
        return jsonify({"last_diff": None, "journal": []})
    ust = user_state(load_state(), user)
    return jsonify({
        "last_diff": ust.get("last_diff"),
        "journal": ust.get("diff_journal", [])[-30:],
    })


@app.post("/_sync/api/contest_ack")
def api_contest_ack():
    with _lock:
        try:
            user = resolve_user(_req_user())
        except ApiError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        state = load_state()
        user_state(state, user)["contest_import_pending"] = []
        save_state(state)
    return jsonify({"ok": True})


_migrate_lock = threading.Lock()
MIGRATE_ATTEMPT_TIMEOUT = int(os.environ.get("MIGRATE_ATTEMPT_TIMEOUT", "120"))
MIGRATE_TOTAL_TIMEOUT = int(os.environ.get("MIGRATE_TOTAL_TIMEOUT", "1200"))


def _is_migrate(path):
    p = path.rstrip("/")
    return p.endswith("index.php/migrate") or p.endswith("/migrate") or p == "migrate"


def _proxy_migrate(upstream, headers):
    """Robuster Durchlauf des Erstinstallations-Migrate-Schritts.

    Problem: Auf manchen Setups (z. B. Docker Desktop unter Windows) reisst die
    HTTP-Verbindung waehrend der langen Migration ab; der Installer meldet dann
    "500", obwohl die Migration server-seitig weiterlaeuft. Wavelog schuetzt die
    Migration per Lockfile (/tmp/.migration_running) und schreibt die Version nach
    jedem Schritt fort — ein erneuter Aufruf migriert also NICHT parallel, sondern
    wartet bzw. liefert sofort "success", sobald alles durch ist. Daher hier:
    serialisiert (Lock) so lange wiederholen, bis migrate sauberes success liefert.
    """
    with _migrate_lock:
        deadline = time.monotonic() + MIGRATE_TOTAL_TIMEOUT
        last_status = None
        while time.monotonic() < deadline:
            try:
                r = requests.get(upstream, headers=headers,
                                 timeout=(10, MIGRATE_ATTEMPT_TIMEOUT),
                                 allow_redirects=False)
                last_status = r.status_code
                if "application/json" in r.headers.get("Content-Type", ""):
                    try:
                        if r.json().get("status") == "success":
                            return Response(r.content, status=200,
                                            mimetype="application/json")
                    except ValueError:
                        pass
                # non-success / HTML-Fehlerseite: Migration evtl. noch nicht fertig
                # oder transient gescheitert -> gleich erneut versuchen
            except requests.exceptions.RequestException:
                # Verbindung abgerissen: Migration laeuft server-seitig weiter
                last_status = "connection-lost"
            time.sleep(3)
        return Response(
            json.dumps({"status": "error",
                        "reason": f"migrate nicht abgeschlossen (letzter Stand: {last_status})"}),
            status=504, mimetype="application/json")


@app.route("/", defaults={"path": ""},
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.route("/<path:path>",
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def proxy(path):
    # Eigene Seite /offline-sync: Wavelogs Dashboard-Layout (Header/Menü/Footer)
    # ausliefern, Browser-URL bleibt /offline-sync. inject.js ersetzt dann den
    # Inhaltsbereich durch das Sync-UI -> sieht aus wie eine native Wavelog-Seite.
    if path.rstrip("/") in ("offline-sync", "offlinesync"):
        path = "index.php/dashboard"
    upstream = f"{LOCAL_URL}/{path}"
    headers = {k: v for k, v in request.headers if k.lower() not in HOP_HEADERS}
    headers["Accept-Encoding"] = "identity"
    headers["Host"] = request.host
    if request.method == "GET" and _is_migrate(path):
        return _proxy_migrate(upstream, headers)
    # Read-Timeout bewusst unbegrenzt: auch andere Wavelog-Requests koennen legitim
    # laenger dauern (z. B. der DXCC-Import in der Erstinstallation). Der besonders
    # heikle Migrate-Schritt wird oben separat behandelt. Connect bleibt kurz (lokal).
    try:
        resp = requests.request(
            request.method, upstream, params=request.args.to_dict(flat=False),
            headers=headers, data=request.get_data(), allow_redirects=False,
            timeout=(10, None),
        )
    except requests.exceptions.RequestException as e:
        return Response(
            "Offline-Kit-Proxy: keine Antwort von der lokalen Wavelog-Instanz "
            f"({e.__class__.__name__}). Läuft evtl. noch eine lange Operation? "
            "Seite in einem Moment neu laden.",
            status=504, mimetype="text/plain",
        )
    body = resp.content
    if "text/html" in resp.headers.get("Content-Type", "") and b"</head>" in body:
        body = body.replace(b"</head>", INJECT_TAG + b"</head>", 1)
    out_headers = []
    # resp.raw.headers statt resp.headers: mehrfach vorhandene Header (z. B.
    # mehrere Set-Cookie) muessen einzeln durchgereicht werden, sonst
    # verschmilzt requests sie kommagetrennt und der Browser verwirft Cookies
    for k, v in resp.raw.headers.items():
        if k.lower() in HOP_HEADERS:
            continue
        if k.lower() == "location" and v.startswith(LOCAL_URL):
            # Sicherheitsnetz: Upstream-interne Redirects auf den Proxy umbiegen
            v = f"{request.scheme}://{request.host}" + v[len(LOCAL_URL):]
        out_headers.append((k, v))
    return Response(body, status=resp.status_code, headers=out_headers)


# ------------------------------------------------------------------- cli ---

def _cli_user():
    if "--user" in sys.argv:
        i = sys.argv.index("--user")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    user = _cli_user()
    if cmd == "serve":
        from waitress import serve
        print(f"Sync-Sidecar auf :{LISTEN_PORT}, Upstream {LOCAL_URL}", flush=True)
        # channel_timeout grosszuegig: lange durchgereichte Requests (Installer-
        # Migration, DXCC-Import) sollen nicht von waitress selbst gekappt werden
        serve(app, host="0.0.0.0", port=LISTEN_PORT, threads=8,
              channel_timeout=1800)
    elif cmd == "version":
        try:
            u = resolve_user(user)
            cfg = user_cfg(u)
        except ApiError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        out = {"user": u, "local": None, "server": None}
        try:
            out["local"] = get_version(LOCAL_URL, cfg["local_api_key"])
        except (requests.RequestException, ApiError) as e:
            out["local_error"] = str(e)
        try:
            out["server"] = get_version(cfg["server_url"], cfg["server_api_key"])
        except (requests.RequestException, ApiError) as e:
            out["server_error"] = str(e)
        print(json.dumps(out))
        sys.exit(0 if out["local"] and out["local"] == out["server"] else 1)
    elif cmd in ("push", "pull", "sync", "baseline", "seed", "diff", "reset"):
        # ohne --user: alle Profile nacheinander
        targets = [user] if user else (list_users() or [None])
        results = [run_sync(t, cmd, force="--force" in sys.argv) for t in targets]
        print(json.dumps(results if len(results) > 1 else results[0],
                         indent=2, ensure_ascii=False))
        sys.exit(0 if all(r.get("ok") for r in results) else 1)
    elif cmd == "status":
        if user or len(list_users()) <= 1:
            info = pending_info(user)
            print(json.dumps(info, indent=2, ensure_ascii=False))
            sys.exit(0 if info.get("local_ok") else 1)
        infos = {u: pending_info(u) for u in list_users()}
        print(json.dumps(infos, indent=2, ensure_ascii=False))
        sys.exit(0 if any(i.get("local_ok") for i in infos.values()) else 1)
    elif cmd == "has-pending":  # Exit 1 wenn irgendwo ungepushte Daten
        total = 0
        for u in list_users():
            info = pending_info(u)
            total += info["pending_qsos"] + info["stations_unsynced"]
        print(total)
        sys.exit(1 if total else 0)
    else:
        print(f"Unbekanntes Kommando: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
