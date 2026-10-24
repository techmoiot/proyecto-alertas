"use strict";
/* Mapa de red: OLT → PON (VLAN) → pools Internet/TV → clientes.
 *
 * Grafo en <canvas> al estilo Obsidian: nodos y líneas con una simulación de
 * fuerzas, zoom con la rueda / pellizco, arrastre del fondo y de los nodos.
 * Usa `$`, `esc`, `api`, `toast`, `S` y `fmtDur` de app.js.
 */

const NET = {
  data: null,          // última respuesta de /api/network
  oltId: null,         // OLT que se está mirando
  nodes: [],
  edges: [],
  byId: new Map(),
  view: { x: 0, y: 0, k: 1 },
  alpha: 0,            // "temperatura" de la simulación
  fitted: false,
  active: false,       // la pestaña está a la vista
  raf: 0,
  timer: null,
  size: { w: 0, h: 0, dpr: 1 },
  selected: null,      // id del nodo PON elegido
  hover: null,
  pointers: new Map(),
  gesture: null,
  ctx: null,
  ro: null,
};

const NET_COLOR = {
  up: "#34c77b",
  grace: "#f5a524",
  alert: "#f0443a",
  mass: "#f0443a",
  exp: "#8a3a36",
  ok: "#34c77b",
  warn: "#f5a524",
  down: "#f0443a",
  empty: "#5b6676",
  inet: "#4aa3ff",
  tv: "#b583ff",
  olt: "#e6ebf2",
};
const NET_STATUS_ES = { up: "conectado", grace: "en espera", alert: "ALERTA", mass: "caída masiva", exp: "no volvió" };
const NET_POOL_ES = { inet: "Internet", tv: "TV" };

/* ------------------------------------------------------------- construcción */

function netDiscRadius(n) {
  return 8 + 6.2 * Math.sqrt(Math.max(n, 1));
}

function netPoolStatus(pool, ponStatus) {
  const total = pool.up + pool.down;
  if (ponStatus === "down") return "down";
  if (!total) return "empty";
  const pct = (100 * pool.down) / total;
  if (pct >= 50 && pool.down >= 2) return "down";
  if (pool.down >= 2 || pct >= 20) return "warn";
  return "ok";
}

function netUpsert(id, init) {
  let n = NET.byId.get(id);
  if (!n) {
    n = { id, x: 0, y: 0, vx: 0, vy: 0, pinned: false, ...init };
    NET.byId.set(id, n);
  } else {
    const { x, y, ...rest } = init;
    Object.assign(n, rest);
  }
  return n;
}

function netApply(snap) {
  NET.data = snap;
  const olt = snap.olts.find((o) => o.id === NET.oltId) || snap.olts[0];
  const seen = new Set();
  const edges = [];
  let changed = false;
  if (!olt) {
    NET.nodes = [];
    NET.edges = [];
    NET.byId.clear();
    return;
  }
  NET.oltId = olt.id;

  const n = olt.pons.length;
  const discs = olt.pons.map((p) => p.pools.map((pl) => netDiscRadius(pl.clients.length)));
  const maxDisc = Math.max(40, ...discs.flat());
  const foot = 4 * maxDisc + 60;
  const R = Math.max(260, (n * foot) / (2 * Math.PI));

  const hub = netUpsert("olt:" + olt.id, { type: "olt", r: 24, cr: 40, label: olt.name, sub: n + " PON", status: "olt" });
  seen.add(hub.id);

  olt.pons.forEach((p, i) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(n, 1);
    const ox = Math.cos(ang), oy = Math.sin(ang);
    const tx = -oy, ty = ox;
    const pid = "pon:" + p.id;
    const isNew = !NET.byId.has(pid);
    const pon = netUpsert(pid, {
      type: "pon", r: 14, cr: 30, x: ox * R, y: oy * R, label: p.name,
      sub: (p.vlan ? "VLAN " + p.vlan + " · " : "") + (p.total - p.down) + "/" + p.total,
      status: p.status, alert: p.alert, pon: p, ponId: p.id, ox, oy,
    });
    if (isNew) changed = true;
    seen.add(pid);
    edges.push({ a: hub, b: pon, len: R, k: 0.02, kind: "pon" });

    let slot = 0;
    p.pools.forEach((pl, k) => {
      if (!pl.cidr) return;
      const disc = discs[i][k];
      const poolId = "pool:" + p.id + ":" + k;
      const side = pl.kind === "inet" ? -1 : 1;
      const px = pon.x + ox * (disc + 38) + tx * side * (maxDisc * 0.9 + 14);
      const py = pon.y + oy * (disc + 38) + ty * side * (maxDisc * 0.9 + 14);
      const poolNew = !NET.byId.has(poolId);
      const pool = netUpsert(poolId, {
        type: "pool", r: 9, cr: disc + 6, x: px, y: py, kind: pl.kind, group: poolId,
        label: NET_POOL_ES[pl.kind], sub: pl.clients.length + "/" + pl.capacity,
        status: netPoolStatus(pl, p.status), pool: pl, ponId: p.id, ponNode: pon,
      });
      if (poolNew) changed = true;
      seen.add(poolId);
      edges.push({ a: pon, b: pool, len: disc + 40, k: 0.05, kind: "pool" });
      slot += 1;

      pl.clients.forEach((c, j) => {
        const cid = "cl:" + p.id + ":" + k + ":" + c[0] + ":" + c[3];
        const a = j * 2.39996;
        const rr = 7 * Math.sqrt(j + 1);
        const isNewC = !NET.byId.has(cid);
        const cl = netUpsert(cid, {
          type: "cl", r: 2.7, cr: 4, x: pool.x + Math.cos(a) * rr, y: pool.y + Math.sin(a) * rr,
          label: c[0], status: c[1], ip: c[2], router: c[3], group: poolId, ponId: p.id, poolNode: pool,
          pool: pl.kind,
        });
        if (isNewC) changed = true;
        seen.add(cid);
        edges.push({ a: pool, b: cl, len: 14 + 3.2 * Math.sqrt(pl.clients.length), k: 0.08, kind: "cl" });
      });
    });
  });

  for (const id of [...NET.byId.keys()]) {
    if (!seen.has(id)) {
      NET.byId.delete(id);
      changed = true;
    }
  }
  NET.nodes = [...NET.byId.values()];
  NET.edges = edges;
  NET.alpha = Math.max(NET.alpha, changed ? 0.9 : 0.04);
}

