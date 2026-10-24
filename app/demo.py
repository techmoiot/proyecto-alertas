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
    "pppoe_poll_s": 4,
    "pppoe_grace_s": 20,
    "pppoe_mass_threshold": 15,
    "pppoe_mass_window_s": 30,
    "pon_down_after_s": 10,
    "pon_min_clients": 4,
    "pon_down_pct": 50,
}

# Cantidad de clientes PPPoE simulados por router.
PPPOE_CLIENTS = {"Demo-Core": 120}

# OLT de ejemplo: 6 PON de 20 clientes (16 de Internet + 4 de TV). Cada PON tiene su VLAN
# y una red /24 partida en dos /25: la primera para Internet y la segunda para TV.
DEMO_PONS = 6
CLIENTS_PER_PON = 20
INET_PER_PON = 16

# Escenario PPPoE (se repite cada 6 minutos): (clientes, desde s, hasta s, motivo)
PPPOE_SCENARIO = [
    ([7], 30, 45, "session closed"),                  # se reconecta rápido: no genera alerta
    ([12], 60, 200, "peer is not responding"),        # caída individual con alerta
    ([20], 120, 170, "session closed"),
    (list(range(21, 51)), 250, 300, "peer is not responding"),   # caída masiva (30 clientes)
    (list(range(101, 121)), 305, 352, "peer is not responding"),  # se cae el PON 6 completo
]


def demo_address(i: int) -> str:
    """IP del cliente i (1..120): su PON define la red, y los últimos 4 de cada PON son de TV."""
    pon, k = divmod(i - 1, CLIENTS_PER_PON)
    host = 1 + k if k < INET_PER_PON else 129 + (k - INET_PER_PON)
    return f"10.{101 + pon}.0.{host}"

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
    if not db.query("SELECT 1 FROM olts"):
        oid = db.execute("INSERT INTO olts(name) VALUES('OLT-Demo')").lastrowid
        db.executemany(
            "INSERT INTO pons(olt_id, pos, name, vlan, inet_cidr, tv_cidr) VALUES(?,?,?,?,?,?)",
            [(oid, n, f"PON {n + 1}", 101 + n, f"10.{101 + n}.0.0/25", f"10.{101 + n}.0.128/25")
             for n in range(DEMO_PONS)],
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
        self.n_clients = PPPOE_CLIENTS.get(router["name"], 0)
        self._offline: dict[int, str] = {}
        self._log: list[tuple[str, str, str]] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    @staticmethod
    def _client_name(i: int) -> str:
        return f"cliente{i:03d}"

    def fetch_pppoe_active(self) -> list[dict]:
        now = time.time()
        phase = (now - self.t0) % 360
        offline: dict[int, str] = {}
        for clients, start, end, reason in PPPOE_SCENARIO:
            if start <= phase < end:
                for c in clients:
                    if c <= self.n_clients:
                        offline[c] = reason
        # Genera las líneas de "log" de los cambios, como lo haría el MikroTik.
        for c, reason in offline.items():
            if c not in self._offline:
                self._log.append((self._client_name(c), "disconnect", reason))
        for c in self._offline:
            if c not in offline:
                self._log.append((self._client_name(c), "connect", ""))
        self._offline = offline
        up_s = int(now - self.t0)
        return [
            {
                "name": self._client_name(i),
                "service": "pppoe",
                "address": demo_address(i),
                "caller-id": f"AA:BB:CC:00:{i // 256:02X}:{i % 256:02X}",
                "uptime": f"{(up_s // 3600) + i}h{(up_s % 3600) // 60}m",
            }
            for i in range(1, self.n_clients + 1)
            if i not in offline
        ]

    def fetch_vlans(self):
        vlans = [{"name": f"vlan{101 + n}", "vlan-id": 101 + n, "interface": "sfp-sfpplus2", "disabled": False}
                 for n in range(DEMO_PONS)]
        addrs = [{"address": f"10.{101 + n}.0.1/24", "interface": f"vlan{101 + n}", "disabled": False}
                 for n in range(DEMO_PONS)]
        vlans.append({"name": "vlan-gestion", "vlan-id": 10, "interface": "ether5", "disabled": False})
        addrs.append({"address": "192.168.10.1/24", "interface": "vlan-gestion", "disabled": False})
        return vlans, addrs

    def drain_log(self) -> list[tuple[str, str, str]]:
        out, self._log = self._log, []
        return out

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
