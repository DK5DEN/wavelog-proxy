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
import secrets
import socket
import string
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

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


def _clean_server_url(url):
    """Server-URL normalisieren: haeufige Kopierfehler wie angehaengtes
    /index.php, /api oder /api/v2 (z. B. aus der API-Doku oder der Token-
    Seite kopiert) entfernen — die API-Pfade baut der Sync selbst an.
    Sonst entstehen Pfade wie /index.php/api/v2/index.php/api/v2/station,
    die der v2-Dispatcher mit 404 'Unknown resource path' beantwortet."""
    url = (url or "").strip().rstrip("/")
    return re.sub(r"/(?:index\.php(?:/api(?:/v2)?)?|api(?:/v2)?)$", "", url)


def user_cfg(user):
    """Einstellungen eines Profils (Server-URL, Tokens)."""
    u = (load_config().get("users") or {}).get(user, {})
    server_key = u.get("server_api_key", "")
    return {
        "server_url": _clean_server_url(u.get("server_url")),
        "server_api_key": server_key,
        # optionaler Legacy-v1-Key, ausschliesslich fuer den Versionsabgleich
        "server_v1_key": u.get("server_v1_key", ""),
        "local_api_key": u.get("local_api_key") or server_key,
        "diff_auto_limit": int(u.get("diff_auto_limit") or DIFF_AUTO_LIMIT),
        # Server-Stations-uuids, die NICHT gesynct werden (Ausschlussliste:
        # neue Stationen am Server syncen damit automatisch mit)
        "station_exclude": list(u.get("station_exclude") or []),
        # Feature-Auswahl: was der Sync zusaetzlich zur Basis (QSOs +
        # Stationen) macht — bestimmt auch die noetigen Token-Scopes
        "sync_edits": bool(u.get("sync_edits", True)),
        "sync_deletions": bool(u.get("sync_deletions", True)),
        "sync_qsl": bool(u.get("sync_qsl", True)),
        "sync_version": bool(u.get("sync_version", True)),
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
        "uuid_pairs": {},           # lokale station_uuid -> Server station_uuid
                                    # (noetig fuer via APIv2 angelegte Stationen)
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


# Der Sync laeuft komplett ueber die APIv2 (Wavelog >= 3.1: REST, Bearer-
# Token mit Praefix wl2_, Scopes qso:read/qso:write/station:read/
# station:write, optional statistic:read). Aeltere Server werden nicht mehr
# unterstuetzt. Einzige verbliebene v1-Nutzung: der optionale Legacy-Key
# fuer den Versionsabgleich, weil APIv2 die Version nur Admins verraet
# (system-Statistik).
V2_PREFIX = "wl2_"
V2_SCOPES = ("qso:read,qso:write,qso:delete,station:read,station:write,"
             "statistic:read,confirmation:read")


def _v2(key):
    return (key or "").startswith(V2_PREFIX)


def _v2_url(base, resource):
    # index.php immer mitschicken — laut Doku die kompatibelste Form
    return f"{base}/index.php/api/v2/{resource}"


def _v2_hdr(key):
    return {"Authorization": f"Bearer {key}"}


def _station_from_v2(s):
    """APIv2-Stationsobjekt auf die Legacy-Feldnamen des Sync abbilden."""
    return {
        "station_id": s.get("id"),
        "station_uuid": s.get("uuid"),
        "station_profile_name": s.get("name"),
        "station_callsign": s.get("callsign"),
        "station_gridsquare": s.get("gridsquare"),
        "station_city": s.get("city"),
        "station_dxcc": s.get("dxcc"),
        "station_cnty": s.get("cnty"),
        "station_cq": s.get("cq"),
        "station_itu": s.get("itu"),
        "station_state": s.get("state"),
        "station_iota": s.get("iota"),
        "station_sota": s.get("sota"),
        "station_wwff": s.get("wwff"),
        "station_pota": s.get("pota"),
        "station_sig": s.get("sig"),
        "station_sig_info": s.get("sig_info"),
    }


def get_version(base, key, v1_key=None):
    """Wavelog-Version der Instanz. APIv2 verraet sie nur Admins (system-
    Statistik); optional kann dafuer ein Legacy-v1-Key hinterlegt werden —
    der einzige verbliebene v1-Einsatz im Kit."""
    if _v2(key):
        try:
            r = _request("GET", _v2_url(base, "statistic"),
                         params={"profile": "system"}, headers=_v2_hdr(key),
                         timeout=30)
            data = _check(r)
            v = ((data.get("data") or {}).get("system") or {}).get("wavelog")
            if v:
                return str(v)
            raise ApiError("APIv2: Version nicht in der Antwort")
        except ApiError:
            if not v1_key:
                raise
            key = v1_key  # Fallback: Legacy-Key nur fuer den Versionsabgleich
    r = _request("POST", f"{base}/api/version", json={"key": key}, timeout=30)
    data = _check(r)
    if data.get("status") != "ok":
        raise ApiError(f"version failed: {data}")
    return str(data.get("version"))


def get_stations(base, key):
    r = _request("GET", _v2_url(base, "station"), headers=_v2_hdr(key),
                 timeout=60)
    data = _check(r)
    return [_station_from_v2(s) for s in (data.get("data") or [])]


def get_contacts(base, key, station_id, fetchfromid, limit=PULL_LIMIT, fmt="adif"):
    # since_id lauft den Primaerschluessel ab, die Antwort meldet
    # lastfetchedid — gleicher Delta-Kontrakt wie frueher bei v1
    r = _request("GET", _v2_url(base, "qso"),
                 params={"format": "adif", "since_id": int(fetchfromid),
                         "station_id": int(station_id),
                         "per_page": min(int(limit), 5000)},
                 headers=_v2_hdr(key), timeout=300)
    d = (_check(r).get("data") or {})
    out = {"exported_qsos": int(d.get("exported") or 0),
           "lastfetchedid": int(d.get("lastfetchedid") or fetchfromid),
           "adif": d.get("adif")}
    if fmt != "adif":
        out["qsos"] = [{"CALL": x.get("call"), "QSO_DATE": x.get("qso_date"),
                        "TIME_ON": x.get("time_on"), "BAND": x.get("band"),
                        "MODE": x.get("mode"),
                        "CONTEST_ID": x.get("contest_id")}
                       for x in parse_adif(out["adif"] or "")]
    return out


def fetch_id_map(base, key, station_id):
    """Natuerlicher QSO-Schluessel -> QSO-id (v2 JSON-Liste, seitenweise).
    Fuer Edit-/Loesch-Sync noetig: das ADIF traegt keine ids, die JSON-Liste
    schon. Schluessel identisch zu qso_key()."""
    out, page = {}, 1
    while True:
        r = _request("GET", _v2_url(base, "qso"),
                     params={"station_id": int(station_id), "page": page,
                             "per_page": 5000},
                     headers=_v2_hdr(key), timeout=300)
        env = _check(r)
        for q in env.get("data") or []:
            dt = str(q.get("qso_date") or "")
            out[_nk(q.get("call"), dt[:10].replace("-", ""),
                    dt[11:16].replace(":", ""), q.get("band"),
                    q.get("mode"))] = q["id"]
        if not (env.get("meta") or {}).get("has_more"):
            return out
        page += 1


def patch_qso(base, key, qso_id, fields):
    """Einzelne QSO-Felder aendern (Edit-Sync)."""
    r = _request("PATCH", _v2_url(base, f"qso/{int(qso_id)}"), json=fields,
                 headers=_v2_hdr(key), timeout=60)
    return _check(r)


def delete_qso(base, key, qso_id):
    """QSO endgueltig loeschen (voller Teardown wie im UI). 404 = war schon
    weg — kein Fehler."""
    r = _request("DELETE", _v2_url(base, f"qso/{int(qso_id)}"),
                 headers=_v2_hdr(key), timeout=60)
    if r.status_code not in (200, 204, 404):
        raise ApiError(f"delete failed: HTTP {r.status_code}: {r.text[:200]}")
    return r.status_code != 404


def get_confirmations(base, key, since=None):
    """QSL-Bestaetigungen (LoTW/eQSL/QSL/QRZ/Clublog), optional inkrementell
    ab Eingangsdatum (since, inklusiv)."""
    out, page = [], 1
    while True:
        params = {"page": page, "per_page": 1000}
        if since:
            params["since"] = since
        r = _request("GET", _v2_url(base, "confirmation"), params=params,
                     headers=_v2_hdr(key), timeout=300)
        env = _check(r)
        out.extend(env.get("data") or [])
        if not (env.get("meta") or {}).get("has_more"):
            return out
        page += 1


def push_adif(base, key, station_profile_id, adif):
    """ADIF-Bulk-Import; Dupes meldet die APIv2 sauber als "skipped"."""
    r = _request("POST", _v2_url(base, "qso"),
                 json={"import_type": "adif",
                       "station_profile_id": int(station_profile_id),
                       "adif": adif},
                 headers=_v2_hdr(key), timeout=300)
    try:
        data = r.json()
    except ValueError:
        raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}")
    d = (data or {}).get("data") or {}
    if r.status_code in (200, 201):
        return {"status": "created", "dupes": int(d.get("skipped") or 0)}
    raise ApiError(f"qso push failed: HTTP {r.status_code}: {str(data)[:300]}")


