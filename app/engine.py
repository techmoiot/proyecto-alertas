"""Motor de alertas.

Recibe las lecturas de interfaces de cada MikroTik, calcula la velocidad a
partir de los contadores y decide qué alertas abrir o resolver:

  link_down      interfaz que alguna vez estuvo arriba y ahora no tiene enlace
  threshold_rx   entrada sobre el tope fijo configurado
  threshold_tx   salida sobre el tope fijo configurado
  anomaly_rx     entrada muy por encima de su promedio habitual
  anomaly_tx     salida muy por encima de su promedio habitual
  router_down    el MikroTik no responde por la API

No hace red: es lógica pura sobre la base de datos, por eso se puede probar sin
ningún router.
"""

import math
import threading
import time
from collections import deque
from typing import Any

SPARK_POINTS = 60
BASELINE_PERSIST_EVERY_S = 300
BASELINE_MAX_AGE_S = 3 * 86400
RESOLVED_ALERTS_KEEP_S = 90 * 86400

DIR_ES = {"rx": "entrada (RX)", "tx": "salida (TX)"}


def fmt_rate(bps: float | None) -> str:
    if bps is None:
        return "—"
    if bps >= 1e9:
        return f"{bps / 1e9:.2f} Gbps"
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} Mbps"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f} kbps"
    return f"{bps:.0f} bps"


class Hold:
    """Estado de una condición con tiempos de espera.

    Se dispara cuando `over` se mantiene `fire_after` segundos y se resuelve
    cuando `below` se mantiene `clear_after` segundos (histéresis).
    """

    __slots__ = ("active", "t_over", "t_below")

    def __init__(self, active: bool = False):
        self.active = active
        self.t_over: float | None = None
        self.t_below: float | None = None

    def update(
        self, over: bool, below: bool, now: float, fire_after: float, clear_after: float
    ) -> str | None:
        if not self.active:
            self.t_below = None
            if over:
                if self.t_over is None:
                    self.t_over = now
                if now - self.t_over >= fire_after:
                    self.active = True
                    self.t_over = None
                    return "fire"
            else:
                self.t_over = None
        else:
            self.t_over = None
            if below:
                if self.t_below is None:
                    self.t_below = now
                if now - self.t_below >= clear_after:
                    self.active = False
                    self.t_below = None
                    return "clear"
            else:
                self.t_below = None
        return None


class IfaceState:
    def __init__(self, name: str, now: float):
        self.name = name
        self.type = ""
        self.comment = ""
        self.running: bool | None = None
        self.disabled = False
        self.last_ts: float | None = None
        self.last_rx = 0
        self.last_tx = 0
        self.dt = 0.0
        self.rx_bps: float | None = None
        self.tx_bps: float | None = None
        self.base_rx: float | None = None
        self.base_tx: float | None = None
        self.first_ts = now
        self.holds: dict[str, Hold] = {}
        self.hist: deque[tuple[float | None, float | None]] = deque(maxlen=SPARK_POINTS)
        self.last_store = 0.0
        self.last_persist = now


