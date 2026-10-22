"use strict";

/* ---------------------------------------------------------------- utilidades */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const KIND_ES = {
  link_down: "Enlace caído",
  threshold_rx: "Tope de entrada",
  threshold_tx: "Tope de salida",
  anomaly_rx: "Consumo anormal RX",
  anomaly_tx: "Consumo anormal TX",
  router_down: "Router sin respuesta",
};

function fmtRate(bps) {
  if (bps == null) return "—";
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + " Gbps";
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mbps";
  if (bps >= 1e3) return Math.round(bps / 1e3) + " kbps";
  return Math.round(bps) + " bps";
}

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " s";
  if (s < 3600) return Math.floor(s / 60) + " min";
  if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min";
  return Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h";
}

const pad = (n) => String(n).padStart(2, "0");
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function fmtDateTime(ts) {
  const d = new Date(ts * 1000);
  return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + " " + fmtClock(ts) + ":" + pad(d.getSeconds());
}

async function api(path, opts = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const res = await fetch(path, o);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || detail;
      if (Array.isArray(detail)) detail = detail.map((x) => x.msg).join("; ");
    } catch (_) { /* sin cuerpo JSON */ }
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isErr ? 6000 : 3000);
}

/* -------------------------------------------------------------------- sonido */

let soundOn = localStorage.getItem("mk_sound") === "1";
let audio = null;

function ensureAudio() {
  try {
    if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === "suspended") audio.resume();
  } catch (_) { audio = null; }
}

function beep(urgent) {
  if (!soundOn || !audio || audio.state !== "running") return;
  const tones = urgent ? [880, 660, 880, 660] : [660, 660];
  tones.forEach((f, i) => {
    const t = audio.currentTime + i * 0.22;
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = f;
    osc.type = "square";
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.15, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
    osc.connect(gain).connect(audio.destination);
    osc.start(t);
    osc.stop(t + 0.2);
  });
}

function paintSoundBtn() {
  $("#soundBtn").textContent = soundOn ? "🔊 Sonido" : "🔇 Sonido";
}

/* ------------------------------------------------------------------- estado */

let S = null;
let tab = "panel";
const seen = new Set();
let lastRepeat = 0;

async function refresh() {
  try {
    S = await api("/api/state");
    $("#offline").hidden = true;
    onState();
  } catch (e) {
    $("#offline").hidden = false;
  }
}

function onState() {
  $("#demoBadge").hidden = !S.info.demo;

  const enabled = S.routers.filter((r) => r.enabled);
  const up = enabled.filter((r) => r.reachable === true).length;
  const chipR = $("#chipRouters");
  chipR.textContent = `Routers ${up}/${enabled.length}`;
  chipR.className = "chip " + (enabled.length && up === enabled.length ? "ok" : enabled.length ? "crit" : "");

  const unacked = S.alerts.filter((a) => !a.acked_at);
  const chipA = $("#chipAlerts");
  chipA.textContent = S.alerts.length ? `${S.alerts.length} alerta${S.alerts.length > 1 ? "s" : ""}` : "Sin alertas";
  chipA.className = "chip " + (!S.alerts.length ? "ok" : unacked.some((a) => a.severity === "critical") ? "crit" : "warn");
  document.title = (unacked.length ? `(${unacked.length}) ` : "") + "MK Monitor";

  // Sonido: al aparecer alertas nuevas y cada 30 s mientras queden sin reconocer.
  const fresh = unacked.filter((a) => !seen.has(a.id));
  S.alerts.forEach((a) => seen.add(a.id));
  const now = Date.now();
  if (fresh.length) {
    beep(fresh.some((a) => a.severity === "critical"));
    lastRepeat = now;
  } else if (unacked.length && now - lastRepeat > 30000) {
    beep(unacked.some((a) => a.severity === "critical"));
    lastRepeat = now;
  }

  if (tab === "panel") renderPanel();
}

/* -------------------------------------------------------------------- panel */

function alertHTML(a) {
  const icon = a.severity === "critical" ? "🔴" : "🟠";
  const where = esc(a.router || "?") + (a.iface ? " · " + esc(a.iface) : "");
  const cls = "alert " + a.severity + (a.acked_at ? " acked" : " new");
  const action = a.acked_at
    ? '<span class="muted">reconocida</span>'
    : `<button class="ghost small" data-ack="${a.id}">Reconocer</button>`;
  return `<div class="${cls}">
    <div class="icon">${icon}</div>
    <div><div class="where">${where} <span class="pill">${esc(KIND_ES[a.kind] || a.kind)}</span></div>
      <div class="msg">${esc(a.message)}</div>
      <div class="when">hace ${fmtDur(S.now - a.started_at)} · desde ${fmtDateTime(a.started_at)}</div></div>
    <div class="actions">${action}</div></div>`;
}

