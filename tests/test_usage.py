import time

import pytest
from fastapi.testclient import TestClient

from app.usage import GB, UsageMonitor
from fake_routeros import FakeRouterOS
from test_login import login, new_app
from test_routeros_client import router_for

T0 = 1_800_000_000.0     # múltiplo de 3600 y de 900


def rows(**users):
    """rows(a=(rx, tx)) -> filas de /interface/print de las sesiones PPPoE."""
    return [{"name": f"<pppoe-{u}>", "rx-byte": rx, "tx-byte": tx} for u, (rx, tx) in users.items()]


@pytest.fixture
def mon(db):
    return UsageMonitor(db)


def test_first_read_is_only_a_baseline_then_deltas_are_counted(mon, db):
    assert mon.process(1, rows(ana=(1000, 5000)), T0) == 0
    assert db.query("SELECT * FROM usage_h") == []
    assert mon.process(1, rows(ana=(1500, 9000)), T0 + 300) == 1
    h = db.one("SELECT * FROM usage_h WHERE user='ana'")
    # tx del router = bajada del cliente; rx = subida
    assert (h["down_b"], h["up_b"]) == (4000, 500)
    mon.process(1, rows(ana=(1600, 9100)), T0 + 600)
    assert db.one("SELECT down_b, up_b FROM usage_h WHERE user='ana'") == {"down_b": 4100, "up_b": 600}
    assert db.one("SELECT COUNT(*) n FROM usage_q")["n"] == 1                # mismo intervalo de 15 min


def test_counter_reset_and_new_sessions_count_from_zero(mon, db):
    mon.process(1, rows(ana=(10_000, 10_000)), T0)
    mon.process(1, rows(ana=(300, 700)), T0 + 300)               # se reconectó: contadores más chicos
    assert db.one("SELECT down_b, up_b FROM usage_h") == {"down_b": 700, "up_b": 300}
    mon.process(1, rows(), T0 + 600)                              # se fue
    mon.process(1, rows(ana=(50, 80)), T0 + 900)                  # sesión nueva en un router ya conocido
    assert db.one("SELECT down_b, up_b FROM usage_h") == {"down_b": 780, "up_b": 350}


def test_garbage_names_and_impossible_rates_are_ignored(mon, db):
    mon.process(1, rows(ana=(0, 0)), T0)
    bad = rows(ana=(10**15, 10**15)) + [{"name": "ether1", "rx-byte": 5, "tx-byte": 5}, {"name": "<pppoe-x>", "rx-byte": "?"}]
    assert mon.process(1, bad, T0 + 300) == 0
    assert db.query("SELECT * FROM usage_h") == []


def seed_days(db, user, days_gb, today_gb, rid=1, now=T0):
    """days_gb: consumo (GB) de cada uno de los días anteriores, del más viejo al más nuevo."""
    for n, gb in enumerate(reversed(days_gb), start=1):
        db.execute("INSERT INTO usage_h VALUES(?,?,?,?,?)", (rid, user, int(now - n * 86400 - 1800), int(gb * GB * 0.9), int(gb * GB * 0.1)))
    db.execute("INSERT INTO usage_h VALUES(?,?,?,?,?)", (rid, user, int(now - 1800), int(today_gb * GB * 0.9), int(today_gb * GB * 0.1)))


def test_top_flags_only_real_outliers(db):
    db.execute("INSERT INTO routers(name, host, port, username, password, updated_at) VALUES('R1','h',8728,'u','p',1)")
    seed_days(db, "normal", [10, 11, 9, 10, 12], 11)
    seed_days(db, "abusivo", [10, 11, 9, 10, 12], 60)
    seed_days(db, "chico", [0.1, 0.1, 0.1, 0.1], 1)              # 10× pero muy poco volumen: no se marca
    seed_days(db, "nuevo", [], 80)                                # sin base de comparación: no se marca
    mon = UsageMonitor(db, clock=lambda: T0)
    top = mon.top(24)
    by = {r["user"]: r for r in top["rows"]}
    assert by["abusivo"]["abnormal"] and by["abusivo"]["ratio"] == pytest.approx(60 / 10, rel=0.01)
    assert not by["normal"]["abnormal"] and not by["chico"]["abnormal"] and not by["nuevo"]["abnormal"]
    assert by["nuevo"]["ratio"] is None
    assert top["abnormal"] == 1 and top["rows"][0]["user"] == "abusivo"        # primero el anormal
    assert [r["user"] for r in mon.top(24, only_abnormal=True)["rows"]] == ["abusivo"]
    assert [r["user"] for r in mon.top(24, q="norm")["rows"]] == ["normal"]
    assert mon.top(24, router_id=2)["rows"] == []
    db.set_settings({"usage_factor": 8})
    assert mon.top(24)["abnormal"] == 0


def test_client_history_has_points_totals_and_daily_comparison(db):
    db.execute("INSERT INTO routers(name, host, port, username, password, updated_at) VALUES('R1','h',8728,'u','p',1)")
    seed_days(db, "ana", [5, 5, 5, 5], 40)
    for i in range(8):
        db.execute("INSERT INTO usage_q VALUES(1,'ana',?,?,?)", (int(T0 - 900 * (i + 1)), 900 * 8 * 1000, 900 * 8 * 200))
    mon = UsageMonitor(db, clock=lambda: T0)
    c = mon.client(1, "ana", 24)
    assert c["router"] == "R1" and c["bucket_s"] == 900 and len(c["points"]) == 8
    ts, down, up, *_ = c["points"][0]
    assert down == pytest.approx(8000 * 8) and up == pytest.approx(1600 * 8)
    assert c["abnormal"] and c["ratio"] == pytest.approx(8, rel=0.01) and c["typical_b"] == pytest.approx(5 * GB)
    assert c["daily"][0]["days_ago"] == 0
    week = mon.client(1, "ana", 168)
    assert week["bucket_s"] == 3600 and len(week["points"]) == 5
    assert mon.client(1, "nadie", 24)["points"] == []


