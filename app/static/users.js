"use strict";
/* Usuarios y contraseñas. Usa `$`, `esc`, `api`, `toast` de app.js. */

const ROLE_ES = { admin: "Administrador", viewer: "Solo ver" };

function fmtWhen(ts) {
  if (!ts) return "nunca";
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function accountHTML() {
  return `<h3 style="margin-top:0">Mi contraseña${ME && ME.user ? ` <span class="muted">(${esc(ME.user)})</span>` : ""}</h3>
    <div class="form-grid">
      <div class="field"><label>Contraseña actual</label><input id="pw_cur" type="password" autocomplete="current-password"></div>
      <div class="field"><label>Contraseña nueva</label><input id="pw_new" type="password" autocomplete="new-password"><small>Mínimo 8 caracteres.</small></div>
      <div class="field"><label>Repetir la nueva</label><input id="pw_new2" type="password" autocomplete="new-password"></div>
    </div>
    <div class="row" style="margin-top:.6rem"><button id="pwChange">Cambiar mi contraseña</button></div>`;
}

async function renderUsers() {
  const box = $("#usersBox");
  if (!box) return;
  try {
    const admin = !ME || ME.role === "admin";
    let list = "";
    if (admin) {
      const users = await api("/api/users");
      const rows = users
        .map(
          (u) => `<tr><td><b>${esc(u.username)}</b>${ME && u.username === ME.user ? ' <span class="muted">(vos)</span>' : ""}</td>
          <td><select data-userrole="${u.id}">${Object.entries(ROLE_ES)
            .map(([k, v]) => `<option value="${k}" ${u.role === k ? "selected" : ""}>${v}</option>`).join("")}</select></td>
          <td class="muted">${fmtWhen(u.last_login)}</td>
          <td><div class="row"><button class="ghost small" data-userpw="${u.id}" data-username="${esc(u.username)}">Cambiar contraseña</button>
          <button class="danger small" data-userdel="${u.id}" data-username="${esc(u.username)}">Eliminar</button></div></td></tr>`
        )
        .join("");
      list = `<h3>Usuarios</h3>
        <p class="muted" style="margin-top:0"><b>Administrador</b>: puede cambiar todo. <b>Solo ver</b>: mira el panel y el mapa (ideal para la pantalla/TV), sin tocar nada.</p>
        <div class="tablewrap"><table><thead><tr><th>Usuario</th><th>Permiso</th><th>Último acceso</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
        <h3 style="margin-top:1rem">Agregar usuario</h3>
        <div class="form-grid">
          <div class="field"><label>Usuario</label><input id="nu_name" autocomplete="off" autocapitalize="none" maxlength="40" placeholder="maria"></div>
          <div class="field"><label>Contraseña</label><input id="nu_pass" type="password" autocomplete="new-password"><small>Mínimo 8 caracteres.</small></div>
          <div class="field"><label>Permiso</label><select id="nu_role"><option value="viewer">Solo ver</option><option value="admin">Administrador</option></select></div>
        </div>
        <div class="row" style="margin-top:.6rem"><button id="userAdd">Agregar usuario</button></div>
        <hr style="border:0;border-top:1px solid var(--line);margin:1.2rem 0">`;
    }
    box.innerHTML = list + accountHTML();
  } catch (e) {
    box.innerHTML = `<p class="router-err">${esc(e.message)}</p>`;
  }
}

document.addEventListener("change", async (ev) => {
  const sel = ev.target;
  if (!sel.dataset || sel.dataset.userrole === undefined) return;
  try {
    await api(`/api/users/${sel.dataset.userrole}`, { method: "PUT", body: { role: sel.value } });
    toast("Permiso actualizado");
  } catch (e) {
    toast(e.message, true);
  }
  renderUsers();
});

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  try {
    if (t.id === "userAdd") {
      await api("/api/users", { method: "POST", body: { username: $("#nu_name").value, password: $("#nu_pass").value, role: $("#nu_role").value } });
      toast("Usuario agregado");
      renderUsers();
    } else if (t.dataset.userpw) {
      const pw = prompt(`Contraseña nueva para «${t.dataset.username}» (mínimo 8 caracteres).\nSi tiene sesiones abiertas, se cierran.`);
      if (!pw) return;
      await api(`/api/users/${t.dataset.userpw}`, { method: "PUT", body: { password: pw } });
      toast("Contraseña cambiada");
      renderUsers();
    } else if (t.dataset.userdel) {
      if (!confirm(`¿Eliminar al usuario «${t.dataset.username}»?`)) return;
      await api(`/api/users/${t.dataset.userdel}`, { method: "DELETE" });
      toast("Usuario eliminado");
      renderUsers();
    } else if (t.id === "pwChange") {
      const n1 = $("#pw_new").value;
      if (n1 !== $("#pw_new2").value) throw new Error("Las contraseñas nuevas no coinciden");
      await api("/api/me/password", { method: "POST", body: { current: $("#pw_cur").value, new: n1 } });
      toast("Contraseña cambiada");
      renderUsers();
    }
  } catch (e) {
    toast(e.message, true);
  }
});