function rateRow(dir, cls, bps, maxMbps, base) {
  const scale = maxMbps ? maxMbps * 1e6 : Math.max((base || 0) * 4, 100e6);
  const pct = bps == null ? 0 : Math.min(100, (bps / scale) * 100);
  let fill = "";
  if (maxMbps && bps != null) fill = bps >= maxMbps * 1e6 ? "over" : bps >= maxMbps * 1e6 * 0.9 ? "hot" : "";
  const tick = base && !maxMbps ? `<b style="left:${Math.min(100, (base / scale) * 100)}%" title="promedio habitual"></b>` : "";
  return `<div class="rate"><span class="dir">${dir}</span>
    <div class="bar ${cls}"><i class="${fill}" style="width:${pct.toFixed(1)}%"></i>${tick}</div>
    <span class="val">${fmtRate(bps)}</span></div>`;
}

function sparkSVG(spark) {
  if (!spark || spark.length < 2) return '<svg class="spark" viewBox="0 0 100 34"></svg>';
  const max = Math.max(1, ...spark.map((p) => Math.max(p[0], p[1])));
  const x = (i) => (i / (spark.length - 1)) * 100;
  const y = (v) => 32 - (v / max) * 30;
  const line = (k, color) =>
    `<polyline fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" points="${spark
      .map((p, i) => x(i).toFixed(1) + "," + y(p[k]).toFixed(1))
      .join(" ")}"/>`;
  return `<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none">${line(0, "#4aa3ff")}${line(1, "#37d6b0")}</svg>`;
}

function tileHTML(r, i) {
  const stale = r.reachable === false;
  const linkDown = i.alerts.includes("link_down");
  const warn = i.alerts.some((a) => a !== "link_down");
  let cls = "tile";
  if (!i.monitored) cls += " off";
  else if (linkDown) cls += " down";
  else if (warn) cls += " warn";
  else if (i.running === false && !i.ever_up) cls += " idle";
  if (stale) cls += " stale";

  let dot = "";
  if (i.monitored && !i.disabled) dot = i.running ? "ok" : i.ever_up ? "crit" : "";

  const pills = [];
  if (!i.monitored) pills.push('<span class="pill">no monitoreada</span>');
  else if (i.disabled) pills.push('<span class="pill">deshabilitada</span>');
  else if (linkDown) pills.push('<span class="pill crit">ENLACE CAÍDO</span>');
  else if (i.running === false) pills.push('<span class="pill">sin enlace</span>');
  if (i.alerts.some((a) => a.startsWith("threshold"))) pills.push('<span class="pill warn">TOPE</span>');
  if (i.alerts.some((a) => a.startsWith("anomaly"))) pills.push('<span class="pill warn">ANOMALÍA</span>');

  let foot = "";
  if (i.monitored && i.running) {
    foot = i.learning
      ? "aprendiendo consumo habitual…"
      : i.base_rx_bps != null
        ? `habitual RX ${fmtRate(i.base_rx_bps)} · TX ${fmtRate(i.base_tx_bps)}`
        : "";
  }

  return `<div class="${cls}" data-iface="${esc(i.name)}" data-rid="${r.id}">
    <div class="tile-head"><span class="dot ${dot}"></span>
      <div style="min-width:0"><div class="tile-name">${esc(i.name)}</div>
      <div class="tile-label">${esc(i.label)}</div></div>
      <div class="tile-badges">${pills.join("")}</div></div>
    ${rateRow("RX", "rx", i.running === false ? null : i.rx_bps, i.max_rx_mbps, i.base_rx_bps)}
    ${rateRow("TX", "tx", i.running === false ? null : i.tx_bps, i.max_tx_mbps, i.base_tx_bps)}
    ${sparkSVG(i.spark)}
    <div class="tile-foot">${esc(foot)}</div></div>`;
}

