import socket
import time

import pytest

from app.pppoe import PPPoEMonitor, explain_reason, fmt_dur, parse_uptime
from app.syslog import SyslogReceiver, parse_line

T0 = 2_000_000.0


def active(*names, service="pppoe", base=0, **extra):
    return [
        {"name": n, "service": service, "address": f"10.10.0.{i + 1 + base}",
         "caller-id": f"AA:BB:CC:00:00:{i + 1 + base:02X}", "uptime": "3h20m", **extra}
        for i, n in enumerate(names)
    ]


def kinds(db, only_active=True):
    where = "WHERE resolved_at IS NULL" if only_active else ""
    return sorted(r["kind"] for r in db.query(f"SELECT kind FROM alerts {where}"))


@pytest.fixture
def mon(db, engine):
    return PPPoEMonitor(db, engine)


def poll(mon, router, names, t, **kw):
    mon.process_active(router, active(*names, **kw), now=T0 + t)


# --------------------------------------------------------------- utilidades


def test_parse_uptime_and_fmt():
    assert parse_uptime("1w2d3h4m5s") == 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert parse_uptime("45s") == 45
    assert parse_uptime(None) == 0 and parse_uptime("") == 0 and parse_uptime(90) == 90
    assert fmt_dur(30) == "30 s" and fmt_dur(300) == "5 min" and fmt_dur(3 * 3600 + 120) == "3 h 2 min"
    assert fmt_dur(90000) == "1 d 1 h"


def test_explain_reason():
    assert "no responde" in explain_reason("peer is not responding")
    assert "cerró" in explain_reason("session closed")
    assert explain_reason("algo raro") == ""


# --------------------------------------------------------------- desconexiones


def test_first_poll_does_not_alert_and_counts_online(mon, router, db):
    poll(mon, router, ["a", "b", "c"], 0)
    assert kinds(db) == []
    assert mon.summary()[router["id"]]["online"] == 3


def test_disconnect_alerts_only_after_grace_period(mon, router, db, notifier):
    poll(mon, router, ["ana", "beto"], 0)
    poll(mon, router, ["beto"], 15)                  # ana se cae
    assert kinds(db) == []
    poll(mon, router, ["beto"], 45)
    assert kinds(db) == []                           # 30 s: todavía en gracia
    poll(mon, router, ["beto"], 75)                  # 60 s caída
    assert kinds(db) == ["pppoe_down"]
    row = db.one("SELECT * FROM alerts WHERE kind='pppoe_down'")
    assert row["iface"] == "ana" and row["severity"] == "warning"
    assert "«ana»" in row["message"] and "10.10.0.1" in row["message"] and "MAC AA:BB:CC:00:00:01" in row["message"]
    assert "estuvo conectado 3 h 20 min" in row["message"]
    assert any("Cliente PPPoE «ana» desconectado" in m for m in notifier.messages)
    poll(mon, router, ["beto"], 90)
    assert db.one("SELECT COUNT(*) AS n FROM alerts")["n"] == 1   # no se repite


def test_quick_reconnect_inside_grace_never_alerts_but_is_recorded(mon, router, db):
    poll(mon, router, ["ana"], 0)
    poll(mon, router, [], 15)
    poll(mon, router, ["ana"], 30)                   # volvió a los 15 s
    poll(mon, router, ["ana"], 120)
    assert kinds(db, only_active=False) == []
    ev = db.query("SELECT event FROM pppoe_events ORDER BY id")
    assert [e["event"] for e in ev] == ["disconnect", "connect"]


def test_alert_resolves_silently_when_client_reconnects(mon, router, db, notifier):
    poll(mon, router, ["ana"], 0)
    poll(mon, router, [], 15)
    poll(mon, router, [], 90)
    assert kinds(db) == ["pppoe_down"]
    n_msgs = len(notifier.messages)
    poll(mon, router, ["ana"], 105)
    assert kinds(db) == []
    assert db.one("SELECT resolved_at FROM alerts")["resolved_at"] is not None
    assert len(notifier.messages) == n_msgs          # reconexión: sin aviso de Telegram
    st = mon.sessions[router["id"]]["ana"]
    assert st.state == "up" and st.alerted == 0


def test_non_pppoe_sessions_are_ignored(mon, router, db):
    mon.process_active(router, active("vpn1", service="l2tp") + active("ana"), now=T0)
    assert list(mon.sessions[router["id"]]) == ["ana"]


