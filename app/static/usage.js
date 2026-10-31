"use strict";
/* Consumo por cliente PPPoE: ranking, comparación con lo habitual y gráfica por cliente.
   Usa `$`, `esc`, `api`, `toast`, `S`, `tab`, `chartSVG`, `fmtRate` de app.js. */

function fmtBig(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
}

let U = { hours: 24, rid: "", q: "", onlyBad: false, known: "" };
let UM = null;   // cliente abierto en el modal: {rid, user, hours}

function usageRatioHTML(r) {
  if (r.ratio == null) return '<span class="muted" title="Todavía no hay suficientes días de historial">sin base</span>';
  const cls = r.abnormal ? "pill crit" : r.ratio >= 1.5 ? "pill warn" : "pill";
  return `<span class="${cls}">${r.ratio >= 10 ? Math.round(r.ratio) : r.ratio.toFixed(1)}×</span>`;
}

async function renderUsage() {
  const box = $("#usageBox");
  if (!box) return;
  const sel = $("#usageRouter");
  const routers = (S && S.routers ? S.routers : []).filter((r) => r.enabled);
  const key = routers.map((r) => r.id + r.name).join("|");
  if (key !== U.known) {
    U.known = key;
    sel.innerHTML = '<option value="">todos</option>' + routers.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
    sel.value = U.rid;
  }
  try {
    const qs = `hours=${U.hours}${U.rid ? "&router_id=" + U.rid : ""}${U.q ? "&q=" + encodeURIComponent(U.q) : ""}${U.onlyBad ? "&only_abnormal=true" : ""}&limit=100`;
    const res = await api(`/api/usage/top?${qs}`);
    $("#usageSummary").innerHTML =
      `<span class="chip">${res.total} cliente${res.total === 1 ? "" : "s"} con consumo</span> ` +
      `<span class="chip ${res.abnormal ? "crit" : "ok"}">${res.abnormal ? res.abnormal + " con consumo anormal" : "Sin consumo anormal"}</span> ` +
      `<span class="muted">«vs habitual» compara con la mediana de las ${U.hours >= 168 ? "semanas" : "ventanas"} anteriores; es anormal desde ${res.factor}× y más de ${res.min_gb} GB por 24 h.</span>`;
    if (!res.rows.length) {
      box.innerHTML = '<p class="muted">Todavía no hay datos de consumo. MAGI lee los contadores de cada cliente cada pocos minutos; el historial aparece a partir de la segunda lectura.</p>';
      return;
    }
    const body = res.rows.map((r) => `<tr class="${r.abnormal ? "bad" : ""}" data-uopen="${r.router_id}" data-uuser="${esc(r.user)}" style="cursor:pointer">
      <td><b>${esc(r.user)}</b></td><td>${esc(r.router)}</td>
      <td class="num">${fmtBig(r.down_b)}</td><td class="num">${fmtBig(r.up_b)}</td><td class="num"><b>${fmtBig(r.total_b)}</b></td>
      <td class="num">${r.avg_down_mbps.toFixed(2)} / ${r.avg_up_mbps.toFixed(2)}</td><td class="num">${r.peak_down_mbps.toFixed(1)}</td>
      <td>${usageRatioHTML(r)}${r.abnormal ? ' <span class="pill crit">ANORMAL</span>' : ""}</td>
      <td><button class="ghost small" data-uopen="${r.router_id}" data-uuser="${esc(r.user)}">Historial</button></td></tr>`).join("");
    box.innerHTML = `<table><thead><tr><th>Cliente</th><th>Router</th><th class="num">Bajó</th><th class="num">Subió</th><th class="num">Total</th>
      <th class="num">Prom. Mbps ↓/↑</th><th class="num">Pico ↓ Mbps</th><th>vs habitual</th><th></th></tr></thead><tbody>${body}</tbody></table>` +
      (res.total > res.rows.length ? `<p class="muted">Se muestran los ${res.rows.length} primeros de ${res.total}. Usá el buscador para encontrar a uno en particular.</p>` : "");
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function dailyBarsHTML(d) {
  const byAgo = new Map(d.daily.map((x) => [x.days_ago, x]));
  const days = Array.from({ length: 14 }, (_, k) => 13 - k).map((ago) => {
    const x = byAgo.get(ago) || { down_b: 0, up_b: 0 };
    return { ago, down: x.down_b, up: x.up_b, total: x.down_b + x.up_b };
  });
  const max = Math.max(1, ...days.map((x) => x.total), d.typical_b || 0);
  const bars = days.map((x) => `<div class="dbar" title="${x.ago ? "hace " + x.ago + " d" : "últimas 24 h"}: ↓ ${fmtBig(x.down)} · ↑ ${fmtBig(x.up)}">
      <div class="dcol"><i style="height:${(100 * x.total / max).toFixed(1)}%" class="${x.ago === 0 && d.abnormal ? "bad" : ""}"></i></div>
      <small>${x.ago ? "-" + x.ago : "hoy"}</small></div>`).join("");
  return `<div class="dbars">${bars}</div>`;
}

async function loadUsageClient() {
  if (!UM) return;
  const box = $("#usageClientBox");
  try {
    const d = await api(`/api/usage/client?router_id=${UM.rid}&user=${encodeURIComponent(UM.user)}&hours=${UM.hours}`);
    if (!UM) return;
    const ratio = d.ratio == null ? "sin base de comparación todavía" : `${d.ratio.toFixed(1)}× lo habitual (últimas 24 h)`;
    box.innerHTML = `
      <div class="row" style="margin:.4rem 0;gap:1rem;flex-wrap:wrap">
        <span>Bajó <b>${fmtBig(d.down_b)}</b></span><span>Subió <b>${fmtBig(d.up_b)}</b></span>
        <span class="muted">pico ↓ ${d.peak_down_mbps} Mbps (promedio de un intervalo)</span>
        <span class="pill ${d.abnormal ? "crit" : ""}">${ratio}${d.abnormal ? " · ANORMAL" : ""}</span></div>
      ${chartSVG(d.points, d.bucket_s, d.hours * 60)}
      <div class="legend"><span>Bajada (lo que descarga)</span><span class="tx">Subida (lo que envía)</span></div>
      <h3 style="margin:.8rem 0 .3rem">Consumo por día (últimos 14)</h3>${dailyBarsHTML(d)}
      <p class="muted" style="margin:.3rem 0 0">${d.typical_b != null ? "Consumo diario habitual: " + fmtBig(d.typical_b) + " (mediana de los días anteriores)." : "Faltan días de historial para calcular lo habitual."}
        Una subida sostenida muy alta suele indicar equipo infectado, torrents o cámaras/servidores en casa del cliente.</p>`;
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function openUsageClient(rid, user) {
  UM = { rid, user, hours: 24 };
  const ranges = [[6, "6 h"], [24, "24 h"], [72, "3 d"], [168, "7 d"], [720, "30 d"]];
  $("#modalBody").innerHTML = `<h2>${esc(user)} <span class="muted">· consumo</span></h2>
    <div class="row" style="margin:.4rem 0">${ranges.map(([h, l]) => `<button class="ghost small" data-urange="${h}">${l}</button>`).join("")}</div>
    <div id="usageClientBox"><p class="muted">Cargando…</p></div>`;
  $("#modal").hidden = false;
  loadUsageClient();
}

document.addEventListener("click", (ev) => {
  const range = ev.target.closest("[data-urange]");
  if (range && UM) { UM.hours = parseInt(range.dataset.urange, 10); loadUsageClient(); return; }
  const open = ev.target.closest("[data-uopen]");
  if (open) openUsageClient(parseInt(open.dataset.uopen, 10), open.dataset.uuser);
});
document.addEventListener("change", (ev) => {
  if (ev.target.id === "usageRouter") { U.rid = ev.target.value; renderUsage(); }
  else if (ev.target.id === "usageHours") { U.hours = parseInt(ev.target.value, 10); renderUsage(); }
  else if (ev.target.id === "usageBad") { U.onlyBad = ev.target.checked; renderUsage(); }
});
let usageTimer = null;
document.addEventListener("input", (ev) => {
  if (ev.target.id !== "usageQ") return;
  clearTimeout(usageTimer);
  usageTimer = setTimeout(() => { U.q = ev.target.value.trim(); renderUsage(); }, 350);
});
setInterval(() => { if (typeof tab !== "undefined" && tab === "usage") renderUsage(); }, 30000);
