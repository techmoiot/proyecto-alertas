"use strict";
/* Respaldos de la base de MAGI y de la configuración de los MikroTik. Usa `$`, `esc`, `api`, `toast`, `fmtDur` de app.js. */

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function fmtStamp(ts) {
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function backupLink(kind, f, rid) {
  const q = `kind=${kind}&name=${encodeURIComponent(f.name)}${rid ? "&router_id=" + rid : ""}`;
  return `<a href="/api/backups/download?${q}" download>Descargar</a>`;
}

async function renderBackups() {
  const box = $("#backupsBox");
  if (!box) return;
  try {
    const b = await api("/api/backups");
    if (!b.enabled) {
      box.innerHTML = '<p class="muted">Los respaldos no están disponibles en este modo (base de datos en memoria).</p>';
      return;
    }
    const dbRows = b.db
      .slice(0, 10)
      .map((f) => `<tr><td>${fmtStamp(f.created)}</td><td class="num">${fmtBytes(f.size)}</td><td>${esc(f.name)}</td><td>${backupLink("db", f)}</td></tr>`)
      .join("");
    const rRows = b.routers
      .map((r) => {
        const last = r.files[0];
        const err = r.error ? `<div class="router-err">${esc(r.error)}${/cambió/.test(r.error) ? ` <button class="ghost small" data-bkforget="${r.router_id}">Olvidar clave SSH</button>` : ""}</div>` : "";
        const versions = r.files
          .slice(0, 5)
          .map((f) => `<div>${fmtStamp(f.created)} <span class="muted">${fmtBytes(f.size)}</span> ${backupLink("router", f, r.router_id)}</div>`)
          .join("");
        return `<tr><td><b>${esc(r.router)}</b><div class="muted mono">${esc(r.host)}</div></td>
          <td><label style="display:flex;gap:.4rem;align-items:center"><input type="checkbox" data-bkcfg="${r.router_id}" ${r.cfg_backup ? "checked" : ""}> activo</label>
            <label class="muted">SSH <input type="number" data-bkport="${r.router_id}" value="${r.ssh_port}" style="width:5.5rem"></label></td>
          <td>${last ? fmtStamp(last.created) : '<span class="muted">sin respaldos</span>'}${err}</td>
          <td>${versions || '<span class="muted">—</span>'}${r.files.length > 5 ? `<div class="muted">y ${r.files.length - 5} más</div>` : ""}</td>
          <td><button class="ghost small" data-bknow="${r.router_id}">Respaldar ahora</button></td></tr>`;
      })
      .join("");
    box.innerHTML = `
      <h3 style="margin-top:0">Base de MAGI</h3>
      <p class="muted" style="margin-top:0">Usuarios, routers, OLT, alertas e historial. Se respalda sola cada ${""}<span id="bkEvery"></span> h y se guardan las últimas copias en <code>${esc(b.dir)}</code>.
        <b>Cada copia contiene contraseñas de routers y claves del túnel</b>: guardala en un lugar seguro.</p>
      ${b.db_error ? `<p class="router-err">${esc(b.db_error)}</p>` : ""}
      <div class="row" style="margin-bottom:.6rem"><button id="bkDbNow">Respaldar ahora</button></div>
      ${dbRows ? `<div class="tablewrap"><table><thead><tr><th>Fecha</th><th class="num">Tamaño</th><th>Archivo</th><th></th></tr></thead><tbody>${dbRows}</tbody></table></div>` : '<p class="muted">Todavía no hay copias.</p>'}
      <details style="margin-top:.6rem"><summary>Cómo restaurar una copia</summary>
        <p class="muted">En el servidor, como root: <code>magi-restore /ruta/magi-AAAAMMDD-HHMMSS.db.gz</code>. Detiene MAGI, guarda la base actual como respaldo de seguridad, restaura y vuelve a arrancar.</p></details>

      <h3 style="margin-top:1.4rem">Configuración de los MikroTik</h3>
      <p class="muted" style="margin-top:0">Se exporta por <b>SSH</b> (<code>/export terse</code>) y solo se guarda una versión nueva si la configuración cambió. Cada router necesita el servicio SSH accesible desde este servidor
        y que su usuario tenga el permiso <code>ssh</code> (<code>/user group set monitor-ro policy=read,api,ssh</code>). Al ser un usuario de solo lectura, el export no incluye contraseñas.
        La primera conexión memoriza la clave SSH del router y avisa si cambia.</p>
      ${b.routers.length ? `<div class="tablewrap"><table><thead><tr><th>Router</th><th>Respaldo</th><th>Último</th><th>Versiones</th><th></th></tr></thead><tbody>${rRows}</tbody></table></div>` : '<p class="muted">Sin routers.</p>'}`;
    api("/api/settings").then((s) => { const e = $("#bkEvery"); if (e) e.textContent = s.settings.backup_every_h; }).catch(() => {});
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

document.addEventListener("change", async (ev) => {
  const el = ev.target;
  if (!el.dataset || (el.dataset.bkcfg === undefined && el.dataset.bkport === undefined)) return;
  const rid = el.dataset.bkcfg ?? el.dataset.bkport;
  try {
    const chk = document.querySelector(`[data-bkcfg="${rid}"]`), port = document.querySelector(`[data-bkport="${rid}"]`);
    await api(`/api/backups/routers/${rid}/config`, { method: "PUT", body: { cfg_backup: chk.checked, ssh_port: parseInt(port.value, 10) || 22 } });
    toast(chk.checked ? "Respaldo de configuración activado" : "Respaldo de configuración desactivado");
  } catch (e) {
    toast(e.message, true);
    renderBackups();
  }
});

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  try {
    if (t.id === "bkDbNow") {
      t.disabled = true;
      await api("/api/backups/db", { method: "POST" });
      toast("Respaldo de la base creado");
      renderBackups();
    } else if (t.dataset.bknow) {
      t.disabled = true;
      t.textContent = "Conectando…";
      try {
        const r = await api(`/api/backups/routers/${t.dataset.bknow}`, { method: "POST" });
        toast(`Configuración respaldada (${fmtBytes(r.size)})`);
      } finally {
        renderBackups();
      }
    } else if (t.dataset.bkforget) {
      if (!confirm("¿Olvidar la clave SSH guardada de este router? Se aprenderá de nuevo en la próxima conexión.")) return;
      await api(`/api/backups/routers/${t.dataset.bkforget}/hostkey`, { method: "DELETE" });
      toast("Clave SSH olvidada");
      renderBackups();
    }
  } catch (e) {
    toast(e.message, true);
    renderBackups();
  }
});
