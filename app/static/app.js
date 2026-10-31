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
  pppoe_down: "Cliente PPPoE caído",
  pppoe_mass: "Caída masiva PPPoE",
  pon_down: "Sector caído (PON)",
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
  const o = { headers: { "Content-Type": "application/json", "X-Requested-With": "MAGI" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const res = await fetch(path, o);
  if (res.status === 401 && path !== "/api/login") {
    // La sesión venció (o nunca hubo): se vuelve a la pantalla de acceso.
    window.location.href = "/login";
    throw new Error("Sesión vencida");
  }
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

/* Las tres unidades de MAGI resumen la salud del sistema (como en el anime: cada una "vota"). */
function magiUnitStates() {
  const k = (kind) => S.alerts.filter((a) => a.kind === kind);
  const iface = S.alerts.filter((a) => ["link_down", "threshold_rx", "threshold_tx", "anomaly_rx", "anomaly_tx", "router_down"].includes(a.kind));
  const enabled = S.routers.filter((r) => r.enabled);
  const m1 = enabled.some((r) => r.reachable === false) || iface.some((a) => a.severity === "critical") ? "crit" : iface.length ? "warn" : "ok";
  const m2 = k("pppoe_mass").length ? "crit" : k("pppoe_down").length ? "warn" : "ok";
  const m3 = k("pon_down").length ? "crit" : "ok";
  return [m1, m2, m3];
}

function renderMagiUnits() {
  const st = magiUnitStates();
  st.forEach((v, i) => ($("#mu" + (i + 1)).className = "mu " + v));
  const worst = st.includes("crit") ? "crit" : st.includes("warn") ? "warn" : "ok";
  $("#magiUnits").dataset.state = worst;
}

let ME = null;   // { authed, user, role } de /api/me

async function loadMe() {
  try {
    const me = await api("/api/me");
    ME = me;
    const b = $("#logoutBtn");
    b.hidden = false;
    b.textContent = me.authed ? "Salir" : "Entrar";
    b.dataset.authed = me.authed ? "1" : "";
  } catch (_) { /* la pantalla de acceso se abre sola si hace falta */ }
}

function onState() {
  $("#demoBadge").hidden = !S.info.demo;
  renderMagiUnits();

  const enabled = S.routers.filter((r) => r.enabled);
  const up = enabled.filter((r) => r.reachable === true).length;
  const chipR = $("#chipRouters");
  chipR.textContent = `Routers ${up}/${enabled.length}`;
  chipR.className = "chip " + (enabled.length && up === enabled.length ? "ok" : enabled.length ? "crit" : "");

  const unacked = S.alerts.filter((a) => !a.acked_at);
  const chipA = $("#chipAlerts");
  chipA.textContent = S.alerts.length ? `${S.alerts.length} alerta${S.alerts.length > 1 ? "s" : ""}` : "Sin alertas";
  chipA.className = "chip " + (!S.alerts.length ? "ok" : unacked.some((a) => a.severity === "critical") ? "crit" : "warn");
  document.title = (unacked.length ? `(${unacked.length}) ` : "") + "MAGI";

  // Sonido: al aparecer alertas nuevas y cada 30 s mientras queden sin reconocer.
  const fresh = unacked.filter((a) => !seen.has(a.id));
  S.alerts.forEach((a) => seen.add(a.id));
  const now = Date.now();
  if (fresh.length) {
    beep(fresh.some((a) => a.severity === "critical"));
    lastRepeat = now;
  } else if (now - lastRepeat > 30000) {
    // Las alertas de clientes PPPoE sueltos suenan al aparecer, pero no se repiten.
    const loud = unacked.filter((a) => a.kind !== "pppoe_down");
    if (loud.length) {
      beep(loud.some((a) => a.severity === "critical"));
      lastRepeat = now;
    }
  }

  if (tab === "panel") renderPanel();
}

/* -------------------------------------------------------------------- panel */

const canWrite = () => !(ME && ME.authed && ME.role !== "admin");

function alertHTML(a) {
  const icon = a.severity === "critical" ? "🔴" : "🟠";
  const where = esc(a.router || "?") + (a.iface ? " · " + esc(a.iface) : "");
  const cls = "alert " + a.severity + (a.acked_at ? " acked" : " new");
  const w = canWrite();
  const action = a.acked_at
    ? `<span class="muted">reconocida</span>${w ? ` <button class="ghost small" data-delalert="${a.id}" title="Borrar esta alerta">🗑 Borrar</button>` : ""}`
    : w ? `<button class="ghost small" data-ack="${a.id}">Reconocer</button>` : "";
  const map = a.kind === "pon_down" ? ' <button class="ghost small" data-tabgo="network">Ver mapa</button>' : "";
  return `<div class="${cls}">
    <div class="icon">${icon}</div>
    <div><div class="where">${where} <span class="pill">${esc(KIND_ES[a.kind] || a.kind)}</span></div>
      <div class="msg">${esc(a.message)}</div>
      <div class="when">hace ${fmtDur(S.now - a.started_at)} · desde ${fmtDateTime(a.started_at)}</div></div>
    <div class="actions">${action}${map}</div></div>`;
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
  // En el Panel solo se muestran las interfaces elegidas para monitorear (Configuración → Interfaces de cada router).
  const shown = r.ifaces.filter((i) => i.monitored);
  const tiles = shown.length
    ? shown.map((i) => tileHTML(r, i)).join("")
    : r.ifaces.length
      ? '<div class="muted">Ninguna interfaz de este router está seleccionada para monitorear. Elegilas en Configuración → Interfaces de cada router.</div>'
      : '<div class="muted">Todavía no hay interfaces leídas de este router.</div>';
  let pp = "";
  const ps = r.pppoe_enabled && r.pppoe;
  if (ps && ps.online + ps.down > 0) {
    const cls = ps.mass ? "pill crit" : ps.down ? "pill warn" : "pill";
    pp = `<span class="${cls}" data-tabgo="pppoe" style="cursor:pointer">PPPoE ${ps.online} en línea${ps.down ? " · " + ps.down + " caídos" : ""}${ps.mass ? " · CAÍDA MASIVA" : ""}</span>`;
  }
  return `<div class="router"><div class="router-head"><h2>${esc(r.name)}</h2>
    <span class="host">${esc(r.host)}:${r.port}</span>${state}${pp}</div>${err}
    <div class="grid">${tiles}</div></div>`;
}

function renderPanel() {
  const box = $("#alertsBox");
  if (!S.alerts.length) {
    box.innerHTML = '<div class="allclear">✅ Todo normal: sin alertas activas</div>';
  } else {
    const unacked = S.alerts.filter((a) => !a.acked_at).length;
    const acked = S.alerts.length - unacked;
    const w = canWrite();
    const head = `<div class="alerts-head"><h2>Alertas activas (${S.alerts.length})</h2><div class="row">
      ${w && unacked > 1 ? '<button class="ghost small" data-ackall="1">Reconocer todas</button>' : ""}
      ${w && acked > 0 ? `<button class="ghost small" data-clearalerts="acked">🗑 Borrar reconocidas (${acked})</button>` : ""}</div></div>`;
    const pppoe = S.alerts.filter((a) => a.kind === "pppoe_down");
    const others = S.alerts.filter((a) => a.kind !== "pppoe_down");
    const shown = pppoe.slice(0, 5);
    let html = head + others.map(alertHTML).join("") + shown.map(alertHTML).join("");
    if (pppoe.length > shown.length) {
      html += `<div class="alert warning more"><div class="icon">🟠</div>
        <div><div class="where">+ ${pppoe.length - shown.length} clientes PPPoE más desconectados</div>
        <div class="msg">Se muestran los 5 más recientes.</div></div>
        <div class="actions"><button class="ghost small" data-tabgo="pppoe">Ver todos</button></div></div>`;
    }
    box.innerHTML = html;
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

/* -------------------------------------------------------------------- PPPoE */

const STATUS_ES = { up: "conectado", alert: "ALERTA", grace: "en espera", mass: "caída masiva", exp: "no volvió" };

function ago(ts, now) {
  return ts ? "hace " + fmtDur(now - ts) : "—";
}

function reasonHTML(r) {
  if (!r.reason) return '<span class="muted">—</span>';
  return r.reason_es ? `${esc(r.reason_es)} <span class="muted">(${esc(r.reason)})</span>` : esc(r.reason);
}

function pppoeSummaryHTML(sum, selected) {
  const cards = sum.routers
    .map((r) => {
      const pick = `data-pprouter="${r.id}" tabindex="0" role="button" title="Clic para ver solo este router"`;
      const on = String(r.id) === String(selected) ? " sel" : "";
      if (!r.monitored) {
        return `<div class="pcard${on}" ${pick}><h3>${esc(r.name)}</h3><div class="sub">PPPoE no se monitorea en este router.</div></div>`;
      }
      if (r.online == null) {
        return `<div class="pcard${on}" ${pick}><h3>${esc(r.name)}</h3><div class="sub">${r.error ? esc(r.error) : "Esperando la primera lectura…"}</div></div>`;
      }
      const cls = r.mass ? "pcard mass" : r.down ? "pcard warn" : "pcard";
      return `<div class="pcard ${cls.replace("pcard", "")}${on}" ${pick}><h3>${esc(r.name)}${r.mass ? ' <span class="pill crit">CAÍDA MASIVA</span>' : ""}</h3>
        <div class="big">${r.online}</div><div class="sub">clientes en línea${r.registered != null ? ` de ${r.registered} registrados` : ""}</div>
        ${r.registered != null ? `<div class="sub">${r.offline} sin conexión${r.disabled ? " · " + r.disabled + " deshabilitados" : ""}</div>` : ""}
        <div class="sub" style="margin-top:.3rem"><span class="${r.down ? "down-n" : ""}">${r.down} desconectados</span>
        · lectura ${ago(r.last_poll, sum.now)}</div>${r.error ? `<div class="router-err">${esc(r.error)}</div>` : ""}</div>`;
    })
    .join("");
  return cards || '<div class="muted">Sin routers.</div>';
}

function syslogLine(sl) {
  if (!sl.listening) {
    return sl.error
      ? `Log del MikroTik: <span class="router-err">no se pudo abrir el puerto UDP ${sl.port} (${esc(sl.error)})</span>. Las alertas funcionan igual, pero sin el motivo de la desconexión.`
      : "Log del MikroTik: apagado (las alertas funcionan igual, pero sin el motivo de la desconexión).";
  }
  if (!sl.received) {
    return `Log del MikroTik: escuchando en UDP ${sl.port}, <b>todavía no llegó ninguna línea</b>. Corré el script de conexión en el router o revisá que el firewall deje pasar UDP ${sl.port}.`;
  }
  return `Log del MikroTik: UDP ${sl.port} · ${sl.received} líneas recibidas, ${sl.parsed} interpretadas.`;
}

function downTableHTML(res) {
  if (!res.sessions.length) return '<p class="muted">Ningún cliente desconectado. ✅</p>';
  const rows = res.sessions
    .map(
      (r) => `<tr><td><b>${esc(r.name)}</b></td><td><span class="st ${r.status}">${STATUS_ES[r.status]}</span></td>
      <td>${ago(r.since, res.now)}</td><td class="wrap">${reasonHTML(r)}</td>
      <td class="mono">${esc(r.address)}</td><td class="mono">${esc(r.caller_id)}</td><td>${esc(r.router)}</td></tr>`
    )
    .join("");
  return `<table><thead><tr><th>Cliente</th><th>Estado</th><th>Caído</th><th>Motivo</th><th>IP</th><th>MAC</th><th>Router</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function eventsTableHTML(res) {
  if (!res.events.length) return '<p class="muted">Todavía no hubo desconexiones.</p>';
  const rows = res.events
    .map(
      (e) => `<tr><td>${fmtDateTime(e.ts)}</td><td>${esc(e.router || "?")}</td><td><b>${esc(e.user)}</b></td>
      <td class="wrap">${reasonHTML(e)}</td></tr>`
    )
    .join("");
  return `<table><thead><tr><th>Hora</th><th>Router</th><th>Cliente</th><th>Motivo</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function searchTableHTML(res) {
  if (!res.sessions.length) return '<p class="muted">Sin resultados.</p>';
  const rows = res.sessions
    .map((r) => {
      const state = r.state === "up" ? `<span class="st up">conectado</span>` : `<span class="st ${r.status}">${STATUS_ES[r.status]}</span>`;
      const since = r.state === "up" ? "conectado " + fmtDur(r.was_up_s) : ago(r.since, res.now);
      return `<tr><td><b>${esc(r.name)}</b></td><td>${state}</td><td>${since}</td><td class="mono">${esc(r.address)}</td>
        <td class="mono">${esc(r.caller_id)}</td><td>${esc(r.router)}</td><td class="wrap">${r.state === "down" ? reasonHTML(r) : ""}</td></tr>`;
    })
    .join("");
  return `<table><thead><tr><th>Cliente</th><th>Estado</th><th>Tiempo</th><th>IP</th><th>MAC</th><th>Router</th><th>Motivo</th></tr></thead><tbody>${rows}</tbody></table>`;
}

let pppoeKnownRouters = "";

async function renderPppoe() {
  try {
    const sel = $("#pppoeRouter");
    const rid = sel.value;
    const qs = rid ? `&router_id=${rid}` : "";
    const [sum, down, ev] = await Promise.all([
      api("/api/pppoe/summary"),
      api(`/api/pppoe/down?limit=300${qs}`),
      api(`/api/pppoe/events?limit=100${qs}`),
    ]);
    const key = sum.routers.map((r) => r.id + r.name).join("|");
    if (key !== pppoeKnownRouters) {
      pppoeKnownRouters = key;
      sel.innerHTML = '<option value="">todos</option>' + sum.routers.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
      sel.value = rid;
    }
    $("#pppoeSummary").innerHTML = pppoeSummaryHTML(sum, rid);
    const picked = sum.routers.find((r) => String(r.id) === String(rid));
    $("#pppoeScope").innerHTML = picked
      ? `<span class="chip warn">Mostrando solo <b>${esc(picked.name)}</b></span> <button class="ghost small" data-pprouter="">Ver todos los routers</button>`
      : sum.routers.length > 1 ? '<span class="muted">Tocá un router para ver solo sus conexiones.</span>' : "";
    $("#pppoeSyslog").innerHTML = syslogLine(sum.syslog);
    $("#pppoeDownCount").textContent = down.total ? `(${down.total})` : "";
    $("#pppoeDown").innerHTML = downTableHTML(down);
    $("#pppoeEvents").innerHTML = eventsTableHTML(ev);
    renderPppoeReg();
    renderPppoeRep();
    if ($("#pppoeLogBox").open) renderPppoeLog();
  } catch (e) {
    $("#pppoeSummary").innerHTML = `<div class="card router-err">${esc(e.message)}</div>`;
  }
}

function repeatHTML(res) {
  const hot = res.hot_pons.length
    ? `<p class="router-err" style="margin:.2rem 0 .6rem">⚠ Varios clientes problemáticos en el mismo PON: ${res.hot_pons
        .map((h) => `<b>${esc(h.pon)}</b> (${h.clients})`).join(", ")}. Puede ser la fibra o el splitter, no cada cliente.</p>`
    : "";
  if (!res.rows.length) {
    return { hot, table: `<p class="muted">Nadie llegó a ${res.min} caídas en ${res.days === 1 ? "el último día" : "los últimos " + res.days + " días"}. ✅</p>` };
  }
  const now = res.now;
  const rows = res.rows
    .map((r) => {
      const others = r.reasons.slice(1).map((x) => `${esc(x.reason_es || x.reason)} ×${x.n}`).join(" · ");
      const top = r.top_reason ? `${esc(r.top_reason_es || r.top_reason)} <span class="muted">×${r.reasons[0].n}</span>` : '<span class="muted">sin motivo en el log</span>';
      const st = r.state === "up" ? '<span class="pill">conectado</span>' : r.state === "down" ? '<span class="pill crit">caído</span>' : "";
      return `<tr><td><b>${esc(r.name)}</b><div class="muted mono">${esc(r.address)}</div></td><td>${esc(r.router)}</td><td>${esc(r.pon) || '<span class="muted">—</span>'}</td>
        <td class="num"><b>${r.count}</b><div class="muted">${r.per_day}/día</div></td>
        <td>${ago(r.last_ts, now)}</td><td>${top}${others ? `<div class="muted">${others}</div>` : ""}</td><td>${st}</td></tr>`;
    })
    .join("");
  return { hot, table: `<table><thead><tr><th>Cliente</th><th>Router</th><th>PON</th><th class="num">Caídas</th><th>Última</th><th>Motivo más común</th><th>Ahora</th></tr></thead><tbody>${rows}</tbody></table>` };
}

async function renderPppoeRep() {
  const box = $("#pppoeRep");
  if (!box) return;
  const rid = $("#pppoeRouter").value;
  try {
    const res = await api(`/api/pppoe/repeat?days=${$("#pppoeRepDays").value}&min=${$("#pppoeRepMin").value}${rid ? "&router_id=" + rid : ""}`);
    $("#pppoeRepCount").textContent = res.total ? `(${res.total})` : "";
    const v = repeatHTML(res);
    $("#pppoeRepHot").innerHTML = v.hot;
    box.innerHTML = v.table;
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function registeredTableHTML(res) {
  if (!res.rows.length) {
    const err = res.routers.map((r) => r.error).filter(Boolean)[0];
    if (err) return `<p class="router-err">${esc(err)}</p>`;
    return res.routers.length
      ? '<p class="muted">Todos los usuarios registrados están conectados.</p>'
      : '<p class="muted">Todavía no se leyeron los usuarios registrados (se leen cada minuto). Si tu router usa RADIUS, no tiene Secrets y esta lista queda vacía.</p>';
  }
  const kindPill = (r) =>
    r.kind === "disabled" ? '<span class="pill warn">deshabilitado</span>'
    : r.never ? '<span class="pill">nunca se conectó</span>'
    : '<span class="pill crit">sin conexión</span>';
  const now = res.now;
  const rows = res.rows
    .map((r) => `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.router)}</td><td>${kindPill(r)}</td>
      <td>${esc(r.last_logged_out) || '<span class="muted">—</span>'}${r.down_since ? `<div class="muted">visto caer ${ago(r.down_since, now)}</div>` : ""}</td>
      <td>${reasonHTML(r)}</td><td>${esc(r.profile)}</td><td>${esc(r.comment)}</td></tr>`)
    .join("");
  const more = res.total > res.rows.length ? `<p class="muted">Mostrando ${res.rows.length} de ${res.total}. Usá el buscador para filtrar.</p>` : "";
  return `<table><thead><tr><th>Usuario</th><th>Router</th><th>Estado</th><th>Última desconexión</th><th>Motivo</th><th>Perfil</th><th>Nota</th></tr></thead><tbody>${rows}</tbody></table>${more}`;
}

async function renderPppoeReg() {
  const box = $("#pppoeReg");
  if (!box) return;
  const rid = $("#pppoeRouter").value;
  const kind = $("#pppoeRegKind").value;
  try {
    const res = await api(`/api/pppoe/registered?kind=${kind}${rid ? "&router_id=" + rid : ""}`);
    $("#pppoeRegCount").textContent = res.total ? `(${res.total})` : "";
    box.innerHTML = registeredTableHTML(res);
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

async function renderPppoeSearch() {
  const q = $("#pppoeQ").value.trim();
  const box = $("#pppoeSearch");
  if (q.length < 2) { box.innerHTML = ""; return; }
  try {
    box.innerHTML = searchTableHTML(await api(`/api/pppoe/search?q=${encodeURIComponent(q)}`));
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

async function renderPppoeLog() {
  const box = $("#pppoeLog");
  try {
    const sl = await api("/api/pppoe/syslog");
    box.innerHTML = sl.lines.length
      ? `<table><thead><tr><th>Hora</th><th>Desde</th><th>Línea</th><th>¿Interpretada?</th></tr></thead><tbody>${sl.lines
          .map((l) => `<tr><td>${fmtDateTime(l.ts)}</td><td class="mono">${esc(l.ip)}</td><td class="mono wrap">${esc(l.text)}</td><td>${l.parsed ? "sí" : "—"}</td></tr>`)
          .join("")}</tbody></table>`
      : '<p class="muted">Todavía no llegó ninguna línea.</p>';
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
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
          <td>${esc(a.message)}</td><td>${fmtDur(end - a.started_at)}</td><td>${status}</td>
          ${canWrite() ? `<td><button class="ghost small" data-delalert="${a.id}" title="Borrar">🗑</button></td>` : ""}</tr>`;
      })
      .join("");
    const hb = $("#histBulk");
    if (hb) hb.hidden = !canWrite() || !rows.length;
    $("#histBox").innerHTML = rows.length
      ? `<div class="card tablewrap"><table><thead><tr><th>Inicio</th><th>Router</th><th>Interfaz</th><th>Tipo</th>
         <th>Detalle</th><th>Duración</th><th>Estado</th>${canWrite() ? "<th></th>" : ""}</tr></thead><tbody>${body}</tbody></table></div>`
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
  ["backup_enabled", "Respaldos automáticos (1 = sí, 0 = no)", "Base de MAGI y configuración de los routers que tengan el respaldo activado."],
  ["backup_every_h", "Cada cuántas horas respaldar", "24 = una vez al día."],
  ["backup_keep", "Copias de la base de MAGI que se conservan", "Las más viejas se borran solas."],
  ["router_backup_keep", "Versiones de la config de cada router que se conservan", "Solo se guarda una versión nueva si la configuración cambió."],
  ["usage_enabled", "Historial de consumo por cliente (1 = sí, 0 = no)", "Lee los contadores de bytes de cada sesión PPPoE para la pestaña Consumo."],
  ["usage_poll_s", "Cada cuántos segundos leer el consumo", "Mínimo 60. 300 = cada 5 minutos."],
  ["usage_factor", "Veces lo habitual para marcar consumo anormal", "Compara las últimas 24 h con la mediana de los días anteriores del mismo cliente."],
  ["usage_min_gb", "GB mínimos en 24 h para marcar anormal", "Evita marcar a clientes que consumen muy poco."],
  ["pppoe_enabled", "Monitorear PPPoE (1 = sí, 0 = no)", "Apaga todo el monitoreo de clientes PPPoE."],
  ["pppoe_poll_s", "Lectura de clientes PPPoE (s)", "Cada cuántos segundos se lee la lista de clientes conectados."],
  ["pppoe_grace_s", "Segundos caído antes de alertar", "Si el cliente se reconecta antes, no se alerta: en PPPoE es normal."],
  ["pppoe_mass_threshold", "Clientes caídos a la vez = caída masiva", "Se avisa UNA alerta crítica en vez de una por cliente. 0 = desactivado."],
  ["pppoe_mass_window_s", "Ventana de la caída masiva (s)", "Tiempo en el que se cuentan las desconexiones."],
  ["pon_down_pct", "Sector caído: % de clientes caídos", "Si un PON tiene al menos este % de sus clientes sin conexión, se alerta el sector. 0 = apagado."],
  ["pon_min_clients", "Sector caído: mínimo de clientes del PON", "Un PON con menos clientes conocidos no genera alerta de sector."],
  ["pon_down_after_s", "Sector caído: segundos sostenido", "Tiempo que debe mantenerse para alertar (y para resolver)."],
  ["pppoe_expire_h", "Horas hasta dar por vencida una alerta", "Los clientes que no vuelven (bajas) dejan de alertar pasado este tiempo."],
];

let cfgRouter = null;
let editingId = null;

async function renderConfig() {
  const box = $("#configBox");
  if (ME && ME.authed && ME.role !== "admin") {
    // Un usuario de solo lectura únicamente puede cambiar su propia contraseña.
    box.innerHTML = '<div class="card"><h2>Mi cuenta</h2><div id="usersBox"></div></div>';
    renderUsers();
    return;
  }
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
          <td>${esc(r.username)}</td><td>${esc(r.types)}${r.include ? " + " + esc(r.include) : ""}</td><td>${r.pppoe_enabled ? "sí" : "—"}</td><td>${st}</td>
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
      <div class="card"><h2>Usuarios y contraseñas</h2><div id="usersBox"><p class="muted">Cargando…</p></div></div>

      <div class="card"><h2>Conectar un MikroTik nuevo</h2>
        <ol class="steps">
          <li>Copiá el script de abajo y pegalo en una terminal del MikroTik (Winbox → New Terminal).</li>
          <li>El script crea un usuario de <b>solo lectura</b>, limita la API a este servidor y registra el router acá automáticamente.</li>
          <li>En unos segundos aparece en el Panel. Si el servidor no es alcanzable por esa URL, cambiá la línea <code>serverUrl</code> del script.</li>
        </ol>
        <div class="row" style="margin-bottom:.5rem"><button id="copyScript">Copiar script</button>
          <span class="muted">Contiene el token de registro: no lo compartas fuera de tu equipo.</span></div>
        <pre class="script" id="scriptText">${esc(script)}</pre></div>

      <div class="card"><h2>Respaldos</h2><div id="backupsBox"><p class="muted">Cargando…</p></div></div>

      <div class="card"><h2>Router REMOTO (por Internet)</h2>
        <div id="remoteBox"><p class="muted">Cargando…</p></div></div>

      <div class="card"><h2>OLT y PON (mapa de red)</h2>
        <p class="muted">Cargá cada OLT con sus PON: una VLAN por PON y dos pools (Internet y TV). Los clientes se ubican solos por la IP que les da el PPPoE.</p>
        <div id="oltBox"><p class="muted">Cargando…</p></div></div>

      <div class="card"><h2>Routers</h2>
        <div class="tablewrap">${routers.length ? `<table><thead><tr><th>Nombre</th><th>Dirección</th><th>Usuario API</th><th>Tipos leídos</th><th>PPPoE</th><th>Estado</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">Sin routers todavía.</p>'}</div>
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
          <div class="field"><label>Clientes PPPoE</label><select id="f_pppoe"><option value="1">Monitorear</option><option value="0">No monitorear</option></select></div>
          <div class="field"><label>Respaldar la configuración (SSH)</label><select id="f_cfgbackup"><option value="0">No</option><option value="1">Sí</option></select><small>Necesita el permiso «ssh» en el usuario del router.</small></div>
          <div class="field"><label>Puerto SSH</label><input id="f_sshport" type="number" value="22"></div>
          <div class="field"><label>IP de origen del syslog</label><input id="f_syslog" placeholder="(igual a la IP del router)"><small>Solo si el router manda el log desde otra IP.</small></div>
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
        Se activa con <code>MKMON_TELEGRAM_TOKEN</code> y <code>MKMON_TELEGRAM_CHAT</code> en <code>/etc/mk-monitor/mk-monitor.env</code>.</p>
        <p>Log de PPPoE (syslog UDP): <b>${S && S.info.syslog_listening ? "escuchando en el puerto " + S.info.syslog_port : "apagado"}</b>.
        Se cambia con <code>MKMON_SYSLOG_PORT</code> (0 = apagado).</p></div>`;

    if (typeof renderUsers === "function") renderUsers();
    if (typeof renderBackups === "function") renderBackups();
    if (typeof renderRemote === "function") renderRemote();
    if (typeof renderOltConfig === "function") renderOltConfig();
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
    pppoe_enabled: $("#f_pppoe").value === "1",
    syslog_ip: $("#f_syslog").value.trim(),
    cfg_backup: $("#f_cfgbackup").value === "1",
    ssh_port: parseInt($("#f_sshport").value, 10) || 22,
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
  $("#f_pppoe").value = r.pppoe_enabled ? "1" : "0";
  $("#f_syslog").value = r.syslog_ip || "";
  $("#f_cfgbackup").value = r.cfg_backup ? "1" : "0";
  $("#f_sshport").value = r.ssh_port || 22;
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

function showTab(name) {
  if (tab === "network" && name !== "network" && typeof netStop === "function") netStop();
  tab = name;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  ["panel", "pppoe", "usage", "network", "history", "config"].forEach((n) => ($("#tab-" + n).hidden = n !== name));
  if (name === "panel" && S) renderPanel();
  if (name === "pppoe") renderPppoe();
  if (name === "usage" && typeof renderUsage === "function") renderUsage();
  if (name === "network") netStart();
  if (name === "history") renderHistory();
  if (name === "config") renderConfig();
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, .tile");
  if (!t) return;
  ensureAudio();
  try {
    if (t.dataset.tab) {
      showTab(t.dataset.tab);
    } else if (t.dataset.tabgo) {
      showTab(t.dataset.tabgo);
    } else if (t.id === "logoutBtn") {
      if (t.dataset.authed) await api("/api/logout", { method: "POST" });
      window.location.href = "/login";
    } else if (t.id === "soundBtn") {
      soundOn = !soundOn;
      localStorage.setItem("mk_sound", soundOn ? "1" : "0");
      paintSoundBtn();
      ensureAudio();
      if (soundOn) beep(false);
    } else if (t.dataset.ack) {
      await api(`/api/alerts/${t.dataset.ack}/ack`, { method: "POST" });
      refresh();
    } else if (t.dataset.delalert) {
      await api(`/api/alerts/${t.dataset.delalert}`, { method: "DELETE" });
      toast("Alerta borrada");
      refresh();
      if (!$("#tab-history").hidden) renderHistory();
    } else if (t.dataset.clearalerts) {
      const sc = t.dataset.clearalerts;
      const msg = {
        acked: "¿Borrar las alertas activas que ya reconociste?",
        resolved: "¿Borrar del historial todas las alertas ya resueltas?",
        all: "¿Borrar TODO el historial de alertas, incluidas las activas? Las activas que sigan sin resolverse no volverán a avisar hasta que se normalicen.",
      }[sc];
      if (!confirm(msg)) return;
      const r = await api(`/api/alerts?scope=${sc}`, { method: "DELETE" });
      toast(`${r.deleted} alerta${r.deleted === 1 ? "" : "s"} borrada${r.deleted === 1 ? "" : "s"}`);
      refresh();
      if (!$("#tab-history").hidden) renderHistory();
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
  if (ev.target.id === "pppoeRouter") renderPppoe();
  if (ev.target.id === "pppoeRegKind") renderPppoeReg();
  if (ev.target.id === "pppoeRepDays" || ev.target.id === "pppoeRepMin") renderPppoeRep();
});

let searchTimer = null;
document.addEventListener("input", (ev) => {
  if (ev.target.id === "pppoeQ") {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderPppoeSearch, 300);
  }
});
$("#pppoeLogBox").addEventListener("toggle", () => { if ($("#pppoeLogBox").open) renderPppoeLog(); });

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
setInterval(() => {
  if (tab === "pppoe") { renderPppoe(); renderPppoeSearch(); }
}, 5000);
loadMe();
refresh();

/* Clic (o Enter) en el recuadro de un router: las listas pasan a mostrar solo ese router; otro clic lo suelta. */
function pickPppoeRouter(el) {
  const sel = $("#pppoeRouter");
  const id = el.dataset.pprouter;
  sel.value = id === sel.value ? "" : id;
  renderPppoe();
}
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-pprouter]");
  if (el) pickPppoeRouter(el);
});
document.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.dataset && ev.target.dataset.pprouter !== undefined && ev.target.classList.contains("pcard")) {
    ev.preventDefault();
    pickPppoeRouter(ev.target);
  }
});