def create_station(base, key, st):
    """Station per APIv2 anlegen. ACHTUNG: die uuid vergibt der Server immer
    NEU (kein Durchreichen) und die Station wird nicht ins aktive Logbuch
    verknuepft — der Aufrufer kuemmert sich (uuid_pairs bzw. lokaler
    DB-Fixup)."""
    body = {
        "name": st.get("station_profile_name") or "",
        "callsign": st.get("station_callsign") or "",
        "gridsquare": st.get("station_gridsquare") or "",
        "city": st.get("station_city") or "",
        "iota": st.get("station_iota") or "",
        "sota": st.get("station_sota") or "",
        "wwff": st.get("station_wwff") or "",
        "pota": st.get("station_pota") or "",
        "sig": st.get("station_sig") or "",
        "sig_info": st.get("station_sig_info") or "",
        "dxcc": int(st.get("station_dxcc") or 0),
        "cnty": st.get("station_cnty") or "",
        "cq": int(st.get("station_cq") or 0),
        "itu": int(st.get("station_itu") or 0),
        "state": st.get("station_state") or "",
    }
    if not (body["dxcc"] and body["cq"] and body["itu"]):
        # APIv2 verlangt dxcc/cq/itu > 0 (PHP empty()). Stationen aus der
        # einfachen Installation haben 0 — aus den lokalen DXCC-Tabellen
        # nachschlagen (statische Daten, fuer beide Seiten gueltig)
        try:
            adif_id, cqz, ituz = _dxcc_lookup_local(body["callsign"])
            body["dxcc"] = body["dxcc"] or adif_id
            body["cq"] = body["cq"] or cqz
            body["itu"] = body["itu"] or ituz
        except Exception:
            pass
    r = _request("POST", _v2_url(base, "station"), json=body,
                 headers=_v2_hdr(key), timeout=60)
    data = _check(r)
    return _station_from_v2(data.get("data") or {})


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


# Felder, die die APIv2 per PATCH aendern kann — nur diese werden beim
# Edit-Sync automatisch uebertragen. Bewusst nicht dabei (API kann sie nicht
# schreiben): qslmsg, submode, contest_id, operator.
PATCH_FIELDS = {
    "rst_sent", "rst_rcvd", "name", "comment", "notes", "gridsquare", "qth",
    "stx", "srx", "stx_string", "srx_string", "tx_pwr",
    "sota_ref", "pota_ref", "wwff_ref", "iota", "sat_name", "prop_mode",
}


def do_diff(cfg, ust, state, log):
    """Beide Seiten voll vergleichen. Feld-Aenderungen mit eindeutiger
    Richtung (aus den Snapshots) werden per PATCH auf die andere Seite
    uebertragen (Edit-Sync); Konflikte, unklare Richtung und nicht per API
    aenderbare Felder bleiben zur manuellen Entscheidung. Einseitig fehlende
    QSOs mit bekanntem Snapshot gelten als drueben geloescht und werden als
    Loesch-Kandidaten gesammelt — geloescht wird erst nach Bestaetigung im
    Panel (apply_deletions)."""
    sync_stations(cfg, ust, log)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snaps = ust.setdefault("diff_snapshots", {})
    journal = ust.setdefault("diff_journal", [])
    names = {str(s["station_id"]): s["station_profile_name"]
             for s in get_stations(LOCAL_URL, cfg["local_api_key"])}
    diffs, only_local, only_server, total = [], [], [], 0
    deletions = []
    edits_up = edits_down = 0

    for lid, sid in ust["station_map"].items():
        station = names.get(str(lid), str(lid))
        lrecs = {qso_key(r): r
                 for r in fetch_all_records(LOCAL_URL, cfg["local_api_key"], lid)}
        srecs = {qso_key(r): r
                 for r in fetch_all_records(cfg["server_url"], cfg["server_api_key"], sid)}
        lids = fetch_id_map(LOCAL_URL, cfg["local_api_key"], lid)
        sids = fetch_id_map(cfg["server_url"], cfg["server_api_key"], sid)
        total += len(srecs)

        for k, lr in lrecs.items():
            if k not in srecs:
                d = _describe(lr, station)
                d["_key"], d["_id"] = k, lids.get(k)
                only_local.append(d)
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
                # Edit-Sync: eindeutige Richtung -> PATCH auf die Gegenseite
                auto = {f for f in changed if f in PATCH_FIELDS}
                rest = {f: v for f, v in changed.items() if f not in PATCH_FIELDS}
                applied = False
                if not cfg.get("sync_edits", True):
                    auto = set()  # Edit-Sync abgewählt -> nur anzeigen
                try:
                    if auto and l_edit and not s_edit and sids.get(k):
                        patch_qso(cfg["server_url"], cfg["server_api_key"],
                                  sids[k], {f: (lr.get(f) or "") for f in auto})
                        edits_up += len(auto)
                        applied = True
                    elif auto and s_edit and not l_edit and lids.get(k):
                        patch_qso(LOCAL_URL, cfg["local_api_key"],
                                  lids[k], {f: (sr.get(f) or "") for f in auto})
                        edits_down += len(auto)
                        applied = True
                except (requests.RequestException, ApiError) as e:
                    applied = False
                    log.append(f"Edit-Übertragung fehlgeschlagen "
                               f"({lr.get('call')}): {str(e)[:120]}")
                show = rest if applied else changed
                if show:
                    diffs.append({**_describe(lr, station), "fields": show,
                                  "edited": side + (" — Rest nicht per API "
                                  "änderbar" if applied and rest else "")})
                journal.append({"ts": now, "qso": _describe(lr, station),
                                "lokal_geaendert": l_edit,
                                "server_geaendert": s_edit,
                                "auto_uebertragen": applied})
                if applied and not rest:
                    # konvergiert — Snapshot auf den gemeinsamen Stand setzen
                    h = lh if l_edit else sh
                    snaps[k] = {"local": h, "server": h}
                    continue
            elif l_edit or s_edit:
                journal.append({"ts": now, "qso": _describe(lr, station),
                                "lokal_geaendert": l_edit,
                                "server_geaendert": s_edit})
            snaps[k] = {"local": lh, "server": sh}

        for k, sr in srecs.items():
            if k not in lrecs:
                d = _describe(sr, station)
                d["_key"], d["_id"] = k, sids.get(k)
                only_server.append(d)

    time_edits, only_local, only_server = _pair_time_edits(only_local, only_server)

    # Loesch-Kandidaten: einseitig fehlend UND Snapshot vorhanden = das QSO
    # war schon beidseitig bekannt -> auf der anderen Seite geloescht.
    # Bei abgewaehltem Loesch-Sync bleiben die Eintraege in den Nur-Listen.
    collect_deletions = cfg.get("sync_deletions", True)

    def _split(entries, action):
        rest = []
        for d in entries:
            if collect_deletions and d.get("_key") in snaps and d.get("_id"):
                deletions.append({"action": action, "id": d["_id"],
                                  "key": d["_key"], "station": d["station"],
                                  "call": d["call"], "date": d["date"],
                                  "time": d["time"], "band": d["band"],
                                  "mode": d["mode"]})
            else:
                rest.append(d)
        for d in rest:
            d.pop("_key", None)
            d.pop("_id", None)
        return rest

    only_local = _split(only_local, "delete_local")
    only_server = _split(only_server, "delete_server")
    for d in time_edits:
        d.pop("_key", None)
        d.pop("_id", None)

    del journal[:-200]
    ust["last_diff"] = {
        "ts": now, "total_qsos": total, "diffs": diffs,
        "time_edits": time_edits, "deletions": deletions,
        "only_local": only_local, "only_server": only_server,
    }
    save_state(state)
    if edits_up or edits_down:
        log.append(f"Änderungen übertragen: {edits_up} Feld(er) -> Server, "
                   f"{edits_down} Feld(er) -> lokal")
    if deletions:
        log.append(f"{len(deletions)} Löschung(en) erkannt — Bestätigung im "
                   "Panel unter Abweichungen nötig")
    if diffs or time_edits or only_local or only_server:
        log.append(f"Abweichungen: {len(diffs)} Feld-Diff(s), "
                   f"{len(time_edits)} Zeit-Änderung(en), "
                   f"{len(only_local)} nur lokal, {len(only_server)} nur Server")
    else:
        log.append("Abweichungs-Check: beide Seiten identisch")
    return len(diffs) + len(time_edits)


