"""Base de datos SQLite (un solo archivo) con acceso serializado por un lock."""

import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS routers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 8728,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    use_ssl INTEGER NOT NULL DEFAULT 0,
    types TEXT NOT NULL DEFAULT 'ether',
    include TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    pppoe_enabled INTEGER NOT NULL DEFAULT 1,
    syslog_ip TEXT NOT NULL DEFAULT '',
    UNIQUE(host, port)
);

CREATE TABLE IF NOT EXISTS iface_config(
    router_id INTEGER NOT NULL,
    iface TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    monitor INTEGER NOT NULL DEFAULT 1,
    link_alert INTEGER NOT NULL DEFAULT 1,
    max_rx_mbps REAL,
    max_tx_mbps REAL,
    ever_up INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(router_id, iface)
);

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    router_id INTEGER NOT NULL,
    iface TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    value REAL,
    started_at REAL NOT NULL,
    resolved_at REAL,
    acked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_resolved ON alerts(resolved_at);
CREATE INDEX IF NOT EXISTS idx_alerts_started ON alerts(started_at);

CREATE TABLE IF NOT EXISTS samples(
    router_id INTEGER NOT NULL,
    iface TEXT NOT NULL,
    ts REAL NOT NULL,
    rx_bps REAL,
    tx_bps REAL
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples(router_id, iface, ts);

CREATE TABLE IF NOT EXISTS usage_q(
    router_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    ts INTEGER NOT NULL,
    down_b INTEGER NOT NULL DEFAULT 0,
    up_b INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(router_id, user, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_usage_q_ts ON usage_q(ts);

CREATE TABLE IF NOT EXISTS usage_h(
    router_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    ts INTEGER NOT NULL,
    down_b INTEGER NOT NULL DEFAULT 0,
    up_b INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(router_id, user, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_usage_h_ts ON usage_h(ts);

CREATE TABLE IF NOT EXISTS baselines(
    router_id INTEGER NOT NULL,
    iface TEXT NOT NULL,
    rx_bps REAL NOT NULL,
    tx_bps REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(router_id, iface)
);

CREATE TABLE IF NOT EXISTS pppoe_sessions(
    router_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    caller_id TEXT NOT NULL DEFAULT '',
    since REAL NOT NULL,
    last_seen REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    alerted INTEGER NOT NULL DEFAULT 0,
    was_up_s REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(router_id, name)
);
CREATE INDEX IF NOT EXISTS idx_pppoe_state ON pppoe_sessions(router_id, state);

CREATE TABLE IF NOT EXISTS pppoe_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    router_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    user TEXT NOT NULL,
    event TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pppoe_events_ts ON pppoe_events(ts);
CREATE INDEX IF NOT EXISTS idx_pppoe_events_user ON pppoe_events(router_id, user, ts);

CREATE TABLE IF NOT EXISTS wg_peers(
    router_id INTEGER PRIMARY KEY,
    private_key TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    ip TEXT NOT NULL UNIQUE,
    activated INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    created_at REAL NOT NULL,
    last_login REAL
);

CREATE TABLE IF NOT EXISTS olts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS pons(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    olt_id INTEGER NOT NULL,
    pos INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    vlan INTEGER NOT NULL DEFAULT 0,
    inet_cidr TEXT NOT NULL DEFAULT '',
    tv_cidr TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pons_olt ON pons(olt_id, pos);
"""

# Columnas agregadas después de la primera versión: se suman a bases ya existentes.
MIGRATIONS = [
    ("routers", "pppoe_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("routers", "syslog_ip", "TEXT NOT NULL DEFAULT ''"),
    ("routers", "cfg_backup", "INTEGER NOT NULL DEFAULT 0"),
    ("routers", "ssh_port", "INTEGER NOT NULL DEFAULT 22"),
    ("olts", "cards", "INTEGER NOT NULL DEFAULT 1"),
    ("olts", "ports_per_card", "INTEGER NOT NULL DEFAULT 0"),
    ("olts", "router_id", "INTEGER NOT NULL DEFAULT 0"),
    ("pons", "card", "INTEGER NOT NULL DEFAULT 1"),
]

# Valores por defecto de la configuración global. Se editan desde el panel.
DEFAULT_SETTINGS: dict[str, float] = {
    "poll_interval": 10,         # segundos entre lecturas a cada MikroTik
    "default_max_rx_mbps": 0,    # tope fijo de entrada por interfaz (0 = apagado)
    "default_max_tx_mbps": 0,    # tope fijo de salida por interfaz (0 = apagado)
    "threshold_sustain_s": 30,   # segundos sobre el tope antes de alertar
    "threshold_clear_s": 30,     # segundos bajo el 90% del tope antes de resolver
    "anomaly_enabled": 1,        # detección de consumo anormal contra el promedio
    "anomaly_factor": 3.0,       # veces el promedio habitual que se considera anormal
    "anomaly_min_mbps": 50,      # no alertar por anomalía debajo de este consumo
    "anomaly_sustain_s": 60,     # segundos sostenido antes de alertar
    "anomaly_clear_s": 60,       # segundos normal antes de resolver
    "baseline_warmup_s": 1800,   # tiempo de aprendizaje inicial del promedio
    "baseline_tau_s": 3600,      # constante de tiempo del promedio móvil
    "router_down_after": 3,      # lecturas fallidas seguidas para dar el router por caído
    "sample_every_s": 30,        # cada cuánto se guarda un punto del historial
    "retention_days": 7,         # días de historial de tráfico que se conservan
    "pppoe_enabled": 1,          # monitoreo de desconexiones PPPoE (1 = sí)
    "pppoe_poll_s": 15,          # cada cuántos segundos se lee /ppp/active
    "pppoe_grace_s": 60,         # un cliente debe seguir caído este tiempo para alertar
    "pppoe_mass_threshold": 20,  # desconexiones en la ventana para tratarlo como caída masiva
    "pppoe_mass_window_s": 60,   # ventana (s) para detectar la caída masiva
    "pon_down_pct": 50,          # % de clientes de un PON caídos para considerar el sector caído
    "pon_min_clients": 4,        # un PON con menos clientes conocidos no genera alerta de sector
    "pon_down_after_s": 30,      # segundos sostenido antes de alertar (y antes de resolver)
    "pppoe_expire_h": 24,        # horas que una alerta de cliente caído queda activa
    "backup_enabled": 1,         # respaldos automáticos (1 = sí)
    "backup_every_h": 24,        # cada cuántas horas se respalda la base de MAGI y la config de los routers
    "backup_keep": 14,           # copias de la base de MAGI que se conservan
    "router_backup_keep": 30,    # versiones distintas de la config de cada router que se conservan
    "usage_enabled": 1,          # historial de consumo por cliente PPPoE (1 = sí)
    "usage_poll_s": 300,         # cada cuántos segundos se leen los contadores de cada cliente
    "usage_factor": 3.0,         # veces su consumo habitual (mediana de días anteriores) que se considera anormal
    "usage_min_gb": 5.0,         # no marcar como anormal por debajo de estos GB en 24 h
}


class DB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: list) -> None:
        if not seq:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, seq)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- configuración global -------------------------------------------------

    def get_settings(self) -> dict[str, float]:
        out = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key, value FROM settings"):
            key = row["key"]
            if key in out:
                try:
                    out[key] = float(row["value"])
                except ValueError:
                    pass
        return out

    def set_settings(self, values: dict[str, Any]) -> dict[str, float]:
        clean: dict[str, float] = {}
        for key, raw in values.items():
            if key not in DEFAULT_SETTINGS:
                raise ValueError(f"Ajuste desconocido: {key}")
            try:
                num = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"Valor inválido para {key}") from None
            if num < 0 or num != num or num == float("inf"):
                raise ValueError(f"El valor de {key} no puede ser negativo")
            clean[key] = num
        if "poll_interval" in clean and clean["poll_interval"] < 2:
            raise ValueError("poll_interval mínimo: 2 segundos")
        if "anomaly_factor" in clean and clean["anomaly_factor"] < 1.1:
            raise ValueError("anomaly_factor mínimo: 1.1")
        if "usage_poll_s" in clean and clean["usage_poll_s"] < 60:
            raise ValueError("usage_poll_s mínimo: 60 segundos")
        if "usage_factor" in clean and clean["usage_factor"] < 1.1:
            raise ValueError("usage_factor mínimo: 1.1")
        if "baseline_tau_s" in clean and clean["baseline_tau_s"] < 10:
            raise ValueError("baseline_tau_s mínimo: 10 segundos")
        with self._lock:
            for key, num in clean.items():
                self._conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, repr(num)),
                )
        return self.get_settings()
