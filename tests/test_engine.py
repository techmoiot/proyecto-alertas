from app.engine import Engine, Hold, fmt_rate
from conftest import Sim, active_kinds


def test_hold_fires_after_sustain_and_clears_with_hysteresis():
    h = Hold()
    assert h.update(True, False, 0, 30, 30) is None
    assert h.update(True, False, 29, 30, 30) is None
    assert h.update(True, False, 30, 30, 30) == "fire"
    assert h.update(False, False, 40, 30, 30) is None  # ni sobre ni bajo: se mantiene
    assert h.update(False, True, 50, 30, 30) is None
    assert h.update(False, True, 80, 30, 30) == "clear"


def test_hold_resets_when_condition_interrupted():
    h = Hold()
    h.update(True, False, 0, 30, 30)
    h.update(False, True, 20, 30, 30)  # se corta antes de los 30 s
    assert h.update(True, False, 25, 30, 30) is None
    assert h.update(True, False, 54, 30, 30) is None
    assert h.update(True, False, 55, 30, 30) == "fire"


def test_fmt_rate():
    assert fmt_rate(None) == "—"
    assert fmt_rate(950) == "950 bps"
    assert fmt_rate(12_000) == "12 kbps"
    assert fmt_rate(12_500_000) == "12.5 Mbps"
    assert fmt_rate(2_000_000_000) == "2.00 Gbps"


# ------------------------------------------------------------------- link caído


def test_link_down_alert_when_interface_was_up(sim, db, notifier):
    sim.run(30, rx_mbps=10)
    assert active_kinds(db) == []
    sim.step(10, running=False)
    assert active_kinds(db) == ["link_down"]
    assert any("Enlace caído en ether1 (Uplink)" in m for m in notifier.messages)
    sim.step(10, rx_mbps=10)
    assert active_kinds(db) == []
    assert any("Resuelto" in m for m in notifier.messages)


def test_no_link_alert_for_interface_that_never_came_up(sim, db):
    sim.run(60, running=False)
    assert active_kinds(db) == []


def test_no_link_alert_for_disabled_interface(sim, db):
    sim.run(30, rx_mbps=10)
    sim.step(10, running=False, disabled=True)
    assert active_kinds(db) == []


def test_link_alert_can_be_turned_off_per_interface(sim, db):
    sim.run(30, rx_mbps=10)
    db.execute("UPDATE iface_config SET link_alert=0 WHERE iface='ether1'")
    sim.step(10, running=False)
    assert active_kinds(db) == []


def test_link_down_alert_clears_if_disabled_while_down(sim, db):
    sim.run(30, rx_mbps=10)
    sim.step(10, running=False)
    assert active_kinds(db) == ["link_down"]
    sim.step(10, running=False, disabled=True)
    assert active_kinds(db) == []


# ------------------------------------------------------------------ tope fijo


def test_threshold_fires_after_sustain_and_clears(sim, db):
    db.set_settings({"default_max_rx_mbps": 800, "threshold_sustain_s": 30, "threshold_clear_s": 30,
                     "anomaly_enabled": 0})
    sim.run(20, rx_mbps=100)
    sim.run(20, rx_mbps=900)           # solo 20 s sobre el tope: todavía no
    assert active_kinds(db) == []
    sim.run(20, rx_mbps=900)           # ya pasaron 30 s
    assert active_kinds(db) == ["threshold_rx"]
    row = db.one("SELECT * FROM alerts WHERE kind='threshold_rx'")
    assert "entrada (RX)" in row["message"] and "tope 800 Mbps" in row["message"]
    sim.run(20, rx_mbps=750)           # entre 90% y 100% del tope: sigue activa (histéresis)
    assert active_kinds(db) == ["threshold_rx"]
    sim.run(40, rx_mbps=100)
    assert active_kinds(db) == []


def test_threshold_per_interface_overrides_default(sim, db):
    db.set_settings({"default_max_rx_mbps": 0, "default_max_tx_mbps": 0, "threshold_sustain_s": 20,
                     "anomaly_enabled": 0})
    sim.run(10, rx_mbps=10)
    db.execute("UPDATE iface_config SET max_tx_mbps=300 WHERE iface='ether1'")
    sim.run(40, rx_mbps=10, tx_mbps=500)
    assert active_kinds(db) == ["threshold_tx"]