function routerHTML(r) {
  let state = '<span class="dot"></span><span class="muted">conectando…</span>';
  if (r.reachable === true) state = '<span class="dot ok"></span><span class="muted">en línea</span>';
  else if (r.reachable === false) state = '<span class="dot crit"></span><span class="router-err">sin respuesta</span>';
  const err = r.reachable === false && r.error ? `<div class="router-err">${esc(r.error)}</div>` : "";
  const tiles = r.ifaces.length
    ? r.ifaces.map((i) => tileHTML(r, i)).join("")
    : '<div class="muted">Todavía no hay interfaces leídas de este router.</div>';
  return `<div class="router"><div class="router-head"><h2>${esc(r.name)}</h2>
    <span class="host">${esc(r.host)}:${r.port}</span>${state}</div>${err}
    <div class="grid">${tiles}</div></div>`;
}

function renderPanel() {
  const box = $("#alertsBox");
  if (!S.alerts.length) {
    box.innerHTML = '<div class="allclear">✅ Todo normal: sin alertas activas</div>';
  } else {
    const unacked = S.alerts.filter((a) => !a.acked_at).length;
    const head = `<div class="alerts-head"><h2>Alertas activas (${S.alerts.length})</h2>
      ${unacked > 1 ? '<button class="ghost small" data-ackall="1">Reconocer todas</button>' : ""}</div>`;
    box.innerHTML = head + S.alerts.map(alertHTML).join("");
  }
  const enabled = S.routers.filter((r) => r.enabled);
  $("#routers").innerHTML = enabled.length
    ? enabled.map(routerHTML).join("")
    : '<div class="card">Todavía no hay MikroTik cargados. Andá a <b>Configuración</b> para conectar el primero.</div>';
}

/* -------------------------------------------------------------- modal/gráfico */

let M = null; // {rid, name, minutes}

function niceMax(v) {
  const e = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
}

function chartSVG(points, bucket, minutes) {
  if (!points.length) return '<p class="muted">Todavía no hay historial para este período.</p>';
  const W = 800, H = 260, L = 70, R = 12, T = 12, B = 26;
  const t0 = points[0][0];
  const t1 = Math.max(points[points.length - 1][0], t0 + 1);
  const ymax = niceMax(Math.max(1, ...points.map((p) => Math.max(p[3] || 0, p[4] || 0))));
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - v / ymax) * (H - T - B);
  const path = (k) => {
    let d = "", prev = null;
    for (const p of points) {
      const cmd = prev === null || p[0] - prev > bucket * 3 ? "M" : "L";
      d += `${cmd}${x(p[0]).toFixed(1)},${y(p[k] || 0).toFixed(1)} `;
      prev = p[0];
    }
    return d;
  };
  let grid = "";
  for (let k = 0; k <= 4; k++) {
    const v = (ymax * k) / 4, yy = y(v);
    grid += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="#2a3441"/><text x="${L - 8}" y="${yy + 4}" text-anchor="end">${fmtRate(v)}</text>`;
  }
  let xl = "";
  for (let k = 0; k <= 5; k++) {
    const t = t0 + ((t1 - t0) * k) / 5;
    const label = minutes > 1440 ? fmtDateTime(t).slice(0, 11) : fmtClock(t);
    xl += `<text x="${x(t)}" y="${H - 6}" text-anchor="middle">${label}</text>`;
  }
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">${grid}${xl}
    <path d="${path(1)}" fill="none" stroke="#4aa3ff" stroke-width="1.8"/>
    <path d="${path(2)}" fill="none" stroke="#37d6b0" stroke-width="1.8"/></svg>`;
}

