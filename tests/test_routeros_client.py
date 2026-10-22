import time

import pytest
from librouteros.exceptions import TrapError

from app.db import DB
from app.engine import Engine
from app.poller import Poller, RouterClient
from fake_routeros import FakeRouterOS, default_interfaces


@pytest.fixture
def server():
    s = FakeRouterOS()
    yield s
    s.stop()


def router_for(server, **over):
    base = {"id": 1, "name": "Fake", "host": "127.0.0.1", "port": server.port, "username": "mkmonitor",
            "password": "secret", "use_ssl": 0, "types": "ether", "include": "", "updated_at": 1.0}
    base.update(over)
    return base


def test_reads_only_physical_interfaces_and_parses_types(server):
    client = RouterClient(router_for(server))
    client.connect()
    rows = client.fetch_interfaces()
    client.close()
    assert sorted(r["name"] for r in rows) == ["ether1", "ether2", "sfp-sfpplus1"]
    e1 = next(r for r in rows if r["name"] == "ether1")
    assert e1["running"] is True and e1["disabled"] is False
    assert isinstance(e1["rx-byte"], int)
    assert e1["comment"] == "Upstream A"


def test_filter_is_done_by_the_router_not_in_python(server):
    client = RouterClient(router_for(server))
    client.connect()
    client.fetch_interfaces()
    client.close()
    req = next(r for r in server.requests if r[0] == "/interface/print")
    assert "?=type=ether" in req
    assert any(w.startswith("=.proplist=") and "rx-byte" in w and "tx-byte" in w for w in req)


def test_include_interfaces_by_name_and_extra_types(server):
    client = RouterClient(router_for(server, include="bridge1"))
    client.connect()
    names = sorted(r["name"] for r in client.fetch_interfaces())
    client.close()
    assert names == ["bridge1", "ether1", "ether2", "sfp-sfpplus1"]


def test_wrong_password_raises(server):
    client = RouterClient(router_for(server, password="mala"))
    with pytest.raises(TrapError):
        client.connect()


def test_connection_refused_raises():
    s = FakeRouterOS()
    port = s.port
    s.stop()
    client = RouterClient({"host": "127.0.0.1", "port": port, "username": "u", "password": "p",
                           "types": "ether", "include": ""}, timeout=1)
    with pytest.raises(OSError):
        client.connect()


def test_falls_back_to_client_side_filter_if_router_rejects_query():
    s = FakeRouterOS(reject_queries=True)
    try:
        client = RouterClient(router_for(s))
        client.connect()
        rows = client.fetch_interfaces()
        client.close()
        assert sorted(r["name"] for r in rows) == ["ether1", "ether2", "sfp-sfpplus1"]
    finally:
        s.stop()


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_poller_end_to_end_computes_rates_from_real_socket_reads():
    server = FakeRouterOS(bytes_per_request=25_000_000)   # ~100 Mbps cada 2 s
    db = DB(":memory:")
    db.set_settings({"poll_interval": 2})
    engine = Engine(db)
    db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Fake", "127.0.0.1", server.port, "mkmonitor", "secret", time.time()),
    )
    poller = Poller(db, engine)
    poller.start()
    try:
        def ready():
            live = engine.snapshot().get(1)
            return bool(live and live["ifaces"].get("ether1", {}).get("rx_bps"))

        assert wait_for(ready, 12), "no se calculó velocidad"
        live = engine.snapshot()[1]
        assert live["reachable"] is True
        assert sorted(live["ifaces"]) == ["ether1", "ether2", "sfp-sfpplus1"]
        rx = live["ifaces"]["ether1"]["rx_bps"]
        assert 50e6 < rx < 150e6, rx
        assert live["ifaces"]["ether2"]["running"] is False
        assert db.query("SELECT 1 FROM alerts") == []   # ether2 nunca estuvo arriba: sin alerta
    finally:
        poller.stop()
        server.stop()


def test_poller_raises_router_down_alert_on_bad_credentials():
    server = FakeRouterOS(password="otra")
    db = DB(":memory:")
    db.set_settings({"router_down_after": 1, "poll_interval": 2})
    engine = Engine(db)
    db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Fake", "127.0.0.1", server.port, "mkmonitor", "secret", time.time()),
    )
    poller = Poller(db, engine)
    poller.start()
    try:
        assert wait_for(lambda: bool(db.query("SELECT 1 FROM alerts WHERE kind='router_down'")), 8)
        assert engine.snapshot()[1]["reachable"] is False
    finally:
        poller.stop()
        server.stop()


def test_poller_detects_link_drop_and_recovery_over_the_wire():
    server = FakeRouterOS(bytes_per_request=1_000_000)
    db = DB(":memory:")
    db.set_settings({"poll_interval": 2})
    engine = Engine(db)
    db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Fake", "127.0.0.1", server.port, "mkmonitor", "secret", time.time()),
    )
    poller = Poller(db, engine)
    poller.start()
    try:
        assert wait_for(lambda: bool(db.query("SELECT 1 FROM iface_config WHERE iface='ether1' AND ever_up=1")), 8)
        for i in server.interfaces:
            if i["name"] == "ether1":
                i["running"] = False
        assert wait_for(lambda: bool(db.query("SELECT 1 FROM alerts WHERE kind='link_down' AND resolved_at IS NULL")), 8)
        for i in server.interfaces:
            if i["name"] == "ether1":
                i["running"] = True
        assert wait_for(lambda: bool(db.query("SELECT 1 FROM alerts WHERE kind='link_down' AND resolved_at IS NOT NULL")), 8)
    finally:
        poller.stop()
        server.stop()


def test_poller_sync_starts_and_stops_workers_with_router_table():
    server = FakeRouterOS()
    db = DB(":memory:")
    engine = Engine(db)
    poller = Poller(db, engine)
    cur = db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Fake", "127.0.0.1", server.port, "mkmonitor", "secret", time.time()),
    )
    try:
        poller.sync()
        assert list(poller.workers) == [cur.lastrowid]
        db.execute("UPDATE routers SET enabled=0 WHERE id=?", (cur.lastrowid,))
        poller.sync()
        assert poller.workers == {}
    finally:
        poller.stop()
        server.stop()