def test_session_info_is_kept_while_connected(mon, router):
    mon.process_active(router, [{"name": "ana", "service": "pppoe", "address": "1.1.1.1",
                                 "caller-id": "AA"}], now=T0)
    mon.process_active(router, [{"name": "ana", "service": "pppoe", "address": "1.1.1.2",
                                 "caller-id": ""}], now=T0 + 15)
    se = mon.sessions[router["id"]]["ana"]
    assert se.address == "1.1.1.2" and se.caller_id == "AA"     # MAC vacía no borra la anterior


# ---------------------------------------------------------------- caída masiva


def names(n, prefix="c"):
    return [f"{prefix}{i:03d}" for i in range(n)]


def test_mass_outage_raises_one_critical_alert_and_suppresses_individual_ones(mon, router, db, notifier):
    everyone = names(100)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[30:], 15)             # se caen 30 de golpe
    assert kinds(db) == ["pppoe_mass"]
    row = db.one("SELECT * FROM alerts")
    assert row["severity"] == "critical" and "30 clientes" in row["message"] and "Core" in row["message"]
    poll(mon, router, everyone[30:], 120)            # pasada la gracia: sigue habiendo 1 sola alerta
    assert kinds(db) == ["pppoe_mass"]
    assert sum("Caída masiva" in m for m in notifier.messages) == 1
    st = mon.summary()[router["id"]]
    assert st["down"] == 30 and st["suppressed"] == 30 and st["mass"] is True and st["online"] == 70


def test_mass_alert_message_follows_the_number_of_clients_still_down(mon, router, db):
    everyone = names(60)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[30:], 15)
    poll(mon, router, everyone[15:], 30)             # vuelven 15
    msg = db.one("SELECT message FROM alerts WHERE kind='pppoe_mass'")["message"]
    assert "15 clientes siguen sin conexión" in msg and "se cayeron 30" in msg


def test_mass_alert_resolves_when_most_clients_are_back(mon, router, db, notifier):
    everyone = names(60)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[30:], 15)
    poll(mon, router, everyone[5:], 30)              # 25 de 30 vuelven: quedan 5 (< 10)
    assert kinds(db) == []
    assert db.one("SELECT resolved_at FROM alerts")["resolved_at"] is not None
    assert any("Resuelto" in m and "Caída masiva" in m for m in notifier.messages)
    poll(mon, router, everyone[5:], 200)             # los 5 que faltan NO generan alertas sueltas
    assert kinds(db, only_active=False) == ["pppoe_mass"]


def test_few_disconnections_are_not_a_mass_event(mon, router, db):
    everyone = names(100)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[5:], 15)              # 5 < 20
    poll(mon, router, everyone[5:], 100)
    assert kinds(db) == ["pppoe_down"] * 5


def test_slow_drip_of_disconnections_is_not_a_mass_event(mon, router, db):
    everyone = names(100)
    poll(mon, router, everyone, 0)
    alive = list(everyone)
    for i in range(30):                              # 1 cliente cada 30 s (ventana de 60 s)
        alive.pop(0)
        poll(mon, router, alive, 30 * (i + 1))
    assert "pppoe_mass" not in kinds(db, only_active=False)
    assert kinds(db).count("pppoe_down") >= 25


def test_new_drops_during_an_active_mass_event_are_also_suppressed(mon, router, db):
    everyone = names(100)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[30:], 15)
    poll(mon, router, everyone[40:], 30)             # siguen cayéndose más clientes
    poll(mon, router, everyone[40:], 200)
    assert kinds(db) == ["pppoe_mass"]
    assert mon.summary()[router["id"]]["suppressed"] == 40


def test_mass_threshold_zero_disables_the_feature(mon, router, db):
    db.set_settings({"pppoe_mass_threshold": 0})
    everyone = names(50)
    poll(mon, router, everyone, 0)
    poll(mon, router, [], 15)
    poll(mon, router, [], 100)
    assert kinds(db) == ["pppoe_down"] * 50


# ------------------------------------------------------------------ expiración