# ------------------------------------------------------------- sync core ---

def sync_stations(cfg, ust, log):
    """Gleicht Stationen in beide Richtungen ab (Matching via station_uuid).

    APIv2-Besonderheit: beim Anlegen ueber v2 vergibt der Server immer eine
    NEUE uuid (v1 uebernimmt die mitgeschickte). Solche Gegenstuecke werden
    als Paar in ust["uuid_pairs"] (lokale uuid -> Server-uuid) gemerkt und
    beim Matching wie identische uuids behandelt."""
    local = get_stations(LOCAL_URL, cfg["local_api_key"])
    server = get_stations(cfg["server_url"], cfg["server_api_key"])
    l_by_uuid = {s["station_uuid"]: s for s in local if s.get("station_uuid")}
    s_by_uuid = {s["station_uuid"]: s for s in server if s.get("station_uuid")}
    pairs = ust.setdefault("uuid_pairs", {})

    # Stationsauswahl: ausgeschlossene Server-Stationen (station_exclude)
    # werden weder gespiegelt noch gesynct; ein bereits vorhandener lokaler
    # Spiegel wird samt QSOs entfernt (lokal ist Wegwerf-Spiegel, der
    # Server bleibt unberuehrt).
    excl = set(cfg.get("station_exclude") or [])
    if excl:
        doomed = {u: st for u, st in l_by_uuid.items() if pairs.get(u, u) in excl}
        if doomed:
            ids = [int(st["station_id"]) for st in doomed.values()]
            counts = reset_local_db(ids)
            log.append(f"{len(ids)} vom Sync ausgeschlossene Station(en) lokal "
                       f"entfernt ({counts['qsos']} QSO(s))")
            for u, st in doomed.items():
                l_by_uuid.pop(u, None)
                ust.get("push_marks", {}).pop(str(st["station_id"]), None)
                pairs.pop(u, None)
        for u in excl:
            srv = s_by_uuid.pop(u, None)
            if srv:
                ust.get("pull_marks", {}).pop(str(srv["station_id"]), None)

    def _same_station(cands, st):
        """Gegenstueck per Name+Rufzeichen finden (fuer 409 'identical
        station': gleiche Station existiert schon, nur mit anderer uuid —
        z. B. beidseitig identisch angelegte Erstinstallations-Station)."""
        for s in cands.values():
            if (s.get("station_profile_name") == st.get("station_profile_name")
                    and (s.get("station_callsign") or "")
                    == (st.get("station_callsign") or "")):
                return s
        return None

    created_up = created_down = 0
    for uuid, st in l_by_uuid.items():
        if pairs.get(uuid, uuid) in s_by_uuid:
            continue
        try:
            res = create_station(cfg["server_url"], cfg["server_api_key"], st)
        except ApiError as e:
            m = _same_station(s_by_uuid, st) if "identical station" in str(e).lower() else None
            if not m:
                raise
            pairs[uuid] = m["station_uuid"]
            log.append(f"Station '{st['station_profile_name']}' existiert "
                       "bereits identisch am Server — verknüpft")
            continue
        new_uuid = (res or {}).get("station_uuid")
        if new_uuid and new_uuid != uuid:
            pairs[uuid] = new_uuid
        log.append(f"Station '{st['station_profile_name']}' -> Server angelegt "
                   "(bei Bedarf am Server ins Logbuch verknüpfen)")
        created_up += 1
    known_server = {pairs.get(u, u) for u in l_by_uuid} | set(pairs.values())
    for uuid, st in s_by_uuid.items():
        if uuid in known_server:
            continue
        try:
            res = create_station(LOCAL_URL, cfg["local_api_key"], st)
        except ApiError as e:
            m = _same_station(l_by_uuid, st) if "identical station" in str(e).lower() else None
            if not m:
                raise
            pairs[m["station_uuid"]] = uuid
            log.append(f"Station '{st['station_profile_name']}' existiert "
                       "bereits identisch lokal — verknüpft")
            continue
        # Lokal per DB nachziehen, was die APIv2 nicht kann: Server-uuid
        # uebernehmen (haelt das Matching trivial) und ins aktive Logbuch
        # verknuepfen (sonst waeren die QSOs im lokalen UI unsichtbar)
        try:
            _localize_created_station(int(res["station_id"]), uuid)
        except Exception as e:
            if res.get("station_uuid"):
                pairs[res["station_uuid"]] = uuid  # Fallback: Paar merken
            log.append(f"Hinweis: lokaler Station-Fixup fehlgeschlagen ({e})")
        created_down += 1
        log.append(f"Station '{st['station_profile_name']}' -> lokal angelegt")

    if created_up or created_down:
        local = get_stations(LOCAL_URL, cfg["local_api_key"])
        server = get_stations(cfg["server_url"], cfg["server_api_key"])
        l_by_uuid = {s["station_uuid"]: s for s in local if s.get("station_uuid")}
        s_by_uuid = {s["station_uuid"]: s for s in server if s.get("station_uuid")}

    ust["station_map"] = {
        str(st["station_id"]): int(s_by_uuid[pairs.get(u, u)]["station_id"])
        for u, st in l_by_uuid.items()
        if pairs.get(u, u) in s_by_uuid
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
            # Contest-Erinnerung nur, wenn der Batch wirklich Neues enthielt —
            # reine Dupe-Batches (z. B. zurueckgespielte Pull-QSOs) stammen
            # vom Server und brauchen dort keinen Contest-Import
            if int(r["exported_qsos"]) > dupes:
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


# ------------------------------------------------------------- qsl pull ---

QSL_COLUMNS = {
    "lotw": ("COL_LOTW_QSL_RCVD", "COL_LOTW_QSLRDATE"),
    "eqsl": ("COL_EQSL_QSL_RCVD", "COL_EQSL_QSLRDATE"),
    "qsl": ("COL_QSL_RCVD", "COL_QSLRDATE"),
    "qrz": ("COL_QRZCOM_QSO_DOWNLOAD_STATUS", "COL_QRZCOM_QSO_DOWNLOAD_DATE"),
    "clublog": ("COL_CLUBLOG_QSO_DOWNLOAD_STATUS",
                "COL_CLUBLOG_QSO_DOWNLOAD_DATE"),
}


def _qsl_type_key(label):
    """API-Label ('LoTW', 'eQSL', 'QSL Card', 'QRZ.com', 'Clublog') auf den
    Spalten-Schluessel abbilden."""
    t = (label or "").lower()
    for k in ("lotw", "eqsl", "clublog", "qrz"):
        if k in t:
            return k
    return "qsl" if "qsl" in t else None


def do_qsl_pull(cfg, ust, state, log):
    """QSL-Bestaetigungen vom Server holen und in die lokalen QSO-Zeilen
    schreiben (Matching per Call + Minute + Band + Mode). Nur lokal per DB —
    die APIv2 laesst Confirmation-Buchhaltung bewusst nicht schreiben, der
    Server bleibt Master. Inkrementell ueber das Eingangsdatum (qsl_since)."""
    confs = get_confirmations(cfg["server_url"], cfg["server_api_key"],
                              ust.get("qsl_since"))
    if not confs:
        return 0
    qso_table = os.environ.get("QSO_TABLE", "TABLE_HRD_CONTACTS_V01")
    applied = 0
    latest = ust.get("qsl_since") or ""
    conn = _local_db()
    try:
        with conn.cursor() as cur:
            for c in confs:
                cols = QSL_COLUMNS.get(_qsl_type_key(c.get("type")) or "")
                dt = str(c.get("qso_date") or "")
                if not cols or len(dt) < 16:
                    continue
                minute = dt[:16] + ":00"
                try:
                    applied += cur.execute(
                        f"UPDATE {qso_table} SET {cols[0]}='Y', {cols[1]}=%s "
                        f"WHERE COL_CALL=%s AND LOWER(COL_BAND)=LOWER(%s) "
                        f"AND COL_TIME_ON >= %s "
                        f"AND COL_TIME_ON < %s + INTERVAL 1 MINUTE "
                        f"AND (COL_MODE=%s OR COL_SUBMODE=%s) "
                        f"AND ({cols[0]} IS NULL OR {cols[0]} <> 'Y')",
                        (c.get("confirmation_date"), c.get("callsign"),
                         c.get("band") or "", minute, minute,
                         c.get("mode"), c.get("mode")))
                except Exception:
                    continue  # z. B. Spalte fehlt — Typ ueberspringen
                cd = str(c.get("confirmation_date") or "")
                if cd > latest:
                    latest = cd
        conn.commit()
    finally:
        conn.close()
    if latest:
        ust["qsl_since"] = latest
        save_state(state)
    if applied:
        log.append(f"{applied} QSL-Bestätigung(en) übernommen (LoTW/eQSL/…)")
    return applied


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
    """Legt ein lokales APIv2-Token fuer den User (Rufzeichen) direkt in der
    lokalen Wavelog-DB an und gibt den Klartext zurueck — damit der User es
    nicht von Hand erzeugen/eintragen muss. In der DB liegt nur der SHA-256-
    Hash (Tabelle api_token); ein frueheres Kit-Token wird ersetzt, weil der
    Klartext nicht rekonstruierbar ist. Rein lokal."""
    import hashlib
    token = V2_PREFIX + secrets.token_hex(20)
    conn = _local_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM users WHERE UPPER(user_callsign)=UPPER(%s) LIMIT 1",
                (callsign,))
            row = cur.fetchone()
            if not row:
                raise ApiError(f"Lokaler Benutzer '{callsign}' nicht gefunden — "
                               "bitte lokales APIv2-Token manuell eintragen")
            uid = row[0]
            cur.execute("DELETE FROM api_token WHERE user_id=%s AND token_name=%s",
                        (uid, LOCAL_KEY_DESC))
            cur.execute(
                "INSERT INTO api_token (user_id, created_by, token_name, "
                "token_hash, scopes, status, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, 'active', NULL)",
                (uid, uid, LOCAL_KEY_DESC,
                 hashlib.sha256(token.encode()).hexdigest(), V2_SCOPES))
        conn.commit()
        return token
    finally:
        conn.close()


