"""API web y servidor del panel."""

import ipaddress
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import __version__
from .config import Config, load
from .db import DB, DEFAULT_SETTINGS
from .demo import DemoClient, apply_demo
from .engine import Engine
from .notify import Notifier
from .poller import Poller, RouterClient
from .pppoe import PPPoEMonitor, explain_reason
from .syslog import SyslogReceiver

log = logging.getLogger("mkmonitor")

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
SCRIPT_TEMPLATE = BASE.parent / "routeros" / "conectar-mikrotik.rsc"

ROUTER_PUBLIC_FIELDS = (
    "id, name, host, port, username, use_ssl, types, include, enabled, pppoe_enabled, syslog_ip"
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
    syslog_ip: str = Field("", max_length=45)


class RouterTest(RouterIn):
    router_id: int | None = None


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    host: str | None = Field(None, max_length=255)
    port: int = Field(8728, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=128)
    use_ssl: bool = False


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


def create_app(
    cfg: Config | None = None, *, start_poller: bool = True, client_factory=None
) -> FastAPI:
    cfg = cfg or load()
    db = DB(cfg.db_path)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat)
    engine = Engine(db, notifier)
    pppoe = PPPoEMonitor(db, engine)
    syslog = SyslogReceiver(cfg.syslog_port, pppoe.on_syslog)
    if client_factory is None:
        client_factory = DemoClient if cfg.demo else RouterClient
    poller = Poller(db, engine, client_factory, pppoe)
    if cfg.demo:
        apply_demo(db)

    admin_password = _persistent_secret(db, "admin_password", cfg.admin_password, "Contraseña de admin")
    register_token = _persistent_secret(db, "register_token", cfg.register_token, "Token de registro")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_poller:
            poller.start()
            syslog.start()
        yield
        poller.stop()
        syslog.stop()

    app = FastAPI(title="MK Monitor", version=__version__, lifespan=lifespan)
    app.state.db, app.state.engine, app.state.poller = db, engine, poller
    app.state.pppoe, app.state.syslog = pppoe, syslog
    app.state.cfg, app.state.notifier = cfg, notifier

    basic = HTTPBasic(auto_error=False)

    def _is_admin(creds: HTTPBasicCredentials | None) -> bool:
        if creds is None:
            return False
        user_ok = secrets.compare_digest(creds.username.encode(), cfg.admin_user.encode())
        pass_ok = secrets.compare_digest(creds.password.encode(), admin_password.encode())
        return user_ok and pass_ok

    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=401,
            detail="Credenciales requeridas",
            headers={"WWW-Authenticate": 'Basic realm="MK Monitor"'},
        )

    def admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not _is_admin(creds):
            raise _unauthorized()

    def viewer(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not (cfg.public_view or _is_admin(creds)):
            raise _unauthorized()

    def _sync() -> None:
        if start_poller:
            poller.sync()

    # ------------------------------------------------------------------ páginas

    def _page(name: str, media: str) -> FileResponse:
        return FileResponse(STATIC / name, media_type=media, headers={"Cache-Control": "no-cache"})

    @app.get("/", dependencies=[Depends(viewer)], include_in_schema=False)
    def index():
        return _page("index.html", "text/html")

    @app.get("/app.js", dependencies=[Depends(viewer)], include_in_schema=False)
    def app_js():
        return _page("app.js", "application/javascript")

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
            "SELECT a.id, a.router_id, r.name AS router, a.iface, a.kind, a.severity, a.message, "
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
            "SELECT a.id, a.router_id, r.name AS router, a.iface, a.kind, a.severity, a.message, "
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
        }

    @app.post("/api/routers", dependencies=[Depends(admin)])
    def router_add(body: RouterIn):
        v = _router_values(body)
        if db.one("SELECT id FROM routers WHERE host=? AND port=?", (v["host"], v["port"])):
            raise HTTPException(409, "Ya existe un router con esa IP y puerto")
        cur = db.execute(
            "INSERT INTO routers(name, host, port, username, password, use_ssl, types, include, "
            "enabled, updated_at, pppoe_enabled, syslog_ip) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["name"], v["host"], v["port"], v["username"], body.password, v["use_ssl"],
             v["types"], v["include"], v["enabled"], time.time(), v["pppoe_enabled"],
             v["syslog_ip"]),
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
            "include=?, enabled=?, updated_at=?, pppoe_enabled=?, syslog_ip=? WHERE id=?",
            (v["name"], v["host"], v["port"], v["username"], password, v["use_ssl"], v["types"],
             v["include"], v["enabled"], time.time(), v["pppoe_enabled"], v["syslog_ip"], rid),
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
        routers = []
        for r in db.query("SELECT id, name, enabled, pppoe_enabled FROM routers ORDER BY name"):
            st = stats.get(r["id"]) or {}
            routers.append({
                "id": r["id"], "name": r["name"],
                "monitored": bool(r["enabled"] and r["pppoe_enabled"]),
                "online": st.get("online"), "down": st.get("down"),
                "suppressed": st.get("suppressed"), "mass": st.get("mass", False),
                "last_poll": st.get("last_poll"), "error": st.get("error"),
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