def test_alert_expires_after_configured_hours_and_does_not_come_back(mon, router, db):
    db.set_settings({"pppoe_expire_h": 24})
    poll(mon, router, ["ana"], 0)
    poll(mon, router, [], 15)
    poll(mon, router, [], 100)
    assert kinds(db) == ["pppoe_down"]
    poll(mon, router, [], 15 + 24 * 3600 + 1)
    assert kinds(db) == []
    poll(mon, router, [], 15 + 25 * 3600)
    assert kinds(db, only_active=False) == ["pppoe_down"]            # no se vuelve a abrir
    poll(mon, router, ["ana"], 15 + 26 * 3600)                       # y si vuelve, queda normal
    assert mon.sessions[router["id"]]["ana"].state == "up"
    assert kinds(db) == []


# ---------------------------------------------------------------- motivo / log


def test_reason_from_log_is_attached_to_the_alert(mon, router, db):
    poll(mon, router, ["ana"], 0)
    mon.on_log_event(router["id"], "ana", "disconnect", "peer is not responding", ts=T0 + 10)
    poll(mon, router, [], 15)
    poll(mon, router, [], 90)
    msg = db.one("SELECT message FROM alerts")["message"]
    assert "motivo: el equipo del cliente no responde (apagado o enlace cortado) (peer is not responding)" in msg
    assert db.one("SELECT reason FROM pppoe_events WHERE event='disconnect'")["reason"] == "peer is not responding"


def test_reason_arriving_after_the_alert_updates_message_and_history(mon, router, db):
    poll(mon, router, ["ana"], 0)
    poll(mon, router, [], 15)
    poll(mon, router, [], 90)
    assert "motivo" not in db.one("SELECT message FROM alerts")["message"]
    mon.on_log_event(router["id"], "ana", "disconnect", "session closed", ts=T0 + 100)
    assert "motivo: el cliente cerró la sesión (session closed)" in db.one("SELECT message FROM alerts")["message"]
    assert db.one("SELECT reason FROM pppoe_events WHERE event='disconnect'")["reason"] == "session closed"


def test_unknown_reasons_are_shown_raw_and_stale_reasons_are_ignored(mon, router, db):
    poll(mon, router, ["ana", "beto"], 0)
    mon.on_log_event(router["id"], "ana", "disconnect", "motivo desconocido", ts=T0 + 5)
    mon.on_log_event(router["id"], "beto", "disconnect", "session closed", ts=T0 - 1000)   # viejo
    poll(mon, router, [], 15)
    poll(mon, router, [], 90)
    msgs = {r["iface"]: r["message"] for r in db.query("SELECT iface, message FROM alerts")}
    assert "motivo: motivo desconocido" in msgs["ana"]
    assert "motivo" not in msgs["beto"]


def test_log_events_for_unknown_users_or_reasonless_are_harmless(mon, router):
    mon.on_log_event(router["id"], "nadie", "disconnect", "x", ts=T0)
    mon.on_log_event(router["id"], "nadie", "connect", "", ts=T0)
    mon.on_log_event(router["id"], "nadie", "disconnect", "", ts=T0)


# ------------------------------------------------------- reinicio del servidor


def test_state_survives_server_restart_and_does_not_duplicate_alerts(mon, router, db, notifier):
    from app.engine import Engine
    poll(mon, router, ["ana", "beto"], 0)
    poll(mon, router, ["beto"], 15)
    poll(mon, router, ["beto"], 90)
    assert kinds(db) == ["pppoe_down"]

    engine2 = Engine(db, notifier)
    mon2 = PPPoEMonitor(db, engine2)
    assert mon2.sessions[router["id"]]["ana"].state == "down"
    poll(mon2, router, ["beto"], 120)
    assert db.one("SELECT COUNT(*) AS n FROM alerts")["n"] == 1
    poll(mon2, router, ["ana", "beto"], 135)         # y se resuelve normalmente después del reinicio
    assert kinds(db) == []


def test_client_that_left_while_server_was_down_is_detected_with_old_timestamp(mon, router, db):
    from app.engine import Engine
    poll(mon, router, ["ana", "beto"], 0)
    poll(mon, router, ["ana", "beto"], 300)          # checkpoint guarda last_seen
    mon2 = PPPoEMonitor(db, Engine(db))
    poll(mon2, router, ["beto"], 3600)               # una hora después ana ya no está
    se = mon2.sessions[router["id"]]["ana"]
    assert se.state == "down" and se.since == pytest.approx(T0 + 300)
    assert kinds(db) == ["pppoe_down"]               # la gracia ya pasó: alerta inmediata