LOCAL_KEY_DESC = "Offline-Kit (auto)"


def _localize_created_station(station_id, server_uuid):
    """NUR lokal, direkt nach create_station(): die Server-uuid uebernehmen
    und die Station ins aktive Logbuch des Besitzers verknuepfen — beides
    macht die APIv2 beim Anlegen nicht (uuid wird neu vergeben, kein
    Logbuch-Link). Der Server wird nie per DB angefasst."""
    conn = _local_db()
    try:
        with conn.cursor() as cur:
            if server_uuid:
                cur.execute("UPDATE station_profile SET station_uuid=%s "
                            "WHERE station_id=%s", (server_uuid, station_id))
            cur.execute("SELECT user_id FROM station_profile WHERE station_id=%s",
                        (station_id,))
            row = cur.fetchone()
            if row:
                cur.execute("SELECT active_station_logbook FROM users "
                            "WHERE user_id=%s", (row[0],))
                lb = (cur.fetchone() or [None])[0]
                if lb:
                    cur.execute(
                        "SELECT 1 FROM station_logbooks_relationship "
                        "WHERE station_logbook_id=%s AND station_location_id=%s",
                        (lb, station_id))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO station_logbooks_relationship "
                            "(station_logbook_id, station_location_id) "
                            "VALUES (%s, %s)", (lb, station_id))
        conn.commit()
    finally:
        conn.close()


