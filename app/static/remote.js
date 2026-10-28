"use strict";
/* Routers REMOTOS (túnel WireGuard). Usa `$`, `esc`, `api`, `toast`, `fmtDur` de app.js. */

let REMOTE_SCRIPT = null;   // { id, name, text } del último script mostrado
let remoteTimer = null;

function remoteStatusHTML(st) {
  const warn = (t) => `<p class="router-err">${t}</p>`;
  let html = "";
  if (!st.server_ready) {
    html += warn("El servidor todavía no tiene claves de WireGuard. Corré <code>./install.sh</code> de nuevo.");
  } else if (!st.helper_running) {
    html += `<p class="muted">El servicio del túnel no está publicando estado. Si este equipo es un LXC, puede faltar el módulo
      WireGuard en el host (ver README, «Router remoto»). Podés crear igual el script.</p>`;
  }
  if (st.write_error) html += warn(esc(st.write_error));
  const rows = st.peers
    .map((p) => {
      const chip = p.connected
        ? '<span class="st up">CONECTADO</span>'
        : p.last_handshake
        ? `<span class="st alert">SIN TÚNEL (hace ${fmtDur(Date.now() / 1000 - p.last_handshake)})</span>`
        : '<span class="st grace">esperando el script</span>';
      return `<tr><td><b>${esc(p.name || "?")}</b></td><td class="mono">${esc(p.ip)}</td><td>${chip}</td>
        <td><button class="ghost small" data-remscript="${p.router_id}">Ver script</button></td></tr>`;
    })
    .join("");
  html += rows
    ? `<div class="tablewrap"><table><thead><tr><th>Router remoto</th><th>IP del túnel</th><th>Estado</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="muted">Todavía no hay routers remotos.</p>';
  return html;
}

async function renderRemote() {
  const box = $("#remoteBox");
  if (!box) return;
  try {
    const st = await api("/api/remote/status");
    box.innerHTML = `
      <p class="muted">Para un MikroTik que está en <b>otra red</b> (otro sitio, detrás de NAT/CGNAT). El router abre un túnel
        <b>WireGuard</b> hacia este servidor y todo (API y log) viaja cifrado por ese túnel. No se abre la API a Internet.</p>
      <ol class="steps">
        <li>Indicá la IP pública o el dominio de este servidor, y reenviá el puerto <b>UDP ${st.port}</b> hacia él.</li>
        <li>Creá el router remoto y copiá el script que se genera.</li>
        <li>Pegalo en una terminal del MikroTik. En unos segundos pasa a <b>CONECTADO</b> y empieza a monitorearse solo.</li>
      </ol>
      <div class="form-grid">
        <div class="field"><label>IP pública o dominio del servidor</label>
          <input id="rem_endpoint" value="${esc(st.endpoint)}" placeholder="magi.midominio.com o 200.10.20.30">
          <small>Sin http:// ni puerto. Red del túnel: ${esc(st.network)}.</small></div>
        <div class="field" style="align-self:end"><button class="ghost" id="remSaveEndpoint">Guardar</button></div>
      </div>
      <div class="form-grid" style="margin-top:.6rem">
        <div class="field"><label>Nombre del router remoto</label><input id="rem_name" placeholder="Torre-Norte" maxlength="60"></div>
        <div class="field" style="align-self:end"><button id="remCreate">Crear router remoto</button></div>
      </div>
      <div id="remScriptBox"></div>
      <h3 style="margin-top:1rem">Estado de los túneles</h3>
      <div id="remoteStatus">${remoteStatusHTML(st)}</div>`;
    if (REMOTE_SCRIPT) remoteShowScript();
    clearInterval(remoteTimer);
    remoteTimer = setInterval(async () => {
      if (!$("#remoteStatus")) return clearInterval(remoteTimer);
      try { $("#remoteStatus").innerHTML = remoteStatusHTML(await api("/api/remote/status")); } catch (_) { /* reintenta */ }
    }, 5000);
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

function remoteShowScript() {
  const b = $("#remScriptBox");
  if (!b || !REMOTE_SCRIPT) return;
  b.innerHTML = `<div class="card" style="background:var(--panel-2);margin-top:.8rem">
    <b>Script para «${esc(REMOTE_SCRIPT.name)}»</b>
    <p class="muted" style="margin:.2rem 0 .5rem">Pegalo completo en una terminal del MikroTik (Winbox → New Terminal). Contiene claves privadas: no lo compartas.</p>
    <div class="row" style="margin-bottom:.5rem"><button id="remCopy">Copiar script</button></div>
    <pre class="script" id="remScriptText">${esc(REMOTE_SCRIPT.text)}</pre></div>`;
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  try {
    if (t.id === "remSaveEndpoint") {
      await api("/api/remote/endpoint", { method: "PUT", body: { endpoint: $("#rem_endpoint").value } });
      toast("Guardado");
    } else if (t.id === "remCreate") {
      const name = $("#rem_name").value.trim();
      if (!name) throw new Error("Poné un nombre para el router");
      const r = await api("/api/remote/routers", { method: "POST", body: { name } });
      REMOTE_SCRIPT = { id: r.id, name, text: await api(`/api/remote/routers/${r.id}/script`) };
      toast("Router remoto creado");
      await renderRemote();
    } else if (t.dataset.remscript) {
      const id = parseInt(t.dataset.remscript, 10);
      const st = await api("/api/remote/status");
      const p = st.peers.find((x) => x.router_id === id);
      REMOTE_SCRIPT = { id, name: p ? p.name : "", text: await api(`/api/remote/routers/${id}/script`) };
      remoteShowScript();
      $("#remScriptBox").scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (t.id === "remCopy") {
      await navigator.clipboard.writeText(REMOTE_SCRIPT.text);
      toast("Script copiado");
    }
  } catch (e) {
    toast(e.message, true);
  }
});