class Engine:
    def __init__(self, db, notifier=None, clock=time.time):
        self.db = db
        self.notifier = notifier
        self.clock = clock
        self._lock = threading.RLock()
        self.states: dict[tuple[int, str], IfaceState] = {}
        self.live: dict[int, dict[str, Any]] = {}
        self.fails: dict[int, int] = {}
        # (router_id, iface, kind) -> {"id", "message", "router"}
        self.active: dict[tuple[int, str, str], dict[str, Any]] = {}
        self._load_active()

    # ------------------------------------------------------------------ alertas

    def _load_active(self) -> None:
        rows = self.db.query(
            "SELECT a.id, a.router_id, a.iface, a.kind, a.message, r.name AS router "
            "FROM alerts a LEFT JOIN routers r ON r.id = a.router_id "
            "WHERE a.resolved_at IS NULL"
        )
        for row in rows:
            self.active[(row["router_id"], row["iface"], row["kind"])] = {
                "id": row["id"],
                "message": row["message"],
                "router": row["router"] or f"#{row['router_id']}",
            }

    def _notify(self, text: str) -> None:
        if self.notifier is not None:
            self.notifier.send(text)

    def _fire(
        self,
        router: dict,
        iface: str,
        kind: str,
        severity: str,
        message: str,
        value: float | None,
        now: float,
    ) -> None:
        key = (router["id"], iface, kind)
        if key in self.active:
            return
        cur = self.db.execute(
            "INSERT INTO alerts(router_id, iface, kind, severity, message, value, started_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (router["id"], iface, kind, severity, message, value, now),
        )
        self.active[key] = {"id": cur.lastrowid, "message": message, "router": router["name"]}
        icon = "🔴" if severity == "critical" else "🟠"
        self._notify(f"{icon} [{router['name']}] {message}")

    def _clear(self, rid: int, iface: str, kind: str, now: float, quiet: bool = False) -> None:
        info = self.active.pop((rid, iface, kind), None)
        if info is None:
            return
        self.db.execute(
            "UPDATE alerts SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
            (now, info["id"]),
        )
        if not quiet:
            self._notify(f"✅ [{info['router']}] Resuelto: {info['message']}")

    def _hold(self, st: IfaceState, rid: int, kind: str) -> Hold:
        hold = st.holds.get(kind)
        if hold is None:
            hold = Hold(active=(rid, st.name, kind) in self.active)
            st.holds[kind] = hold
        return hold

    # ----------------------------------------------------------------- lecturas

    def process_poll(self, router: dict, rows: list[dict], now: float | None = None) -> None:
        now = self.clock() if now is None else now
        rid = router["id"]
        with self._lock:
            s = self.db.get_settings()
            self.fails[rid] = 0
            self._clear(rid, "", "router_down", now)

            cfgs = {
                r["iface"]: r
                for r in self.db.query("SELECT * FROM iface_config WHERE router_id=?", (rid,))
            }
            live: dict[str, Any] = {
                "reachable": True,
                "last_ok": now,
                "last_poll": now,
                "error": None,
                "ifaces": {},
            }
            seen: set[str] = set()
            samples: list[tuple] = []

            for row in rows:
                name = row.get("name")
                if not name or not isinstance(name, str):
                    continue
                seen.add(name)
                cfg = cfgs.get(name)
                if cfg is None:
                    label = str(row.get("comment") or "")[:60]
                    self.db.execute(
                        "INSERT OR IGNORE INTO iface_config(router_id, iface, label) VALUES(?,?,?)",
                        (rid, name, label),
                    )
                    cfg = {
                        "label": label,
                        "monitor": 1,
                        "link_alert": 1,
                        "max_rx_mbps": None,
                        "max_tx_mbps": None,
                        "ever_up": 0,
                    }
                    cfgs[name] = cfg
                st = self.states.get((rid, name))
                if st is None:
                    st = self._new_state(rid, name, now, s)
                self._update_iface(router, st, row, cfg, s, now, samples)
                live["ifaces"][name] = self._public(rid, st, cfg, s, now)

            # Interfaces que ya no existen en el router: se olvidan.
            for key in [k for k in self.states if k[0] == rid and k[1] not in seen]:
                for kind in (
                    "link_down",
                    "threshold_rx",
                    "threshold_tx",
                    "anomaly_rx",
                    "anomaly_tx",
                ):
                    self._clear(rid, key[1], kind, now, quiet=True)
                del self.states[key]

            self.live[rid] = live
            self.db.executemany(
                "INSERT INTO samples(router_id, iface, ts, rx_bps, tx_bps) VALUES(?,?,?,?,?)",
                samples,
            )

    def process_failure(self, router: dict, error: Any, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        rid = router["id"]
        with self._lock:
            s = self.db.get_settings()
            n = self.fails.get(rid, 0) + 1
            self.fails[rid] = n
            live = self.live.setdefault(
                rid,
                {"reachable": None, "last_ok": None, "last_poll": now, "error": None, "ifaces": {}},
            )
            live["last_poll"] = now
            live["error"] = str(error)[:200]
            # Al reconectar no se calcula velocidad sobre el hueco de tiempo.
            for key, st in self.states.items():
                if key[0] == rid:
                    st.last_ts = None
                    st.rx_bps = st.tx_bps = None
            if n >= s["router_down_after"]:
                live["reachable"] = False
                self._fire(
                    router,
                    "",
                    "router_down",
                    "critical",
                    f"Sin respuesta de la API de {router['name']} ({router['host']}): {live['error']}",
                    None,
                    now,
                )

    def forget_router(self, rid: int) -> None:
        now = self.clock()
        with self._lock:
            for key in [k for k in self.active if k[0] == rid]:
                self._clear(key[0], key[1], key[2], now, quiet=True)
            for key in [k for k in self.states if k[0] == rid]:
                del self.states[key]
            self.live.pop(rid, None)
            self.fails.pop(rid, None)

    # --------------------------------------------------------- lógica por interfaz

    def _new_state(self, rid: int, name: str, now: float, s: dict) -> IfaceState:
        st = IfaceState(name, now)
        saved = self.db.one(
            "SELECT rx_bps, tx_bps, updated_at FROM baselines WHERE router_id=? AND iface=?",
            (rid, name),
        )
        if saved and now - saved["updated_at"] < BASELINE_MAX_AGE_S:
            st.base_rx = saved["rx_bps"]
            st.base_tx = saved["tx_bps"]
            # Ya hay promedio aprendido: se evita el período de aprendizaje.
            st.first_ts = now - s["baseline_warmup_s"]
        self.states[(rid, name)] = st
        return st

    def _update_iface(
        self,
        router: dict,
        st: IfaceState,
        row: dict,
        cfg: dict,
        s: dict,
        now: float,
        samples: list,
    ) -> None:
        rid = router["id"]
        st.type = str(row.get("type") or "")
        st.comment = str(row.get("comment") or "")
        st.running = bool(row.get("running"))
        st.disabled = bool(row.get("disabled"))

        # Velocidad a partir de los contadores acumulados.
        rx, tx = row.get("rx-byte"), row.get("tx-byte")
        if isinstance(rx, int) and isinstance(tx, int):
            if st.last_ts is None:
                st.last_ts, st.last_rx, st.last_tx = now, rx, tx
            elif now - st.last_ts >= 0.5:
                if rx >= st.last_rx and tx >= st.last_tx:
                    st.dt = now - st.last_ts
                    st.rx_bps = (rx - st.last_rx) * 8 / st.dt
                    st.tx_bps = (tx - st.last_tx) * 8 / st.dt
                else:
                    # Contadores reiniciados (reinicio del router): se descarta la muestra.
                    st.rx_bps = st.tx_bps = None
                st.last_ts, st.last_rx, st.last_tx = now, rx, tx

        if st.running and not cfg["ever_up"]:
            self.db.execute(
                "UPDATE iface_config SET ever_up=1 WHERE router_id=? AND iface=?",
                (rid, st.name),
            )
            cfg["ever_up"] = 1

        if not cfg["monitor"]:
            for kind in list(st.holds):
                self._clear(rid, st.name, kind, now, quiet=True)
            st.holds.clear()
            return

        shown = f"{st.name} ({cfg['label'] or st.comment})" if (cfg["label"] or st.comment) else st.name

        # --- enlace caído
        down = (not st.running) and (not st.disabled) and bool(cfg["link_alert"]) and bool(cfg["ever_up"])
        recovered = bool(st.running) or st.disabled or not cfg["link_alert"]
        act = self._hold(st, rid, "link_down").update(down, recovered, now, 0, 0)
        if act == "fire":
            self._fire(router, st.name, "link_down", "critical", f"Enlace caído en {shown}", None, now)
        elif act == "clear":
            self._clear(rid, st.name, "link_down", now)

        # --- tope fijo y consumo anormal, por dirección
        for direction, bps, cfg_max in (
            ("rx", st.rx_bps, cfg["max_rx_mbps"]),
            ("tx", st.tx_bps, cfg["max_tx_mbps"]),
        ):
            limit = self._limit(cfg_max, s[f"default_max_{direction}_mbps"])
            self._threshold(router, st, shown, direction, bps, limit, s, now)
            self._anomaly(router, st, shown, direction, bps, s, now)

        # --- historial
        if st.rx_bps is not None and st.tx_bps is not None:
            st.hist.append((st.rx_bps, st.tx_bps))
            if now - st.last_store >= s["sample_every_s"]:
                samples.append((rid, st.name, now, st.rx_bps, st.tx_bps))
                st.last_store = now
        if (
            st.base_rx is not None
            and st.base_tx is not None
            and now - st.last_persist >= BASELINE_PERSIST_EVERY_S
        ):
            self.db.execute(
                "INSERT INTO baselines(router_id, iface, rx_bps, tx_bps, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(router_id, iface) DO UPDATE SET "
                "rx_bps=excluded.rx_bps, tx_bps=excluded.tx_bps, updated_at=excluded.updated_at",
                (rid, st.name, st.base_rx, st.base_tx, now),
            )
            st.last_persist = now

    @staticmethod
    def _limit(own: float | None, default: float) -> float:
        """Tope efectivo en Mbps: el de la interfaz si lo tiene, si no el global. 0 = apagado."""
        value = default if own is None else own
        return float(value or 0)

    def _threshold(self, router, st, shown, direction, bps, limit, s, now) -> None:
        kind = f"threshold_{direction}"
        hold = self._hold(st, router["id"], kind)
        if limit <= 0 or not st.running:
            over, below = False, True
        elif bps is None:
            return
        else:
            over = bps >= limit * 1e6
            below = bps < limit * 1e6 * 0.9
        act = hold.update(over, below, now, s["threshold_sustain_s"], s["threshold_clear_s"])
        if act == "fire":
            self._fire(
                router,
                st.name,
                kind,
                "warning",
                f"Consumo alto de {DIR_ES[direction]} en {shown}: {fmt_rate(bps)} (tope {limit:g} Mbps)",
                bps,
                now,
            )
        elif act == "clear":
            self._clear(router["id"], st.name, kind, now)

    def _anomaly(self, router, st, shown, direction, bps, s, now) -> None:
        kind = f"anomaly_{direction}"
        attr = f"base_{direction}"
        hold = self._hold(st, router["id"], kind)
        if not s["anomaly_enabled"] or not st.running:
            act = hold.update(False, True, now, 0, 0)
            if act == "clear":
                self._clear(router["id"], st.name, kind, now)
            return
        if bps is None:
            return
        base = getattr(st, attr)
        if base is None:
            setattr(st, attr, bps)
            return

        factor = s["anomaly_factor"]
        min_bps = s["anomaly_min_mbps"] * 1e6
        learning = (now - st.first_ts) < s["baseline_warmup_s"]
        suspicious = bps >= factor * base

        over = (not learning) and bps >= min_bps and suspicious
        below = bps < min_bps or bps < factor * base * 0.8
        act = hold.update(over, below, now, s["anomaly_sustain_s"], s["anomaly_clear_s"])

        # El promedio solo aprende con tráfico "normal": se congela durante picos.
        if (learning or (not hold.active and not suspicious)) and st.dt > 0:
            tau = min(s["baseline_tau_s"], max(now - st.first_ts, st.dt))
            alpha = 1 - math.exp(-st.dt / tau)
            setattr(st, attr, base + alpha * (bps - base))

        if act == "fire":
            ratio = bps / base if base > 0 else float("inf")
            self._fire(
                router,
                st.name,
                kind,
                "warning",
                f"Consumo anormal de {DIR_ES[direction]} en {shown}: {fmt_rate(bps)}, "
                f"{ratio:.1f}× su promedio habitual ({fmt_rate(base)})",
                bps,
                now,
            )
        elif act == "clear":
            self._clear(router["id"], st.name, kind, now)

    # ------------------------------------------------------------- vista pública

    def _public(self, rid: int, st: IfaceState, cfg: dict, s: dict, now: float) -> dict:
        learning = (now - st.first_ts) < s["baseline_warmup_s"]
        kinds = [
            k
            for k in (
                "link_down",
                "threshold_rx",
                "threshold_tx",
                "anomaly_rx",
                "anomaly_tx",
            )
            if (rid, st.name, k) in self.active
        ]
        max_rx = self._limit(cfg["max_rx_mbps"], s["default_max_rx_mbps"])
        max_tx = self._limit(cfg["max_tx_mbps"], s["default_max_tx_mbps"])
        return {
            "name": st.name,
            "type": st.type,
            "label": cfg["label"] or st.comment,
            "running": st.running,
            "disabled": st.disabled,
            "monitored": bool(cfg["monitor"]),
            "ever_up": bool(cfg["ever_up"]),
            "rx_bps": st.rx_bps,
            "tx_bps": st.tx_bps,
            "base_rx_bps": st.base_rx,
            "base_tx_bps": st.base_tx,
            "max_rx_mbps": max_rx or None,
            "max_tx_mbps": max_tx or None,
            "learning": bool(s["anomaly_enabled"]) and learning,
            "spark": [[round(a or 0), round(b or 0)] for a, b in st.hist],
            "alerts": kinds,
        }

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Copia del estado en vivo de todos los routers (para la API web)."""
        with self._lock:
            return {
                rid: {**live, "ifaces": dict(live["ifaces"])} for rid, live in self.live.items()
            }

    # -------------------------------------------------------------- mantenimiento

    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        s = self.db.get_settings()
        self.db.execute(
            "DELETE FROM samples WHERE ts < ?", (now - s["retention_days"] * 86400,)
        )
        self.db.execute(
            "DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?",
            (now - RESOLVED_ALERTS_KEEP_S,),
        )
