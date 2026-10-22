"""Modo demo: routers simulados con tráfico, picos y caídas de enlace.

Se activa con MKMON_DEMO=1. Sirve para ver el panel y probar las alertas sin
tener un MikroTik conectado.
"""

import math
import random
import time

DEMO_SETTINGS = {
    "poll_interval": 2,
    "baseline_warmup_s": 20,
    "anomaly_sustain_s": 8,
    "anomaly_clear_s": 8,
    "threshold_sustain_s": 6,
    "threshold_clear_s": 6,
    "default_max_rx_mbps": 900,
    "sample_every_s": 5,
}

# nombre -> interfaces: (nombre, tipo, comentario, rx base Mbps, tx base Mbps)
PROFILES = {
    "Demo-Core": [
        ("ether1", "ether", "Upstream A", 300, 40),
        ("sfp-sfpplus1", "ether", "Upstream B", 450, 60),
        ("ether2", "ether", "Switch distribución", 120, 220),
        ("ether3", "ether", "Servidores", 20, 35),
        ("ether5", "ether", "Sin uso", 0, 0),
    ],
    "Demo-Torre": [
        ("ether1", "ether", "Enlace a Core", 180, 90),
        ("sfp1", "ether", "Radio sector norte", 60, 140),
        ("ether2", "ether", "Radio sector sur", 45, 100),
    ],
}


def apply_demo(db) -> None:
    db.set_settings(DEMO_SETTINGS)
    now = time.time()
    for i, name in enumerate(PROFILES, start=1):
        db.execute(
            "INSERT OR IGNORE INTO routers(name, host, port, username, password, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (name, f"demo-{i}", 8728, "demo", "demo", now),
        )


class DemoClient:
    """Imita a RouterClient con contadores que suben según un perfil de tráfico."""

    def __init__(self, router: dict):
        self.router = router
        self.profile = PROFILES.get(router["name"], PROFILES["Demo-Torre"])
        self.t0 = time.time()
        self.last = self.t0
        self.rng = random.Random(router["id"])
        self.counters = {p[0]: [0, 0] for p in self.profile}

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def fetch_interfaces(self) -> list[dict]:
        now = time.time()
        dt, self.last = now - self.last, now
        phase = (now - self.t0) % 360  # el escenario se repite cada 6 minutos
        rows = []
        for idx, (name, typ, comment, rx_mbps, tx_mbps) in enumerate(self.profile):
            running = rx_mbps > 0 or tx_mbps > 0
            rx_f = tx_f = 1.0
            wave = 1 + 0.15 * math.sin(now / 40 + idx)
            if name == "ether1" and 100 <= phase < 190:
                rx_f = 4.5  # pico de consumo anormal
            if name.startswith("sfp") and 200 <= phase < 260:
                running = False  # cae el enlace
            if running:
                noise = 1 + self.rng.uniform(-0.05, 0.05)
                rx_bps = rx_mbps * 1e6 * rx_f * wave * noise
                tx_bps = tx_mbps * 1e6 * tx_f * wave * noise
            else:
                rx_bps = tx_bps = 0
            c = self.counters[name]
            c[0] += int(rx_bps * dt / 8)
            c[1] += int(tx_bps * dt / 8)
            rows.append(
                {
                    "name": name,
                    "type": typ,
                    "comment": comment,
                    "running": running,
                    "disabled": False,
                    "rx-byte": c[0],
                    "tx-byte": c[1],
                }
            )
        return rows
