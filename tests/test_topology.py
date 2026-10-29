import pytest

from app.pppoe import PPPoEMonitor
from app.topology import ConfigError, Topology, validate_pons

T0 = 3_000_000.0


def pons2():
    return [
        {"name": "PON 1", "vlan": 101, "inet_cidr": "10.1.0.0/25", "tv_cidr": "10.1.0.128/25"},
        {"name": "PON 2", "vlan": 102, "inet_cidr": "10.2.0.0/25", "tv_cidr": "10.2.0.128/25"},
    ]


def rows(spec):
    """spec: {nombre: ip} -> filas de /ppp/active."""
    return [{"name": n, "service": "pppoe", "address": ip, "caller-id": "AA:BB:CC:00:00:01", "uptime": "1h"}
            for n, ip in spec.items()]


@pytest.fixture
def mon(db, engine):
    return PPPoEMonitor(db, engine)


@pytest.fixture
def topo(db, engine, mon):
    t = Topology(db, engine, mon)
    t.save_olt("OLT-A", pons2())
    return t


def pon_clients(pon_idx, n, tv=0):
    """n clientes de Internet y tv de TV del PON indicado (1 o 2)."""
    d = {f"p{pon_idx}-c{i}": f"10.{pon_idx}.0.{i + 1}" for i in range(n)}
    d.update({f"p{pon_idx}-tv{i}": f"10.{pon_idx}.0.{129 + i}" for i in range(tv)})
    return d


def active_pon_alerts(db):
    return db.query("SELECT iface, severity, message FROM alerts WHERE kind='pon_down' AND resolved_at IS NULL")


# ------------------------------------------------------------ configuración


def test_validate_pons_normalizes_and_rejects_bad_input():
    out = validate_pons([{"name": "", "vlan": "101", "inet_cidr": "10.1.0.5/25", "tv_cidr": ""}])
    assert out == [{"name": "PON 1", "vlan": 101, "card": 1, "inet_cidr": "10.1.0.0/25", "tv_cidr": ""}]
    with pytest.raises(ConfigError, match="no es una red válida"):
        validate_pons([{"name": "A", "inet_cidr": "10.1.0/25"}])
    with pytest.raises(ConfigError, match="VLAN"):
        validate_pons([{"name": "A", "vlan": 5000}])
    with pytest.raises(ConfigError, match="se pisan"):
        validate_pons([{"name": "A", "inet_cidr": "10.1.0.0/24"}, {"name": "B", "tv_cidr": "10.1.0.128/25"}])
    with pytest.raises(ConfigError, match="mismo nombre"):
        validate_pons([{"name": "A"}, {"name": "a"}])
    with pytest.raises(ConfigError):
        validate_pons([])
    with pytest.raises(ConfigError, match="IPv4"):
        validate_pons([{"name": "A", "inet_cidr": "2001:db8::/64"}])


def test_olt_crud_and_overlap_between_olts(topo, db):
    assert [o["name"] for o in topo.olts()] == ["OLT-A"]
    with pytest.raises(ConfigError, match="Ya existe"):
        topo.save_olt("olt-a", pons2())
    with pytest.raises(ConfigError, match="se pisa"):
        topo.save_olt("OLT-B", [{"name": "X", "vlan": 9, "inet_cidr": "10.1.0.64/26"}])
    oid = topo.save_olt("OLT-B", [{"name": "X", "vlan": 9, "inet_cidr": "10.9.0.0/25"}])
    topo.save_olt("OLT-B2", [{"name": "X", "vlan": 9, "inet_cidr": "10.9.0.0/25"}], oid)   # renombrar la misma
    assert sorted(o["name"] for o in topo.olts()) == ["OLT-A", "OLT-B2"]
    assert topo.delete_olt(oid) and not topo.delete_olt(oid)
    assert db.query("SELECT COUNT(*) AS n FROM pons")[0]["n"] == 2
    with pytest.raises(KeyError):
        topo.save_olt("Z", [{"name": "Q", "inet_cidr": "10.77.0.0/25"}], 999)


def test_locate_assigns_ip_to_pon_and_pool(topo):
    pon1, pon2 = topo.olts()[0]["pons"]
    assert topo.locate("10.1.0.5") == (pon1["id"], 0)
    assert topo.locate("10.1.0.200") == (pon1["id"], 1)
    assert topo.locate("10.2.0.127") == (pon2["id"], 0)
    assert topo.locate("10.3.0.1") is None and topo.locate("basura") is None and topo.locate("") is None


# ----------------------------------------------------------------- mapa