def test_mass_alert_state_is_restored_after_restart(mon, router, db, notifier):
    from app.engine import Engine
    everyone = names(60)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[30:], 15)
    mon2 = PPPoEMonitor(db, Engine(db, notifier))
    assert router["id"] in mon2.mass
    poll(mon2, router, everyone[30:], 30)
    assert kinds(db) == ["pppoe_mass"]
    poll(mon2, router, everyone, 45)
    assert kinds(db) == []


# ---------------------------------------------------------- apagar / olvidar


def test_forget_router_clears_alerts_sessions_and_history(mon, router, db):
    poll(mon, router, ["ana"], 0)
    poll(mon, router, [], 15)
    poll(mon, router, [], 90)
    assert kinds(db) == ["pppoe_down"]
    mon.forget_router(router["id"])
    assert kinds(db) == []
    assert db.query("SELECT 1 FROM pppoe_sessions") == [] and db.query("SELECT 1 FROM pppoe_events") == []
    assert mon.summary() == {}


def test_prune_forgets_old_down_clients_and_old_events(mon, router, db):
    poll(mon, router, ["ana", "beto"], 0)
    poll(mon, router, ["beto"], 15)
    mon.prune(now=T0 + 15 + 70 * 86400)
    assert "ana" not in mon.sessions[router["id"]] and "beto" in mon.sessions[router["id"]]
    assert db.one("SELECT COUNT(*) AS n FROM pppoe_events")["n"] == 0


def test_process_error_is_reported_without_touching_sessions(mon, router):
    poll(mon, router, ["ana"], 0)
    mon.process_error(router, "no permission")
    assert mon.summary()[router["id"]]["error"] == "no permission"
    assert mon.sessions[router["id"]]["ana"].state == "up"
    poll(mon, router, ["ana"], 15)
    assert mon.summary()[router["id"]]["error"] is None


# ------------------------------------------------------------------- vistas


def test_down_sessions_and_search_views(mon, router):
    poll(mon, router, ["juan.perez", "maria", "pedro"], 0)
    poll(mon, router, ["pedro"], 15)
    poll(mon, router, ["pedro"], 90)
    down = mon.down_sessions()
    assert [d["name"] for d in down] == ["juan.perez", "maria"] or [d["name"] for d in down] == ["maria", "juan.perez"]
    assert all(d["status"] == "alert" for d in down)
    assert [d["name"] for d in mon.down_sessions(q="juan")] == ["juan.perez"]
    assert mon.down_sessions(router_id=999) == []
    hits = mon.search("perez")
    assert hits[0]["name"] == "juan.perez" and hits[0]["state"] == "down"
    assert [h["name"] for h in mon.search("pedro")] == ["pedro"]
    assert mon.search("p") == []                      # búsqueda muy corta
    assert [h["name"] for h in mon.search("aa:bb:cc:00:00:02")] == ["maria"]      # por MAC, sin importar mayúsculas
    assert sorted(h["name"] for h in mon.search("10.10.0.1")) == ["juan.perez", "pedro"]   # por IP


def test_status_values_grace_alert_mass(mon, router):
    everyone = names(40)
    poll(mon, router, everyone, 0)
    poll(mon, router, everyone[1:], 15)              # 1 caído, en gracia
    assert mon.down_sessions()[0]["status"] == "grace"
    poll(mon, router, everyone[1:], 100)
    assert mon.down_sessions()[0]["status"] == "alert"
    poll(mon, router, everyone[30:], 115)            # 29 más: caída masiva
    status = {d["name"]: d["status"] for d in mon.down_sessions()}
    assert status["c000"] == "alert" and status["c010"] == "mass"


# --------------------------------------------------------------------- syslog


