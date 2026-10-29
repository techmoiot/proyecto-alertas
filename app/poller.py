"""Lectura de los MikroTik por la API de RouterOS y supervisión de los hilos."""

import logging
import ssl
import threading
import time
from typing import Any, Callable

from librouteros import connect
from librouteros.exceptions import TrapError
from librouteros.query import Key, Or

log = logging.getLogger("mkmonitor.poller")

FIELDS = ("name", "type", "running", "disabled", "rx-byte", "tx-byte", "comment")
PPP_FIELDS = ("name", "service", "address", "caller-id", "uptime")
SECRET_FIELDS = ("name", "service", "profile", "disabled", "comment",
                 "last-logged-out", "last-caller-id", "last-disconnect-reason")
SECRETS_EVERY_S = 60
PRUNE_EVERY_S = 3600


def split_list(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


class RouterClient:
    """Cliente de la API de RouterOS 7 (puerto 8728, o 8729 con SSL)."""

    def __init__(self, router: dict[str, Any], timeout: float = 5.0):
        self.router = router
        self.timeout = timeout
        self.api = None
        self._filter_trusted = False

    def connect(self) -> None:
        r = self.router
        kwargs: dict[str, Any] = {
            "port": int(r["port"]),
            "timeout": self.timeout,
            "encoding": "utf-8",
        }
        if r.get("use_ssl"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl_wrapper"] = ctx.wrap_socket
        self.api = connect(r["host"], r["username"], r["password"], **kwargs)

    def fetch_interfaces(self) -> list[dict[str, Any]]:
        """Lee solo las interfaces pedidas (tipo ether por defecto).

        El filtro se hace en el router, así no se bajan las miles de interfaces
        PPPoE dinámicas en cada lectura.
        """
        types = split_list(self.router.get("types")) or ["ether"]
        include = split_list(self.router.get("include"))
        query = Key("type").In(*types)
        if include:
            query = Or(query, Key("name").In(*include))
        keys = [Key(k) for k in FIELDS]
        path = self.api.path("interface")
        try:
            rows = list(path.select(*keys).where(query))
        except TrapError:
            rows = []  # el router rechazó la consulta: se cae al filtro local
        if not rows and not self._filter_trusted:
            # Sin resultados con el filtro del router: se confirma leyendo todo una vez.
            rows = list(path.select(*keys))
        elif rows:
            self._filter_trusted = True
        wanted_types, wanted_names = set(types), set(include)
        return [r for r in rows if r.get("type") in wanted_types or r.get("name") in wanted_names]

    def fetch_vlans(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Interfaces VLAN y direcciones IP del router (para detectar los PON)."""
        vlans = list(self.api.path("interface", "vlan").select(
            Key("name"), Key("vlan-id"), Key("interface"), Key("disabled")))
        addrs = list(self.api.path("ip", "address").select(
            Key("address"), Key("interface"), Key("disabled")))
        return vlans, addrs

    def fetch_pppoe_active(self) -> list[dict[str, Any]]:
        """Sesiones PPPoE activas (/ppp/active).

        No se filtra en el router a propósito: si el filtro fallara en silencio,
        la lista vendría vacía y parecería que se cayeron todos los clientes.
        """
        keys = [Key(k) for k in PPP_FIELDS]
        rows = list(self.api.path("ppp", "active").select(*keys))
        return [r for r in rows if str(r.get("service", "")).lower() == "pppoe"]

    def fetch_pppoe_secrets(self) -> list[dict[str, Any]]:
        """Usuarios PPPoE registrados (/ppp/secret): permiten saber cuáles NO están conectados."""
        keys = [Key(k) for k in SECRET_FIELDS]
        rows = list(self.api.path("ppp", "secret").select(*keys))
        return [r for r in rows if str(r.get("service", "any")).lower() in ("pppoe", "any", "")]

    def close(self) -> None:
        if self.api is not None:
            try:
                self.api.close()
            except Exception:  # noqa: BLE001
                pass
            self.api = None


class Worker(threading.Thread):
    """Un hilo por MikroTik: lee cada `poll_interval` segundos."""

    def __init__(self, router: dict, db, engine, factory: Callable[[dict], Any], pppoe=None):
        super().__init__(name=f"poll-{router['name']}", daemon=True)
        self.router = router
        self.db = db
        self.engine = engine
        self.factory = factory
        self.pppoe = pppoe
        self._last_pppoe = 0.0
        self._last_secrets = 0.0
        self._pppoe_on = False
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def _pppoe_step(self, client) -> None:
        if self.pppoe is None:
            return
        s = self.db.get_settings()
        enabled = bool(self.router.get("pppoe_enabled", 1)) and bool(s["pppoe_enabled"])
        if not enabled:
            if self._pppoe_on:
                self.pppoe.forget_router(self.router["id"])
                self._pppoe_on = False
            return
        now = time.time()
        if now - self._last_pppoe < s["pppoe_poll_s"]:
            return
        self._last_pppoe = now
        self._pppoe_on = True
        try:
            rows = client.fetch_pppoe_active()
        except TrapError as exc:
            # El router contestó con error (p. ej. sin permiso): no es una caída del router.
            self.pppoe.process_error(self.router, exc)
            return
        self.pppoe.process_active(self.router, rows, now)
        fetch_secrets = getattr(client, "fetch_pppoe_secrets", None)
        if fetch_secrets is not None and now - self._last_secrets >= SECRETS_EVERY_S:
            self._last_secrets = now
            try:
                self.pppoe.process_secrets(self.router, fetch_secrets(), now)
            except TrapError as exc:
                self.pppoe.process_secrets_error(self.router, exc)
        drain = getattr(client, "drain_log", None)
        if drain is not None:  # modo demo: el "log" viene del cliente simulado
            for user, event, reason in drain():
                self.pppoe.on_log_event(self.router["id"], user, event, reason, now)

    def run(self) -> None:
        client = None
        while not self._stop_evt.is_set():
            started = time.time()
            try:
                if client is None:
                    client = self.factory(self.router)
                    client.connect()
                rows = client.fetch_interfaces()
                self.engine.process_poll(self.router, rows)
                self._pppoe_step(client)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] fallo de lectura: %s", self.router["name"], exc)
                self.engine.process_failure(self.router, exc)
                if client is not None:
                    client.close()
                    client = None
            interval = max(2.0, float(self.db.get_settings()["poll_interval"]))
            self._stop_evt.wait(max(0.5, interval - (time.time() - started)))
        if client is not None:
            client.close()


class Poller:
    def __init__(self, db, engine, factory: Callable[[dict], Any] = RouterClient, pppoe=None,
                 topology=None, remote=None, backup=None):
        self.db = db
        self.engine = engine
        self.factory = factory
        self.pppoe = pppoe
        self.topology = topology
        self.remote = remote
        self.backup = backup
        self.workers: dict[int, Worker] = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = 0.0

    def start(self) -> None:
        self.sync()
        self._thread = threading.Thread(target=self._supervise, name="supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            for w in self.workers.values():
                w.stop()
            self.workers.clear()

    def sync(self) -> None:
        """Arranca, reinicia o detiene hilos según la tabla de routers."""
        with self._lock:
            rows = {r["id"]: r for r in self.db.query("SELECT * FROM routers WHERE enabled=1")}
            for rid, worker in list(self.workers.items()):
                row = rows.get(rid)
                if row is None or row["updated_at"] != worker.router["updated_at"]:
                    worker.stop()
                    del self.workers[rid]
                    if row is None:
                        self.engine.forget_router(rid)
                        if self.pppoe is not None:
                            self.pppoe.forget_router(rid)
            for rid, row in rows.items():
                if rid not in self.workers:
                    worker = Worker(row, self.db, self.engine, self.factory, self.pppoe)
                    self.workers[rid] = worker
                    worker.start()

    def _supervise(self) -> None:
        while not self._stop_evt.wait(2.0):
            try:
                self.sync()
                if self.topology is not None:
                    self.topology.evaluate()
                if self.remote is not None:
                    self.remote.tick()
                if self.backup is not None:
                    self.backup.tick()
                if time.time() - self._last_prune >= PRUNE_EVERY_S:
                    self.engine.prune()
                    if self.pppoe is not None:
                        self.pppoe.prune()
                    self._last_prune = time.time()
            except Exception:  # noqa: BLE001
                log.exception("Error en el supervisor")