def _dxcc_lookup_local(callsign):
    """DXCC-Entity/CQ-/ITU-Zone eines Rufzeichens aus den lokalen Wavelog-
    DXCC-Tabellen (Exceptions exakt, sonst laengster Praefix-Treffer). Die
    Daten sind statisch und instanzunabhaengig — taugt daher auch, um
    Stationsdaten fuer den Server zu vervollstaendigen. (0, 0, 0) wenn
    nichts gefunden (z. B. DXCC-Import noch nicht gelaufen)."""
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return 0, 0, 0
    conn = _local_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT x.adif, x.cqz, e.ituz FROM dxcc_exceptions x "
                "JOIN dxcc_entities e ON e.adif = x.adif "
                "WHERE x.`call` = %s "
                "AND (x.`start` IS NULL OR x.`start` <= CURDATE()) "
                "AND (x.`end` IS NULL OR x.`end` >= CURDATE()) LIMIT 1",
                (callsign,))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT p.adif, p.cqz, e.ituz FROM dxcc_prefixes p "
                    "JOIN dxcc_entities e ON e.adif = p.adif "
                    "WHERE %s LIKE CONCAT(p.`call`, '%%') "
                    "AND (p.`start` IS NULL OR p.`start` <= CURDATE()) "
                    "AND (p.`end` IS NULL OR p.`end` >= CURDATE()) "
                    "ORDER BY LENGTH(p.`call`) DESC LIMIT 1", (callsign,))
                row = cur.fetchone()
            if row:
                return int(row[0]), int(row[1]), int(row[2])
    finally:
        conn.close()
    return 0, 0, 0


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


# ------------------------------------------------------------ autoinstall ---

# Platzhalter fuer die einfache Installation — jederzeit im Wavelog-Account
# aenderbar. Fachlich unkritisch: QSO-Zeiten sind UTC, echte Stationsdaten
# kommen beim Seed vom Server.
AUTOINSTALL_LOCATOR = "JO50AA"
AUTOINSTALL_TIMEZONE = "102"      # (GMT+01:00) Amsterdam, Berlin, ...
AUTOINSTALL_LANGUAGE = "german"


def _wait(fn, timeout, what):
    deadline = time.monotonic() + timeout
    err = None
    while time.monotonic() < deadline:
        try:
            return fn()
        except Exception as e:
            err = e
            time.sleep(2)
    raise ApiError(f"{what} (letzter Fehler: {err})")


# ---- Versionswahl: wavelog-Container gegen andere Image-Version tauschen ----

WAVELOG_IMAGE = "ghcr.io/wavelog/wavelog"
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.\-]*$")


def _docker():
    import docker
    return docker.from_env()


def _wavelog_container(cli):
    """wavelog-Container desselben Compose-Projekts finden (via Labels des
    eigenen sync-Containers, Hostname = Container-ID)."""
    me = cli.containers.get(socket.gethostname())
    project = me.labels.get("com.docker.compose.project")
    for c in cli.containers.list(all=True):
        if (c.labels.get("com.docker.compose.project") == project
                and c.labels.get("com.docker.compose.service") == "wavelog"):
            return c
    raise ApiError("wavelog-Container nicht gefunden")


def wavelog_current_version():
    """Version des laufenden wavelog-Containers (Image-Tag); Fallback .env."""
    try:
        c = _wavelog_container(_docker())
        return c.attrs["Config"]["Image"].rsplit(":", 1)[-1]
    except Exception:
        return os.environ.get("WAVELOG_VERSION") or None