def test_snapshot_groups_clients_by_pon_and_pool(topo, mon, router):
    mon.process_active(router, rows({**pon_clients(1, 3, tv=2), **pon_clients(2, 1), "afuera": "172.16.0.9"}), T0)
    mon.process_active(router, rows({**pon_clients(1, 3, tv=1), **pon_clients(2, 1), "afuera": "172.16.0.9"}), T0 + 15)
    snap = topo.snapshot(T0 + 20)
    p1, p2 = snap["olts"][0]["pons"]
    assert snap["unassigned"] == 1
    assert [len(pl["clients"]) for pl in p1["pools"]] == [3, 2]
    assert p1["pools"][0]["capacity"] == 126 and p1["pools"][0]["cidr"] == "10.1.0.0/25"
    assert p1["pools"][1]["up"] == 1 and p1["pools"][1]["down"] == 1
    down = [c for c in p1["pools"][1]["clients"] if c[1] != "up"]
    assert down[0][0] == "p1-tv1" and down[0][1] == "grace" and down[0][2] == "10.1.0.130"
    assert p1["total"] == 5 and p1["down"] == 1 and p2["total"] == 1 and p2["down"] == 0
    assert p1["status"] in ("ok", "warn") and p1["alert"] is False


def test_empty_pon_has_status_empty(topo):
    snap = topo.snapshot(T0)
    assert [p["status"] for p in snap["olts"][0]["pons"]] == ["empty", "empty"]


# ----------------------------------------------------------- sector caído


def test_sector_down_alert_fires_after_sustain_and_resolves(topo, mon, router, db, notifier):
    full = {**pon_clients(1, 10), **pon_clients(2, 10)}
    mon.process_active(router, rows(full), T0)
    only2 = pon_clients(2, 10)
    mon.process_active(router, rows(only2), T0 + 15)            # se cae el PON 1 entero
    topo.evaluate(T0 + 16)
    assert active_pon_alerts(db) == []                          # todavía no pasó el tiempo sostenido
    topo.evaluate(T0 + 50)
    alerts = active_pon_alerts(db)
    assert len(alerts) == 1 and alerts[0]["iface"] == "OLT-A · PON 1" and alerts[0]["severity"] == "critical"
    assert "Sector caído" in alerts[0]["message"] and "10 de 10" in alerts[0]["message"] and "VLAN 101" in alerts[0]["message"]
    assert any("Sector caído" in m for m in notifier.messages)
    assert topo.snapshot(T0 + 51)["olts"][0]["pons"][0]["status"] == "down"
    topo.evaluate(T0 + 60)
    assert len(active_pon_alerts(db)) == 1                      # no se duplica
    mon.process_active(router, rows(full), T0 + 75)             # vuelven todos
    topo.evaluate(T0 + 76)
    assert len(active_pon_alerts(db)) == 1                      # histéresis: aún no se resuelve
    topo.evaluate(T0 + 120)
    assert active_pon_alerts(db) == []
    assert topo.snapshot(T0 + 121)["olts"][0]["pons"][0]["status"] == "ok"


def test_individual_failures_do_not_make_a_sector_alert(topo, mon, router, db):
    full = pon_clients(1, 20)
    mon.process_active(router, rows(full), T0)
    mon.process_active(router, rows({k: v for k, v in full.items() if k not in ("p1-c0", "p1-c1")}), T0 + 15)
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 100)
    assert active_pon_alerts(db) == []


def test_tiny_pon_does_not_alert(topo, mon, router, db):
    mon.process_active(router, rows(pon_clients(1, 3)), T0)
    mon.process_active(router, rows({}), T0 + 15)
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 100)
    assert active_pon_alerts(db) == []          # menos de pon_min_clients (4)


def test_sector_alert_stays_when_clients_expire(topo, mon, router, db):
    full = pon_clients(1, 10)
    mon.process_active(router, rows(full), T0)
    mon.process_active(router, rows({}), T0 + 15)
    mon.process_active(router, rows({}), T0 + 100)                 # ya pasó la gracia: alerta por cliente
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 100)
    assert len(active_pon_alerts(db)) == 1
    later = T0 + 25 * 3600                                         # pasan más de 24 h: los clientes vencen
    mon.process_active(router, rows({}), later)
    snap = topo.snapshot(later)
    assert {c[1] for c in snap["olts"][0]["pons"][0]["pools"][0]["clients"]} == {"exp"}
    topo.evaluate(later)
    topo.evaluate(later + 100)
    assert len(active_pon_alerts(db)) == 1                         # sigue: no volvió nadie