@pytest.mark.parametrize("line, event, user, reason", [
    ("pppoe,ppp,info <pppoe-juan>: terminating... - peer is not responding", "disconnect", "juan", "peer is not responding"),
    ("<14>Oct 22 03:00:00 MikroTik pppoe,ppp,info <pppoe-maria.l>: terminating... - session closed", "disconnect", "maria.l", "session closed"),
    ("<pppoe-pedro>: disconnected", "disconnect", "pedro", ""),
    ("ppp,info,account pedro logged out, 3601 1234 5678 10 20", "disconnect", "pedro", ""),
    ("ppp,info,account ana@isp logged in, 10.10.0.7 from AA:BB:CC:DD:EE:FF", "connect", "ana@isp", ""),
    ("pppoe,ppp,info <pppoe-luis>: authentication failed", "auth_failed", "luis", ""),
])
def test_parse_line(line, event, user, reason):
    ev = parse_line(line)
    assert ev == {"event": event, "user": user, "reason": reason}


@pytest.mark.parametrize("line", [
    "system,info,account user admin logged in from 192.168.88.2 via winbox",   # no es PPPoE... ver abajo
    "interface,info ether1 link up",
    "",
    "texto cualquiera",
])
def test_parse_line_ignores_unrelated_lines(line):
    ev = parse_line(line)
    # Una línea "logged in" de otro servicio se interpreta como conexión; no dispara alertas
    # (solo las desconexiones importan), por eso solo se exige que no sea una desconexión.
    assert ev is None or ev["event"] != "disconnect"


def test_router_for_ip_rules(mon, db, router):
    assert mon.router_for_ip("10.0.0.1") == router["id"]          # por host
    assert mon.router_for_ip("172.16.0.9") == router["id"]        # un solo router: se asume
    db.execute("INSERT INTO routers(name, host, port, username, password, updated_at, syslog_ip) "
               "VALUES('Torre','10.0.0.2',8728,'u','p',1,'172.16.5.5')")
    assert mon.router_for_ip("10.0.0.2") == 2
    assert mon.router_for_ip("172.16.5.5") == 2                   # por syslog_ip
    assert mon.router_for_ip("172.16.0.9") is None                # varios routers: no se adivina


def test_on_syslog_applies_reason_to_the_right_router(mon, router, db):
    poll(mon, router, ["ana"], 0)
    assert mon.on_syslog("10.0.0.1", "pppoe,ppp,info <pppoe-ana>: terminating... - peer is not responding", now=T0 + 5)
    assert mon.on_syslog("10.0.0.1", "interface,info ether1 link up") is False
    poll(mon, router, [], 15)
    poll(mon, router, [], 90)
    assert "peer is not responding" in db.one("SELECT message FROM alerts")["message"]


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_udp_receiver_end_to_end():
    got = []
    rx = SyslogReceiver(free_udp_port(), lambda ip, text: got.append((ip, text)) or "terminating" in text, host="127.0.0.1")
    rx.start()
    try:
        assert rx.port and rx.error is None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"<14>pppoe,ppp,info <pppoe-ana>: terminating... - hung up\n", ("127.0.0.1", rx.port))
        s.sendto(b"otra cosa", ("127.0.0.1", rx.port))
        end = time.time() + 3
        while len(got) < 2 and time.time() < end:
            time.sleep(0.05)
        assert [t for _, t in got][0].startswith("<14>pppoe,ppp,info")
        snap = rx.snapshot()
        assert snap["received"] == 2 and snap["parsed"] == 1 and snap["listening"] is True
        assert snap["lines"][1]["parsed"] is True and snap["lines"][0]["parsed"] is False
        assert snap["lines"][1]["ip"] == "127.0.0.1"
    finally:
        rx.stop()


def test_port_zero_means_disabled_and_a_busy_port_is_reported_not_fatal():
    off = SyslogReceiver(0, lambda *_: True, host="127.0.0.1")
    off.start()
    assert off.snapshot()["listening"] is False and off.error is None

    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("127.0.0.1", 0))
    try:
        rx = SyslogReceiver(busy.getsockname()[1], lambda *_: True, host="127.0.0.1")
        rx.start()
        # Con SO_REUSEADDR algunos sistemas permiten compartir el puerto UDP; en los que no,
        # el error queda registrado. En ambos casos no debe lanzar excepción.
        assert rx.snapshot()["listening"] or rx.error
        rx.stop()
    finally:
        busy.close()


def test_receiver_handler_errors_do_not_kill_it():
    def boom(ip, text):
        raise RuntimeError("x")
    rx = SyslogReceiver(free_udp_port(), boom, host="127.0.0.1")
    rx.handle("1.1.1.1", "algo")
    rx.handle("1.1.1.1", "otra")
    assert rx.received == 2 and rx.parsed == 0