def _set_env_file(key, value):
    """Eintrag in der gemounteten Kit-.env setzen/ersetzen, damit spaetere
    docker-compose-Laeufe (setup.sh/ps1) dieselbe Version verwenden."""
    path = os.environ.get("ENV_FILE", "/kit/.env")
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    out, found = [], False
    for ln in lines:
        if ln.startswith(key + "="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def switch_wavelog_version(version, log):
    """wavelog-Container durch einen mit der gewuenschten Image-Version
    ersetzen (Name, Mounts, Env, Netz-Aliases und Labels bleiben erhalten,
    damit docker compose ihn weiter als 'wavelog' verwaltet)."""
    try:
        cli = _docker()
        old = _wavelog_container(cli)
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("Docker nicht erreichbar — Versionswechsel nicht "
                       f"möglich ({e})")
    if old.attrs["Config"]["Image"].rsplit(":", 1)[-1] == version:
        return
    try:
        cli.images.pull(WAVELOG_IMAGE, tag=version)
    except Exception as e:
        raise ApiError(f"Wavelog-Image {version} nicht verfügbar: {e}")
    a = old.attrs
    name = a["Name"].lstrip("/")
    nets = a["NetworkSettings"]["Networks"] or {}
    old.stop(timeout=30)
    old.remove()
    api = cli.api
    net_name, net_cfg = next(iter(nets.items()))
    aliases = sorted(set((net_cfg.get("Aliases") or []) + ["wavelog"]))
    cid = api.create_container(
        f"{WAVELOG_IMAGE}:{version}", name=name,
        environment=a["Config"].get("Env") or [],
        labels=a["Config"].get("Labels") or {},
        host_config=api.create_host_config(
            binds=a["HostConfig"].get("Binds") or [],
            restart_policy=a["HostConfig"].get("RestartPolicy") or {}),
        networking_config=api.create_networking_config(
            {net_name: api.create_endpoint_config(aliases=aliases)}),
    )
    api.start(cid)
    _set_env_file("WAVELOG_VERSION", version)
    log.append(f"Wavelog {version} bereitgestellt")


def do_autoinstall(callsign, password, locator=None, log=None, version=None):
    """Wavelog-Erstinstallation vollautomatisch (einfache Installation):
    treibt den offiziellen Wavelog-Installer per HTTP durch alle Schritte —
    config.php/database.php schreiben, Schema inkl. erstem User + aktivem
    Logbuch + Station anlegen, Migration, DXCC-Import, Installer-Lock.
    Ersetzt die manuellen Browser-Schritte 1-3 aus dem README."""
    log = log if log is not None else []
    callsign = callsign.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+", callsign):
        raise ApiError("Rufzeichen darf nur Buchstaben/Ziffern enthalten "
                       "(ohne Prä-/Suffix)")
    if re.search(r"['\"/\\<>]", password):
        raise ApiError("Passwort darf ' \" / \\ < > nicht enthalten")
    if version:
        if not VERSION_RE.fullmatch(version):
            raise ApiError("Ungültige Versionsangabe")
        if version != wavelog_current_version():
            switch_wavelog_version(version, log)
    public = os.environ.get("LOCAL_PUBLIC_URL",
                            "http://localhost:8086").rstrip("/") + "/"
    form = {
        # Tab Konfiguration (Installer-Defaults)
        "directory": "",
        "websiteurl": public,
        "global_call_lookup": "hamqth",
        "callbook_username": "",
        "callbook_password": "",
        "callbook_token": "",
        "log_threshold": "1",
        # Tab Datenbank (aus der .env, via docker compose als Env gesetzt)
        "db_hostname": os.environ.get("LOCAL_DB_HOST", "wavelog-db"),
        "db_name": os.environ.get("LOCAL_DB_NAME", "wavelog"),
        "db_username": os.environ.get("LOCAL_DB_USER", "wavelog"),
        "db_password": os.environ.get("LOCAL_DB_PASSWORD", ""),
        # Tab First User — city wird zugleich Logbuch- und Stationsname
        "firstname": callsign,
        "lastname": "(Offline-Kit)",
        "username": callsign.lower(),
        "password": password,
        "cnfm_password": password,
        "callsign": callsign,
        "userlocator": (locator or AUTOINSTALL_LOCATOR).upper(),
        "city": callsign,
        "user_email": f"{callsign.lower()}@offline.invalid",
        "timezone": AUTOINSTALL_TIMEZONE,
        "dxcc": "0",
        "userlanguage": AUTOINSTALL_LANGUAGE,
    }

    ses = requests.Session()
    r = _wait(lambda: ses.get(f"{LOCAL_URL}/install/index.php", timeout=10,
                              allow_redirects=False),
              180, "lokale Wavelog-Instanz nicht erreichbar")
    # Redirects von Hand folgen: der Installer leitet beim ersten Aufruf auf
    # sich selbst um (Session-/Sprach-Cookie); nur ein Redirect, der aus
    # /install herausfuehrt (-> Dashboard), bedeutet "bereits installiert".
    # Nicht requests folgen lassen: das Ziel traegt die oeffentliche URL
    # (localhost:8086), die aus dem Container heraus nicht erreichbar ist.
    for _ in range(5):
        if r.status_code not in (301, 302):
            break
        loc = urlparse(r.headers.get("Location", ""))
        if "/install" not in loc.path:
            log.append("Wavelog ist bereits installiert — nichts zu tun")
            return {"ok": True, "already_installed": True, "log": log}
        url = f"{LOCAL_URL}{loc.path}" + (f"?{loc.query}" if loc.query else "")
        r = ses.get(url, timeout=10, allow_redirects=False)
    if r.status_code != 200:
        raise ApiError(f"Installer-Seite nicht erreichbar (HTTP {r.status_code})")

    def db_probe():
        import pymysql
        pymysql.connect(host=form["db_hostname"], user=form["db_username"],
                        password=form["db_password"], database=form["db_name"],
                        connect_timeout=5).close()
    _wait(db_probe, 180, "lokale Datenbank nicht bereit")
    log.append("Wavelog und Datenbank erreichbar")

    m = re.search(r'name="form_token" value="([0-9a-f]+)"', r.text)
    if not m:
        raise ApiError("Installer-Seite ohne form_token — unerwartete Antwort")
    r = ses.post(f"{LOCAL_URL}/install/run.php",
                 data={**form, "form_token": m.group(1)}, timeout=60)
    m = re.search(r"_installer_token = '([0-9a-f]+)'", r.text)
    if not m:
        raise ApiError(f"run.php lieferte keinen Installer-Token "
                       f"(HTTP {r.status_code})")
    hdr = {"X-Installer-Token": m.group(1)}

    def step(trigger, timeout):
        payload = {trigger: "1"}
        payload.update({f"data[{k}]": v for k, v in form.items()})
        rr = ses.post(f"{LOCAL_URL}/install/ajax.php", data=payload,
                      headers=hdr, timeout=timeout)
        if rr.text.strip() != "success":
            raise ApiError(f"Installer-Schritt {trigger} fehlgeschlagen: "
                           f"HTTP {rr.status_code}: {rr.text[:300]}")

    step("run_config_file", 60)
    log.append("config.php geschrieben")
    step("run_database_file", 60)
    log.append("database.php geschrieben")
    step("run_database_tables", 600)
    log.append("Datenbank-Schema angelegt (User, aktives Logbuch, Station)")

    # Migration: gleiche Robustheit wie _proxy_migrate — bei Verbindungsabriss
    # laeuft sie server-seitig weiter, wiederholen bis sauberes success
    deadline = time.monotonic() + MIGRATE_TOTAL_TIMEOUT
    migrated = False
    while time.monotonic() < deadline:
        try:
            rr = ses.get(f"{LOCAL_URL}/index.php/migrate",
                         timeout=(10, MIGRATE_ATTEMPT_TIMEOUT))
            if ("application/json" in rr.headers.get("Content-Type", "")
                    and rr.json().get("status") == "success"):
                migrated = True
                break
        except (requests.RequestException, ValueError):
            pass
        time.sleep(3)
    if not migrated:
        raise ApiError("Datenbank-Migration nicht abgeschlossen")
    log.append("Datenbank migriert")

    rr = ses.post(f"{LOCAL_URL}/install/ajax.php", data={"run_cron_token": "1"},
                  headers=hdr, timeout=30)
    cron_token = rr.text.strip()
    dxcc_ok = False
    for _ in range(3):
        try:
            rr = ses.get(f"{LOCAL_URL}/index.php/update/dxcc",
                         headers={"X-Wavelog-Auth": cron_token},
                         timeout=(10, 900))
            if rr.text.strip() == "success":
                dxcc_ok = True
                break
        except requests.RequestException:
            pass
        time.sleep(5)
    if dxcc_ok:
        log.append("DXCC-Daten importiert")
        # Jetzt, wo die DXCC-Tabellen gefuellt sind: Entity/CQ/ITU der
        # Erstinstallations-Station aus dem Rufzeichen bestimmen (der
        # Installer selbst bekam nur dxcc=0 — APIv2 verlangt aber echte
        # Werte, wenn die Station spaeter zum Server gesynct wird)
        try:
            adif_id, cqz, ituz = _dxcc_lookup_local(callsign)
            if adif_id:
                conn = _local_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE station_profile SET station_dxcc=%s, "
                            "station_cq=%s, station_itu=%s WHERE station_id=1",
                            (adif_id, cqz, ituz))
                    conn.commit()
                finally:
                    conn.close()
                log.append(f"Station vervollständigt: DXCC {adif_id}, "
                           f"CQ {cqz}, ITU {ituz}")
        except Exception as e:
            log.append(f"Hinweis: DXCC der Station nicht bestimmbar ({e})")
    else:
        # nicht fatal: Installation ist nutzbar, Import laesst sich im
        # Wavelog-Admin (Update Country Files) nachholen
        log.append("WARNUNG: DXCC-Import fehlgeschlagen — im Wavelog-Admin "
                   "unter 'Update Country Files' nachholen")
    step("run_installer_lock", 30)
    log.append("Installer gesperrt (.lock)")
    _installed_cache["installed"] = True
    return {"ok": True, "already_installed": False, "callsign": callsign,
            "username": callsign.lower(), "url": public, "log": log}


# Web-Flow der Erstinstallation: der Proxy faengt Aufrufe des Wavelog-
# Installers ab und zeigt eine eigene Setup-Seite (Einfach/Experte). "Einfach"
# startet do_autoinstall() als Hintergrund-Thread, die Seite pollt den
# Fortschritt. "Experte" setzt das Cookie kit_expert und bekommt den
# Original-Installer durchgereicht.
_installed_cache = {"installed": False}
_autoinstall_lock = threading.Lock()
_autoinstall_state = {"running": False, "log": [], "result": None,
                      "password": None, "generated": False, "plan": []}

# Schritt-Texte der Checkliste — identisch zu den log.append()-Zeilen in
# do_autoinstall(); die Setup-Seite hakt Schritte anhand dieser Strings ab
INSTALL_STEPS = [
    "Wavelog und Datenbank erreichbar",
    "config.php geschrieben",
    "database.php geschrieben",
    "Datenbank-Schema angelegt (User, aktives Logbuch, Station)",
    "Datenbank migriert",
    "DXCC-Daten importiert",
    "Installer gesperrt (.lock)",
]


def wavelog_uninstalled():
    """True, solange der Wavelog-Installer aktiv ist (kein config/.lock)."""
    if _installed_cache["installed"]:
        return False
    try:
        # Session noetig: der Installer leitet beim ersten Aufruf auf sich
        # selbst um und setzt dabei ein Cookie — ohne Cookie endlose 302s
        ses = requests.Session()
        r = ses.get(f"{LOCAL_URL}/install/index.php", timeout=5,
                    allow_redirects=False)
        for _ in range(4):
            if r.status_code not in (301, 302):
                break
            loc = urlparse(r.headers.get("Location", ""))
            if "/install" not in loc.path:
                _installed_cache["installed"] = True
                return False
            r = ses.get(f"{LOCAL_URL}{loc.path}", timeout=5,
                        allow_redirects=False)
        if r.status_code == 200 and "form_token" in r.text:
            return True
        return False
    except requests.RequestException:
        # Wavelog (noch) nicht erreichbar -> normal durchreichen
        return False