/* --------------------------------------------------------------- simulación */

function netStep() {
  if (NET.alpha < 0.004) return;
  const a = NET.alpha;
  for (const e of NET.edges) {
    const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = ((d - e.len) / d) * e.k * a;
    if (e.kind === "pon" ) {
      // la ruta hacia la OLT sólo ajusta al PON: el centro no se mueve por el anillo
      if (!e.b.pinned) { e.b.vx -= dx * f; e.b.vy -= dy * f; }
      continue;
    }
    if (!e.a.pinned) { e.a.vx += dx * f; e.a.vy += dy * f; }
    if (!e.b.pinned) { e.b.vx -= dx * f; e.b.vy -= dy * f; }
  }
  // repulsión dentro de cada pool (los clientes forman un disco ordenado)
  const groups = new Map();
  const hubs = [];
  for (const n of NET.nodes) {
    if (n.type === "cl") {
      let g = groups.get(n.group);
      if (!g) groups.set(n.group, (g = [n.poolNode]));
      g.push(n);
    } else hubs.push(n);
  }
  for (const g of groups.values()) {
    for (let i = 0; i < g.length; i++) {
      const p = g[i];
      for (let j = i + 1; j < g.length; j++) {
        const q = g[j];
        let dx = q.x - p.x, dy = q.y - p.y;
        const d2 = dx * dx + dy * dy;
        if (d2 > 1600) continue;
        const d = Math.sqrt(d2) || 0.01;
        const f = (14 / (d + 2)) * a;
        dx = (dx / d) * f; dy = (dy / d) * f;
        if (!p.pinned && p.type !== "pool") { p.vx -= dx; p.vy -= dy; }
        if (!q.pinned) { q.vx += dx; q.vy += dy; }
      }
    }
  }
  // los nodos "grandes" no se pisan (usan el radio de su disco de clientes)
  for (let i = 0; i < hubs.length; i++) {
    const p = hubs[i];
    for (let j = i + 1; j < hubs.length; j++) {
      const q = hubs[j];
      let dx = q.x - p.x, dy = q.y - p.y;
      const min = p.cr + q.cr;
      const d2 = dx * dx + dy * dy;
      if (d2 > (min + 160) * (min + 160)) continue;
      const d = Math.sqrt(d2) || 0.01;
      let f = (3500 / (d2 + 400)) * a;
      if (d < min) f += ((min - d) / d) * 0.35 * a * 4;
      dx = (dx / d) * f; dy = (dy / d) * f;
      if (!p.pinned && p.type !== "olt") { p.vx -= dx; p.vy -= dy; }
      if (!q.pinned && q.type !== "olt") { q.vx += dx; q.vy += dy; }
    }
  }
  for (const n of NET.nodes) {
    if (n.pinned || n.type === "olt") { n.vx = n.vy = 0; continue; }
    n.vx *= 0.78; n.vy *= 0.78;
    n.x += n.vx; n.y += n.vy;
  }
  NET.alpha *= 0.985;
}

/* ------------------------------------------------------------------- dibujo */

function netResize() {
  const cv = $("#netCanvas");
  if (!cv) return;
  const box = cv.parentElement.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(200, Math.floor(box.width)), h = Math.max(200, Math.floor(box.height));
  if (w === NET.size.w && h === NET.size.h && dpr === NET.size.dpr) return;
  NET.size = { w, h, dpr };
  cv.width = Math.floor(w * dpr);
  cv.height = Math.floor(h * dpr);
  cv.style.width = w + "px";
  cv.style.height = h + "px";
  if (!NET.fitted) netFit();
}

