import time

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from conftest import Sim

AUTH = ("admin", "pw-admin")
TOKEN = "tok-123"


def make_cfg(**over):
    base = dict(host="127.0.0.1", port=8080, db_path=":memory:", admin_user="admin",
                admin_password="pw-admin", register_token=TOKEN, public_view=False, demo=False,
                telegram_token="", telegram_chat="")
    base.update(over)
    return Config(**base)


class FakeClient:
    """Reemplaza al cliente de RouterOS en las pruebas de la API."""

    fail = False
    seen: list[dict] = []

    def __init__(self, router):
        self.router = router
        FakeClient.seen.append(router)

    def connect(self):
        if FakeClient.fail:
            raise ConnectionRefusedError("conexión rechazada")

    def fetch_interfaces(self):
        return [{"name": "ether1", "type": "ether", "running": True, "disabled": False, "comment": "WAN"}]

    def close(self):
        pass


@pytest.fixture
def app():
    FakeClient.fail = False
    FakeClient.seen = []
    return create_app(make_cfg(), start_poller=False, client_factory=FakeClient)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.auth = AUTH
        yield c


def add_router(client, **over):
    body = {"name": "Core", "host": "10.0.0.1", "port": 8728, "username": "mkmonitor", "password": "s3"}
    body.update(over)
    r = client.post("/api/routers", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ----------------------------------------------------------------------- acceso


def test_health_is_open_and_everything_else_requires_login(app):
    with TestClient(app) as c:
        assert c.get("/api/health").json()["ok"] is True
        for path in ("/", "/api/state", "/api/alerts", "/api/routers", "/api/settings", "/app.js"):
            r = c.get(path)
            assert r.status_code == 401, path
            assert "Basic" in r.headers["www-authenticate"]
        assert c.get("/api/state", auth=("admin", "mala")).status_code == 401
        assert c.get("/api/state", auth=AUTH).status_code == 200


def test_static_pages_are_served(client):
    for path, marker in (("/", "MK Monitor"), ("/app.js", "refresh"), ("/style.css", "--bg")):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text, path


def test_public_view_allows_reading_but_not_writing():
    app = create_app(make_cfg(public_view=True), start_poller=False, client_factory=FakeClient)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 200
        assert c.get("/").status_code == 200
        assert c.post("/api/alerts/ack_all").status_code == 401
        assert c.get("/api/routers").status_code == 401
        assert c.get("/api/connect-script").status_code == 401


def test_generated_credentials_are_persisted_when_not_configured():
    cfg = make_cfg(admin_password="", register_token="")
    app = create_app(cfg, start_poller=False, client_factory=FakeClient)
    db = app.state.db
    pw = db.one("SELECT value FROM settings WHERE key='admin_password'")["value"]
    tok = db.one("SELECT value FROM settings WHERE key='register_token'")["value"]
    assert len(pw) >= 12 and len(tok) >= 12
    with TestClient(app) as c:
        assert c.get("/api/state", auth=("admin", pw)).status_code == 200


# ---------------------------------------------------------------------- routers


def test_router_crud_and_password_is_never_returned(client):
    rid = add_router(client)
    lst = client.get("/api/routers").json()
    assert lst[0]["name"] == "Core" and "password" not in lst[0]
    assert "s3" not in client.get("/api/state").text

    dup = client.post("/api/routers", json={"name": "X", "host": "10.0.0.1", "port": 8728,
                                            "username": "u", "password": "p"})
    assert dup.status_code == 409

    r = client.put(f"/api/routers/{rid}", json={"name": "Core2", "host": "10.0.0.1", "port": 8728,
                                                "username": "mkmonitor", "password": ""})
    assert r.status_code == 200
    db = client.app.state.db
    row = db.one("SELECT name, password FROM routers WHERE id=?", (rid,))
    assert row == {"name": "Core2", "password": "s3"}   # contraseña vacía = se conserva

    client.put(f"/api/routers/{rid}", json={"name": "Core2", "host": "10.0.0.1", "port": 8728,
                                            "username": "mkmonitor", "password": "nueva"})
    assert db.one("SELECT password FROM routers WHERE id=?", (rid,))["password"] == "nueva"

    assert client.delete(f"/api/routers/{rid}").status_code == 200
    assert client.get("/api/routers").json() == []
    assert client.delete(f"/api/routers/{rid}").status_code == 404


def test_router_validation(client):
    bad = client.post("/api/routers", json={"name": "", "host": "x", "username": "u", "password": ""})
    assert bad.status_code == 422
    bad = client.post("/api/routers", json={"name": "a", "host": "x", "port": 70000, "username": "u"})
    assert bad.status_code == 422


def test_deleting_router_resolves_its_alerts(client, app):
    rid = add_router(client)
    engine = app.state.engine
    router = {"id": rid, "name": "Core", "host": "10.0.0.1"}
    for n in range(3):
        engine.process_failure(router, "timeout", now=100 + n)
    assert len(client.get("/api/state").json()["alerts"]) == 1
    client.delete(f"/api/routers/{rid}")
    assert client.get("/api/state").json()["alerts"] == []


def test_router_test_endpoint_ok_and_error(client):
    r = client.post("/api/routers/test", json={"name": "x", "host": "10.0.0.9", "port": 8728,
                                               "username": "u", "password": "p"})
    assert r.json() == {"ok": True, "interfaces": [{"name": "ether1", "type": "ether", "running": True,
                                                    "comment": "WAN"}]}
    FakeClient.fail = True
    r = client.post("/api/routers/test", json={"name": "x", "host": "10.0.0.9", "port": 8728,
                                               "username": "u", "password": "p"})
    assert r.json()["ok"] is False and "rechazada" in r.json()["error"]


def test_router_test_uses_stored_password_when_blank(client):
    rid = add_router(client, password="guardada")
    client.post("/api/routers/test", json={"name": "Core", "host": "10.0.0.1", "port": 8728,
                                           "username": "mkmonitor", "password": "", "router_id": rid})
    assert FakeClient.seen[-1]["password"] == "guardada"


# --------------------------------------------------------------------- registro


def test_register_requires_valid_token(client):
    body = {"name": "Torre", "host": "10.0.0.2", "username": "mkmonitor", "password": "abc"}
    assert client.post("/api/register", json=body).status_code == 403
    assert client.post("/api/register", json=body, headers={"X-Register-Token": "mal"}).status_code == 403
    r = client.post("/api/register", json=body, headers={"X-Register-Token": TOKEN})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/routers").json()[0]["host"] == "10.0.0.2"


def test_register_is_idempotent_and_updates_credentials(client):
    h = {"X-Register-Token": TOKEN}
    base = {"host": "10.0.0.2", "username": "mkmonitor"}
    a = client.post("/api/register", json={**base, "name": "Torre", "password": "uno"}, headers=h).json()
    b = client.post("/api/register", json={**base, "name": "Torre Norte", "password": "dos"}, headers=h).json()
    assert a["id"] == b["id"]
    routers = client.get("/api/routers").json()
    assert len(routers) == 1 and routers[0]["name"] == "Torre Norte"
    db = client.app.state.db
    assert db.one("SELECT password FROM routers")["password"] == "dos"


def test_register_without_host_uses_caller_address(client):
    r = client.post("/api/register", json={"name": "Auto", "username": "u", "password": "p"},
                    headers={"X-Register-Token": TOKEN})
    assert r.status_code == 200 and r.json()["host"]   # IP desde la que llegó la solicitud


def test_register_reenables_a_paused_router(client):
    h = {"X-Register-Token": TOKEN}
    body = {"name": "T", "host": "10.0.0.2", "username": "u", "password": "p"}
    rid = client.post("/api/register", json=body, headers=h).json()["id"]
    client.app.state.db.execute("UPDATE routers SET enabled=0 WHERE id=?", (rid,))
    client.post("/api/register", json=body, headers=h)
    assert client.get("/api/routers").json()[0]["enabled"] == 1


def test_connect_script_is_filled_in(client):
    r = client.get("/api/connect-script", headers={"Host": "192.168.50.10:8080"})
    assert r.status_code == 200
    text = r.text
    assert 'serverUrl "http://192.168.50.10:8080"' in text
    assert 'serverIp "192.168.50.10"' in text
    assert f'token "{TOKEN}"' in text
    assert "__" not in text
    assert text.count("{") == text.count("}")   # el bloque RouterOS se pega completo
    r = client.get("/api/connect-script", headers={"Host": "monitor.local:8080"})
    assert 'serverIp ""' in r.text and 'serverUrl "http://monitor.local:8080"' in r.text


# ----------------------------------------------------------- estado y alertas


def feed(app, router_id, steps=4, running=True):
    sim = Sim(app.state.engine, {"id": router_id, "name": "Core", "host": "10.0.0.1"})
    for _ in range(steps):
        sim.step(10, rx_mbps=80, tx_mbps=20, running=running)
    return sim


def test_state_contains_live_interfaces_and_info(client, app):
    rid = add_router(client)
    assert client.get("/api/state").json()["routers"][0]["reachable"] is None   # aún sin lectura
    feed(app, rid)
    data = client.get("/api/state").json()
    r = data["routers"][0]
    assert r["reachable"] is True and r["name"] == "Core"
    i = r["ifaces"][0]
    assert i["name"] == "ether1" and 79e6 < i["rx_bps"] < 81e6 and 19e6 < i["tx_bps"] < 21e6
    assert data["info"] == {"version": "1.0.0", "demo": False, "telegram": False, "public_view": False}


def test_interfaces_are_sorted_naturally(client, app):
    rid = add_router(client)
    rows = [{"name": n, "type": "ether", "running": True, "disabled": False, "rx-byte": 0, "tx-byte": 0}
            for n in ("ether10", "ether2", "ether1", "sfp-sfpplus1")]
    app.state.engine.process_poll({"id": rid, "name": "Core", "host": "h"}, rows, now=time.time())
    names = [i["name"] for i in client.get("/api/state").json()["routers"][0]["ifaces"]]
    assert names == ["ether1", "ether2", "ether10", "sfp-sfpplus1"]


def test_link_down_alert_ack_and_history(client, app):
    rid = add_router(client)
    sim = feed(app, rid)
    sim.step(10, running=False)
    alerts = client.get("/api/state").json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["kind"] == "link_down" and alerts[0]["severity"] == "critical"
    assert alerts[0]["router"] == "Core" and alerts[0]["acked_at"] is None

    assert client.post(f"/api/alerts/{alerts[0]['id']}/ack").json() == {"acked": 1}
    assert client.post(f"/api/alerts/{alerts[0]['id']}/ack").json() == {"acked": 0}
    assert client.get("/api/state").json()["alerts"][0]["acked_at"] is not None

    sim.step(10, rx_mbps=80)       # vuelve el enlace
    assert client.get("/api/state").json()["alerts"] == []
    hist = client.get("/api/alerts").json()
    assert len(hist) == 1 and hist[0]["resolved_at"] is not None
    assert client.get("/api/alerts?state=active").json() == []


def test_ack_all(client, app):
    rid = add_router(client)
    feed(app, rid)
    engine = app.state.engine
    router = {"id": rid, "name": "Core", "host": "h"}
    for n in range(3):
        engine.process_failure(router, "x", now=time.time() + n)
    sim = Sim(engine, router)
    sim.step(10, rx_mbps=1)
    for n in range(3):
        engine.process_failure(router, "x", now=sim.t + n + 1)
    assert client.post("/api/alerts/ack_all").json()["acked"] >= 1
    assert all(a["acked_at"] for a in client.get("/api/state").json()["alerts"])


def test_history_endpoint_downsamples(client, app):
    rid = add_router(client)
    db = app.state.db
    now = time.time()
    db.executemany("INSERT INTO samples(router_id, iface, ts, rx_bps, tx_bps) VALUES(?,?,?,?,?)",
                   [(rid, "ether1", now - 3600 + i * 30, 1e6 * (i % 10), 5e5) for i in range(120)])
    h = client.get(f"/api/history?router_id={rid}&iface=ether1&minutes=60").json()
    assert 0 < len(h["points"]) <= 320
    t, rx, tx, rx_max, tx_max = h["points"][0]
    assert rx_max >= rx and tx == pytest.approx(5e5)
    empty = client.get(f"/api/history?router_id={rid}&iface=nada&minutes=60").json()
    assert empty["points"] == []


# ------------------------------------------------------- config de interfaces


def test_interface_config_roundtrip_affects_alerts(client, app):
    rid = add_router(client)
    feed(app, rid)
    lst = client.get(f"/api/routers/{rid}/interfaces").json()
    assert lst[0]["name"] == "ether1" and lst[0]["label"] == "Uplink" and lst[0]["monitor"] is True

    r = client.put(f"/api/routers/{rid}/interfaces/ether1",
                   json={"label": "WAN principal", "max_rx_mbps": 50, "link_alert": False})
    assert r.status_code == 200
    row = client.get(f"/api/routers/{rid}/interfaces").json()[0]
    assert row["label"] == "WAN principal" and row["max_rx_mbps"] == 50 and row["link_alert"] is False
    assert row["max_tx_mbps"] is None and row["monitor"] is True     # lo no enviado no cambia

    client.put(f"/api/routers/{rid}/interfaces/ether1", json={"max_rx_mbps": None})
    assert client.get(f"/api/routers/{rid}/interfaces").json()[0]["max_rx_mbps"] is None

    assert client.put(f"/api/routers/{rid}/interfaces/nada", json={"label": "x"}).status_code == 404
    assert client.put(f"/api/routers/{rid}/interfaces/ether1", json={"max_rx_mbps": -5}).status_code == 422


def test_interface_name_with_slash_is_supported(client, app):
    rid = add_router(client)
    row = {"name": "sfp1/1", "type": "ether", "running": True, "disabled": False, "rx-byte": 0, "tx-byte": 0}
    app.state.engine.process_poll({"id": rid, "name": "Core", "host": "h"}, [row], now=time.time())
    r = client.put(f"/api/routers/{rid}/interfaces/sfp1/1", json={"label": "ok"})
    assert r.status_code == 200


# ---------------------------------------------------------------------- ajustes


def test_settings_get_put_and_validation(client):
    s = client.get("/api/settings").json()
    assert s["settings"]["poll_interval"] == 10 and "anomaly_factor" in s["defaults"]
    r = client.put("/api/settings", json={"default_max_rx_mbps": 800, "anomaly_factor": 2.5})
    assert r.status_code == 200 and r.json()["settings"]["default_max_rx_mbps"] == 800
    assert client.put("/api/settings", json={"inventado": 1}).status_code == 400
    assert client.put("/api/settings", json={"poll_interval": 1}).status_code == 400
    assert client.put("/api/settings", json={"default_max_rx_mbps": -1}).status_code == 400
    assert client.put("/api/settings", json={"anomaly_factor": 1}).status_code == 400
    assert client.get("/api/settings").json()["settings"]["default_max_rx_mbps"] == 800


def test_html_in_labels_is_not_executed_by_the_ui_contract(client, app):
    """La API devuelve texto crudo; el panel lo escapa con esc() al dibujarlo."""
    rid = add_router(client)
    row = {"name": "ether1", "type": "ether", "running": True, "disabled": False, "rx-byte": 1, "tx-byte": 1,
           "comment": "<img src=x onerror=alert(1)>"}
    app.state.engine.process_poll({"id": rid, "name": "Core", "host": "h"}, [row], now=time.time())
    js = client.get("/app.js").text
    assert "esc(i.label)" in js and "esc(a.message)" in js and "esc(r.name)" in js