def test_threshold_zero_on_interface_disables_global_limit(sim, db):
    db.set_settings({"default_max_rx_mbps": 100, "threshold_sustain_s": 20, "anomaly_enabled": 0})
    sim.run(10, rx_mbps=10)
    db.execute("UPDATE iface_config SET max_rx_mbps=0 WHERE iface='ether1'")
    sim.run(60, rx_mbps=500)
    assert active_kinds(db) == []


# ----------------------------------------------------------- consumo anormal


ANOMALY = {"anomaly_enabled": 1, "anomaly_factor": 3, "anomaly_min_mbps": 50, "anomaly_sustain_s": 60,
           "anomaly_clear_s": 60, "baseline_warmup_s": 300, "baseline_tau_s": 3600,
           "default_max_rx_mbps": 0, "default_max_tx_mbps": 0}


def test_anomaly_fires_on_spike_after_warmup(sim, db):
    db.set_settings(ANOMALY)
    sim.run(600, rx_mbps=100)                  # aprende ~100 Mbps
    base = sim.engine.states[(sim.router["id"], "ether1")].base_rx
    assert 90e6 < base < 110e6
    sim.run(50, rx_mbps=400)                   # 50 s: todavía no
    assert active_kinds(db) == []
    sim.run(30, rx_mbps=400)
    assert active_kinds(db) == ["anomaly_rx"]
    msg = db.one("SELECT message FROM alerts WHERE kind='anomaly_rx'")["message"]
    assert "Consumo anormal" in msg and "promedio habitual" in msg


def test_anomaly_baseline_is_frozen_during_spike_and_alert_clears(sim, db):
    db.set_settings(ANOMALY)
    sim.run(600, rx_mbps=100)
    sim.run(600, rx_mbps=400)                  # 10 min de pico sostenido
    st = sim.engine.states[(sim.router["id"], "ether1")]
    assert st.base_rx < 110e6                  # el pico no contaminó el promedio
    assert active_kinds(db) == ["anomaly_rx"]
    sim.run(120, rx_mbps=100)
    assert active_kinds(db) == []


def test_no_anomaly_during_warmup(sim, db):
    db.set_settings({**ANOMALY, "baseline_warmup_s": 1800})
    sim.run(300, rx_mbps=100)
    sim.run(200, rx_mbps=900)
    assert active_kinds(db) == []


def test_no_anomaly_below_minimum_consumption(sim, db):
    db.set_settings(ANOMALY)
    sim.run(600, rx_mbps=2)
    sim.run(300, rx_mbps=40)                   # 20× el promedio pero bajo 50 Mbps
    assert active_kinds(db) == []


def test_anomaly_can_be_disabled(sim, db):
    db.set_settings({**ANOMALY, "anomaly_enabled": 0})
    sim.run(600, rx_mbps=100)
    sim.run(300, rx_mbps=900)
    assert active_kinds(db) == []


def test_anomaly_tx_is_independent_from_rx(sim, db):
    db.set_settings(ANOMALY)
    sim.run(600, rx_mbps=100, tx_mbps=100)
    sim.run(120, rx_mbps=100, tx_mbps=500)
    assert active_kinds(db) == ["anomaly_tx"]


# --------------------------------------------------------- contadores y varios


def test_counter_reset_does_not_create_negative_rates_or_alerts(sim, db):
    db.set_settings({"default_max_rx_mbps": 100, "threshold_sustain_s": 10, "anomaly_enabled": 0})
    sim.run(30, rx_mbps=10)
    sim.counters["ether1"] = [0, 0]            # el router se reinició
    sim.step(10, rx_mbps=10)
    st = sim.engine.states[(sim.router["id"], "ether1")]
    assert st.rx_bps is None
    sim.step(10, rx_mbps=10)
    assert 9e6 < st.rx_bps < 11e6
    assert active_kinds(db) == []


def test_rates_are_computed_in_bits_per_second(sim):
    sim.step(10, rx_mbps=0)
    sim.step(10, rx_mbps=250, tx_mbps=40)
    st = sim.engine.states[(sim.router["id"], "ether1")]
    assert abs(st.rx_bps - 250e6) < 1e3
    assert abs(st.tx_bps - 40e6) < 1e3


def test_monitor_off_never_alerts_and_resolves_existing(sim, db):
    sim.run(30, rx_mbps=10)
    sim.step(10, running=False)
    assert active_kinds(db) == ["link_down"]
    db.execute("UPDATE iface_config SET monitor=0 WHERE iface='ether1'")
    sim.step(10, running=False)
    assert active_kinds(db) == []
    sim.run(30, running=False)
    assert active_kinds(db) == []


