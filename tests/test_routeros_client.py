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


# ------------------------------------------------------------------------- PPPoE


def active(db):
    return db.query("SELECT kind, iface FROM alerts WHERE resolved_at IS NULL")


def ppp(name, i, service="pppoe"):
    return {"name": name, "service": service, "address": f"10.20.0.{i}",
            "caller-id": f"AA:BB:CC:00:00:{i:02X}", "uptime": "1h2m3s"}


def test_fetch_pppoe_active_reads_only_pppoe_sessions(server):
    server.ppp_active = [ppp("ana", 1), ppp("beto", 2), ppp("vpn-user", 3, service="l2tp")]
    c = RouterClient(router_for(server))
    c.connect()
    rows = c.fetch_pppoe_active()
    c.close()
    assert sorted(r["name"] for r in rows) == ["ana", "beto"]
    assert rows[0]["caller-id"].startswith("AA:BB") and rows[0]["address"].startswith("10.20.")


def test_fetch_pppoe_secrets_keeps_pppoe_and_any_service(server):
    server.ppp_secrets = [
        {"name": "ana", "service": "pppoe", "disabled": False, "last-logged-out": "2026-10-27 18:42:10"},
        {"name": "beto", "service": "any", "disabled": True},
        {"name": "vpn", "service": "l2tp"},
    ]
    c = RouterClient(router_for(server))
    c.connect()
    rows = c.fetch_pppoe_secrets()
    c.close()
    assert sorted(r["name"] for r in rows) == ["ana", "beto"]
    assert next(r for r in rows if r["name"] == "ana")["last-logged-out"].startswith("2026")
    assert next(r for r in rows if r["name"] == "beto")["disabled"] is True


def test_poller_detects_pppoe_disconnect_over_the_wire_and_reconnect():
    from app.pppoe import PPPoEMonitor

    server = FakeRouterOS()
    server.ppp_active = [ppp(f"cli{i}", i) for i in range(1, 11)]
    db = DB(":memory:")
    db.set_settings({"poll_interval": 2, "pppoe_poll_s": 1, "pppoe_grace_s": 1, "pppoe_mass_threshold": 0})
    engine = Engine(db)
    pppoe = PPPoEMonitor(db, engine)
    db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Fake", "127.0.0.1", server.port, "mkmonitor", "secret", time.time()),
    )
    poller = Poller(db, engine, pppoe=pppoe)
    poller.start()
    try:
        assert wait_for(lambda: (pppoe.summary().get(1) or {}).get("online") == 10, 10)
        server.ppp_active = [r for r in server.ppp_active if r["name"] != "cli3"]
        assert wait_for(lambda: any(a["kind"] == "pppoe_down" and a["iface"] == "cli3"
                                    for a in active(db)), 15), "no alertó la desconexión"
        assert (pppoe.summary()[1]["down"]) == 1
        server.ppp_active.append(ppp("cli3", 3))
        assert wait_for(lambda: not any(a["kind"] == "pppoe_down" for a in active(db)), 15)
    finally:
        poller.stop()
        server.stop()


def test_pppoe_permission_error_does_not_mark_router_down():
    from app.pppoe import PPPoEMonitor

    server = FakeRouterOS()
    server.ppp_error = "not enough permissions (9)"
    db = DB(":memory:")
    db.set_settings({"poll_interval": 2, "pppoe_poll_s": 1})
    engine = Engine(db)
    pppoe = PPPoEMonitor(db, engine)
    db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Fake", "127.0.0.1", server.port, "mkmonitor", "secret", time.time()),
    )
    poller = Poller(db, engine, pppoe=pppoe)
    poller.start()
    try:
        assert wait_for(lambda: (pppoe.summary().get(1) or {}).get("error"), 10)
        assert engine.snapshot()[1]["reachable"] is True
        assert not any(a["kind"] == "router_down" for a in active(db))
    finally:
        poller.stop()
        server.stop()


def test_fetch_vlans_reads_vlan_interfaces_and_addresses(server):
    server.vlans = [{"name": "vlan101", "vlan-id": 101, "interface": "sfp1", "disabled": False}]
    server.addresses = [{"address": "10.10.1.1/24", "interface": "vlan101", "disabled": False}]
    c = RouterClient(router_for(server))
    c.connect()
    vlans, addrs = c.fetch_vlans()
    c.close()
    assert vlans[0]["name"] == "vlan101" and vlans[0]["vlan-id"] == 101
    assert addrs[0]["address"] == "10.10.1.1/24" and addrs[0]["interface"] == "vlan101"
    from app.topology import detect_pons
    assert detect_pons(vlans, addrs)["pons"][0]["inet_cidr"] == "10.10.1.0/25"