def test_expired_clients_alone_do_not_trigger_alert(topo, mon, router, db):
    """Las bajas viejas (clientes vencidos) no deben disparar una alerta nueva."""
    mon.process_active(router, rows(pon_clients(1, 10)), T0)
    mon.process_active(router, rows({}), T0 + 15)
    mon.process_active(router, rows({}), T0 + 100)                  # nadie evaluó sectores todavía
    later = T0 + 25 * 3600
    mon.process_active(router, rows({}), later)                     # vencen
    topo.evaluate(later)
    topo.evaluate(later + 100)
    assert active_pon_alerts(db) == []


def test_editing_olt_resolves_its_sector_alerts_quietly(topo, mon, router, db):
    mon.process_active(router, rows(pon_clients(1, 10)), T0)
    mon.process_active(router, rows({}), T0 + 15)
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 60)
    assert len(active_pon_alerts(db)) == 1
    topo.save_olt("OLT-A", pons2(), topo.olts()[0]["id"])
    assert active_pon_alerts(db) == []


def test_sector_alert_is_restored_after_restart(topo, mon, router, db, engine):
    mon.process_active(router, rows(pon_clients(1, 10)), T0)
    mon.process_active(router, rows({}), T0 + 15)
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 60)
    from app.engine import Engine
    engine2 = Engine(db)
    topo2 = Topology(db, engine2, PPPoEMonitor(db, engine2))
    assert topo2.snapshot(T0 + 70)["olts"][0]["pons"][0]["alert"] is True


def test_sector_detection_can_be_disabled(topo, mon, router, db):
    db.set_settings({"pon_down_pct": 0})
    mon.process_active(router, rows(pon_clients(1, 10)), T0)
    mon.process_active(router, rows({}), T0 + 15)
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 100)
    assert active_pon_alerts(db) == []


def test_message_keeps_last_down_text_while_recovering(topo, mon, router, db):
    full = pon_clients(1, 10)
    mon.process_active(router, rows(full), T0)
    mon.process_active(router, rows({}), T0 + 15)
    topo.evaluate(T0 + 16)
    topo.evaluate(T0 + 60)
    mon.process_active(router, rows(full), T0 + 75)
    topo.evaluate(T0 + 76)                       # ya volvieron todos, pero la alerta sigue abierta unos segundos
    msg = active_pon_alerts(db)[0]["message"]
    assert "10 de 10" in msg and "— 0 de" not in msg


# --------------------------------------------------------- detección en el MikroTik


def test_detect_pons_from_vlans_and_addresses():
    from app.topology import detect_pons

    vlans = [
        {"name": "vlan102", "vlan-id": 102, "disabled": False},
        {"name": "vlan101", "vlan-id": 101, "disabled": False},
        {"name": "vlan-sin-ip", "vlan-id": 150, "disabled": False},
        {"name": "vlan-off", "vlan-id": 160, "disabled": True},
        {"name": "vlan10", "vlan-id": 10, "disabled": False},
        {"name": "vlan-chica", "vlan-id": 170, "disabled": False},
    ]
    addrs = [
        {"address": "10.10.2.1/24", "interface": "vlan102"},
        {"address": "10.10.1.1/24", "interface": "vlan101"},
        {"address": "192.168.10.1/24", "interface": "vlan10"},
        {"address": "10.99.0.1/30", "interface": "vlan-off"},
        {"address": "172.16.0.1/32", "interface": "vlan-chica"},
        {"address": "fe80::1/64", "interface": "vlan-sin-ip"},
    ]
    out = detect_pons(vlans, addrs, 100, 200)
    assert [(p["name"], p["vlan"], p["inet_cidr"], p["tv_cidr"]) for p in out["pons"]] == [
        ("PON 1", 101, "10.10.1.0/25", "10.10.1.128/25"),
        ("PON 2", 102, "10.10.2.0/25", "10.10.2.128/25"),
    ]
    assert out["pons"][0]["from"] == "vlan101 · 10.10.1.1/24"
    assert any("vlan-sin-ip" in x and "IPv4" in x for x in out["skipped"])
    assert any("vlan-chica" in x for x in out["skipped"])
    allv = detect_pons(vlans, addrs)["pons"]                       # sin filtro entra también la VLAN de gestión
    assert [p["vlan"] for p in allv] == [10, 101, 102]
    small = detect_pons([{"name": "v", "vlan-id": 5}], [{"address": "10.0.0.1/26", "interface": "v"}])["pons"][0]
    assert small["inet_cidr"] == "10.0.0.0/26" and small["tv_cidr"] == ""
    validate_pons(out["pons"])                                      # lo detectado es una configuración válida


