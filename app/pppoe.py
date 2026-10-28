"""Monitor de desconexiones PPPoE.

La fuente de verdad es la lista de sesiones activas del MikroTik (`/ppp/active`):
en cada lectura se compara con la anterior y quien desapareció se marca como caído.
El log (syslog) solo aporta el MOTIVO de la desconexión; si no llega o no se
entiende, la alerta sale igual, sin motivo.

Para no inundar de alertas:
  * período de gracia: un cliente que se reconecta enseguida no genera alerta
    (reconectarse es normal en PPPoE);
  * caída masiva: si muchos clientes se caen a la vez (un uplink, un reinicio) se
    abre UNA alerta crítica y las alertas individuales de ese evento se suprimen;
  * expiración: una alerta de cliente caído se da por vencida pasadas N horas
    (los clientes dados de baja no quedan alertando para siempre).
"""

import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from .syslog import parse_line

ALERT_NONE, ALERT_FIRED, ALERT_SUPPRESSED, ALERT_EXPIRED = 0, 1, 2, 3

REASON_MAX_AGE_S = 300
CHECKPOINT_EVERY_S = 300
FORGET_DOWN_AFTER_S = 60 * 86400

_UPTIME = re.compile(r"(\d+)([wdhms])")
_UNIT = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_uptime(value) -> float:
    """'1d2h3m4s' (formato de RouterOS) -> segundos."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(sum(int(n) * _UNIT[u] for n, u in _UPTIME.findall(str(value or ""))))


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60} min"
    return f"{s // 86400} d {(s % 86400) // 3600} h"


def explain_reason(reason: str) -> str:
    """Traducción de los motivos más comunes del log; el resto se muestra tal cual."""
    low = reason.lower()
    if "peer is not responding" in low:
        return "el equipo del cliente no responde (apagado o enlace cortado)"
    if "session closed" in low:
        return "el cliente cerró la sesión"
    return ""


@dataclass
class Session:
    name: str
    state: str = "up"             # 'up' | 'down'
    address: str = ""
    caller_id: str = ""           # MAC del cliente
    since: float = 0.0            # momento del último cambio de estado
    last_seen: float = 0.0
    uptime_s: float = 0.0
    reason: str = ""
    alerted: int = ALERT_NONE
    was_up_s: float = 0.0         # cuánto estuvo conectado antes de caerse


class PPPoEMonitor:
    def __init__(self, db, engine, clock=time.time):
        self.db = db
        self.engine = engine
        self.clock = clock
        self._lock = threading.RLock()
        self.sessions: dict[int, dict[str, Session]] = {}
        self.stats: dict[int, dict] = {}
        self.reasons: dict[int, dict[str, tuple[float, str]]] = {}
        self.secrets: dict[int, dict] = {}        # rid -> {"ts", "rows": {nombre: fila}, "error"}
        self.recent_down: dict[int, deque] = {}
        self.mass: dict[int, dict] = {}
        self._last_checkpoint: dict[int, float] = {}
        self._load()

    # ---------------------------------------------------------------- arranque

    def _load(self) -> None:
        for r in self.db.query("SELECT * FROM pppoe_sessions"):
            self.sessions.setdefault(r["router_id"], {})[r["name"]] = Session(
                name=r["name"], state=r["state"], address=r["address"], caller_id=r["caller_id"],
                since=r["since"], last_seen=r["last_seen"], reason=r["reason"],
                alerted=r["alerted"], was_up_s=r["was_up_s"],
            )
        for r in self.db.query(
            "SELECT router_id, started_at, value FROM alerts "
            "WHERE kind='pppoe_mass' AND resolved_at IS NULL"
        ):
            self.mass[r["router_id"]] = {"start": r["started_at"], "total": int(r["value"] or 0)}

    # ------------------------------------------------------------------ lectura

    def process_active(self, router: dict, rows: list[dict], now: float | None = None) -> None:
        now = self.clock() if now is None else now
        rid = router["id"]
        with self._lock:
            s = self.db.get_settings()
            sess = self.sessions.setdefault(rid, {})
            events: list[tuple] = []
            dirty: dict[str, Session] = {}

            current: dict[str, dict] = {}
            for r in rows:
                name = r.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if str(r.get("service", "pppoe")).lower() != "pppoe":
                    continue
                current[name] = r

            # Clientes que están conectados ahora.
            for name, r in current.items():
                address = str(r.get("address") or "")
                mac = str(r.get("caller-id") or "")
                uptime = parse_uptime(r.get("uptime"))
                se = sess.get(name)
                if se is None:
                    se = Session(name=name, state="up", address=address, caller_id=mac,
                                 since=now, last_seen=now, uptime_s=uptime)
                    sess[name] = se
                    dirty[name] = se
                elif se.state == "down":
                    self._reconnected(rid, se, now, events)
                    dirty[name] = se
                se.address = address or se.address
                se.caller_id = mac or se.caller_id
                se.last_seen = now
                se.uptime_s = uptime

            # Clientes que estaban conectados y ya no aparecen.
            downs: list[Session] = []
            stale_after = 3 * max(s["pppoe_poll_s"], 1)
            for name, se in sess.items():
                if se.state == "up" and name not in current:
                    se.state = "down"
                    # Si el servidor estuvo apagado, la caída fue en algún momento
                    # desde la última vez que se lo vio.
                    gap = now - se.last_seen
                    se.since = se.last_seen if gap > stale_after else now
                    se.was_up_s = se.uptime_s + min(gap, stale_after)
                    se.alerted = ALERT_NONE
                    se.reason = self._reason_for(rid, name, now)
                    downs.append(se)
                    dirty[name] = se
                    events.append((rid, se.since, name, "disconnect", se.reason))

            self._detect_mass(router, sess, downs, s, now, dirty)
            counts = self._tick(router, sess, s, now, dirty)
            self.stats[rid] = {**counts, "mass": rid in self.mass, "last_poll": now, "error": None}

            if dirty:
                self._persist(rid, list(dirty.values()))
            if events:
                self.db.executemany(
                    "INSERT INTO pppoe_events(router_id, ts, user, event, reason) VALUES(?,?,?,?,?)",
                    events,
                )
            if now - self._last_checkpoint.get(rid, 0) >= CHECKPOINT_EVERY_S:
                self._persist(rid, list(sess.values()))
                self._last_checkpoint[rid] = now

    def process_error(self, router: dict, error) -> None:
        with self._lock:
            st = self.stats.setdefault(
                router["id"],
                {"online": 0, "down": 0, "suppressed": 0, "mass": False, "last_poll": None},
            )
            st["error"] = str(error)[:200]

    # ------------------------------------------------------------ lógica de estado

    def _reconnected(self, rid: int, se: Session, now: float, events: list) -> None:
        if se.alerted == ALERT_FIRED:
            self.engine.clear_alert(rid, se.name, "pppoe_down", now, quiet=True)
        se.state = "up"
        se.since = now
        se.reason = ""
        se.alerted = ALERT_NONE
        events.append((rid, now, se.name, "connect", ""))

    def _detect_mass(self, router, sess, downs, s, now, dirty) -> None:
        rid = router["id"]
        window = s["pppoe_mass_window_s"]
        threshold = int(s["pppoe_mass_threshold"])
        times = self.recent_down.setdefault(rid, deque())
        times.extend([now] * len(downs))
        while times and now - times[0] > window:
            times.popleft()
        if rid in self.mass:
            for se in downs:
                se.alerted = ALERT_SUPPRESSED
            return
        if threshold > 0 and len(times) >= threshold:
            total = len(times)
            self.mass[rid] = {"start": now, "total": total}
            for se in sess.values():
                if se.state == "down" and se.alerted == ALERT_NONE and now - se.since <= window:
                    se.alerted = ALERT_SUPPRESSED
                    dirty[se.name] = se
            self.engine.fire_alert(
                router, "", "pppoe_mass", "critical",
                f"Caída masiva de PPPoE en {router['name']}: {total} clientes se desconectaron "
                f"en menos de {int(window)} s", float(total), now,
            )

    def _tick(self, router, sess, s, now, dirty) -> dict:
        """Alertas individuales (tras la gracia), expiración y fin de la caída masiva."""
        rid = router["id"]
        grace = s["pppoe_grace_s"]
        expire = s["pppoe_expire_h"] * 3600
        online = down = suppressed = 0
        for se in sess.values():
            if se.state == "up":
                online += 1
                continue
            down += 1
            if se.alerted == ALERT_SUPPRESSED:
                suppressed += 1
            elif se.alerted == ALERT_NONE and now - se.since >= grace:
                se.alerted = ALERT_FIRED
                dirty[se.name] = se
                self.engine.fire_alert(router, se.name, "pppoe_down", "warning",
                                       self._message(se), None, now)
            elif se.alerted == ALERT_FIRED and expire and now - se.since >= expire:
                se.alerted = ALERT_EXPIRED
                dirty[se.name] = se
                self.engine.clear_alert(rid, se.name, "pppoe_down", now, quiet=True)

        mass = self.mass.get(rid)
        if mass is not None:
            threshold = int(s["pppoe_mass_threshold"])
            if suppressed < max(1, threshold // 2):
                self.engine.clear_alert(rid, "", "pppoe_mass", now)
                del self.mass[rid]
            elif expire and now - mass["start"] >= expire:
                self.engine.clear_alert(rid, "", "pppoe_mass", now, quiet=True)
                del self.mass[rid]
            else:
                self.engine.update_alert(
                    rid, "", "pppoe_mass",
                    f"Caída masiva de PPPoE en {router['name']}: {suppressed} clientes siguen sin "
                    f"conexión (se cayeron {mass['total']} en menos de "
                    f"{int(s['pppoe_mass_window_s'])} s)",
                    float(suppressed),
                )

        horizon = now - REASON_MAX_AGE_S
        reasons = self.reasons.get(rid)
        if reasons:
            for user in [u for u, (ts, _) in reasons.items() if ts < horizon]:
                del reasons[user]
        return {"online": online, "down": down, "suppressed": suppressed}

    # ------------------------------------------------- usuarios registrados (secrets)

    def process_secrets(self, router: dict, rows: list[dict], now: float | None = None) -> None:
        now = self.clock() if now is None else now
        data = {str(r.get("name", "")): r for r in rows if r.get("name")}
        with self._lock:
            self.secrets[router["id"]] = {"ts": now, "rows": data, "error": ""}

    def process_secrets_error(self, router: dict, error) -> None:
        with self._lock:
            old = self.secrets.get(router["id"], {"ts": 0, "rows": {}})
            self.secrets[router["id"]] = {**old, "error": f"No se pudo leer /ppp/secret: {error}"}

    def _registered_rows(self, rid: int) -> list[dict]:
        """Usuarios registrados que NO tienen sesión activa ahora."""
        snap = self.secrets.get(rid)
        if not snap:
            return []
        sess = self.sessions.get(rid, {})
        out = []
        for name, r in snap["rows"].items():
            se = sess.get(name)
            if se is not None and se.state == "up":
                continue
            disabled = str(r.get("disabled", "")).lower() in ("true", "yes", "1")
            last_out = str(r.get("last-logged-out") or "")
            reason = str(r.get("last-disconnect-reason") or "") or (se.reason if se else "") \
                or self.reasons.get(rid, {}).get(name, (0, ""))[1]
            out.append({
                "router_id": rid, "name": name,
                "kind": "disabled" if disabled else "offline",
                "never": not last_out and se is None,
                "last_logged_out": last_out, "profile": str(r.get("profile") or ""),
                "comment": str(r.get("comment") or ""),
                "caller_id": str(r.get("last-caller-id") or (se.caller_id if se else "")),
                "reason": reason, "reason_es": explain_reason(reason) if reason else "",
                "down_since": se.since if se is not None else None,
                "status": self._status(se) if se is not None else "",
            })
        return out

    def registered(self, router_id: int | None = None, q: str = "", kind: str = "all",
                   limit: int = 500) -> dict:
        """Lista de registrados sin conexión (con motivo) y los totales por router."""
        q = q.strip().lower()
        rows, totals = [], {}
        with self._lock:
            for rid, snap in self.secrets.items():
                if router_id is not None and rid != router_id:
                    continue
                mine = self._registered_rows(rid)
                totals[rid] = {
                    "registered": len(snap["rows"]), "offline": sum(1 for r in mine if r["kind"] == "offline"),
                    "disabled": sum(1 for r in mine if r["kind"] == "disabled"),
                    "ts": snap["ts"], "error": snap.get("error", ""),
                }
                rows += [r for r in mine if (kind == "all" or r["kind"] == kind)
                         and (not q or q in r["name"].lower() or q in r["comment"].lower())]
        # Primero los que acaban de caer; después por nombre.
        rows.sort(key=lambda r: (r["down_since"] is None, -(r["down_since"] or 0), r["name"].lower()))
        return {"total": len(rows), "rows": rows[:limit], "routers": totals}

    def _reason_for(self, rid: int, name: str, now: float) -> str:
        item = self.reasons.get(rid, {}).get(name)
        return item[1] if item and now - item[0] <= REASON_MAX_AGE_S else ""

    def _message(self, se: Session) -> str:
        parts = [f"Cliente PPPoE «{se.name}» desconectado"]
        if se.reason:
            hint = explain_reason(se.reason)
            parts.append(f"motivo: {hint} ({se.reason})" if hint else f"motivo: {se.reason}")
        where = " · ".join(p for p in (se.address, f"MAC {se.caller_id}" if se.caller_id else "") if p)
        if where:
            parts.append(where)
        if se.was_up_s >= 60:
            parts.append(f"estuvo conectado {fmt_dur(se.was_up_s)}")
        return " — ".join(parts)

    # ------------------------------------------------------------------ syslog

    def on_log_event(
        self, rid: int, user: str, event: str, reason: str = "", ts: float | None = None
    ) -> None:
        """Evento del log del MikroTik: aporta el motivo de la desconexión."""
        ts = self.clock() if ts is None else ts
        if event != "disconnect" or not reason:
            return
        with self._lock:
            self.reasons.setdefault(rid, {})[user] = (ts, reason)
            se = self.sessions.get(rid, {}).get(user)
            if se is not None and se.state == "down" and not se.reason \
                    and ts - se.since <= REASON_MAX_AGE_S:
                se.reason = reason
                self._persist(rid, [se])
                self.db.execute(
                    "UPDATE pppoe_events SET reason=? WHERE router_id=? AND user=? "
                    "AND event='disconnect' AND reason='' AND ts>=?",
                    (reason, rid, user, se.since - 5),
                )
                if se.alerted == ALERT_FIRED:
                    self.engine.update_alert(rid, user, "pppoe_down", self._message(se))

    def router_for_ip(self, ip: str) -> int | None:
        """Router al que pertenece un datagrama de syslog (por IP de origen)."""
        rows = self.db.query("SELECT id, host, syslog_ip FROM routers WHERE enabled=1")
        for r in rows:
            if ip == r["host"] or (r["syslog_ip"] and ip == r["syslog_ip"]):
                return r["id"]
        # Con un solo router, se asume que el log es suyo aunque salga por otra IP.
        return rows[0]["id"] if len(rows) == 1 else None

    def on_syslog(self, ip: str, text: str, now: float | None = None) -> bool:
        ev = parse_line(text)
        if ev is None:
            return False
        rid = self.router_for_ip(ip)
        if rid is None:
            return False
        self.on_log_event(rid, ev["user"], ev["event"], ev["reason"], now)
        return True

    # --------------------------------------------------------------- persistencia

    def _persist(self, rid: int, sessions: list[Session]) -> None:
        self.db.executemany(
            "INSERT INTO pppoe_sessions(router_id, name, state, address, caller_id, since, "
            "last_seen, reason, alerted, was_up_s) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(router_id, name) DO UPDATE SET state=excluded.state, "
            "address=excluded.address, caller_id=excluded.caller_id, since=excluded.since, "
            "last_seen=excluded.last_seen, reason=excluded.reason, alerted=excluded.alerted, "
            "was_up_s=excluded.was_up_s",
            [(rid, s.name, s.state, s.address, s.caller_id, s.since, s.last_seen, s.reason,
              s.alerted, s.was_up_s) for s in sessions],
        )

    def forget_router(self, rid: int) -> None:
        with self._lock:
            self.engine.clear_kinds(rid, ("pppoe_down", "pppoe_mass"))
            for table in ("pppoe_sessions", "pppoe_events"):
                self.db.execute(f"DELETE FROM {table} WHERE router_id=?", (rid,))
            for store in (self.sessions, self.stats, self.reasons, self.recent_down,
                          self.mass, self._last_checkpoint, self.secrets):
                store.pop(rid, None)

    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        s = self.db.get_settings()
        with self._lock:
            self.db.execute("DELETE FROM pppoe_events WHERE ts < ?", (now - s["retention_days"] * 86400,))
            cutoff = now - FORGET_DOWN_AFTER_S
            for rid, sess in self.sessions.items():
                for name in [n for n, se in sess.items() if se.state == "down" and se.since < cutoff]:
                    del sess[name]
                    self.db.execute("DELETE FROM pppoe_sessions WHERE router_id=? AND name=?", (rid, name))

    # ------------------------------------------------------------- vistas para la API

    def summary(self) -> dict[int, dict]:
        with self._lock:
            return {rid: dict(st) for rid, st in self.stats.items()}

    @staticmethod
    def _status(se: Session) -> str:
        if se.state == "up":
            return "up"
        if se.alerted == ALERT_EXPIRED:
            return "exp"             # no volvió en mucho tiempo (probable baja)
        if se.alerted == ALERT_SUPPRESSED:
            return "mass"            # parte de una caída masiva
        if se.alerted == ALERT_NONE:
            return "grace"           # en período de gracia, todavía sin alerta
        return "alert"

    def all_sessions(self) -> list[tuple[int, Session, str]]:
        """Todas las sesiones conocidas con su estado, para armar el mapa de la red."""
        with self._lock:
            return [(rid, se, self._status(se)) for rid, sess in self.sessions.items() for se in sess.values()]

    def _view(self, rid: int, se: Session, now: float) -> dict:
        status = self._status(se)
        return {
            "router_id": rid, "name": se.name, "state": se.state, "status": status,
            "address": se.address, "caller_id": se.caller_id, "since": se.since,
            "reason": se.reason, "reason_es": explain_reason(se.reason) if se.reason else "",
            "was_up_s": se.was_up_s if se.state == "down" else se.uptime_s,
        }

    def down_sessions(self, router_id: int | None = None, q: str = "", limit: int = 500) -> list[dict]:
        q = q.strip().lower()
        now = self.clock()
        with self._lock:
            out = [
                self._view(rid, se, now)
                for rid, sess in self.sessions.items()
                if router_id is None or rid == router_id
                for se in sess.values()
                if se.state == "down" and (not q or q in se.name.lower())
            ]
        out.sort(key=lambda v: v["since"], reverse=True)
        return out[:limit]

    def search(self, q: str, limit: int = 50) -> list[dict]:
        q = q.strip().lower()
        if len(q) < 2:
            return []
        now = self.clock()
        with self._lock:
            hits = [
                self._view(rid, se, now)
                for rid, sess in self.sessions.items()
                for se in sess.values()
                if q in se.name.lower() or q in se.address or q in se.caller_id.lower()
            ]
        hits.sort(key=lambda v: (v["state"] == "up", v["name"].lower()))
        return hits[:limit]
