import gzip
import socket
import sqlite3
import threading
import time

import paramiko
import pytest
from fastapi.testclient import TestClient

from app.backup import BackupError, BackupManager, digest, normalize_export, ssh_export
from app.db import DB
from test_login import login, new_app

EXPORT = "# 2026-10-28 17:00:00 by RouterOS 7.15\n# model = RB4011\n/ip address\nadd address=10.0.0.1/24 interface=ether1\n"


def add_router(db, name="Core", host="10.0.0.1", cfg=1, enabled=1):
    return db.execute(
        "INSERT INTO routers(name, host, port, username, password, enabled, cfg_backup, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)", (name, host, 8728, "mk", "pw", enabled, cfg, time.time())).lastrowid


@pytest.fixture
def db(tmp_path):
    return DB(str(tmp_path / "m.db"))


def make(db, tmp_path, exporter=None, clock=time.time):
    return BackupManager(db, tmp_path / "backups", exporter=exporter or (lambda r: EXPORT), clock=clock)


# ----------------------------------------------------------------- utilidades


def test_normalize_ignores_the_timestamp_line_only():
    a = "# 2026-10-28 17:00:00 by RouterOS 7.15\n/ip address\nadd a=1\n"
    b = "# 2026-10-29 03:00:00 by RouterOS 7.15\n/ip address\nadd a=1\n"
    c = "# 2026-10-29 03:00:00 by RouterOS 7.15\n/ip address\nadd a=2\n"
    assert digest(a) == digest(b) != digest(c)
    assert normalize_export("/ip address\r\nadd a=1  \r\n") == "/ip address\nadd a=1\n"


# ----------------------------------------------------------------- base de MAGI


def test_db_backup_is_a_consistent_gzip_copy_with_private_permissions(db, tmp_path):
    db.execute("INSERT INTO settings(key, value) VALUES('marca', 'hola')")
    m = make(db, tmp_path)
    info = m.backup_db()
    p = tmp_path / "backups" / "db" / info["name"]
    assert p.name.startswith("magi-") and p.name.endswith(".db.gz") and (p.stat().st_mode & 0o777) == 0o600
    raw = tmp_path / "restored.db"
    raw.write_bytes(gzip.decompress(p.read_bytes()))
    got = sqlite3.connect(raw).execute("SELECT value FROM settings WHERE key='marca'").fetchone()
    assert got == ("hola",)
    assert not list((tmp_path / "backups" / "db").glob(".tmp-*"))


def test_db_backups_are_pruned_to_the_configured_amount(db, tmp_path):
    db.set_settings({"backup_keep": 3})
    t = [1_800_000_000.0]
    m = make(db, tmp_path, clock=lambda: t[0])
    for _ in range(6):
        t[0] += 3600
        m.backup_db()
    assert len(list((tmp_path / "backups" / "db").glob("magi-*.db.gz"))) == 3


def test_disabled_without_a_folder(db):
    m = BackupManager(db, None)
    assert m.enabled is False and m.listing()["enabled"] is False
    with pytest.raises(BackupError):
        m.backup_db()
    m.tick()   # no hace nada


# -------------------------------------------------------------- config MikroTik


def test_router_backup_only_stores_when_config_changed(db, tmp_path):
    rid = add_router(db)
    router = db.one("SELECT * FROM routers WHERE id=?", (rid,))
    cfg = {"text": EXPORT}
    t = [1_800_000_000.0]
    m = make(db, tmp_path, exporter=lambda r: cfg["text"], clock=lambda: t[0])
    first = m.backup_router(router)
    assert first["changed"] is True
    t[0] += 86400
    cfg["text"] = EXPORT.replace("2026-10-28 17:00:00", "2026-10-29 17:00:00")     # solo cambió la fecha
    assert m.backup_router(router)["changed"] is False
    t[0] += 86400
    cfg["text"] = EXPORT + "add address=10.0.1.1/24 interface=ether2\n"            # cambio real
    assert m.backup_router(router)["changed"] is True
    assert m.backup_router(router, force=True)["changed"] is True                   # «respaldar ahora» siempre guarda
    files = m.listing()["routers"][0]["files"]
    assert len(files) >= 2 and all(f["name"].startswith("config-") for f in files)