def test_router_down_after_consecutive_failures_and_recovery(engine, router, db, notifier):
    for i in range(2):
        engine.process_failure(router, "timed out", now=100 + i)
    assert active_kinds(db) == []
    engine.process_failure(router, "timed out", now=103)
    assert active_kinds(db) == ["router_down"]
    assert engine.snapshot()[router["id"]]["reachable"] is False
    engine.process_failure(router, "timed out", now=104)
    assert db.one("SELECT COUNT(*) AS n FROM alerts")["n"] == 1   # no se duplica
    s = Sim(engine, router, start=200)
    s.step(10, rx_mbps=1)
    assert active_kinds(db) == []
    assert engine.snapshot()[router["id"]]["reachable"] is True
    assert any("Sin respuesta de la API de Core" in m for m in notifier.messages)


def test_no_rate_computed_across_an_outage(sim, engine, router):
    sim.run(30, rx_mbps=10)
    engine.process_failure(router, "x", now=sim.t + 1)
    sim.counters["ether1"][0] += 10_000_000_000  # se acumuló mucho mientras estaba caído
    sim.step(600, rx_mbps=0)
    st = engine.states[(router["id"], "ether1")]
    assert st.rx_bps is None
    sim.step(10, rx_mbps=10)
    assert 9e6 < st.rx_bps < 11e6


def test_active_alerts_survive_restart_without_duplicates(sim, db, router, notifier):
    sim.run(30, rx_mbps=10)
    sim.step(10, running=False)
    assert active_kinds(db) == ["link_down"]

    engine2 = Engine(db, notifier)            # reinicio del servicio
    s2 = Sim(engine2, router, start=sim.t)
    s2.counters = sim.counters
    s2.step(10, running=False)
    assert db.one("SELECT COUNT(*) AS n FROM alerts")["n"] == 1
    s2.step(10, rx_mbps=10)                   # y se puede resolver después del reinicio
    assert active_kinds(db) == []


def test_baseline_is_persisted_and_reused_after_restart(sim, db, router, notifier):
    db.set_settings(ANOMALY)
    sim.run(900, rx_mbps=100)                 # más de 5 min: se guarda el promedio
    assert db.one("SELECT COUNT(*) AS n FROM baselines")["n"] == 1
    engine2 = Engine(db, notifier)
    s2 = Sim(engine2, router, start=sim.t)
    s2.counters = sim.counters
    s2.step(10, rx_mbps=100)
    s2.step(10, rx_mbps=100)
    st = engine2.states[(router["id"], "ether1")]
    assert 90e6 < st.base_rx < 110e6
    s2.run(100, rx_mbps=500)                  # sin período de aprendizaje: alerta ya
    assert active_kinds(db) == ["anomaly_rx"]


def test_interface_removed_from_router_is_forgotten(sim, engine, router, db):
    sim.run(30, rx_mbps=10)
    sim.step(10, running=False)
    assert active_kinds(db) == ["link_down"]
    engine.process_poll(router, [], now=sim.t + 10)
    assert active_kinds(db) == []
    assert (router["id"], "ether1") not in engine.states


def test_forget_router_resolves_alerts(sim, engine, router, db):
    sim.run(30, rx_mbps=10)
    sim.step(10, running=False)
    engine.forget_router(router["id"])
    assert active_kinds(db) == []
    assert router["id"] not in engine.snapshot()


def test_samples_are_stored_at_configured_interval_and_pruned(sim, db, engine):
    db.set_settings({"sample_every_s": 30, "retention_days": 1})
    sim.run(300, rx_mbps=10)
    n = db.one("SELECT COUNT(*) AS n FROM samples")["n"]
    assert 8 <= n <= 11
    engine.prune(now=sim.t + 3 * 86400)
    assert db.one("SELECT COUNT(*) AS n FROM samples")["n"] == 0


def test_label_defaults_to_interface_comment(sim, db, engine, router):
    sim.step(10, rx_mbps=1, comment="WAN principal")
    cfg = db.one("SELECT label FROM iface_config WHERE iface='ether1'")
    assert cfg["label"] == "WAN principal"
    live = engine.snapshot()[router["id"]]["ifaces"]["ether1"]
    assert live["label"] == "WAN principal"