async function loadChart() {
  if (!M) return;
  const box = $("#chartBox");
  try {
    const h = await api(`/api/history?router_id=${M.rid}&iface=${encodeURIComponent(M.name)}&minutes=${M.minutes}`);
    if (!M) return;
    box.innerHTML = chartSVG(h.points, h.bucket_s, M.minutes) +
      '<div class="legend"><span>RX (entrada)</span><span class="tx">TX (salida)</span></div>';
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function openIface(rid, name) {
  const r = S.routers.find((x) => x.id === rid);
  const i = r && r.ifaces.find((x) => x.name === name);
  if (!i) return;
  M = { rid, name, minutes: 60 };
  const ranges = [[60, "1 h"], [360, "6 h"], [1440, "24 h"], [10080, "7 d"]];
  $("#modalBody").innerHTML = `<h2>${esc(r.name)} · ${esc(i.name)} <span class="muted">${esc(i.label)}</span></h2>
    <div class="row" style="margin:.5rem 0">
      <span>RX <b>${fmtRate(i.rx_bps)}</b></span><span>TX <b>${fmtRate(i.tx_bps)}</b></span>
      <span class="muted">habitual RX ${fmtRate(i.base_rx_bps)} · TX ${fmtRate(i.base_tx_bps)}</span>
      <span style="margin-left:auto" class="row">${ranges
        .map(([m, l]) => `<button class="ghost small" data-range="${m}">${l}</button>`)
        .join("")}</span></div>
    <div id="chartBox"><p class="muted">Cargando…</p></div>`;
  $("#modal").hidden = false;
  loadChart();
}

function closeModal() {
  $("#modal").hidden = true;
  M = null;
}

/* ---------------------------------------------------------------- historial */

async function renderHistory() {
  const state = $("#histFilter").value;
  try {
    const rows = await api(`/api/alerts?state=${state}&limit=300`);
    const now = S ? S.now : Date.now() / 1000;
    const body = rows
      .map((a) => {
        const end = a.resolved_at || now;
        const status = a.resolved_at ? '<span class="res">resuelta</span>' : '<span class="act">ACTIVA</span>';
        return `<tr><td>${fmtDateTime(a.started_at)}</td><td>${esc(a.router || "(eliminado)")}</td>
          <td>${esc(a.iface || "—")}</td>
          <td><span class="sev ${esc(a.severity)}"></span>${esc(KIND_ES[a.kind] || a.kind)}</td>
          <td>${esc(a.message)}</td><td>${fmtDur(end - a.started_at)}</td><td>${status}</td></tr>`;
      })
      .join("");
    $("#histBox").innerHTML = rows.length
      ? `<div class="card tablewrap"><table><thead><tr><th>Inicio</th><th>Router</th><th>Interfaz</th><th>Tipo</th>
         <th>Detalle</th><th>Duración</th><th>Estado</th></tr></thead><tbody>${body}</tbody></table></div>`
      : '<div class="card muted">Todavía no hubo alertas.</div>';
  } catch (e) {
    $("#histBox").innerHTML = `<div class="card router-err">${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------ configuración */

const SETTING_FIELDS = [
  ["poll_interval", "Intervalo de lectura (s)", "Cada cuántos segundos se lee cada MikroTik. Mínimo 2."],
  ["default_max_rx_mbps", "Tope de entrada por defecto (Mbps)", "0 = sin tope. Cada interfaz puede tener el suyo."],
  ["default_max_tx_mbps", "Tope de salida por defecto (Mbps)", "0 = sin tope. Cada interfaz puede tener el suyo."],
  ["threshold_sustain_s", "Segundos sobre el tope para alertar", "Evita alertas por picos de un instante."],
  ["threshold_clear_s", "Segundos bajo el tope para resolver", "Se resuelve al bajar del 90% del tope."],
  ["anomaly_enabled", "Detectar consumo anormal (1 = sí, 0 = no)", "Compara contra el promedio habitual de cada interfaz."],
  ["anomaly_factor", "Veces el promedio para considerar anormal", "Ej.: 3 = alerta si consume el triple de lo habitual."],
  ["anomaly_min_mbps", "Consumo mínimo para evaluar (Mbps)", "Por debajo de esto nunca se alerta por anomalía."],
  ["anomaly_sustain_s", "Segundos sostenido para alertar", ""],
  ["anomaly_clear_s", "Segundos normal para resolver", ""],
  ["baseline_warmup_s", "Aprendizaje inicial (s)", "Tiempo que se tarda en aprender el consumo habitual de una interfaz nueva."],
  ["baseline_tau_s", "Memoria del promedio (s)", "Qué tan rápido se adapta el promedio habitual."],
  ["router_down_after", "Lecturas fallidas para marcar caído", "Cuántas lecturas seguidas sin respuesta antes de alertar."],
  ["sample_every_s", "Intervalo del historial (s)", "Cada cuánto se guarda un punto para los gráficos."],
  ["retention_days", "Días de historial", "Los puntos más viejos se borran solos."],
];

let cfgRouter = null;
let editingId = null;

async function renderConfig() {
  const box = $("#configBox");
  try {
    const [routers, settings, script] = await Promise.all([
      api("/api/routers"),
      api("/api/settings"),
      api("/api/connect-script"),
    ]);
    if (cfgRouter == null || !routers.find((r) => r.id === cfgRouter)) cfgRouter = routers.length ? routers[0].id : null;

    const rows = routers
      .map((r) => {
        const live = S && S.routers.find((x) => x.id === r.id);
        const st = !r.enabled ? "deshabilitado" : live && live.reachable === true ? "en línea" : live && live.reachable === false ? "sin respuesta" : "conectando…";
        return `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.host)}:${r.port}${r.use_ssl ? " (SSL)" : ""}</td>
          <td>${esc(r.username)}</td><td>${esc(r.types)}${r.include ? " + " + esc(r.include) : ""}</td><td>${st}</td>
          <td class="row"><button class="ghost small" data-edit="${r.id}">Editar</button>
          <button class="ghost small" data-testid="${r.id}">Probar</button>
          <button class="danger small" data-del="${r.id}">Eliminar</button></td></tr>`;
      })
      .join("");

    const fields = SETTING_FIELDS.map(
      ([k, label, help]) => `<div class="field"><label>${esc(label)}</label>
        <input type="number" step="any" min="0" data-setting="${k}" value="${settings.settings[k]}">
        ${help ? `<small>${esc(help)}</small>` : ""}</div>`
    ).join("");

    box.innerHTML = `
      <div class="card"><h2>Conectar un MikroTik nuevo</h2>
        <ol class="steps">
          <li>Copiá el script de abajo y pegalo en una terminal del MikroTik (Winbox → New Terminal).</li>
          <li>El script crea un usuario de <b>solo lectura</b>, limita la API a este servidor y registra el router acá automáticamente.</li>
          <li>En unos segundos aparece en el Panel. Si el servidor no es alcanzable por esa URL, cambiá la línea <code>serverUrl</code> del script.</li>
        </ol>
        <div class="row" style="margin-bottom:.5rem"><button id="copyScript">Copiar script</button>
          <span class="muted">Contiene el token de registro: no lo compartas fuera de tu equipo.</span></div>
        <pre class="script" id="scriptText">${esc(script)}</pre></div>

      <div class="card"><h2>Routers</h2>
        <div class="tablewrap">${routers.length ? `<table><thead><tr><th>Nombre</th><th>Dirección</th><th>Usuario API</th><th>Tipos leídos</th><th>Estado</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">Sin routers todavía.</p>'}</div>
        <h3 style="margin-top:1rem" id="formTitle">Agregar manualmente</h3>
        <div class="form-grid" id="routerForm">
          <div class="field"><label>Nombre</label><input id="f_name" placeholder="Core-Principal"></div>
          <div class="field"><label>IP o dominio</label><input id="f_host" placeholder="192.168.88.1"></div>
          <div class="field"><label>Puerto API</label><input id="f_port" type="number" value="8728"><small>8728 normal · 8729 con SSL</small></div>
          <div class="field"><label>Usuario</label><input id="f_user" placeholder="mkmonitor"></div>
          <div class="field"><label>Contraseña</label><input id="f_pass" type="password" autocomplete="new-password"><small id="f_passhelp"></small></div>
          <div class="field"><label>Tipos de interfaz a leer</label><input id="f_types" value="ether"><small>Separados por coma. Los puertos SFP/SFP+ de RouterOS 7 son tipo <code>ether</code>.</small></div>
          <div class="field"><label>Interfaces extra por nombre</label><input id="f_include" placeholder="bridge1, bonding1"><small>Opcional.</small></div>
          <div class="field"><label>Usar SSL</label><select id="f_ssl"><option value="0">No</option><option value="1">Sí</option></select></div>
          <div class="field"><label>Monitoreo</label><select id="f_enabled"><option value="1">Activo</option><option value="0">Pausado</option></select></div>
        </div>
        <div class="row" style="margin-top:.8rem"><button id="saveRouter">Guardar</button>
          <button class="ghost" id="testForm">Probar conexión</button>
          <button class="ghost" id="cancelEdit" hidden>Cancelar edición</button></div>
        <div id="testResult" style="margin-top:.6rem"></div></div>

      <div class="card"><h2>Umbrales y detección</h2>
        <div class="form-grid">${fields}</div>
        <div class="row" style="margin-top:.8rem"><button id="saveSettings">Guardar umbrales</button></div></div>

      <div class="card"><div class="toolbar"><h2>Interfaces de cada router</h2>
        <label>Router <select id="ifRouter">${routers
          .map((r) => `<option value="${r.id}" ${r.id === cfgRouter ? "selected" : ""}>${esc(r.name)}</option>`)
          .join("")}</select></label></div>
        <div id="ifaceBox"><p class="muted">Cargando…</p></div></div>

      <div class="card"><h2>Notificaciones</h2>
        <p>Telegram: <b>${S && S.info.telegram ? "configurado" : "sin configurar"}</b>.
        Se activa con <code>MKMON_TELEGRAM_TOKEN</code> y <code>MKMON_TELEGRAM_CHAT</code> en <code>/etc/mk-monitor/mk-monitor.env</code>.</p></div>`;

    if (cfgRouter != null) renderIfaces();
  } catch (e) {
    box.innerHTML = `<div class="card router-err">${esc(e.message)}</div>`;
  }
}

async function renderIfaces() {
  const box = $("#ifaceBox");
  if (!box) return;
  try {
    const rows = await api(`/api/routers/${cfgRouter}/interfaces`);
    if (!rows.length) {
      box.innerHTML = '<p class="muted">Todavía no se leyeron interfaces de este router.</p>';
      return;
    }
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th>Interfaz</th><th>Etiqueta</th><th>Monitorear</th>
      <th>Alerta enlace caído</th><th class="num">Tope RX (Mbps)</th><th class="num">Tope TX (Mbps)</th><th></th></tr></thead><tbody>${rows
        .map(
          (r) => `<tr data-row="${esc(r.name)}"><td><b>${esc(r.name)}</b> <span class="muted">${esc(r.type)}</span></td>
          <td><input type="text" data-f="label" value="${esc(r.label)}" maxlength="60"></td>
          <td><input type="checkbox" data-f="monitor" ${r.monitor ? "checked" : ""}></td>
          <td><input type="checkbox" data-f="link_alert" ${r.link_alert ? "checked" : ""}></td>
          <td><input type="number" min="0" step="any" data-f="max_rx_mbps" placeholder="global" value="${r.max_rx_mbps ?? ""}"></td>
          <td><input type="number" min="0" step="any" data-f="max_tx_mbps" placeholder="global" value="${r.max_tx_mbps ?? ""}"></td>
          <td><button class="ghost small" data-saveif="${esc(r.name)}">Guardar</button></td></tr>`
        )
        .join("")}</tbody></table></div>
      <p class="muted" style="margin-top:.5rem">Tope vacío = usa el global · 0 = sin tope. "Alerta enlace caído" solo avisa si la interfaz ya estuvo arriba alguna vez.</p>`;
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function formData() {
  return {
    name: $("#f_name").value.trim(),
    host: $("#f_host").value.trim(),
    port: parseInt($("#f_port").value, 10) || 8728,
    username: $("#f_user").value.trim(),
    password: $("#f_pass").value,
    types: $("#f_types").value.trim() || "ether",
    include: $("#f_include").value.trim(),
    use_ssl: $("#f_ssl").value === "1",
    enabled: $("#f_enabled").value === "1",
  };
}

function fillForm(r) {
  $("#f_name").value = r.name;
  $("#f_host").value = r.host;
  $("#f_port").value = r.port;
  $("#f_user").value = r.username;
  $("#f_pass").value = "";
  $("#f_types").value = r.types;
  $("#f_include").value = r.include;
  $("#f_ssl").value = r.use_ssl ? "1" : "0";
  $("#f_enabled").value = r.enabled ? "1" : "0";
  $("#f_passhelp").textContent = "Vacía = se mantiene la actual.";
  $("#formTitle").textContent = "Editar router";
  $("#cancelEdit").hidden = false;
  $("#routerForm").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function runTest(payload) {
  const out = $("#testResult");
  out.innerHTML = '<span class="muted">Probando conexión…</span>';
  try {
    const res = await api("/api/routers/test", { method: "POST", body: payload });
    if (!res.ok) {
      out.innerHTML = `<span class="router-err">No se pudo conectar: ${esc(res.error)}</span>`;
      return;
    }
    out.innerHTML = `<span class="res">✔ Conexión correcta.</span> Interfaces encontradas (${res.interfaces.length}): ` +
      res.interfaces
        .map((i) => `<span class="pill">${esc(i.name)}${i.running ? " ↑" : " ↓"}</span>`)
        .join(" ");
  } catch (e) {
    out.innerHTML = `<span class="router-err">${esc(e.message)}</span>`;
  }
}

/* ----------------------------------------------------------------- eventos */

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, .tile");
  if (!t) return;
  ensureAudio();
  try {
    if (t.dataset.tab) {
      tab = t.dataset.tab;
      document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === t));
      ["panel", "history", "config"].forEach((n) => ($("#tab-" + n).hidden = n !== tab));
      if (tab === "panel" && S) renderPanel();
      if (tab === "history") renderHistory();
      if (tab === "config") renderConfig();
    } else if (t.id === "soundBtn") {
      soundOn = !soundOn;
      localStorage.setItem("mk_sound", soundOn ? "1" : "0");
      paintSoundBtn();
      ensureAudio();
      if (soundOn) beep(false);
    } else if (t.dataset.ack) {
      await api(`/api/alerts/${t.dataset.ack}/ack`, { method: "POST" });
      refresh();
    } else if (t.dataset.ackall) {
      await api("/api/alerts/ack_all", { method: "POST" });
      refresh();
    } else if (t.dataset.iface) {
      openIface(parseInt(t.dataset.rid, 10), t.dataset.iface);
    } else if (t.dataset.range) {
      if (M) { M.minutes = parseInt(t.dataset.range, 10); loadChart(); }
    } else if (t.id === "modalClose") {
      closeModal();
    } else if (t.id === "copyScript") {
      const text = $("#scriptText").textContent;
      try { await navigator.clipboard.writeText(text); toast("Script copiado"); }
      catch (_) {
        const r = document.createRange(); r.selectNodeContents($("#scriptText"));
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
        toast("Seleccionado: copialo con Ctrl+C");
      }
    } else if (t.id === "saveRouter") {
      const d = formData();
      if (editingId) await api(`/api/routers/${editingId}`, { method: "PUT", body: d });
      else await api("/api/routers", { method: "POST", body: d });
      toast("Router guardado");
      editingId = null;
      await renderConfig();
      refresh();
    } else if (t.id === "testForm") {
      runTest({ ...formData(), router_id: editingId });
    } else if (t.id === "cancelEdit") {
      editingId = null;
      renderConfig();
    } else if (t.dataset.edit) {
      const list = await api("/api/routers");
      const r = list.find((x) => x.id === parseInt(t.dataset.edit, 10));
      if (r) { editingId = r.id; fillForm(r); }
    } else if (t.dataset.testid) {
      const list = await api("/api/routers");
      const r = list.find((x) => x.id === parseInt(t.dataset.testid, 10));
      if (r) runTest({ ...r, password: "", router_id: r.id });
    } else if (t.dataset.del) {
      if (confirm("¿Eliminar este router y su historial de tráfico?")) {
        await api(`/api/routers/${t.dataset.del}`, { method: "DELETE" });
        toast("Router eliminado");
        await renderConfig();
        refresh();
      }
    } else if (t.id === "saveSettings") {
      const body = {};
      document.querySelectorAll("[data-setting]").forEach((el) => (body[el.dataset.setting] = parseFloat(el.value)));
      await api("/api/settings", { method: "PUT", body });
      toast("Umbrales guardados");
    } else if (t.dataset.saveif) {
      const row = document.querySelector(`tr[data-row="${CSS.escape(t.dataset.saveif)}"]`);
      const num = (f) => { const v = row.querySelector(`[data-f="${f}"]`).value; return v === "" ? null : parseFloat(v); };
      await api(`/api/routers/${cfgRouter}/interfaces/${encodeURIComponent(t.dataset.saveif)}`, {
        method: "PUT",
        body: {
          label: row.querySelector('[data-f="label"]').value,
          monitor: row.querySelector('[data-f="monitor"]').checked,
          link_alert: row.querySelector('[data-f="link_alert"]').checked,
          max_rx_mbps: num("max_rx_mbps"),
          max_tx_mbps: num("max_tx_mbps"),
        },
      });
      toast("Interfaz guardada");
    }
  } catch (e) {
    toast(e.message, true);
  }
});

document.addEventListener("change", (ev) => {
  if (ev.target.id === "ifRouter") { cfgRouter = parseInt(ev.target.value, 10); renderIfaces(); }
  if (ev.target.id === "histFilter") renderHistory();
});

$("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeModal(); });

function tickClock() {
  const d = new Date();
  $("#clock").textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

paintSoundBtn();
tickClock();
setInterval(tickClock, 1000);
setInterval(refresh, 3000);
setInterval(() => { if (tab === "history") renderHistory(); }, 8000);
refresh();