def test_router_backup_error_is_remembered_and_cleared(db, tmp_path):
    rid = add_router(db)
    router = db.one("SELECT * FROM routers WHERE id=?", (rid,))
    ok = {"v": False}

    def exporter(r):
        if not ok["v"]:
            raise BackupError("SSH apagado")
        return EXPORT
    m = make(db, tmp_path, exporter=exporter)
    with pytest.raises(BackupError):
        m.backup_router(router)
    assert m.listing()["routers"][0]["error"] == "SSH apagado"
    ok["v"] = True
    m.backup_router(router)
    assert m.listing()["routers"][0]["error"] == ""


def test_unexpected_exporter_errors_are_wrapped(db, tmp_path):
    router = db.one("SELECT * FROM routers WHERE id=?", (add_router(db),))
    m = make(db, tmp_path, exporter=lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(BackupError, match="boom"):
        m.backup_router(router)


# --------------------------------------------------------------------- programa


def test_tick_runs_due_jobs_once_and_respects_settings(db, tmp_path):
    add_router(db, "A", "10.0.0.1", cfg=1)
    add_router(db, "B", "10.0.0.2", cfg=0)                  # sin respaldo de config
    add_router(db, "C", "10.0.0.3", cfg=1, enabled=0)       # pausado
    calls = []
    m = make(db, tmp_path, exporter=lambda r: calls.append(r["name"]) or EXPORT)
    m.tick(); m.wait()
    assert calls == ["A"] and len(m.listing()["db"]) == 1
    m.tick(); m.wait()                                       # todavía no toca
    assert calls == ["A"] and len(m.listing()["db"]) == 1
    # pasado el intervalo, vuelve a respaldar la base y a revisar el router
    m.tick(now=time.time() + 25 * 3600); m.wait()
    assert calls == ["A", "A"]


def test_tick_does_nothing_when_disabled(db, tmp_path):
    db.set_settings({"backup_enabled": 0})
    m = make(db, tmp_path)
    m.tick(); m.wait()
    assert m.listing()["db"] == []


def test_failed_db_backup_is_not_retried_every_tick(db, tmp_path):
    m = make(db, tmp_path)
    m.backup_db = lambda: (_ for _ in ()).throw(OSError("disco lleno"))
    m.tick(); m.wait()
    assert "disco lleno" in m.listing()["db_error"]
    n = []
    m.backup_db = lambda: n.append(1)
    m.tick(); m.wait()
    assert n == []                                           # espera antes de reintentar


# ----------------------------------------------------------------- descargas


def test_path_for_blocks_traversal_and_unknown_files(db, tmp_path):
    rid = add_router(db)
    m = make(db, tmp_path)
    info = m.backup_db()
    m.backup_router(db.one("SELECT * FROM routers WHERE id=?", (rid,)))
    name = m.listing()["routers"][0]["files"][0]["name"]
    assert m.path_for("db", info["name"]).is_file()
    assert m.path_for("router", name, rid).is_file()
    for bad in ("../x", "..", "a/b", "", ".hidden/../x", "x" * 200):
        assert m.path_for("db", bad) is None
        assert m.path_for("router", bad, rid) is None
    assert m.path_for("db", "no-existe.db.gz") is None
    assert m.path_for("router", name, rid + 99) is None
    assert m.path_for("otro", name, rid) is None


# ------------------------------------------------------------------------ API


def test_backup_api_requires_admin_and_downloads(tmp_path):
    app = new_app(db_path=str(tmp_path / "m.db"))
    app.state.backup.exporter = lambda r: EXPORT
    with TestClient(app) as c:
        assert c.get("/api/backups").status_code == 401
        assert c.post("/api/backups/db").status_code == 401
        login(c)
        assert c.get("/api/backups").json()["enabled"] is True
        made = c.post("/api/backups/db").json()
        listing = c.get("/api/backups").json()
        assert [f["name"] for f in listing["db"]] == [made["name"]]
        r = c.get("/api/backups/download", params={"kind": "db", "name": made["name"]})
        assert r.status_code == 200 and gzip.decompress(r.content).startswith(b"SQLite format 3")
        assert c.get("/api/backups/download", params={"kind": "db", "name": "../m.db"}).status_code == 404
        # usuario de solo lectura: ni listar ni bajar
        c.post("/api/users", json={"username": "tv", "password": "clave-tv-1234", "role": "viewer"})
        with TestClient(app) as v:
            login(v, "tv", "clave-tv-1234")
            assert v.get("/api/backups").status_code == 403
            assert v.get("/api/backups/download", params={"kind": "db", "name": made["name"]}).status_code == 403
            assert v.post("/api/backups/db").status_code == 403
        # router con respaldo de config
        rid = c.post("/api/routers", json={"name": "R1", "host": "10.9.9.9", "username": "mk", "password": "p",
                                           "cfg_backup": True, "ssh_port": 2222}).json()["id"]
        row = [r for r in c.get("/api/routers").json() if r["id"] == rid][0]
        assert row["cfg_backup"] == 1 and row["ssh_port"] == 2222
        assert c.post(f"/api/backups/routers/{rid}").json()["changed"] is True
        assert c.put(f"/api/backups/routers/{rid}/config", json={"cfg_backup": False, "ssh_port": 2200}).status_code == 200
        row = [r for r in c.get("/api/routers").json() if r["id"] == rid][0]
        assert row["cfg_backup"] == 0 and row["ssh_port"] == 2200
        assert c.put("/api/backups/routers/999/config", json={"cfg_backup": True}).status_code == 404
        assert c.post("/api/backups/routers/999").status_code == 404
        app.state.backup.exporter = lambda r: (_ for _ in ()).throw(BackupError("sin SSH"))
        e = c.post(f"/api/backups/routers/{rid}")
        assert e.status_code == 502 and "sin SSH" in e.json()["detail"]
        fname = c.get("/api/backups").json()["routers"][0]["files"][0]["name"]
        r = c.get("/api/backups/download", params={"kind": "router", "name": fname, "router_id": rid})
        assert r.status_code == 200 and b"/ip address" in r.content
        assert c.delete(f"/api/backups/routers/{rid}/hostkey").status_code == 200


def test_backups_unavailable_in_memory_mode():
    app = new_app()                      # base en memoria (como el modo demo)
    with TestClient(app) as c:
        login(c)
        assert c.get("/api/backups").json()["enabled"] is False
        assert c.post("/api/backups/db").status_code == 400


# ------------------------------------------------------------------- SSH real


class _Server(paramiko.ServerInterface):
    def __init__(self, output, user="mk", password="pw"):
        self.output, self.user, self.password = output, user, password
        self.commands: list[str] = []
        self.got = threading.Event()

    def check_auth_password(self, u, p):
        return paramiko.AUTH_SUCCESSFUL if (u, p) == (self.user, self.password) else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        self.commands.append(command.decode())
        self.got.set()
        return True


def start_ssh(output, host_key=None, **kw):
    key = host_key or paramiko.ECDSAKey.generate()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    srv = _Server(output, **kw)

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(key)
            try:
                t.start_server(server=srv)
                chan = t.accept(5)
                if chan is not None and srv.got.wait(5):
                    chan.sendall(srv.output.encode())
                    chan.send_exit_status(0)
                    chan.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                time.sleep(0.2)
                t.close()
    threading.Thread(target=serve, daemon=True).start()
    return sock, srv, sock.getsockname()[1]


def test_ssh_export_runs_export_terse_and_pins_the_host_key(db):
    rid = add_router(db, host="127.0.0.1")
    sock, srv, port = start_ssh(EXPORT)
    router = {**db.one("SELECT * FROM routers WHERE id=?", (rid,)), "ssh_port": port}
    try:
        assert ssh_export(db, router, timeout=5) == EXPORT
        assert srv.commands == ["/export terse"]
        assert db.one("SELECT value FROM settings WHERE key=?", (f"sshkey:{rid}",))
        srv.got.clear()
        assert ssh_export(db, router, timeout=5) == EXPORT           # misma clave: ok
    finally:
        sock.close()
    # otro equipo (otra clave) en la misma dirección: se rechaza
    sock2, _, port2 = start_ssh(EXPORT)
    try:
        with pytest.raises(BackupError, match="cambió"):
            ssh_export(db, {**router, "ssh_port": port2}, timeout=5)
    finally:
        sock2.close()


def test_ssh_export_reports_bad_credentials_and_closed_port(db):
    rid = add_router(db, host="127.0.0.1")
    sock, srv, port = start_ssh(EXPORT, password="otra")
    router = {**db.one("SELECT * FROM routers WHERE id=?", (rid,)), "ssh_port": port}
    try:
        with pytest.raises(BackupError, match="ssh"):
            ssh_export(db, router, timeout=5)
    finally:
        sock.close()
    free = socket.socket(); free.bind(("127.0.0.1", 0)); closed = free.getsockname()[1]; free.close()
    with pytest.raises(BackupError, match="No se pudo conectar"):
        ssh_export(db, {**router, "ssh_port": closed}, timeout=3)