function netFit() {
  if (!NET.nodes.length || !NET.size.w) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of NET.nodes) {
    const m = n.type === "cl" ? 6 : n.cr || 10;
    x0 = Math.min(x0, n.x - m); y0 = Math.min(y0, n.y - m);
    x1 = Math.max(x1, n.x + m); y1 = Math.max(y1, n.y + m);
  }
  const bw = Math.max(x1 - x0, 50), bh = Math.max(y1 - y0, 50);
  const k = Math.min(NET.size.w / (bw + 120), NET.size.h / (bh + 120));
  NET.view.k = Math.min(Math.max(k, 0.05), 2.5);
  NET.view.x = NET.size.w / 2 - ((x0 + x1) / 2) * NET.view.k;
  NET.view.y = NET.size.h / 2 - ((y0 + y1) / 2) * NET.view.k;
  NET.fitted = true;
}

function netDraw(t) {
  const ctx = NET.ctx;
  if (!ctx) return;
  const { w, h, dpr } = NET.size;
  const { x, y, k } = NET.view;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, w * dpr, h * dpr);
  ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * x, dpr * y);
  const pulse = 0.5 + 0.5 * Math.sin(t / 220);
  const inv = 1 / k;

  // halo rojo detrás de los sectores caídos
  for (const n of NET.nodes) {
    if (n.type !== "pon" || (n.status !== "down" && n.status !== "warn")) continue;
    let sx = n.x, sy = n.y, c = 1, r = 0;
    const members = NET.nodes.filter((m) => m.ponId === n.ponId && m !== n);
    for (const m of members) { sx += m.x; sy += m.y; c++; }
    sx /= c; sy /= c;
    for (const m of members) r = Math.max(r, Math.hypot(m.x - sx, m.y - sy) + 24);
    r = Math.max(r, 50);
    const rgb = n.status === "down" ? "240,68,58" : "245,165,36";
    const g = ctx.createRadialGradient(sx, sy, r * 0.2, sx, sy, r * 1.15);
    const a0 = n.status === "down" ? 0.16 + 0.12 * pulse : 0.08;
    g.addColorStop(0, `rgba(${rgb},${a0})`);
    g.addColorStop(1, `rgba(${rgb},0)`);
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(sx, sy, r * 1.15, 0, 6.2832); ctx.fill();
  }

  // líneas
  ctx.lineWidth = inv * 0.9;
  ctx.strokeStyle = "rgba(139,151,168,0.20)";
  ctx.beginPath();
  for (const e of NET.edges) {
    if (e.kind === "cl" && e.b.status !== "up") continue;
    ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y);
  }
  ctx.stroke();
  ctx.strokeStyle = "rgba(240,68,58,0.55)";
  ctx.beginPath();
  for (const e of NET.edges) {
    if (e.kind === "cl" && e.b.status !== "up") { ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y); }
  }
  ctx.stroke();
  ctx.strokeStyle = "rgba(139,151,168,0.38)";
  ctx.lineWidth = inv * 1.4;
  ctx.beginPath();
  for (const e of NET.edges) {
    if (e.kind !== "cl") { ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y); }
  }
  ctx.stroke();

  // clientes, agrupados por color para dibujarlos en pocas pasadas
  const rc = Math.max(2.7, 1.9 * inv);
  const byStatus = {};
  for (const n of NET.nodes) if (n.type === "cl") (byStatus[n.status] ||= []).push(n);
  for (const [st, list] of Object.entries(byStatus)) {
    const bad = st !== "up";
    if (bad && st !== "exp") {
      ctx.fillStyle = `rgba(240,68,58,${0.16 + 0.16 * pulse})`;
      ctx.beginPath();
      for (const n of list) { ctx.moveTo(n.x + rc * 2.6, n.y); ctx.arc(n.x, n.y, rc * 2.6, 0, 6.2832); }
      ctx.fill();
    }
    ctx.fillStyle = NET_COLOR[st] || "#888";
    ctx.beginPath();
    const rr = bad ? rc * 1.3 : rc;
    for (const n of list) { ctx.moveTo(n.x + rr, n.y); ctx.arc(n.x, n.y, rr, 0, 6.2832); }
    ctx.fill();
    if (st === "exp") {
      ctx.strokeStyle = "#f0443a"; ctx.lineWidth = inv; ctx.stroke();
    }
  }

  // pools, PON y OLT (con el zoom alejado se agrandan para que se vean)
  for (const n of NET.nodes) if (n.type !== "cl") n.dr = Math.max(n.r, (n.type === "pon" ? 7 : n.type === "olt" ? 10 : 4.5) * inv);
  for (const n of NET.nodes) {
    if (n.type === "cl") continue;
    const col = NET_COLOR[n.status] || "#888";
    const sel = n.type === "pon" && NET.selected === n.id;
    if (n.status === "down") {
      ctx.strokeStyle = `rgba(240,68,58,${0.25 + 0.5 * pulse})`;
      ctx.lineWidth = inv * 3;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.dr + 5 + 5 * pulse, 0, 6.2832); ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(n.x, n.y, n.dr, 0, 6.2832);
    if (n.type === "pool") {
      ctx.fillStyle = "#0e1218"; ctx.fill();
      ctx.lineWidth = inv * 3; ctx.strokeStyle = NET_COLOR[n.kind]; ctx.stroke();
      ctx.beginPath(); ctx.arc(n.x, n.y, n.dr * 0.5, 0, 6.2832); ctx.fillStyle = col; ctx.fill();
    } else if (n.type === "pon") {
      ctx.fillStyle = col; ctx.fill();
      ctx.lineWidth = inv * (sel ? 4 : 2); ctx.strokeStyle = sel ? "#fff" : "#0e1218"; ctx.stroke();
    } else {
      ctx.fillStyle = "#161c25"; ctx.fill();
      ctx.lineWidth = inv * 3.5; ctx.strokeStyle = NET.data && NET.data.olts.some((o) => o.pons.some((p) => p.status === "down")) ? "#f0443a" : NET_COLOR.olt;
      ctx.stroke();
      ctx.beginPath(); ctx.arc(n.x, n.y, n.dr * 0.42, 0, 6.2832); ctx.fillStyle = NET_COLOR.olt; ctx.fill();
    }
  }

  // etiquetas
  ctx.textAlign = "center";
  ctx.fillStyle = "#e6ebf2";
  for (const n of NET.nodes) {
    if (n.type === "cl") continue;
    if (n.type === "pool" && k < 0.28) continue;
    const base = n.type === "olt" ? 15 : n.type === "pon" ? 13 : 10.5;
    const px = Math.max(base * 0.8, base * Math.min(inv * 0.85, 2.4));
    ctx.font = `600 ${px}px system-ui, sans-serif`;
    ctx.lineJoin = "round";
    ctx.strokeStyle = "rgba(11,15,21,0.92)";
    ctx.lineWidth = px * 0.32;
    ctx.fillStyle = "#e6ebf2";
    // El nombre del PON va del lado de la OLT, para no tapar sus clientes.
    let lx = n.x, ly = n.y + n.dr + px + 3 * inv, sy = n.y + n.dr + px * 1.95 + 3 * inv;
    if (n.type === "pon") {
      const off = n.dr + px * 1.1 + 4 * inv;
      lx = n.x - n.ox * off; ly = n.y - n.oy * off + px * 0.35; sy = ly + px * 0.95;
    }
    ctx.strokeText(n.label, lx, ly);
    ctx.fillText(n.label, lx, ly);
    if (n.sub && (n.type !== "pool" || k > 0.45)) {
      ctx.font = `${px * 0.82}px system-ui, sans-serif`;
      ctx.fillStyle = "#9aa7b8";
      ctx.lineWidth = px * 0.3;
      ctx.strokeText(n.sub, lx, sy);
      ctx.fillText(n.sub, lx, sy);
    }
  }
  const hv = NET.hover && NET.byId.get(NET.hover);
  if (hv && hv.type === "cl") {
    ctx.strokeStyle = "#fff"; ctx.lineWidth = inv * 1.5;
    ctx.beginPath(); ctx.arc(hv.x, hv.y, rc * 2, 0, 6.2832); ctx.stroke();
  }
}