def test_prune_and_forget_router(db):
    mon = UsageMonitor(db, clock=lambda: T0)
    db.execute("INSERT INTO usage_q VALUES(1,'a',?,1,1)", (int(T0 - 4 * 86400),))
    db.execute("INSERT INTO usage_q VALUES(1,'a',?,1,1)", (int(T0 - 3600),))
    db.execute("INSERT INTO usage_h VALUES(1,'a',?,1,1)", (int(T0 - 40 * 86400),))
    db.execute("INSERT INTO usage_h VALUES(1,'a',?,1,1)", (int(T0 - 20 * 86400),))
    mon.prune()
    assert db.one("SELECT COUNT(*) n FROM usage_q")["n"] == 1 and db.one("SELECT COUNT(*) n FROM usage_h")["n"] == 1
    mon.forget_router(1)
    assert db.one("SELECT COUNT(*) n FROM usage_q")["n"] == 0 and db.one("SELECT COUNT(*) n FROM usage_h")["n"] == 0


def test_client_reads_pppoe_counters_with_a_router_side_filter():
    server = FakeRouterOS()
    try:
        server.interfaces.append(dict(name="<pppoe-ana>", type="pppoe-in", running=True, disabled=False, **{"rx-byte": 7, "tx-byte": 9, ".id": "*FFFF"}))
        from app.poller import RouterClient
        client = RouterClient(router_for(server))
        client.connect()
        got = client.fetch_pppoe_usage()
        client.close()
        assert len(got) == 301 and {"name": "<pppoe-ana>", "rx-byte": 7, "tx-byte": 9} == {k: v for k, v in next(r for r in got if r["name"] == "<pppoe-ana>").items() if k != ".id"}
        assert any("?=type=pppoe-in" in w for req in server.requests for w in req)
    finally:
        server.stop()


def test_usage_api_requires_login_validates_and_returns_data(tmp_path):
    app = new_app(db_path=str(tmp_path / "m.db"))
    now = time.time()
    with TestClient(app) as c:
        assert c.get("/api/usage/top").status_code == 401
        assert c.get("/api/usage/client?router_id=1&user=a").status_code == 401
        login(c)
        db = app.state.db
        rid = db.execute("INSERT INTO routers(name, host, port, username, password, updated_at) VALUES('R','h',8728,'u','p',1)").lastrowid
        db.execute("INSERT INTO usage_h VALUES(?,?,?,?,?)", (rid, "ana", int(now // 3600) * 3600, 5 * GB, GB))
        top = c.get("/api/usage/top?hours=24").json()
        assert top["total"] == 1 and top["rows"][0]["user"] == "ana" and top["rows"][0]["router"] == "R"
        assert c.get("/api/usage/top?hours=5").status_code == 400
        assert c.get("/api/usage/client?router_id=%d&user=ana&hours=168" % rid).json()["down_b"] == 5 * GB
        assert c.get("/api/usage/client?router_id=1&user=").status_code == 400
        assert c.get("/usage.js").status_code == 200
        assert c.put("/api/settings", json={"usage_poll_s": 10}).status_code == 400
        assert c.put("/api/settings", json={"usage_poll_s": 120, "usage_factor": 4}).status_code == 200
        # al borrar el router se va su historial
        assert c.delete(f"/api/routers/{rid}").status_code == 200
        assert db.one("SELECT COUNT(*) n FROM usage_h")["n"] == 0


def test_worker_usage_step_records_deltas_and_never_breaks_the_router_loop(db):
    from app.poller import RouterClient, Worker
    server = FakeRouterOS()
    try:
        server.interfaces.append(dict(name="<pppoe-ana>", type="pppoe-in", running=True, disabled=False, **{"rx-byte": 100, "tx-byte": 1000, ".id": "*FFFF"}))
        rid = db.execute("INSERT INTO routers(name, host, port, username, password, updated_at) VALUES('Fake','127.0.0.1',?,'mkmonitor','secret',1)", (server.port,)).lastrowid
        router = router_for(server, id=rid)
        mon = UsageMonitor(db)
        w = Worker(router, db, None, RouterClient, None, mon)
        client = RouterClient(router)
        client.connect()
        w._usage_step(client)                                   # base
        assert db.query("SELECT * FROM usage_h") == []
        ana = next(i for i in server.interfaces if i["name"] == "<pppoe-ana>")
        ana["rx-byte"], ana["tx-byte"] = 600, 9000
        w._last_usage = 0.0
        w._usage_step(client)
        row = db.one("SELECT down_b, up_b FROM usage_h WHERE user='ana'")
        assert row == {"down_b": 8000, "up_b": 500}
        w._last_usage = 0.0
        db.set_settings({"usage_enabled": 0})
        ana["tx-byte"] = 99999
        w._usage_step(client)                                   # apagado: no lee
        assert db.one("SELECT down_b FROM usage_h WHERE user='ana'")["down_b"] == 8000
        db.set_settings({"usage_enabled": 1})
        client.close()
        w._last_usage = 0.0
        w._usage_step(client)                                   # conexión cerrada: se traga el error
    finally:
        server.stop()
