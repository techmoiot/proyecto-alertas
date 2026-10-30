"""API web y servidor del panel."""

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import signal
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import __version__
from .backup import MAX_RESTORE_BYTES, BackupError, BackupManager
from .config import Config, load
from .db import DB, DEFAULT_SETTINGS
from .demo import DemoClient, apply_demo
from .engine import Engine
from .notify import Notifier
from .poller import Poller, RouterClient
from .pppoe import PPPoEMonitor, explain_reason
from .syslog import SyslogReceiver
from .topology import ConfigError, Topology, detect_pons
from .users import UserError, Users, fingerprint
from .wg import RemoteError, WGManager

log = logging.getLogger("mkmonitor")
min_ = min          # (algunos parámetros de la API se llaman `min`)

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
SCRIPT_TEMPLATE = BASE.parent / "routeros" / "conectar-mikrotik.rsc"

ROUTER_PUBLIC_FIELDS = (
    "id, name, host, port, username, use_ssl, types, include, enabled, pppoe_enabled, syslog_ip, "
    "cfg_backup, ssh_port"
)


class RouterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(8728, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field("", max_length=128)
    use_ssl: bool = False
    types: str = Field("ether", max_length=120)
    include: str = Field("", max_length=300)
    enabled: bool = True
    pppoe_enabled: bool = True
    cfg_backup: bool = False
    ssh_port: int = Field(22, ge=1, le=65535)
    syslog_ip: str = Field("", max_length=45)


class BackupRouterIn(BaseModel):
    cfg_backup: bool
    ssh_port: int = Field(22, ge=1, le=65535)


class RouterTest(RouterIn):
    router_id: int | None = None


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    host: str | None = Field(None, max_length=255)
    port: int = Field(8728, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=128)
    use_ssl: bool = False


class PonIn(BaseModel):
    name: str = Field("", max_length=40)
    vlan: int = Field(0, ge=0, le=4094)
    card: int = Field(1, ge=1, le=32)
    inet_cidr: str = Field("", max_length=64)
    tv_cidr: str = Field("", max_length=64)


class OltIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    cards: int = Field(1, ge=1, le=32)
    ports_per_card: int = Field(0, ge=0, le=64)
    pons: list[PonIn] = Field(min_length=1, max_length=128)


class LoginIn(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=256)
    remember: bool = False


class UserIn(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=256)
    role: str = "viewer"


class UserEdit(BaseModel):
    password: str | None = Field(None, max_length=256)
    role: str | None = None


class PasswordChange(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class LoginRedirect(Exception):
    """Se pidió una página sin sesión: el navegador debe ir a /login."""


SESSION_COOKIE = "magi_session"
SESSION_HOURS = 12
REMEMBER_DAYS = 30
LOGIN_MAX_FAILS = 6          # intentos fallidos por IP dentro de la ventana
LOGIN_WINDOW_S = 300


class RemoteRouterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class EndpointIn(BaseModel):
    endpoint: str = Field(max_length=253)


class DetectIn(BaseModel):
    vlan_from: int | None = Field(None, ge=1, le=4094)
    vlan_to: int | None = Field(None, ge=1, le=4094)


class IfaceConfigIn(BaseModel):
    label: str | None = Field(None, max_length=60)
    monitor: bool | None = None
    link_alert: bool | None = None
    max_rx_mbps: float | None = Field(None, ge=0)
    max_tx_mbps: float | None = Field(None, ge=0)


def _natural(name: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def _persistent_secret(db: DB, key: str, configured: str, label: str) -> str:
    """Usa el valor del entorno; si no hay, genera uno y lo guarda en la base."""
    if configured:
        return configured
    row = db.one("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        return row["value"]
    value = secrets.token_urlsafe(12)
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, value))
    log.warning("%s (autogenerado, queda guardado): %s", label, value)
    return value


def _silent_secret(db: DB, key: str) -> str:
    """Secreto interno generado una vez y guardado en la base (no se imprime en el log)."""
    row = db.one("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        return row["value"]
    value = secrets.token_urlsafe(32)
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, value))
    return value


def create_app(
    cfg: Config | None = None, *, start_poller: bool = True, client_factory=None
) -> FastAPI:
    cfg = cfg or load()
    db = DB(cfg.db_path)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat)
    engine = Engine(db, notifier)
    pppoe = PPPoEMonitor(db, engine)
    syslog = SyslogReceiver(cfg.syslog_port, pppoe.on_syslog)
    topology = Topology(db, engine, pppoe)
    remote = WGManager(db, cfg)
    backup_dir = cfg.backup_dir or (str(Path(cfg.db_path).parent / "backups") if cfg.db_path != ":memory:" else "")
    backup = BackupManager(db, backup_dir or None, wg_state_dir=cfg.wg_state_dir)
    if client_factory is None:
        client_factory = DemoClient if cfg.demo else RouterClient
    poller = Poller(db, engine, client_factory, pppoe, topology, remote, backup)
    if cfg.demo:
        apply_demo(db)

    users = Users(db)
    if users.count() == 0:
        # Primer arranque (o instalación anterior a los usuarios): el admin nace con la contraseña
        # del entorno o una generada. Desde acá la base manda: se cambia en el panel o con magi-passwd.
        seed = _persistent_secret(db, "admin_password", cfg.admin_password, "Contraseña de admin")
        users.create(cfg.admin_user, seed, "admin", enforce=False)
    register_token = _persistent_secret(db, "register_token", cfg.register_token, "Token de registro")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_poller:
            poller.start()
            syslog.start()
        yield
        poller.stop()
        syslog.stop()

    app = FastAPI(title="MAGI", version=__version__, lifespan=lifespan)
    app.state.db, app.state.engine, app.state.poller = db, engine, poller
    app.state.pppoe, app.state.syslog, app.state.topology = pppoe, syslog, topology
    app.state.remote = remote
    app.state.backup = backup
    app.state.restart = lambda: os.kill(os.getpid(), signal.SIGTERM)   # systemd lo vuelve a levantar
    app.state.cfg, app.state.notifier = cfg, notifier

    basic = HTTPBasic(auto_error=False)
    # La sesión se firma con un secreto de la base y con una huella del hash de contraseña del usuario:
    # al cambiarle la contraseña, sus sesiones abiertas dejan de valer.
    session_key = _silent_secret(db, "session_secret").encode()
    login_fails: dict[str, list[float]] = {}

    def _sign(row: dict, exp: float) -> str:
        msg = f"{row['username']}|{int(exp)}"
        sig = hmac.new(session_key, f"{msg}|{fingerprint(row['pw_hash'])}".encode(), hashlib.sha256).hexdigest()
        return msg + "|" + sig

    def _session_user(token: str | None) -> dict | None:
        if not token:
            return None
        try:
            name, exp, sig = token.rsplit("|", 2)
            row = users.get(name)
            if row is None or float(exp) <= time.time():
                return None
            good = hmac.new(session_key, f"{name}|{exp}|{fingerprint(row['pw_hash'])}".encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, good) and name == row["username"]:
                return row
        except ValueError:
            pass
        return None

    def _current(request: Request, creds: HTTPBasicCredentials | None) -> dict | None:
        row = _session_user(request.cookies.get(SESSION_COOKIE))
        if row is None and creds is not None:
            row = users.authenticate(creds.username, creds.password)
        return row

    def _set_session(resp, request: Request, row: dict, life: int, persistent: bool) -> None:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
        resp.set_cookie(SESSION_COOKIE, _sign(row, time.time() + life), max_age=life if persistent else None,
                        httponly=True, samesite="lax", secure=secure, path="/")

    def _unauthorized(request: Request) -> Exception:
        if request.url.path == "/":
            return LoginRedirect()
        headers = {}
        if request.headers.get("x-requested-with", "").lower() != "magi":
            # Los clientes que no son el panel (scripts, curl) siguen pudiendo usar usuario/contraseña HTTP.
            headers["WWW-Authenticate"] = 'Basic realm="MAGI"'
        return HTTPException(status_code=401, detail="Credenciales requeridas", headers=headers)

    def admin(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        row = _current(request, creds)
        if row is None:
            raise _unauthorized(request)
        if row["role"] != "admin":
            raise HTTPException(403, "Tu usuario es de solo lectura")

    def viewer(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not (cfg.public_view or _current(request, creds)):
            raise _unauthorized(request)

    def logged_in(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> dict:
        row = _current(request, creds)
        if row is None:
            raise _unauthorized(request)
        return row

    @app.exception_handler(LoginRedirect)
    async def _login_redirect(request: Request, exc: LoginRedirect):
        return RedirectResponse("/login", status_code=302)

    def _sync() -> None:
        if start_poller:
            poller.sync()

    # ------------------------------------------------------------------ páginas

    def _page(name: str, media: str) -> FileResponse:
        return FileResponse(STATIC / name, media_type=media, headers={"Cache-Control": "no-cache"})

    @app.get("/", dependencies=[Depends(viewer)], include_in_schema=False)
    def index():
        return _page("index.html", "text/html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if _session_user(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=302)
        return _page("login.html", "text/html")

    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in login_fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
        if len(recent) >= LOGIN_MAX_FAILS:
            wait = int(LOGIN_WINDOW_S - (now - recent[0])) + 1
            raise HTTPException(429, f"Demasiados intentos. Esperá {wait} s.", headers={"Retry-After": str(wait)})
        row = users.authenticate(body.username, body.password)
        if row is None:
            recent.append(now)
            login_fails[ip] = recent
            log.warning("Intento de acceso fallido desde %s", ip)
            raise HTTPException(401, "Usuario o contraseña incorrectos")
        login_fails.pop(ip, None)
        users.touch_login(row["id"])
        life = REMEMBER_DAYS * 86400 if body.remember else SESSION_HOURS * 3600
        resp = JSONResponse({"ok": True, "user": row["username"], "role": row["role"]})
        _set_session(resp, request, row, life, body.remember)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
        row = _current(request, creds)
        return {"authed": row is not None, "user": row["username"] if row else None,
                "role": row["role"] if row else None, "public_view": cfg.public_view}

    @app.post("/api/me/password")
    def change_own_password(body: PasswordChange, request: Request, row: dict = Depends(logged_in)):
        if not users.authenticate(row["username"], body.current):
            raise HTTPException(400, "La contraseña actual no es correcta")
        try:
            users.set_password(row["id"], body.new)
        except UserError as exc:
            raise HTTPException(400, str(exc)) from None
        resp = JSONResponse({"ok": True})
        _set_session(resp, request, users.get_id(row["id"]), SESSION_HOURS * 3600, False)
        return resp

    # ------------------------------------------------------------------ usuarios

    @app.get("/api/users", dependencies=[Depends(admin)])
    def users_list():
        return users.list()

    @app.post("/api/users", dependencies=[Depends(admin)])
    def users_create(body: UserIn):
        try:
            return {"id": users.create(body.username, body.password, body.role)}
        except UserError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/users/{uid}")
    def users_edit(uid: int, body: UserEdit, request: Request, me_row: dict = Depends(logged_in)):
        if me_row["role"] != "admin":
            raise HTTPException(403, "Tu usuario es de solo lectura")
        if not users.get_id(uid):
            raise HTTPException(404, "Usuario no encontrado")
        try:
            if body.role is not None:
                users.set_role(uid, body.role)
            if body.password:
                users.set_password(uid, body.password)
        except UserError as exc:
            raise HTTPException(400, str(exc)) from None
        resp = JSONResponse({"ok": True})
        if uid == me_row["id"]:
            # Su propia sesión se renueva para que no lo saque del panel.
            _set_session(resp, request, users.get_id(uid), SESSION_HOURS * 3600, False)
        return resp

    @app.delete("/api/users/{uid}")
    def users_delete(uid: int, me_row: dict = Depends(logged_in)):
        if me_row["role"] != "admin":
            raise HTTPException(403, "Tu usuario es de solo lectura")
        if uid == me_row["id"]:
            raise HTTPException(400, "No podés eliminar tu propio usuario")
        try:
            users.delete(uid)
        except KeyError:
            raise HTTPException(404, "Usuario no encontrado") from None
        except UserError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"deleted": uid}

    @app.get("/app.js", dependencies=[Depends(viewer)], include_in_schema=False)
    def app_js():
        return _page("app.js", "application/javascript")

    @app.get("/network.js", dependencies=[Depends(viewer)], include_in_schema=False)
    def network_js():
        return _page("network.js", "application/javascript")

    @app.get("/remote.js", dependencies=[Depends(viewer)], include_in_schema=False)
    def remote_js():
        return _page("remote.js", "application/javascript")

    @app.get("/users.js", dependencies=[Depends(viewer)], include_in_schema=False)
    def users_js():
        return _page("users.js", "application/javascript")

    @app.get("/backups.js", dependencies=[Depends(viewer)], include_in_schema=False)
    def backups_js():
        return _page("backups.js", "application/javascript")

    @app.get("/style.css", dependencies=[Depends(viewer)], include_in_schema=False)
    def style_css():
        return _page("style.css", "text/css")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------------- estado

    @app.get("/api/state", dependencies=[Depends(viewer)])
    def state():
        snap = engine.snapshot()
        pppoe_stats = pppoe.summary()
        routers = []
        for r in db.query(f"SELECT {ROUTER_PUBLIC_FIELDS} FROM routers ORDER BY name"):
            live = snap.get(r["id"])
            ifaces = sorted(live["ifaces"].values(), key=lambda i: _natural(i["name"])) if live else []
            routers.append(
                {
                    **r,
                    "enabled": bool(r["enabled"]),
                    "use_ssl": bool(r["use_ssl"]),
                    "pppoe_enabled": bool(r["pppoe_enabled"]),
                    "pppoe": pppoe_stats.get(r["id"]),
                    "reachable": live["reachable"] if live else None,
                    "last_ok": live["last_ok"] if live else None,
                    "error": live["error"] if live else None,
                    "ifaces": ifaces,
                }
            )
        alerts = db.query(
            "SELECT a.id, a.router_id, COALESCE(r.name, CASE WHEN a.router_id = 0 THEN 'OLT' END) AS router, a.iface, a.kind, a.severity, a.message, "
            "a.value, a.started_at, a.acked_at FROM alerts a "
            "LEFT JOIN routers r ON r.id = a.router_id WHERE a.resolved_at IS NULL "
            "ORDER BY (a.severity = 'critical') DESC, a.started_at DESC"
        )
        return {
            "now": time.time(),
            "routers": routers,
            "alerts": alerts,
            "info": {
                "version": __version__,
                "demo": cfg.demo,
                "telegram": notifier.enabled,
                "public_view": cfg.public_view,
                "syslog_port": syslog.port,
                "syslog_listening": syslog.snapshot()["listening"],
            },
        }

    @app.get("/api/alerts", dependencies=[Depends(viewer)])
    def alerts(state: str = "all", limit: int = 200, offset: int = 0):
        limit = max(1, min(limit, 1000))
        where = "WHERE a.resolved_at IS NULL" if state == "active" else ""
        return db.query(
            "SELECT a.id, a.router_id, COALESCE(r.name, CASE WHEN a.router_id = 0 THEN 'OLT' END) AS router, a.iface, a.kind, a.severity, a.message, "
            "a.value, a.started_at, a.resolved_at, a.acked_at FROM alerts a "
            f"LEFT JOIN routers r ON r.id = a.router_id {where} "
            "ORDER BY a.started_at DESC LIMIT ? OFFSET ?",
            (limit, max(0, offset)),
        )

    @app.post("/api/alerts/ack_all", dependencies=[Depends(admin)])
    def ack_all():
        cur = db.execute(
            "UPDATE alerts SET acked_at=? WHERE resolved_at IS NULL AND acked_at IS NULL",
            (time.time(),),
        )
        return {"acked": cur.rowcount}

    @app.post("/api/alerts/{alert_id}/ack", dependencies=[Depends(admin)])
    def ack(alert_id: int):
        cur = db.execute(
            "UPDATE alerts SET acked_at=? WHERE id=? AND acked_at IS NULL", (time.time(), alert_id)
        )
        return {"acked": cur.rowcount}

    @app.delete("/api/alerts/{alert_id}", dependencies=[Depends(admin)])
    def alert_delete(alert_id: int):
        cur = db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Alerta no encontrada")
        return {"deleted": 1}

    @app.delete("/api/alerts", dependencies=[Depends(admin)])
    def alerts_clear(scope: str):
        """Borra alertas en bloque: `acked` (activas ya reconocidas), `resolved` (ya resueltas) o `all`.
        Una alerta activa que se borra no vuelve a avisar mientras el problema siga igual."""
        where = {
            "acked": "resolved_at IS NULL AND acked_at IS NOT NULL",
            "resolved": "resolved_at IS NOT NULL",
            "all": "1=1",
        }.get(scope)
        if where is None:
            raise HTTPException(400, "scope debe ser acked, resolved o all")
        cur = db.execute(f"DELETE FROM alerts WHERE {where}")
        return {"deleted": cur.rowcount}

    @app.get("/api/history", dependencies=[Depends(viewer)])
    def history(router_id: int, iface: str, minutes: int = 60):
        minutes = max(5, min(minutes, 7 * 1440))
        since = time.time() - minutes * 60
        bucket = max(10, int(minutes * 60 / 300))
        rows = db.query(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG(rx_bps) AS rx, AVG(tx_bps) AS tx, "
            "MAX(rx_bps) AS rx_max, MAX(tx_bps) AS tx_max FROM samples "
            "WHERE router_id=? AND iface=? AND ts>=? GROUP BY CAST(ts / ? AS INTEGER) ORDER BY t",
            (bucket, bucket, router_id, iface, since, bucket),
        )
        return {
            "bucket_s": bucket,
            "points": [[r["t"], r["rx"], r["tx"], r["rx_max"], r["tx_max"]] for r in rows],
        }

    # ------------------------------------------------------------------ routers

    @app.get("/api/routers", dependencies=[Depends(admin)])
    def routers_list():
        return db.query(f"SELECT {ROUTER_PUBLIC_FIELDS} FROM routers ORDER BY name")

    def _router_values(body: RouterIn) -> dict[str, Any]:
        return {
            "name": body.name.strip(),
            "host": body.host.strip(),
            "port": body.port,
            "username": body.username.strip(),
            "use_ssl": int(body.use_ssl),
            "types": ",".join(p.strip() for p in body.types.split(",") if p.strip()) or "ether",
            "include": ",".join(p.strip() for p in body.include.split(",") if p.strip()),
            "enabled": int(body.enabled),
            "pppoe_enabled": int(body.pppoe_enabled),
            "syslog_ip": body.syslog_ip.strip(),
            "cfg_backup": int(body.cfg_backup),
            "ssh_port": body.ssh_port,
        }

    @app.post("/api/routers", dependencies=[Depends(admin)])
    def router_add(body: RouterIn):
        v = _router_values(body)
        if db.one("SELECT id FROM routers WHERE host=? AND port=?", (v["host"], v["port"])):
            raise HTTPException(409, "Ya existe un router con esa IP y puerto")
        cur = db.execute(
            "INSERT INTO routers(name, host, port, username, password, use_ssl, types, include, "
            "enabled, updated_at, pppoe_enabled, syslog_ip, cfg_backup, ssh_port) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["name"], v["host"], v["port"], v["username"], body.password, v["use_ssl"],
             v["types"], v["include"], v["enabled"], time.time(), v["pppoe_enabled"],
             v["syslog_ip"], v["cfg_backup"], v["ssh_port"]),
        )
        _sync()
        return {"id": cur.lastrowid}

    @app.put("/api/routers/{rid}", dependencies=[Depends(admin)])
    def router_update(rid: int, body: RouterIn):
        old = db.one("SELECT * FROM routers WHERE id=?", (rid,))
        if not old:
            raise HTTPException(404, "Router no encontrado")
        v = _router_values(body)
        clash = db.one(
            "SELECT id FROM routers WHERE host=? AND port=? AND id<>?", (v["host"], v["port"], rid)
        )
        if clash:
            raise HTTPException(409, "Ya existe otro router con esa IP y puerto")
        password = body.password or old["password"]
        db.execute(
            "UPDATE routers SET name=?, host=?, port=?, username=?, password=?, use_ssl=?, types=?, "
            "include=?, enabled=?, updated_at=?, pppoe_enabled=?, syslog_ip=?, cfg_backup=?, ssh_port=? WHERE id=?",
            (v["name"], v["host"], v["port"], v["username"], password, v["use_ssl"], v["types"],
             v["include"], v["enabled"], time.time(), v["pppoe_enabled"], v["syslog_ip"],
             v["cfg_backup"], v["ssh_port"], rid),
        )
        _sync()
        return {"id": rid}

    @app.delete("/api/routers/{rid}", dependencies=[Depends(admin)])
    def router_delete(rid: int):
        if not db.one("SELECT id FROM routers WHERE id=?", (rid,)):
            raise HTTPException(404, "Router no encontrado")
        db.execute("DELETE FROM routers WHERE id=?", (rid,))
        for table in ("iface_config", "samples", "baselines"):
            db.execute(f"DELETE FROM {table} WHERE router_id=?", (rid,))
        if start_poller:
            poller.sync()  # detiene el hilo del router
        remote.remove_peer(rid)     # si era un router remoto, libera su túnel
        backup.forget_router(rid)   # (los archivos ya guardados se conservan en disco)
        engine.forget_router(rid)   # resuelve sus alertas
        pppoe.forget_router(rid)    # y olvida sus sesiones PPPoE
        return {"deleted": rid}

    @app.post("/api/routers/test", dependencies=[Depends(admin)])
    def router_test(body: RouterTest):
        data = _router_values(body)
        password = body.password
        if not password and body.router_id:
            old = db.one("SELECT password FROM routers WHERE id=?", (body.router_id,))
            password = old["password"] if old else ""
        router = {**data, "password": password, "id": 0}
        client = client_factory(router)
        try:
            client.connect()
            rows = client.fetch_interfaces()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return {
            "ok": True,
            "interfaces": [
                {
                    "name": r.get("name"),
                    "type": r.get("type"),
                    "running": bool(r.get("running")),
                    "comment": r.get("comment", ""),
                }
                for r in rows
            ],
        }

    # ------------------------------------------------------- config de interfaces

    @app.get("/api/routers/{rid}/interfaces", dependencies=[Depends(admin)])
    def iface_list(rid: int):
        live = engine.snapshot().get(rid, {"ifaces": {}})["ifaces"]
        rows = db.query("SELECT * FROM iface_config WHERE router_id=?", (rid,))
        out = []
        for r in sorted(rows, key=lambda r: _natural(r["iface"])):
            li = live.get(r["iface"], {})
            out.append(
                {
                    "name": r["iface"],
                    "label": r["label"],
                    "monitor": bool(r["monitor"]),
                    "link_alert": bool(r["link_alert"]),
                    "max_rx_mbps": r["max_rx_mbps"],
                    "max_tx_mbps": r["max_tx_mbps"],
                    "ever_up": bool(r["ever_up"]),
                    "type": li.get("type", ""),
                    "running": li.get("running"),
                }
            )
        return out

    @app.put("/api/routers/{rid}/interfaces/{iface:path}", dependencies=[Depends(admin)])
    def iface_update(rid: int, iface: str, body: IfaceConfigIn):
        if not db.one("SELECT 1 FROM iface_config WHERE router_id=? AND iface=?", (rid, iface)):
            raise HTTPException(404, "Interfaz no encontrada (todavía no fue leída)")
        sets, params = [], []
        for field in body.model_fields_set:
            value = getattr(body, field)
            if field in ("monitor", "link_alert"):
                value = int(bool(value))
            elif field == "label":
                value = (value or "").strip()
            sets.append(f"{field}=?")
            params.append(value)
        if sets:
            db.execute(
                f"UPDATE iface_config SET {', '.join(sets)} WHERE router_id=? AND iface=?",
                (*params, rid, iface),
            )
        return {"ok": True}

    # ---------------------------------------------------------------------- PPPoE

    def _with_reason(rows: list[dict]) -> list[dict]:
        for r in rows:
            r["reason_es"] = explain_reason(r.get("reason") or "")
        return rows

    @app.get("/api/pppoe/summary", dependencies=[Depends(viewer)])
    def pppoe_summary():
        stats = pppoe.summary()
        reg = pppoe.registered(limit=0)["routers"]
        routers = []
        for r in db.query("SELECT id, name, enabled, pppoe_enabled FROM routers ORDER BY name"):
            st = stats.get(r["id"]) or {}
            routers.append({
                "id": r["id"], "name": r["name"],
                "monitored": bool(r["enabled"] and r["pppoe_enabled"]),
                "online": st.get("online"), "down": st.get("down"),
                "suppressed": st.get("suppressed"), "mass": st.get("mass", False),
                "last_poll": st.get("last_poll"), "error": st.get("error"),
                "registered": reg.get(r["id"], {}).get("registered"),
                "offline": reg.get(r["id"], {}).get("offline"),
                "disabled": reg.get(r["id"], {}).get("disabled"),
            })
        snap = syslog.snapshot()
        return {"now": time.time(), "routers": routers,
                "syslog": {k: snap[k] for k in ("port", "listening", "error", "received", "parsed")}}

    @app.get("/api/pppoe/down", dependencies=[Depends(viewer)])
    def pppoe_down(router_id: int | None = None, q: str = "", limit: int = 500):
        rows = pppoe.down_sessions(router_id, q, max(1, min(limit, 2000)))
        names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM routers")}
        for r in rows:
            r["router"] = names.get(r["router_id"], "?")
        return {"now": time.time(), "total": len(rows), "sessions": rows}

    @app.get("/api/pppoe/registered", dependencies=[Depends(viewer)])
    def pppoe_registered(router_id: int | None = None, q: str = "", kind: str = "all", limit: int = 1000):
        """Usuarios registrados en el MikroTik (/ppp/secret) que no están conectados ahora."""
        if kind not in ("all", "offline", "disabled"):
            raise HTTPException(400, "kind debe ser all, offline o disabled")
        res = pppoe.registered(router_id, q, kind, max(1, min(limit, 3000)))
        names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM routers")}
        for r in res["rows"]:
            r["router"] = names.get(r["router_id"], "?")
        res["routers"] = [{"router_id": k, "router": names.get(k, "?"), **v} for k, v in res["routers"].items()]
        res["now"] = time.time()
        return res

    @app.get("/api/pppoe/repeat", dependencies=[Depends(viewer)])
    def pppoe_repeat(days: int = 7, min: int = 3, router_id: int | None = None, q: str = "",
                     limit: int = 200, mass: bool = False):
        """Clientes que se desconectan seguido. `mass=true` cuenta también las caídas masivas."""
        days = max(1, min_(days, 35))
        rows = pppoe.repeat_offenders(days, max(2, min), router_id, q, max(1, min_(limit, 1000)), not mass)
        names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM routers")}
        pons = {r["id"]: f"{r['olt']} · {r['pon']}" for r in db.query(
            "SELECT p.id, p.name AS pon, o.name AS olt FROM pons p JOIN olts o ON o.id = p.olt_id")}
        per_pon: dict[str, int] = {}
        for r in rows:
            r["router"] = names.get(r["router_id"], "?")
            loc = topology.locate(r["address"]) if r["address"] else None
            r["pon"] = pons.get(loc[0], "") if loc else ""
            if r["pon"]:
                per_pon[r["pon"]] = per_pon.get(r["pon"], 0) + 1
        # Varios clientes problemáticos en el mismo PON apuntan a la red (fibra/splitter), no a cada cliente.
        hot = sorted(({"pon": k, "clients": v} for k, v in per_pon.items() if v >= 3), key=lambda x: -x["clients"])
        return {"now": time.time(), "days": days, "min": max(2, min), "total": len(rows), "rows": rows, "hot_pons": hot}

    @app.get("/api/pppoe/search", dependencies=[Depends(viewer)])
    def pppoe_search(q: str = ""):
        rows = pppoe.search(q)
        names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM routers")}
        for r in rows:
            r["router"] = names.get(r["router_id"], "?")
        return {"now": time.time(), "sessions": rows}

    @app.get("/api/pppoe/events", dependencies=[Depends(viewer)])
    def pppoe_events(router_id: int | None = None, q: str = "", limit: int = 200):
        where, params = ["e.event = 'disconnect'"], []
        if router_id is not None:
            where.append("e.router_id = ?")
            params.append(router_id)
        if q.strip():
            where.append("e.user LIKE ?")
            params.append(f"%{q.strip()}%")
        rows = db.query(
            "SELECT e.ts, e.router_id, r.name AS router, e.user, e.reason FROM pppoe_events e "
            f"LEFT JOIN routers r ON r.id = e.router_id WHERE {' AND '.join(where)} "
            "ORDER BY e.ts DESC LIMIT ?",
            (*params, max(1, min(limit, 1000))),
        )
        return {"now": time.time(), "events": _with_reason(rows)}

    # ------------------------------------------------------------------ respaldos

    @app.get("/api/backups", dependencies=[Depends(admin)])
    def backups_list():
        return {**backup.listing(), "now": time.time()}

    @app.post("/api/backups/db", dependencies=[Depends(admin)])
    def backups_db_now():
        try:
            return backup.backup_db()
        except BackupError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @app.post("/api/backups/routers/{rid}", dependencies=[Depends(admin)])
    def backups_router_now(rid: int):
        router = db.one("SELECT * FROM routers WHERE id=?", (rid,))
        if not router:
            raise HTTPException(404, "Router no encontrado")
        try:
            return backup.backup_router(router, force=True)
        except BackupError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.put("/api/backups/routers/{rid}/config", dependencies=[Depends(admin)])
    def backups_router_config(rid: int, body: BackupRouterIn):
        if not db.one("SELECT id FROM routers WHERE id=?", (rid,)):
            raise HTTPException(404, "Router no encontrado")
        db.execute("UPDATE routers SET cfg_backup=?, ssh_port=? WHERE id=?", (int(body.cfg_backup), body.ssh_port, rid))
        return {"ok": True}

    @app.delete("/api/backups/routers/{rid}/hostkey", dependencies=[Depends(admin)])
    def backups_forget_key(rid: int):
        backup.forget_host_key(rid)
        return {"ok": True}

    @app.get("/api/backups/download", dependencies=[Depends(admin)])
    def backups_download(kind: str, name: str, router_id: int | None = None):
        path = backup.path_for(kind, name, router_id)
        if path is None:
            raise HTTPException(404, "Archivo no encontrado")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/backups/full", dependencies=[Depends(admin)])
    def backups_full():
        """Respaldo completo para migrar a otro servidor: base de MAGI + clave del túnel."""
        try:
            path = backup.make_bundle()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"No se pudo armar el respaldo: {type(exc).__name__}: {exc}") from None
        name = "magi-completo-" + time.strftime("%Y%m%d-%H%M%S", time.localtime()) + ".tar.gz"
        return FileResponse(path, filename=name, media_type="application/gzip",
                            headers={"Cache-Control": "no-store"}, background=BackgroundTask(path.unlink, missing_ok=True))

    @app.post("/api/backups/restore", dependencies=[Depends(admin)])
    async def backups_restore(request: Request, tasks: BackgroundTasks):
        """Reemplaza todo por el contenido de un respaldo subido. El cuerpo es el archivo tal cual."""
        if request.headers.get("x-magi-restore") != "1":
            raise HTTPException(400, "Falta la cabecera de confirmación")
        fd, name = tempfile.mkstemp(prefix="magi-upload-")
        tmp = Path(name)
        try:
            size = 0
            with os.fdopen(fd, "wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_RESTORE_BYTES:
                        raise HTTPException(413, "El archivo es demasiado grande")
                    fh.write(chunk)
            if size == 0:
                raise HTTPException(400, "No llegó ningún archivo")
            try:
                result = await run_in_threadpool(backup.restore_bundle, tmp)
            except BackupError as exc:
                raise HTTPException(400, str(exc)) from None
        finally:
            tmp.unlink(missing_ok=True)
        remote.write_files()          # rearma la lista de peers del túnel con la base restaurada
        tasks.add_task(lambda: threading.Timer(1.0, app.state.restart).start())
        return {**result, "restarting": True}

    # --------------------------------------------------- OLT / PON / mapa de red

    @app.get("/api/network", dependencies=[Depends(viewer)])
    def network():
        return topology.snapshot()

    @app.get("/api/olts", dependencies=[Depends(admin)])
    def olts_list():
        return topology.olts()

    @app.post("/api/olts", dependencies=[Depends(admin)])
    def olts_create(body: OltIn):
        try:
            return {"id": topology.save_olt(body.name, [p.model_dump() for p in body.pons],
                                                    cards=body.cards, ports_per_card=body.ports_per_card)}
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/routers/{rid}/detect-vlans", dependencies=[Depends(admin)])
    def detect_vlans(rid: int, body: DetectIn):
        """Lee las VLAN y sus IP del MikroTik y propone los PON (no guarda nada)."""
        router = db.one("SELECT * FROM routers WHERE id=?", (rid,))
        if not router:
            raise HTTPException(404, "Router no encontrado")
        client = client_factory(dict(router))
        try:
            client.connect()
            vlans, addrs = client.fetch_vlans()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"No se pudo leer el router: {type(exc).__name__}: {exc}") from None
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        found = detect_pons(vlans, addrs, body.vlan_from, body.vlan_to)
        found["vlans_total"] = len(vlans)
        return found

    @app.put("/api/olts/{oid}", dependencies=[Depends(admin)])
    def olts_update(oid: int, body: OltIn):
        try:
            return {"id": topology.save_olt(body.name, [p.model_dump() for p in body.pons], oid,
                                                    cards=body.cards, ports_per_card=body.ports_per_card)}
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "OLT no encontrada") from None

    @app.delete("/api/olts/{oid}", dependencies=[Depends(admin)])
    def olts_delete(oid: int):
        if not topology.delete_olt(oid):
            raise HTTPException(404, "OLT no encontrada")
        return {"deleted": oid}

    @app.get("/api/pppoe/syslog", dependencies=[Depends(admin)])
    def pppoe_syslog():
        return syslog.snapshot()

    # ------------------------------------------------------------ ajustes globales

    @app.get("/api/settings", dependencies=[Depends(admin)])
    def settings_get():
        return {"settings": db.get_settings(), "defaults": DEFAULT_SETTINGS}

    @app.put("/api/settings", dependencies=[Depends(admin)])
    def settings_put(body: dict[str, float]):
        try:
            return {"settings": db.set_settings(body)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    # ------------------------------------------- conexión de nuevos MikroTik

    @app.get("/api/connect-script", dependencies=[Depends(admin)])
    def connect_script(request: Request):
        netloc = request.headers.get("host", request.url.netloc)
        server_url = f"{request.url.scheme}://{netloc}"
        # La IP se usa para limitar la API del MikroTik; si el panel se abrió por
        # nombre de dominio, se deja vacía y el script no restringe nada.
        host = netloc.rsplit(":", 1)[0] if not netloc.startswith("[") else ""
        try:
            server_ip = str(ipaddress.IPv4Address(host))
        except ValueError:
            server_ip = ""
        text = SCRIPT_TEMPLATE.read_text(encoding="utf-8")
        text = (
            text.replace("__SERVER_URL__", server_url)
            .replace("__SERVER_IP__", server_ip)
            .replace("__TOKEN__", register_token)
            .replace("__SYSLOG_PORT__", str(cfg.syslog_port))
        )
        return PlainTextResponse(text)

    # ------------------------------------------------- routers remotos (WireGuard)

    @app.get("/api/remote/status", dependencies=[Depends(admin)])
    def remote_status():
        return remote.status()

    @app.put("/api/remote/endpoint", dependencies=[Depends(admin)])
    def remote_endpoint(body: EndpointIn):
        try:
            return {"endpoint": remote.set_endpoint(body.endpoint)}
        except RemoteError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/remote/routers", dependencies=[Depends(admin)])
    def remote_create(body: RemoteRouterIn):
        """Da de alta un router remoto: reparte su IP del túnel y genera todas las claves."""
        name = body.name.strip()
        if db.one("SELECT id FROM routers WHERE lower(name)=lower(?)", (name,)):
            raise HTTPException(400, f"Ya existe un router llamado «{name}»")
        if not remote.server_pub():
            raise HTTPException(409, "El servidor todavía no tiene WireGuard instalado (corré ./install.sh de nuevo)")
        if not remote.endpoint():
            raise HTTPException(409, "Primero cargá la IP pública o el dominio del servidor")
        try:
            ip = remote._allocate_ip()
        except RemoteError as exc:
            raise HTTPException(409, str(exc)) from None
        password = secrets.token_urlsafe(18)
        # Queda pausado hasta que el túnel conecte por primera vez (así no genera alertas de «sin respuesta»).
        cur = db.execute(
            "INSERT INTO routers(name, host, port, username, password, use_ssl, enabled, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (name, ip, 8728, "mkmonitor", password, 0, 0, time.time()))
        rid = cur.lastrowid
        remote.create_peer(rid)
        log.info("Router remoto creado: %s (túnel %s)", name, ip)
        return {"id": rid, "ip": ip}

    @app.get("/api/remote/routers/{rid}/script", dependencies=[Depends(admin)])
    def remote_script(rid: int):
        router = db.one("SELECT * FROM routers WHERE id=?", (rid,))
        peer = remote.peer(rid)
        if not router or not peer:
            raise HTTPException(404, "Ese router no es un router remoto")
        try:
            return PlainTextResponse(remote.render_script(router, peer, cfg.syslog_port))
        except RemoteError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/register")
    def register(
        body: RegisterIn, request: Request, x_register_token: str = Header(default="")
    ):
        if not secrets.compare_digest(x_register_token.encode(), register_token.encode()):
            raise HTTPException(403, "Token de registro inválido")
        host = (body.host or (request.client.host if request.client else "")).strip()
        if not host:
            raise HTTPException(400, "No se pudo determinar la IP del router")
        now = time.time()
        db.execute(
            "INSERT INTO routers(name, host, port, username, password, use_ssl, updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(host, port) DO UPDATE SET name=excluded.name, "
            "username=excluded.username, password=excluded.password, use_ssl=excluded.use_ssl, "
            "enabled=1, updated_at=excluded.updated_at",
            (body.name.strip(), host, body.port, body.username.strip(), body.password,
             int(body.use_ssl), now),
        )
        row = db.one("SELECT id FROM routers WHERE host=? AND port=?", (host, body.port))
        _sync()
        log.info("MikroTik registrado: %s (%s)", body.name, host)
        return JSONResponse({"ok": True, "id": row["id"], "host": host})

    return app