function netLoop(t) {
  NET.raf = 0;
  if (!NET.active) return;
  if (!document.hidden) {
    netStep();
    netStep();
    netDraw(t);
  }
  NET.raf = requestAnimationFrame(netLoop);
}

/* ------------------------------------------------------------- interacción */

function netToWorld(px, py) {
  return { x: (px - NET.view.x) / NET.view.k, y: (py - NET.view.y) / NET.view.k };
}

function netHit(px, py) {
  const p = netToWorld(px, py);
  const tol = 6 / NET.view.k;
  let best = null, bd = Infinity;
  const order = { olt: 0, pon: 1, pool: 2, cl: 3 };
  for (const n of NET.nodes) {
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    const lim = (n.type === "cl" ? Math.max(n.r, 1.9 / NET.view.k) : n.dr || n.r) + tol;
    if (d <= lim && (best === null || order[n.type] < order[best.type] || (order[n.type] === order[best.type] && d < bd))) {
      best = n; bd = d;
    }
  }
  return best;
}

function netZoomAt(px, py, factor) {
  const k0 = NET.view.k;
  const k1 = Math.min(Math.max(k0 * factor, 0.04), 12);
  NET.view.x = px - ((px - NET.view.x) / k0) * k1;
  NET.view.y = py - ((py - NET.view.y) / k0) * k1;
  NET.view.k = k1;
}

function netTip(n, ev) {
  const tip = $("#netTip");
  if (!n) { tip.hidden = true; return; }
  let html = "";
  if (n.type === "cl") {
    html = `<b>${esc(n.label)}</b><br>${esc(NET_STATUS_ES[n.status] || n.status)} · ${n.pool === "tv" ? "TV" : "Internet"}<br>
      <span class="muted">${esc(n.ip)} · ${esc(n.router)}</span>`;
  } else if (n.type === "pon") {
    html = `<b>${esc(n.label)}</b>${n.pon.vlan ? " · VLAN " + n.pon.vlan : ""}<br>${n.pon.total - n.pon.down} de ${n.pon.total} clientes en línea`;
  } else if (n.type === "pool") {
    const pl = n.pool;
    html = `<b>${NET_POOL_ES[n.kind]}</b> ${esc(pl.cidr)}<br>${pl.up} en línea · ${pl.down} caídos · ${pl.clients.length}/${pl.capacity} usados`;
  } else {
    html = `<b>${esc(n.label)}</b><br>${esc(n.sub)}`;
  }
  tip.innerHTML = html;
  tip.hidden = false;
  const wrap = $("#netWrap").getBoundingClientRect();
  let x = ev.clientX - wrap.left + 14, y = ev.clientY - wrap.top + 14;
  x = Math.min(x, wrap.width - tip.offsetWidth - 8);
  y = Math.min(y, wrap.height - tip.offsetHeight - 8);
  tip.style.left = Math.max(4, x) + "px";
  tip.style.top = Math.max(4, y) + "px";
}