def _run_autoinstall_bg(callsign, password, version=None):
    try:
        res = do_autoinstall(callsign, password, log=_autoinstall_state["log"],
                             version=version)
    except Exception as e:
        res = {"ok": False, "error": str(e)}
        _autoinstall_state["log"].append(f"FEHLER: {e}")
    _autoinstall_state["result"] = res
    _autoinstall_state["running"] = False


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
        pairs = ust.get("uuid_pairs") or {}
        out["stations_unsynced"] = sum(
            1 for s in local
            if pairs.get(s.get("station_uuid"), s.get("station_uuid"))
            not in server_uuids
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
    if cfg.get("sync_version", True):
        try:
            out["local_version"] = get_version(LOCAL_URL, cfg["local_api_key"])
            if out["server_reachable"]:
                out["server_version"] = get_version(cfg["server_url"],
                                                    cfg["server_api_key"],
                                                    cfg.get("server_v1_key"))
                out["version_mismatch"] = (out["local_version"]
                                           != out["server_version"])
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
                # Pull VOR Push: die gerade geholten QSOs laufen im
                # anschliessenden Push als Dupes zurueck (Server skippt sie),
                # dadurch wandert die Push-Marke ueber die Spiegel-QSOs und
                # sie zaehlen nicht als "ungesynct". Mit v1 war das wegen des
                # fragilen Dupe-Parsings umgekehrt; APIv2 meldet Dupes sauber.
                if mode in ("pull", "sync"):
                    result["pulled"] = do_pull(cfg, ust, state, log)
                if mode in ("push", "sync"):
                    result["pushed"] = do_push(cfg, ust, state, log)
                if mode == "sync" and cfg.get("sync_qsl", True):
                    # QSL-Bestaetigungen sind nice-to-have: fehlender Token-
                    # Scope (confirmation:read) o. Ae. bricht den Sync nicht ab
                    try:
                        result["qsl"] = do_qsl_pull(cfg, ust, state, log)
                    except (requests.RequestException, ApiError) as e:
                        log.append("QSL-Abgleich übersprungen: " + str(e)[:150])
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


def _setup_page():
    """Setup-Seite (Einfach/Experte) mit eingesetzten DB-Daten fuer Experten."""
    with open(os.path.join(os.path.dirname(__file__), "setup.html"), "rb") as f:
        html = f.read().decode("utf-8")
    for k, v in {
        "%%DB_HOST%%": os.environ.get("LOCAL_DB_HOST", "wavelog-db"),
        "%%DB_NAME%%": os.environ.get("LOCAL_DB_NAME", "wavelog"),
        "%%DB_USER%%": os.environ.get("LOCAL_DB_USER", "wavelog"),
        "%%DB_PASSWORD%%": os.environ.get("LOCAL_DB_PASSWORD", ""),
    }.items():
        html = html.replace(k, v)
    return Response(html, mimetype="text/html")


@app.get("/_sync/setup")
def setup_page():
    return _setup_page()


@app.get("/_sync/api/versions")
def api_versions():
    """Waehlbare Wavelog-Versionen (GitHub-Releases, neueste zuerst) plus die
    aktuell laufende — offline bleibt nur die laufende uebrig."""
    current = wavelog_current_version()
    versions = []
    try:
        r = requests.get("https://api.github.com/repos/wavelog/wavelog/releases",
                         params={"per_page": 15}, timeout=10)
        versions = [rel["tag_name"] for rel in r.json()
                    if not rel.get("prerelease") and not rel.get("draft")]
    except (requests.RequestException, ValueError, TypeError, KeyError):
        pass
    if current and current not in versions:
        versions.append(current)
    return jsonify({"current": current, "versions": versions})


@app.get("/_sync/api/autoinstall")
def api_autoinstall_status():
    # Passwort nur ausgeben, wenn es automatisch erzeugt wurde — ein selbst
    # gewaehltes Passwort kennt der User und erscheint nie in einer Antwort
    st = _autoinstall_state
    return jsonify({"running": st["running"], "log": st["log"],
                    "plan": st["plan"], "result": st["result"],
                    "password": st["password"] if st["generated"]
                    and st["result"] and st["result"].get("ok") else None})


@app.post("/_sync/api/autoinstall")
def api_autoinstall_start():
    body = request.get_json(silent=True) or {}
    callsign = str(body.get("callsign") or "").strip().upper().replace(" ", "")
    password = str(body.get("password") or "").strip()
    version = str(body.get("version") or "").strip()
    if not re.fullmatch(r"[A-Z0-9]+", callsign):
        return jsonify({"started": False, "error":
                        "Rufzeichen darf nur Buchstaben/Ziffern enthalten "
                        "(ohne Prä-/Suffix)"}), 400
    if re.search(r"['\"/\\<>]", password):
        return jsonify({"started": False, "error":
                        "Passwort darf ' \" / \\ < > nicht enthalten"}), 400
    if version and not VERSION_RE.fullmatch(version):
        return jsonify({"started": False,
                        "error": "Ungültige Versionsangabe"}), 400
    generated = not password
    if generated:
        password = "".join(secrets.choice(string.ascii_letters + string.digits)
                           for _ in range(12))
    plan = list(INSTALL_STEPS)
    if version and version != wavelog_current_version():
        plan.insert(0, f"Wavelog {version} bereitgestellt")
    with _autoinstall_lock:
        if _autoinstall_state["running"]:
            return jsonify({"started": False,
                            "error": "Installation läuft bereits"}), 409
        if not wavelog_uninstalled():
            return jsonify({"started": False,
                            "error": "Wavelog ist bereits installiert"}), 409
        _autoinstall_state.update(running=True, log=[], result=None,
                                  password=password, generated=generated,
                                  plan=plan)
        threading.Thread(target=_run_autoinstall_bg,
                         args=(callsign, password, version or None),
                         daemon=True).start()
    return jsonify({"started": True})


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
        "server_v1_key_masked": _mask(cfg.get("server_v1_key")),
        "local_api_key_masked": _mask(cfg["local_api_key"]),
        "sync_edits": bool(cfg.get("sync_edits", True)),
        "sync_deletions": bool(cfg.get("sync_deletions", True)),
        "sync_qsl": bool(cfg.get("sync_qsl", True)),
        "sync_version": bool(cfg.get("sync_version", True)),
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
    for field in ("server_url", "server_api_key", "server_v1_key",
                  "local_api_key", "diff_auto_limit"):
        val = (str(body.get(field)).strip() if body.get(field) is not None else "")
        if val:
            profile[field] = (_clean_server_url(val) if field == "server_url"
                              else val)
    # Stationsauswahl: Liste der ausgeschlossenen Server-uuids; leere Liste
    # = alles syncen. Nur ueberschreiben, wenn das Feld mitgeschickt wurde.
    if isinstance(body.get("station_exclude"), list):
        profile["station_exclude"] = [str(x) for x in body["station_exclude"] if x]
    # Feature-Auswahl (nur ueberschreiben, wenn mitgeschickt)
    for flag in ("sync_edits", "sync_deletions", "sync_qsl", "sync_version"):
        if isinstance(body.get(flag), bool):
            profile[flag] = body[flag]
    if profile.get("server_api_key") and not _v2(profile["server_api_key"]):
        return jsonify({"saved": False, "error":
                        "Server-Token muss ein APIv2-Token (wl2_…) sein — "
                        "Legacy-Keys werden nur noch optional für den "
                        "Versionsabgleich akzeptiert"}), 400
    # Lokales APIv2-Token automatisch anlegen — wenn keins gesetzt ist, noch
    # ein alter v1-Key gespeichert ist oder das gespeicherte Token nicht mehr
    # funktioniert (z. B. im UI widerrufen): selbstheilend neu erzeugen.
    key_note = None
    lk = profile.get("local_api_key") or ""
    need_new = not _v2(lk)
    if not need_new:
        # Token muss funktionieren UND alle noetigen Scopes haben (aeltere
        # Kit-Tokens kennen z. B. qso:delete/confirmation:read noch nicht)
        try:
            r = requests.get(_v2_url(LOCAL_URL, "token"), headers=_v2_hdr(lk),
                             timeout=10)
            scopes = set(((r.json().get("data") or {}).get("scopes")) or []) \
                if r.status_code == 200 else set()
            need_new = not set(V2_SCOPES.split(",")) <= scopes
        except (requests.RequestException, ValueError):
            need_new = True
    if need_new:
        try:
            profile["local_api_key"] = ensure_local_api_key(user)
            key_note = "lokales APIv2-Token automatisch erstellt"
        except (ApiError, Exception) as e:
            key_note = f"lokales APIv2-Token nicht automatisch erstellbar: {str(e)[:120]}"
    save_config(full)
    cfg = user_cfg(user)

    checks = {"key_note": key_note}
    try:
        n = len(get_stations(cfg["server_url"], cfg["server_api_key"]))
        checks["server"] = {"ok": True, "stations": n, "version": None}
        if cfg.get("sync_version", True):
            try:
                checks["server"]["version"] = get_version(
                    cfg["server_url"], cfg["server_api_key"],
                    cfg["server_v1_key"])
            except (requests.RequestException, ApiError):
                pass
    except (requests.RequestException, ApiError) as e:
        checks["server"] = {"ok": False, "error": str(e)[:200]}
    try:
        n = len(get_stations(LOCAL_URL, cfg["local_api_key"]))
        checks["local"] = {"ok": True, "stations": n,
                           "version": get_version(LOCAL_URL, cfg["local_api_key"])}
    except (requests.RequestException, ApiError) as e:
        checks["local"] = {"ok": False, "error": str(e)[:200]}
    return jsonify({"saved": True, "user": user, "checks": checks})


@app.get("/_sync/api/stations")
def api_stations():
    """Server-Stationsliste fuer die Stationsauswahl in den Einstellungen —
    je Station, ob sie aktuell gesynct wird (sync=false = ausgeschlossen)."""
    try:
        user = resolve_user(_req_user())
        cfg = user_cfg(user)
    except ApiError as e:
        return jsonify({"ok": False, "error": str(e)})
    if not (cfg["server_url"] and cfg["server_api_key"]):
        return jsonify({"ok": False, "error": "Server-Zugang nicht konfiguriert"})
    try:
        server = get_stations(cfg["server_url"], cfg["server_api_key"])
    except (requests.RequestException, ApiError) as e:
        return jsonify({"ok": False, "error": str(e)[:200]})
    excl = set(cfg["station_exclude"])
    return jsonify({"ok": True, "stations": [
        {"uuid": s["station_uuid"],
         "name": s["station_profile_name"],
         "callsign": s["station_callsign"],
         "sync": s["station_uuid"] not in excl}
        for s in server if s.get("station_uuid")
    ]})


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
    # bewusst OHNE die _request-Retry-Logik: kurzer Timeout, ein Versuch, damit
    # ein nicht erreichbarer Server schnell (statt nach ~50s Retries) gemeldet
    # wird. Der v2-Status-Endpoint ist oeffentlich (kein Token noetig).
    try:
        r = requests.get(_v2_url(cfg["server_url"], "status"), timeout=5)
        ok = (r.status_code == 200
              and ((r.json() or {}).get("data") or {}).get("status") == "ok")
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
    # erkannte "am Server geloescht"-Kandidaten ebenfalls als 🗑 markieren
    only_local |= {_nk(d["call"], d["date"], d["time"], d["band"], d["mode"])
                   for d in ld.get("deletions", [])
                   if d.get("action") == "delete_local"}
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


@app.post("/_sync/api/apply_deletions")
def api_apply_deletions():
    """Die im letzten Abweichungs-Check erkannten Löschungen ausführen —
    das Panel hat vorher EINE Sicherheitsabfrage mit der kompletten Liste
    gezeigt. delete_local = am Server gelöscht -> lokal nachziehen;
    delete_server = lokal gelöscht -> am Server nachziehen."""
    with _lock:
        try:
            user = resolve_user(_req_user())
        except ApiError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        cfg = user_cfg(user)
        state = load_state()
        ust = user_state(state, user)
        dels = (ust.get("last_diff") or {}).get("deletions") or []
        if not dels:
            return jsonify({"ok": True, "deleted": 0,
                            "log": ["keine Löschungen offen"]})
        log, remaining = [], []
        for d in dels:
            try:
                if d["action"] == "delete_local":
                    delete_qso(LOCAL_URL, cfg["local_api_key"], d["id"])
                    log.append(f"lokal gelöscht: {d['date']} {d['time']} "
                               f"{d['call']} ({d['band']} {d['mode']})")
                else:
                    delete_qso(cfg["server_url"], cfg["server_api_key"], d["id"])
                    log.append(f"am Server gelöscht: {d['date']} {d['time']} "
                               f"{d['call']} ({d['band']} {d['mode']})")
                ust.setdefault("diff_snapshots", {}).pop(d.get("key"), None)
            except (requests.RequestException, ApiError) as e:
                log.append(f"FEHLER bei {d['call']}: {str(e)[:150]}")
                remaining.append(d)
        ust["last_diff"]["deletions"] = remaining
        save_state(state)
        return jsonify({"ok": not remaining,
                        "deleted": len(dels) - len(remaining), "log": log})


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
    # Erstinstallation: Aufrufe des Wavelog-Installers auf die eigene
    # Setup-Seite (Einfach/Experte) umleiten. Experten haben das Cookie
    # kit_expert gesetzt und bekommen den Original-Installer durchgereicht.
    if (request.method == "GET"
            and path.rstrip("/") in ("install", "install/index.php")
            and request.cookies.get("kit_expert") != "1"
            and wavelog_uninstalled()):
        return _setup_page()
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
            out["server"] = get_version(cfg["server_url"], cfg["server_api_key"],
                                        cfg.get("server_v1_key"))
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
    elif cmd == "autoinstall":
        def _arg(name):
            if name in sys.argv:
                i = sys.argv.index(name)
                if i + 1 < len(sys.argv):
                    return sys.argv[i + 1]
            return None
        callsign = _arg("--callsign")
        password = _arg("--password")
        if not callsign or not password:
            print("autoinstall --callsign RUFZEICHEN --password PASSWORT "
                  "[--locator AA00AA] [--version 3.1.0]", file=sys.stderr)
            sys.exit(2)
        log = []
        try:
            out = do_autoinstall(callsign, password, _arg("--locator"), log,
                                 version=_arg("--version"))
        except (requests.RequestException, ApiError) as e:
            out = {"ok": False, "error": str(e), "log": log}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0 if out["ok"] else 1)
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