def test_cards_layout_is_validated_and_saved(tmp_path):
    from app.db import DB
    from app.engine import Engine
    from app.notify import Notifier
    from app.pppoe import PPPoEMonitor
    from app.topology import Topology
    db = DB(":memory:")
    eng = Engine(db, Notifier("", ""))
    topo = Topology(db, eng, PPPoEMonitor(db, eng))
    pons = [{"name": f"PON {c}/{p}", "vlan": 100 + c * 10 + p, "card": c} for c in (1, 2) for p in (1, 2)]
    oid = topo.save_olt("OLT-A", pons, cards=2, ports_per_card=2)
    o = topo.olts()[0]
    assert (o["cards"], o["ports_per_card"]) == (2, 2)
    assert [p["card"] for p in o["pons"]] == [1, 1, 2, 2]
    snap = topo.snapshot()["olts"][0]
    assert snap["cards"] == 2 and [p["card"] for p in snap["pons"]] == [1, 1, 2, 2]
    with pytest.raises(ConfigError):                 # tarjeta que no existe
        topo.save_olt("OLT-A", [{"name": "x", "card": 3}], oid, cards=2, ports_per_card=2)
    with pytest.raises(ConfigError):                 # más PON que puertos en la tarjeta 1
        topo.save_olt("OLT-A", [{"name": f"p{i}", "card": 1} for i in range(3)], oid, cards=2, ports_per_card=2)
    with pytest.raises(ConfigError):
        topo.save_olt("OLT-A", pons, oid, cards=0, ports_per_card=2)
    with pytest.raises(ConfigError):
        topo.save_olt("OLT-A", pons, oid, cards=40, ports_per_card=2)


def two_card_pons():
    """«PON 1» y «PON 2» existen en las dos tarjetas (mismo nombre, tarjetas distintas)."""
    out = []
    for card in (1, 2):
        for port in (1, 2):
            n = (card - 1) * 2 + port
            out.append({"name": f"PON {port}", "vlan": 100 + n, "card": card,
                        "inet_cidr": f"10.{n}.0.0/25", "tv_cidr": f"10.{n}.0.128/25"})
    return out


def test_same_pon_name_is_allowed_on_different_cards_but_not_on_the_same_one():
    assert len(validate_pons(two_card_pons(), cards=2, ports_per_card=2)) == 4
    dup = two_card_pons()
    dup[1]["name"] = "PON 1"                       # «PON 1» dos veces en la tarjeta 1
    with pytest.raises(ConfigError, match="tarjeta 1"):
        validate_pons(dup, cards=2, ports_per_card=2)
    with pytest.raises(ConfigError, match="mismo nombre"):
        validate_pons([{"name": "a"}, {"name": "A"}])         # con una sola tarjeta sigue igual


def test_duplicate_names_get_unique_labels_alerts_and_restore(db, engine, mon, router):
    t = Topology(db, engine, mon)
    t.save_olt("OLT-B", two_card_pons(), cards=2, ports_per_card=2)
    snap = t.snapshot(T0)["olts"][0]["pons"]
    assert [p["label"] for p in snap] == ["PON 1 (T1)", "PON 2 (T1)", "PON 1 (T2)", "PON 2 (T2)"]
    # se cae la tarjeta 2, puerto 1 (PON 3 = 10.3.x): la alerta dice cuál de los dos «PON 1» es
    full = {f"c{n}-{i}": f"10.{n}.0.{i + 1}" for n in (1, 2, 3, 4) for i in range(8)}
    mon.process_active(router, rows(full), T0)
    mon.process_active(router, rows({k: v for k, v in full.items() if not v.startswith("10.3.")}), T0 + 15)
    t.evaluate(T0 + 16)
    t.evaluate(T0 + 60)
    alerts = active_pon_alerts(db)
    assert [a["iface"] for a in alerts] == ["OLT-B · PON 1 (T2)"]
    assert "PON 1 (T2)" in alerts[0]["message"]
    # tras reiniciar, se retoma la alerta del PON correcto y no la del homónimo
    from app.engine import Engine
    topo2 = Topology(db, Engine(db), PPPoEMonitor(db, Engine(db)))
    flags = {p["label"]: p["alert"] for p in topo2.snapshot(T0 + 70)["olts"][0]["pons"]}
    assert flags == {"PON 1 (T1)": False, "PON 2 (T1)": False, "PON 1 (T2)": True, "PON 2 (T2)": False}