function netBind() {
  const cv = $("#netCanvas");
  if (!cv || cv.dataset.bound) return;
  cv.dataset.bound = "1";
  cv.style.touchAction = "none";

  cv.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = cv.getBoundingClientRect();
    netZoomAt(ev.clientX - r.left, ev.clientY - r.top, Math.exp(-ev.deltaY * 0.0016));
  }, { passive: false });

  cv.addEventListener("pointerdown", (ev) => {
    const r = cv.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    try { cv.setPointerCapture(ev.pointerId); } catch (_) { /* jsdom */ }
    NET.pointers.set(ev.pointerId, { x: px, y: py });
    if (NET.pointers.size === 2) {
      const [a, b] = [...NET.pointers.values()];
      NET.gesture = { type: "pinch", d: Math.hypot(a.x - b.x, a.y - b.y) || 1 };
      return;
    }
    const hit = netHit(px, py);
    NET.gesture = { type: hit ? "node" : "pan", node: hit, sx: px, sy: py, moved: false, t: Date.now() };
    if (hit) { hit.pinned = true; }
  });

  cv.addEventListener("pointermove", (ev) => {
    const r = cv.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const prev = NET.pointers.get(ev.pointerId);
    if (prev) NET.pointers.set(ev.pointerId, { x: px, y: py });
    const g = NET.gesture;
    if (g && g.type === "pinch" && NET.pointers.size >= 2) {
      const [a, b] = [...NET.pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      netZoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, d / g.d);
      g.d = d;
      return;
    }
    if (g && prev) {
      if (Math.hypot(px - g.sx, py - g.sy) > 5) g.moved = true;
      if (g.type === "pan" && g.moved) {
        NET.view.x += px - prev.x; NET.view.y += py - prev.y;
      } else if (g.type === "node" && g.moved) {
        const w = netToWorld(px, py);
        g.node.x = w.x; g.node.y = w.y; g.node.vx = g.node.vy = 0;
        NET.alpha = Math.max(NET.alpha, 0.35);
      }
      return;
    }
    const hit = netHit(px, py);
    NET.hover = hit ? hit.id : null;
    cv.style.cursor = hit ? "pointer" : "grab";
    netTip(hit, ev);
  });

  const end = (ev) => {
    const g = NET.gesture;
    NET.pointers.delete(ev.pointerId);
    if (NET.pointers.size > 0) { if (g && g.type === "pinch") NET.gesture = null; return; }
    NET.gesture = null;
    if (!g) return;
    if (g.node) g.node.pinned = false;
    if (ev.type === "pointercancel") return;
    if (!g.moved && Date.now() - g.t < 600) {
      const n = g.node;
      if (n && n.ponId != null) NET.selected = "pon:" + n.ponId;
      else if (n && n.type === "olt") NET.selected = null;
      else NET.selected = null;
      netRenderPanel();
    }
  };
  cv.addEventListener("pointerup", end);
  cv.addEventListener("pointercancel", end);
  cv.addEventListener("pointerleave", () => { NET.hover = null; netTip(null); });
  cv.addEventListener("dblclick", (ev) => {
    const r = cv.getBoundingClientRect();
    const n = netHit(ev.clientX - r.left, ev.clientY - r.top);
    if (n && n.ponId != null) netFocusPon(n.ponId);
    else netFit();
  });
}

function netFocusPon(ponId) {
  const list = NET.nodes.filter((n) => n.ponId === ponId);
  if (!list.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of list) {
    const m = n.type === "cl" ? 8 : n.cr || 12;
    x0 = Math.min(x0, n.x - m); y0 = Math.min(y0, n.y - m);
    x1 = Math.max(x1, n.x + m); y1 = Math.max(y1, n.y + m);
  }
  const k = Math.min(NET.size.w / (x1 - x0 + 160), NET.size.h / (y1 - y0 + 160), 6);
  NET.view.k = k;
  NET.view.x = NET.size.w / 2 - ((x0 + x1) / 2) * k;
  NET.view.y = NET.size.h / 2 - ((y0 + y1) / 2) * k;
}

/* ----------------------------------------------------------- paneles / datos */

function netPonById(id) {
  if (!NET.data) return null;
  for (const o of NET.data.olts) for (const p of o.pons) if (p.id === id) return p;
  return null;
}

