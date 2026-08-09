// Wavelog Offline-Kit — Sync-Integration (per Proxy injiziert, Wavelog unverändert)
// - Menüpunkt "Offline-Sync" im Usermenü unter "Hardware-Schnittstellen" (nur eingeloggt)
// - eigene Seite /offline-sync, die sich Header/Menü/Footer/Theme mit Wavelog teilt
// - Hinweis-Banner solange der Sync noch nicht eingerichtet ist
(function () {
  "use strict";
  if (window.__wlSyncLoaded) return;

  const hardwareLink = document.querySelector(
    'ul.dropdown-menu a.dropdown-item[href$="/radio"], ul.dropdown-menu a.dropdown-item[href*="index.php/radio"]'
  );
  if (!hardwareLink || !hardwareLink.closest("li")) return; // nur bei eingeloggtem User
  window.__wlSyncLoaded = true;

  const PAGE_URL = "/offline-sync";
  const onPage = location.pathname.replace(/\/+$/, "").endsWith("/offline-sync");

  // Profil = Rufzeichen des eingeloggten Users (aus dem Menü-Toggle; Ø->0)
  let user = "";
  const toggle = hardwareLink.closest("li.nav-item, li.dropdown")
    ?.querySelector("a.nav-link.dropdown-toggle");
  if (toggle) user = (toggle.textContent || "").trim().split(/\s+/)[0].replace(/Ø/g, "0").toUpperCase();
  const uq = user ? "user=" + encodeURIComponent(user) : "";
  const withUser = (u) => (uq ? u + (u.includes("?") ? "&" : "?") + uq : u);

  // --- Menüpunkt (echter Link zur eigenen Seite, im Wavelog-Stil) ---------
  const li = document.createElement("li");
  li.innerHTML =
    '<a class="dropdown-item" href="' + PAGE_URL + '"><i class="fas fa-sync"></i> Offline-Sync' +
    '<span id="wlsync-badge" class="badge text-bg-danger ms-1" style="display:none"></span></a>';
  hardwareLink.closest("li").after(li);
  const badge = li.querySelector("#wlsync-badge");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // Hinweis-Banner (auf normalen Seiten, solange nicht eingerichtet) -------
  function showSetupHint() {
    if (onPage || document.getElementById("wlsync-hint") || sessionStorage.getItem("wlsyncHintDismissed")) return;
    const nav = document.querySelector("nav.main-nav, #header-menu, nav.navbar");
    // in einen .container gepackt -> gleiche Breite wie Wavelogs eigene Hinweise
    const wrap = document.createElement("div");
    wrap.id = "wlsync-hint";
    wrap.className = "container mt-3";
    wrap.innerHTML =
      '<div class="alert alert-warning alert-dismissible d-flex align-items-center mb-0">' +
      '<i class="fas fa-sync me-2"></i><div>Der <b>Offline-Sync</b> ist noch nicht eingerichtet. ' +
      '<a href="' + PAGE_URL + '" class="alert-link">Jetzt einrichten</a>.</div>' +
      '<button type="button" class="btn-close ms-auto" aria-label="Schließen"></button></div>';
    if (nav && nav.parentNode) nav.after(wrap); else document.body.prepend(wrap);
    wrap.querySelector(".btn-close").addEventListener("click", () => {
      wrap.remove(); sessionStorage.setItem("wlsyncHintDismissed", "1");
    });
  }
  function hideSetupHint() { document.getElementById("wlsync-hint")?.remove(); }

  // gemeinsame Toast-Meldung (Bootstrap)
  function toast(msg, ok) {
    const t = document.createElement("div");
    t.className = "toast align-items-center text-bg-" + (ok ? "success" : "danger") + " border-0 show position-fixed";
    t.style.cssText = "bottom:1rem;right:1rem;z-index:1090";
    t.innerHTML = '<div class="d-flex"><div class="toast-body">' + esc(msg) +
      '</div><button type="button" class="btn-close btn-close-white me-2 m-auto"></button></div>';
    document.body.appendChild(t);
    t.querySelector(".btn-close").addEventListener("click", () => t.remove());
    setTimeout(() => t.remove(), 6000);
  }

  // Sync-Icon in der oberen Menuleiste — nur wenn eingerichtet UND ein erster
  // Sync gelaufen ist. Klick prueft erst die Server-Erreichbarkeit, dann Sync;
  // waehrend des Laufs dreht sich das Icon.
  let navBtn = null, navBusy = false;
  function ensureNavButton(p) {
    const show = p && p.configured && p.last_sync;
    if (!show) { navBtn?.remove(); navBtn = null; return; }
    if (navBtn && document.body.contains(navBtn)) return;
    const rightNav = document.querySelector("ul.navbar-nav-right, nav .navbar-nav:last-of-type");
    if (!rightNav) return;
    const li = document.createElement("li");
    li.className = "nav-item d-flex align-items-center";
    li.innerHTML = '<a class="nav-link" id="wlsync-navbtn" href="#" title="Offline-Sync jetzt ausführen"><i class="fas fa-sync"></i></a>';
    rightNav.prepend(li);
    navBtn = li;
    li.querySelector("#wlsync-navbtn").addEventListener("click", navSync);
  }
  async function navSync(e) {
    e.preventDefault();
    if (navBusy) return;
    const icon = navBtn.querySelector("i");
    let ping;
    try { ping = await (await fetch(withUser("/_sync/api/ping"))).json(); }
    catch (err) { ping = { reachable: false }; }
    if (!ping.reachable) { toast("Server nicht erreichbar — Sync nicht möglich.", false); return; }
    navBusy = true; icon.classList.add("fa-spin");
    try {
      const r = await (await fetch(withUser("/_sync/api/sync?mode=sync"), { method: "POST" })).json();
      const ok = r.ok !== false && r.error == null;
      toast(ok ? "Sync fertig: " + (r.pushed || 0) + " hoch, " + (r.pulled || 0) + " runter"
               : "Sync-Fehler: " + (r.error || "unbekannt"), ok);
    } catch (err) { toast("Sync-Sidecar nicht erreichbar.", false); }
    icon.classList.remove("fa-spin"); navBusy = false;
    updateBadge();
  }

  // Badge auf allen Seiten aktualisieren (auch ohne die Seite zu öffnen) ---
  async function updateBadge() {
    let p;
    try { p = await (await fetch(withUser("/_sync/api/pending"))).json(); } catch (e) { return; }
    const n = p.pending_qsos + p.stations_unsynced + (p.diff_count || 0);
    const warn = (p.contest_import_pending || []).length || p.version_mismatch;
    badge.textContent = (p.contest_import_pending || []).length ? n + " ⚑" : (p.version_mismatch ? "⚠" : n);
    badge.className = "badge ms-1 " + (warn ? "text-bg-warning" : "text-bg-danger");
    badge.style.display = (n > 0 || warn) ? "" : "none";
    if (p.configured === false) showSetupHint(); else hideSetupHint();
    ensureNavButton(p);
    return p;
  }

  // ========================================================================
  //  Eigene Seite /offline-sync — teilt Layout mit Wavelog
  // ========================================================================
  if (!onPage) {
    updateBadge();
    setInterval(updateBadge, 60000);

    // ---- Sync-Status je QSO in den Listen (Dashboard, Logbook) einblenden ----
    const QSTAT = {
      synced:         { i: "fa-check",                c: "text-success",   t: "synchronisiert" },
      pending:        { i: "fa-arrow-up",             c: "text-secondary", t: "noch nicht synchronisiert" },
      changed:        { i: "fa-triangle-exclamation", c: "text-warning",   t: "lokal/Server unterschiedlich — nachziehen" },
      time_changed:   { i: "fa-clock",                c: "text-warning",   t: "Uhrzeit weicht ab" },
      deleted_remote: { i: "fa-trash",                c: "text-danger",    t: "am Server gelöscht" },
      unknown:        { i: "fa-circle-question",      c: "text-muted",     t: "Status unbekannt — einmal syncen" },
    };
    async function annotateQsos() {
      const rows = {};
      document.querySelectorAll('tr[id^="qso_"], tr[id^="qsoID-"]').forEach((r) => {
        const m = r.id.match(/(\d+)/);
        if (m) rows[m[1]] = r;
      });
      const ids = Object.keys(rows);
      if (!ids.length) return;
      let st;
      try { st = await (await fetch(withUser("/_sync/api/qso_status?ids=" + ids.join(",")))).json(); }
      catch (e) { return; }
      for (const [id, row] of Object.entries(rows)) {
        const s = QSTAT[st[id]] || QSTAT.unknown;
        const cell = row.querySelector("td");
        if (!cell) continue;
        let ic = cell.querySelector(".wlsync-qstat");
        if (!ic) { ic = document.createElement("span"); ic.className = "wlsync-qstat me-1"; cell.prepend(ic); }
        ic.innerHTML = '<i class="fas ' + s.i + " " + s.c + '"></i>';
        ic.title = "Offline-Sync: " + s.t;
      }
    }
    let annTimer = null;
    const scheduleAnn = () => { clearTimeout(annTimer); annTimer = setTimeout(annotateQsos, 400); };
    scheduleAnn();
    // dynamisch nachgeladene Listen (DataTables/Logbook) beobachten
    new MutationObserver(scheduleAnn).observe(document.body, { childList: true, subtree: true });
    setInterval(annotateQsos, 60000);
    return;
  }

  document.title = "Offline-Sync | Wavelog";
  // Dashboard-Inhalt (Basis-Layout) ausblenden, eigenen Container einsetzen
  const nav = document.querySelector("nav.main-nav, #header-menu, nav.navbar");
  document.querySelectorAll("body .container, body .container-fluid, body .container-lg").forEach((el) => {
    if (el.closest("nav")) return;
    el.style.display = "none";
  });
  const page = document.createElement("div");
  page.className = "container my-4";
  page.id = "wlsync-page";
  page.innerHTML = [
    '<div class="d-flex align-items-center justify-content-between mb-3">',
    '  <h2 class="mb-0"><i class="fas fa-sync"></i> Offline-Sync',
    '    <small class="text-body-secondary fs-6" id="wlsync-profile"></small></h2>',
    '  <div class="d-flex gap-2">',
    '    <button class="btn btn-primary" data-act="sync"><i class="fas fa-sync"></i> Sync</button>',
    '    <button class="btn btn-outline-secondary" data-act="push">Nur Push</button>',
    '    <button class="btn btn-outline-secondary" data-act="pull">Nur Pull</button>',
    '  </div>',
    '</div>',
    '<div id="wlsync-summary" class="mb-3"></div>',
    '<div id="wlsync-runstatus" class="mb-3"></div>',
    '<div id="wlsync-contest" class="mb-3"></div>',
    '<div class="row g-3">',
    '  <div class="col-lg-6"><div class="card h-100"><div class="card-header">Ungesyncte QSOs</div>',
    '    <div class="card-body" id="wlsync-pending">–</div></div></div>',
    '  <div class="col-lg-6"><div class="card h-100">',
    '    <div class="card-header d-flex justify-content-between align-items-center">Abweichungen (Edits)',
    '      <button class="btn btn-sm btn-outline-secondary py-0" data-act="diff">jetzt prüfen</button></div>',
    '    <div class="card-body"><p class="text-body-secondary small">Feld-Änderungen mit eindeutiger Richtung werden',
    '      automatisch übertragen; Konflikte bleiben hier zur manuellen Entscheidung. Erkannte Löschungen werden erst',
    '      nach Bestätigung ausgeführt.</p>',
    '      <div id="wlsync-diff">–</div></div></div></div>',
    '</div>',
    '<div class="card mt-3"><div class="card-header">Einstellungen</div><div class="card-body">',
    '  <div id="wlsync-setup-hint"></div>',
    '  <div class="row g-2">',
    '    <div class="col-md-6"><label class="form-label mb-0 small">Server-URL</label>',
    '      <input id="wlsync-url" class="form-control form-control-sm" placeholder="https://wavelog.example.org"></div>',
    '    <div class="col-md-6"><label class="form-label mb-0 small">Server-API-Token (APIv2, wl2_…)</label>',
    '      <input id="wlsync-skey" class="form-control form-control-sm" placeholder="leer = unverändert"></div>',
    '    <div class="col-md-6"><label class="form-label mb-0 small">Server-Legacy-Key (v1, optional — nur Versionsabgleich)</label>',
    '      <input id="wlsync-s1key" class="form-control form-control-sm" placeholder="optional"></div>',
    '  </div>',
    '  <p class="text-body-secondary small mt-1 mb-0">Das lokale API-Token wird automatisch erstellt — nur die Server-Daten eintragen.</p>',
    '  <div class="mt-2"><b class="small">Stationen im Sync</b>',
    '    <div id="wlsync-stations" class="small mt-1">–</div>',
    '    <p class="text-body-secondary small mb-0">Abgewählte Server-Stationen werden nicht gesynct und beim nächsten',
    '      Sync lokal samt QSOs entfernt (der Server bleibt unberührt). Neue Stationen sind automatisch angewählt.',
    '      Gilt nach „Speichern &amp; testen".</p></div>',
    '  <div class="mt-2"><b class="small">Was wird gesynct?</b>',
    '    <div class="small">',
    '      <label class="form-check d-block mb-0"><input class="form-check-input" type="checkbox" checked disabled> QSOs &amp; Stationen <span class="text-body-secondary">(Basis, immer)</span></label>',
    '      <label class="form-check d-block mb-0"><input class="form-check-input wlsync-feat" type="checkbox" id="wlsync-f-edits"> Änderungen automatisch übertragen (Edit-Sync)</label>',
    '      <label class="form-check d-block mb-0"><input class="form-check-input wlsync-feat" type="checkbox" id="wlsync-f-del"> Löschungen erkennen &amp; übernehmen (mit Bestätigung)</label>',
    '      <label class="form-check d-block mb-0"><input class="form-check-input wlsync-feat" type="checkbox" id="wlsync-f-qsl"> QSL-Bestätigungen (LoTW/eQSL/…) holen</label>',
    '      <label class="form-check d-block mb-0"><input class="form-check-input wlsync-feat" type="checkbox" id="wlsync-f-ver"> Wavelog-Versionsabgleich</label>',
    '    </div>',
    '    <div class="small mt-1 alert alert-secondary py-1 px-2 mb-0" id="wlsync-scopes"></div></div>',
    '  <button class="btn btn-primary btn-sm mt-2" data-act="savecfg">Speichern &amp; testen</button>',
    '  <div id="wlsync-cfgresult" class="small mt-2"></div>',
    '</div></div>',
    '<div class="card mt-3"><div class="card-header">Daten</div><div class="card-body">',
    '  <div class="d-flex flex-wrap gap-2 align-items-center">',
    '    <button class="btn btn-outline-secondary btn-sm" data-act="seed">Server-Bestand übernehmen</button>',
    '    <div class="form-check form-check-inline m-0 small"><input class="form-check-input" type="checkbox" id="wlsync-force">',
    '      <label class="form-check-label" for="wlsync-force">trotz ungesyncter Daten</label></div>',
    '    <button class="btn btn-outline-danger btn-sm ms-auto" data-act="reset">Lokalen Bestand zurücksetzen</button>',
    '  </div>',
    '  <p class="text-body-secondary small mt-2 mb-0">„Übernehmen" holt Stationen + alle QSOs vom Server (Erstbefüllung/Auffrischen). ',
    '    „Zurücksetzen" löscht alle lokalen QSOs/Stationen dieses Profils und lädt sie neu (nach Sync-Problemen).</p>',
    '</div></div>',
    '<div class="card mt-3"><div class="card-header">Letzter Lauf</div>',
    '  <div class="card-body"><pre id="wlsync-log" class="small mb-0" style="white-space:pre-wrap">–</pre></div></div>',
  ].join("");
  (nav ? nav.after(page) : document.body.prepend(page));
  const $ = (id) => page.querySelector(id);

  // --- Datenfluss (identisch zur Panel-Logik, targetet die Seite) ---------
  async function refresh() {
    const p = await updateBadge();
    if (!p) return;
    $("#wlsync-profile").textContent = user ? "Profil " + user : "";
    const on = p.server_reachable !== false;
    let s = '<span class="badge text-bg-' + (on ? "success" : "secondary") + '">Server ' +
      (on ? "erreichbar" : "offline") + "</span> ";
    s += '<span class="badge text-bg-light border">ungesynct: ' + p.pending_qsos + " QSO";
    if (p.stations_unsynced) s += ", " + p.stations_unsynced + " Station";
    s += "</span>";
    if (p.local_version)
      s += ' <span class="badge text-bg-light border">Version ' + esc(p.local_version) +
        (p.server_version ? " / " + esc(p.server_version) : "") + "</span>";
    if (p.version_mismatch)
      s += '<div class="alert alert-warning py-1 px-2 mt-2 mb-0 small">⚠ Versions-Unterschied — auf dem Laptop setup.cmd / setup.sh ausführen.</div>';
    if (p.last_sync) s += '<div class="text-body-secondary small mt-1">letzter Sync: ' + esc(p.last_sync) + "</div>";
    $("#wlsync-summary").innerHTML = s;

    const c = $("#wlsync-contest");
    if ((p.contest_import_pending || []).length)
      c.innerHTML = '<div class="alert alert-warning py-2 px-2 mb-0">⚑ Contest-QSOs gepusht (<b>' +
        esc(p.contest_import_pending.join(", ")) + '</b>). Am Server einmal <a target="_blank" href="' +
        esc(p.server_url) + '/contesting_import">Import Historical Contests</a> ausführen. ' +
        '<button class="btn btn-sm btn-outline-secondary py-0 ms-1" data-act="ack">Erledigt</button></div>';
    else c.innerHTML = "";

    const rows = (p.qsos || []).map((q) =>
      "<tr><td>" + esc(q.date) + " " + esc(q.time) + "</td><td>" + esc(q.call) + "</td><td>" +
      esc(q.band) + "</td><td>" + esc(q.mode) + "</td><td>" + esc(q.contest) + "</td></tr>").join("");
    $("#wlsync-pending").innerHTML = rows
      ? '<table class="table table-sm mb-0"><thead><tr><th>Zeit</th><th>Call</th><th>Band</th><th>Mode</th><th>Contest</th></tr></thead><tbody>' + rows + "</tbody></table>"
      : '<span class="text-success">keine 🎉</span>';

    if (p.last_result)
      $("#wlsync-log").textContent = "[" + p.last_result.mode + "] " +
        (p.last_result.ok ? "OK" : "FEHLER") + "\n" + (p.last_result.log || []).join("\n");

    renderDiff();
    loadCfg();
  }

  async function renderDiff() {
    let d;
    try { d = (await (await fetch(withUser("/_sync/api/diff"))).json()).last_diff; } catch (e) { return; }
    const el = $("#wlsync-diff");
    if (!d) { el.textContent = "noch nicht geprüft"; return; }
    const q = (x) => esc(x.date) + " " + esc(x.time) + " " + esc(x.call) + " (" + esc(x.band) + " " + esc(x.mode) + ")";
    let h = '<div class="text-body-secondary small mb-1">Stand: ' + esc(d.ts) + " — " + d.total_qsos + " QSOs verglichen</div>";
    if (d.diffs.length) {
      h += '<div class="table-responsive"><table class="table table-sm mb-2"><thead><tr><th>QSO</th><th>Feld</th><th>lokal</th><th>Server</th><th>geändert</th></tr></thead><tbody>';
      for (const x of d.diffs)
        for (const [f, v] of Object.entries(x.fields))
          h += "<tr><td>" + q(x) + "</td><td>" + esc(f) + "</td><td>" + (esc(v.lokal) || "<i>leer</i>") +
            "</td><td>" + (esc(v.server) || "<i>leer</i>") + "</td><td>" + esc(x.edited) + "</td></tr>";
      h += "</tbody></table></div>";
    }
    if ((d.time_edits || []).length)
      h += "<div class='mb-2'><b>Vermutlich Zeit-Änderung:</b><br>" + d.time_edits.map((x) =>
        esc(x.date) + " " + esc(x.call) + " (" + esc(x.band) + " " + esc(x.mode) + "): lokal " +
        esc(x.zeit_lokal) + " / Server " + esc(x.zeit_server)).join("<br>") + "</div>";
    const dels = d.deletions || [];
    if (dels.length) {
      h += '<div class="alert alert-danger py-2 px-2 mb-2"><b>' + dels.length +
        ' Löschung(en) erkannt</b> — auf der Gegenseite bereits gelöscht:<br>' +
        dels.map((x) => (x.action === "delete_local" ? "wird LOKAL gelöscht: " : "wird am SERVER gelöscht: ") + q(x) +
          ' <span class="text-body-secondary">(' + esc(x.station) + ")</span>").join("<br>") +
        '<br><button class="btn btn-sm btn-danger mt-2" data-act="deletions">Löschungen übernehmen…</button></div>';
    }
    if (d.only_local.length)
      h += "<div class='mb-2'><b>Nur lokal</b> (noch nicht gepusht/unbekannt):<br>" + d.only_local.map(q).join("<br>") + "</div>";
    if (d.only_server.length)
      h += "<div class='mb-2'><b>Nur am Server</b> (noch nicht gepullt/unbekannt):<br>" + d.only_server.map(q).join("<br>") + "</div>";
    if (!d.diffs.length && !(d.time_edits || []).length && !dels.length && !d.only_local.length && !d.only_server.length)
      h += '<span class="text-success">keine Abweichungen 🎉</span>';
    el.innerHTML = h;
  }

  function updateScopes() {
    const scopes = ["qso:read", "qso:write", "station:read", "station:write"];
    if ($("#wlsync-f-del").checked) scopes.push("qso:delete");
    if ($("#wlsync-f-qsl").checked) scopes.push("confirmation:read");
    let t = "Benötigte Scopes für das Server-Token: " + scopes.join(", ");
    if ($("#wlsync-f-ver").checked)
      t += " — für den Versionsabgleich zusätzlich statistic:read (braucht Admin-Rechte) oder den Server-Legacy-Key";
    $("#wlsync-scopes").textContent = t;
  }

  let stationsLoaded = false;
  async function loadStations() {
    const el = $("#wlsync-stations");
    let d;
    try { d = await (await fetch(withUser("/_sync/api/stations"))).json(); }
    catch (e) { d = { ok: false, error: "nicht erreichbar" }; }
    if (!d.ok) {
      el.innerHTML = '<span class="text-body-secondary">' + esc(d.error || "nicht verfügbar") + "</span>";
      return;
    }
    el.innerHTML = d.stations.map((st) =>
      '<label class="form-check d-block mb-0"><input class="form-check-input wlsync-stsel" type="checkbox" value="' +
      esc(st.uuid) + '"' + (st.sync ? " checked" : "") + "> " + esc(st.name) +
      ' <span class="text-body-secondary">(' + esc(st.callsign) + ")</span></label>"
    ).join("") || '<span class="text-body-secondary">keine Stationen am Server</span>';
    stationsLoaded = true;
  }

  async function loadCfg() {
    const c = await (await fetch(withUser("/_sync/api/config"))).json();
    $("#wlsync-url").value = c.server_url || "";
    $("#wlsync-skey").placeholder = c.server_api_key_masked ? "gesetzt (" + c.server_api_key_masked + ") — leer = unverändert" : "APIv2-Token vom Server (wl2_…)";
    $("#wlsync-s1key").placeholder = c.server_v1_key_masked ? "gesetzt (" + c.server_v1_key_masked + ") — leer = unverändert" : "optional";
    $("#wlsync-setup-hint").innerHTML = c.configured ? "" :
      '<div class="alert alert-info py-1 px-2 mb-2 small">Noch nicht konfiguriert — Server-URL und API-Token eintragen und speichern, dann „Server-Bestand übernehmen".</div>';
    $("#wlsync-f-edits").checked = c.sync_edits !== false;
    $("#wlsync-f-del").checked = c.sync_deletions !== false;
    $("#wlsync-f-qsl").checked = c.sync_qsl !== false;
    $("#wlsync-f-ver").checked = c.sync_version !== false;
    updateScopes();
    if (!stationsLoaded) loadStations();
  }

  const LABELS = { sync: "Sync", push: "Push", pull: "Pull", diff: "Abweichungs-Check", seed: "Seed", reset: "Reset" };
  async function run(mode, extra) {
    const rs = $("#wlsync-runstatus");
    rs.innerHTML = '<div class="alert alert-info d-flex align-items-center py-2 mb-0">' +
      '<span class="spinner-border spinner-border-sm me-2"></span>' + (LABELS[mode] || mode) + " läuft…</div>";
    $("#wlsync-log").textContent = mode + " läuft…";
    try {
      const r = await (await fetch(withUser("/_sync/api/sync?mode=" + mode + (extra || "")), { method: "POST" })).json();
      const ok = r.ok !== false && r.error == null;
      rs.innerHTML = '<div class="alert alert-' + (ok ? "success" : "danger") + ' py-2 mb-0">' +
        (ok ? "✓ " + (LABELS[mode] || mode) + " fertig: " + (r.pushed || 0) + " hoch, " + (r.pulled || 0) + " runter" +
              ((r.stations_up + r.stations_down > 0) ? ", " + (r.stations_up + r.stations_down) + " Station(en)" : "")
            : "✗ Fehler: " + esc(r.error || "unbekannt")) + "</div>";
      setTimeout(() => { if (rs.querySelector(".alert-success")) rs.innerHTML = ""; }, 8000);
    } catch (e) {
      rs.innerHTML = '<div class="alert alert-danger py-2 mb-0">✗ Sidecar nicht erreichbar</div>';
    }
    refresh();
  }

  async function saveCfg() {
    const r = await (await fetch(withUser("/_sync/api/config"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user: user,
        server_url: $("#wlsync-url").value,
        server_api_key: $("#wlsync-skey").value,
        server_v1_key: $("#wlsync-s1key").value,
        sync_edits: $("#wlsync-f-edits").checked,
        sync_deletions: $("#wlsync-f-del").checked,
        sync_qsl: $("#wlsync-f-qsl").checked,
        sync_version: $("#wlsync-f-ver").checked,
        ...(stationsLoaded ? { station_exclude: Array.from(page.querySelectorAll(".wlsync-stsel"))
          .filter((cb) => !cb.checked).map((cb) => cb.value) } : {}),
      }),
    })).json();
    if (r.error) { $("#wlsync-cfgresult").innerHTML = '<span class="text-danger">' + esc(r.error) + "</span>"; return; }
    const s = r.checks.server, l = r.checks.local;
    $("#wlsync-cfgresult").innerHTML =
      "Server: " + (s.ok ? '<span class="text-success">OK (' + s.stations + " Station(en)" + (s.version ? ", Wavelog " + esc(s.version) : "") + ")</span>" : '<span class="text-danger">Fehler</span> ' + esc(s.error)) +
      " · Lokal: " + (l.ok ? '<span class="text-success">OK (' + l.stations + " Station(en))</span>" : '<span class="text-danger">Fehler</span> ' + esc(l.error)) +
      (r.checks.key_note ? '<br><span class="text-body-secondary">' + esc(r.checks.key_note) + "</span>" : "");
    $("#wlsync-skey").value = "";
    $("#wlsync-s1key").value = "";
    stationsLoaded = false;
    loadStations();
    refresh();
  }

  async function runSeed() {
    if (!confirm("Kompletten Server-Bestand in die lokale Instanz übernehmen?")) return;
    await run("seed", $("#wlsync-force").checked ? "&force=1" : "");
  }
  async function applyDeletions() {
    let d;
    try { d = (await (await fetch(withUser("/_sync/api/diff"))).json()).last_diff || {}; } catch (e) { return; }
    const dels = d.deletions || [];
    if (!dels.length) return;
    const lines = dels.map((x) =>
      "  " + (x.action === "delete_local" ? "LOKAL:   " : "SERVER:  ") +
      x.date + " " + x.time + "  " + x.call + "  " + x.band + " " + x.mode + "  (" + x.station + ")");
    const msg = "Diese " + dels.length + " QSO(s) werden ENDGÜLTIG gelöscht:\n\n" +
      lines.join("\n") +
      "\n\nLOKAL  = wurde am Server gelöscht, wird jetzt lokal entfernt." +
      "\nSERVER = wurde lokal gelöscht, wird jetzt am Server entfernt." +
      "\n\nFortfahren?";
    if (!confirm(msg)) return;
    const r = await (await fetch(withUser("/_sync/api/apply_deletions"), { method: "POST" })).json();
    $("#wlsync-log").textContent = "[Löschungen] " + (r.ok ? "OK" : "teilweise FEHLER") + "\n" + (r.log || []).join("\n");
    refresh();
  }

  async function runReset() {
    const p = await (await fetch(withUser("/_sync/api/pending"))).json();
    let m = "Profil " + (user || "") + " lokal auf Server-Stand zurücksetzen?\n\nAlle lokalen QSOs, " +
      "Contest-Sessions und Stationen dieses Profils werden GELÖSCHT und neu vom Server geladen.";
    if (p.pending_qsos) m += "\n\nACHTUNG: " + p.pending_qsos + " ungesyncte QSO(s) gehen verloren!";
    if (!confirm(m) || !confirm("Wirklich sicher? Nicht rückgängig zu machen.")) return;
    await run("reset", "&force=1");
  }

  page.addEventListener("change", (e) => {
    if (e.target.classList && e.target.classList.contains("wlsync-feat")) updateScopes();
  });

  page.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    if (act === "savecfg") saveCfg();
    else if (act === "seed") runSeed();
    else if (act === "reset") runReset();
    else if (act === "ack") fetch(withUser("/_sync/api/contest_ack"), { method: "POST" }).then(refresh);
    else if (act === "deletions") applyDeletions();
    else run(act);
  });

  refresh();
  setInterval(refresh, 60000);
})();