function netRenderPanel() {
  const box = $("#netPanel");
  if (!box) return;
  const pon = NET.selected ? netPonById(parseInt(NET.selected.slice(4), 10)) : null;
  if (!pon) { box.hidden = true; return; }
  box.hidden = false;
  const st = { ok: "Normal", warn: "Degradado", down: "SECTOR CAÍDO", empty: "Sin clientes" }[pon.status];
  const pools = pon.pools
    .filter((pl) => pl.cidr)
    .map((pl) => {
      const used = pl.clients.length;
      const pct = pl.capacity ? Math.min(100, (100 * used) / pl.capacity) : 0;
      return `<div class="netpool"><div><b style="color:${NET_COLOR[pl.kind]}">${NET_POOL_ES[pl.kind]}</b>
        <span class="muted">${esc(pl.cidr)}</span></div>
        <div class="bar"><i style="width:${pct.toFixed(0)}%;background:${NET_COLOR[pl.kind]}"></i></div>
        <div class="muted">${used}/${pl.capacity} usados · <span style="color:var(--ok)">${pl.up} en línea</span> ·
        <span style="color:${pl.down ? "#ff8a82" : "var(--muted)"}">${pl.down} caídos</span></div></div>`;
    })
    .join("");
  const downs = pon.pools.flatMap((pl) => pl.clients.filter((c) => c[1] !== "up").map((c) => [...c, pl.kind]));
  const rows = downs
    .slice(0, 40)
    .map((c) => `<tr><td><b>${esc(c[0])}</b></td><td><span class="st ${c[1]}">${esc(NET_STATUS_ES[c[1]] || c[1])}</span></td>
      <td class="mono">${esc(c[2])}</td></tr>`)
    .join("");
  box.innerHTML = `<button class="ghost small close" id="netPanelClose" aria-label="Cerrar">✕</button>
    <h3>${esc(pon.name)} <span class="pill ${pon.status === "down" ? "crit" : pon.status === "warn" ? "warn" : ""}">${st}</span></h3>
    <div class="muted" style="margin-bottom:.5rem">${pon.vlan ? "VLAN " + pon.vlan + " · " : ""}${pon.total - pon.down} de ${pon.total} clientes en línea${pon.total ? ` (${pon.pct}% caídos)` : ""}</div>
    ${pools || '<p class="muted">Este PON no tiene pools configurados.</p>'}
    <div class="row" style="margin:.6rem 0"><button class="ghost small" id="netFocus">Acercar</button></div>
    ${downs.length ? `<h4 style="margin:.4rem 0">Clientes caídos (${downs.length})</h4>
      <div class="tablewrap"><table><tbody>${rows}</tbody></table></div>
      ${downs.length > 40 ? `<p class="muted">Y ${downs.length - 40} más. Mirá la pestaña PPPoE.</p>` : ""}` : '<p class="muted">Ningún cliente caído. ✅</p>'}`;
}

function netRenderSummary() {
  const d = NET.data;
  const sum = $("#netSummary"), ban = $("#netBanner");
  const olt = d && d.olts.find((o) => o.id === NET.oltId);
  if (!olt) { sum.innerHTML = ""; ban.innerHTML = ""; return; }
  let total = 0, down = 0, sectors = 0, warn = 0;
  for (const p of olt.pons) {
    total += p.total; down += p.down;
    if (p.status === "down") sectors++;
    else if (p.status === "warn") warn++;
  }
  const chip = (cls, text) => `<span class="chip ${cls}">${text}</span>`;
  sum.innerHTML =
    chip("", `${olt.pons.length} PON`) +
    chip("ok", `${total - down} en línea`) +
    chip(down ? "warn" : "", `${down} caídos`) +
    chip(sectors ? "crit" : "ok", sectors ? `${sectors} sector${sectors > 1 ? "es" : ""} caído${sectors > 1 ? "s" : ""}` : "Sin sectores caídos") +
    (warn ? chip("warn", `${warn} degradado${warn > 1 ? "s" : ""}`) : "") +
    (d.unassigned ? chip("", `${d.unassigned} fuera de las VLAN`) : "");
  const bad = olt.pons.filter((p) => p.status === "down");
  ban.innerHTML = bad.length
    ? `<div class="netbanner">${bad
        .slice(0, 8)
        .map((p) => `<button class="netsector" data-netpon="${p.id}">🔴 ${esc(p.name)}${p.vlan ? " · VLAN " + p.vlan : ""} — sector caído: ${p.down}/${p.total} (${p.pct}%)</button>`)
        .join("")}${bad.length > 8 ? `<span class="muted">y ${bad.length - 8} más</span>` : ""}</div>`
    : "";
  const sel = $("#netOlt");
  if (d.olts.length > 1) {
    sel.hidden = false;
    sel.innerHTML = d.olts.map((o) => `<option value="${o.id}" ${o.id === NET.oltId ? "selected" : ""}>${esc(o.name)}</option>`).join("");
  } else sel.hidden = true;
}

async function renderNetwork() {
  const empty = $("#netEmpty"), main = $("#netMain");
  try {
    const snap = await api("/api/network");
    if (!snap.olts.length) {
      empty.hidden = false; main.hidden = true;
      NET.nodes = []; NET.edges = []; NET.byId.clear(); NET.data = snap;
      return;
    }
    empty.hidden = true; main.hidden = false;
    const first = !NET.nodes.length;
    netApply(snap);
    if (first) { NET.fitted = false; for (let i = 0; i < 160; i++) netStep(); netResize(); netFit(); }
    netRenderSummary();
    netRenderPanel();
  } catch (e) {
    toast(e.message, true);
  }
}

function netStart() {
  NET.active = true;
  const cv = $("#netCanvas");
  if (!NET.ctx) NET.ctx = cv.getContext("2d");
  netBind();
  if (!NET.ro && window.ResizeObserver) {
    NET.ro = new ResizeObserver(() => netResize());
    NET.ro.observe($("#netWrap"));
  }
  netResize();
  renderNetwork();
  clearInterval(NET.timer);
  NET.timer = setInterval(() => { if (NET.active) renderNetwork(); }, 5000);
  if (!NET.raf) NET.raf = requestAnimationFrame(netLoop);
}

function netStop() {
  NET.active = false;
  clearInterval(NET.timer);
  if (NET.raf) cancelAnimationFrame(NET.raf);
  NET.raf = 0;
}

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  if (t.dataset.netpon) {
    NET.selected = "pon:" + t.dataset.netpon;
    netRenderPanel();
    netFocusPon(parseInt(t.dataset.netpon, 10));
  } else if (t.id === "netFit") netFit();
  else if (t.id === "netFull") {
    const w = $("#netWrap");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (w.requestFullscreen) w.requestFullscreen();
  } else if (t.id === "netPanelClose") { NET.selected = null; netRenderPanel(); }
  else if (t.id === "netFocus" && NET.selected) netFocusPon(parseInt(NET.selected.slice(4), 10));
});
document.addEventListener("change", (ev) => {
  if (ev.target.id === "netOlt") {
    NET.oltId = parseInt(ev.target.value, 10);
    NET.nodes = []; NET.byId.clear(); NET.selected = null; NET.fitted = false;
    renderNetwork();
  }
});
document.addEventListener("fullscreenchange", () => setTimeout(() => { netResize(); netFit(); }, 100));

/* ====================================================== configuración de OLT */

let OLT_EDIT = null;   // { id, name, pons: [{name, vlan, inet_cidr, tv_cidr}] }

function ipToInt(s) {
  const p = s.split(".").map(Number);
  if (p.length !== 4 || p.some((x) => !Number.isInteger(x) || x < 0 || x > 255)) return null;
  return ((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3];
}
const intToIp = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");

/* Parte una red en dos mitades: la primera para Internet y la segunda para TV. */
function splitPool(cidr) {
  const m = /^(\d+\.\d+\.\d+\.\d+)\/(\d+)$/.exec(cidr.trim());
  if (!m) return null;
  const base = ipToInt(m[1]), len = parseInt(m[2], 10);
  if (base === null || len < 8 || len > 29) return null;
  const size = 2 ** (32 - len);
  const net = base - (base % size);
  return [`${intToIp(net)}/${len + 1}`, `${intToIp(net + size / 2)}/${len + 1}`];
}

function oltGenerate(count, vlan0, tpl) {
  const pons = [];
  for (let i = 1; i <= count; i++) {
    const vlan = vlan0 + i - 1;
    const cidr = tpl.replaceAll("{n}", i).replaceAll("{v}", vlan);
    const halves = splitPool(cidr);
    if (!halves) throw new Error(`La plantilla de red no sirve: «${cidr}». Ejemplo: 10.10.{n}.0/24`);
    pons.push({ name: `PON ${i}`, vlan, inet_cidr: halves[0], tv_cidr: halves[1] });
  }
  return pons;
}

async function renderOltConfig() {
  const box = $("#oltBox");
  if (!box) return;
  try {
    const olts = await api("/api/olts");
    const list = olts.length
      ? `<table><thead><tr><th>OLT</th><th>PON</th><th>VLAN</th><th></th></tr></thead><tbody>${olts
          .map((o) => {
            const v = o.pons.map((p) => p.vlan).filter(Boolean);
            return `<tr><td><b>${esc(o.name)}</b></td><td>${o.pons.length}</td>
              <td>${v.length ? Math.min(...v) + "–" + Math.max(...v) : "—"}</td>
              <td class="row"><button class="ghost small" data-oltedit="${o.id}">Editar</button>
              <button class="danger small" data-oltdel="${o.id}">Eliminar</button></td></tr>`;
          })
          .join("")}</tbody></table>`
      : '<p class="muted">Todavía no cargaste ninguna OLT.</p>';
    box.dataset.olts = JSON.stringify(olts);
    box.innerHTML = `${list}<div id="oltForm"></div>
      <div class="row" style="margin-top:.8rem"><button id="oltNew">Agregar OLT</button></div>`;
    if (OLT_EDIT) oltRenderForm();
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function oltRenderForm() {
  const f = $("#oltForm");
  if (!f || !OLT_EDIT) { if (f) f.innerHTML = ""; return; }
  const rows = OLT_EDIT.pons
    .map(
      (p, i) => `<tr><td><input data-pon="${i}" data-k="name" value="${esc(p.name)}" maxlength="40"></td>
      <td><input data-pon="${i}" data-k="vlan" type="number" min="0" max="4094" value="${p.vlan || ""}"></td>
      <td><input data-pon="${i}" data-k="inet_cidr" value="${esc(p.inet_cidr)}" placeholder="10.10.1.0/25"></td>
      <td><input data-pon="${i}" data-k="tv_cidr" value="${esc(p.tv_cidr)}" placeholder="10.10.1.128/25"></td>
      <td><button class="ghost small" data-ponrm="${i}" aria-label="Quitar">✕</button></td></tr>`
    )
    .join("");
  f.innerHTML = `<h3 style="margin-top:1rem">${OLT_EDIT.id ? "Editar OLT" : "Nueva OLT"}</h3>
    <div class="form-grid">
      <div class="field"><label>Nombre de la OLT</label><input id="olt_name" value="${esc(OLT_EDIT.name)}" placeholder="OLT-Centro" maxlength="40"></div>
      <div class="field"><label>Cantidad de PON</label><input id="olt_count" type="number" min="1" max="128" value="16"></div>
      <div class="field"><label>VLAN del primer PON</label><input id="olt_vlan0" type="number" min="1" max="4094" value="101"></div>
      <div class="field"><label>Red de cada VLAN</label><input id="olt_tpl" value="10.10.{n}.0/24">
        <small>{n} = número de PON · {v} = VLAN. Se parte en dos /25: Internet (primera mitad) y TV (segunda).</small></div>
    </div>
    <div class="row" style="margin:.6rem 0"><button class="ghost" id="oltGen">Generar los PON</button>
      <button class="ghost" id="oltAddRow">Agregar un PON</button>
      <span class="muted">Después podés corregir cualquier fila a mano.</span></div>
    <div class="tablewrap"><table><thead><tr><th>PON</th><th>VLAN</th><th>Pool Internet</th><th>Pool TV</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="muted">Generá los PON o agregá uno.</td></tr>'}</tbody></table></div>
    <div class="row" style="margin-top:.8rem"><button id="oltSave">Guardar OLT</button>
      <button class="ghost" id="oltCancel">Cancelar</button></div>`;
}

document.addEventListener("input", (ev) => {
  const el = ev.target;
  if (!OLT_EDIT) return;
  if (el.id === "olt_name") OLT_EDIT.name = el.value;
  else if (el.dataset && el.dataset.pon !== undefined) {
    const p = OLT_EDIT.pons[parseInt(el.dataset.pon, 10)];
    if (p) p[el.dataset.k] = el.dataset.k === "vlan" ? parseInt(el.value, 10) || 0 : el.value;
  }
});

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  try {
    if (t.id === "oltNew") {
      OLT_EDIT = { id: null, name: "", pons: [] };
      oltRenderForm();
    } else if (t.dataset.oltedit) {
      const olts = JSON.parse($("#oltBox").dataset.olts || "[]");
      const o = olts.find((x) => x.id === parseInt(t.dataset.oltedit, 10));
      OLT_EDIT = { id: o.id, name: o.name, pons: o.pons.map((p) => ({ name: p.name, vlan: p.vlan, inet_cidr: p.inet_cidr, tv_cidr: p.tv_cidr })) };
      oltRenderForm();
      $("#oltForm").scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (t.dataset.oltdel) {
      if (!confirm("¿Eliminar esta OLT y su mapa? Las alertas de sus sectores se resuelven.")) return;
      await api(`/api/olts/${t.dataset.oltdel}`, { method: "DELETE" });
      OLT_EDIT = null;
      toast("OLT eliminada");
      renderOltConfig();
    } else if (t.id === "oltGen") {
      const count = parseInt($("#olt_count").value, 10) || 0;
      if (count < 1 || count > 128) throw new Error("La cantidad de PON va de 1 a 128");
      const v0 = parseInt($("#olt_vlan0").value, 10) || 1;
      OLT_EDIT.name = $("#olt_name").value;
      OLT_EDIT.pons = oltGenerate(count, v0, $("#olt_tpl").value);
      oltRenderForm();
    } else if (t.id === "oltAddRow") {
      OLT_EDIT.name = $("#olt_name").value;
      const n = OLT_EDIT.pons.length + 1;
      OLT_EDIT.pons.push({ name: `PON ${n}`, vlan: 0, inet_cidr: "", tv_cidr: "" });
      oltRenderForm();
    } else if (t.dataset.ponrm !== undefined) {
      OLT_EDIT.pons.splice(parseInt(t.dataset.ponrm, 10), 1);
      oltRenderForm();
    } else if (t.id === "oltCancel") {
      OLT_EDIT = null;
      oltRenderForm();
    } else if (t.id === "oltSave") {
      const body = { name: OLT_EDIT.name.trim(), pons: OLT_EDIT.pons };
      if (OLT_EDIT.id) await api(`/api/olts/${OLT_EDIT.id}`, { method: "PUT", body });
      else await api("/api/olts", { method: "POST", body });
      OLT_EDIT = null;
      toast("OLT guardada");
      NET.nodes = []; NET.byId.clear(); NET.fitted = false;
      renderOltConfig();
    }
  } catch (e) {
    toast(e.message, true);
  }
});
