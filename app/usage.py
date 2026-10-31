"""Consumo de cada cliente PPPoE: historial para detectar consumo anormal.

Cada `usage_poll_s` segundos (300 por defecto) se leen los contadores de bytes de las interfaces
dinámicas `<pppoe-USUARIO>` de cada MikroTik. De la diferencia entre lecturas sale cuánto bajó y
cuánto subió el cliente, y se suma a dos tablas:

  - `usage_q`: intervalos de 15 minutos, se conservan 3 días (detalle fino).
  - `usage_h`: intervalos de 1 hora, se conservan 35 días (tendencia).

Sentido: «bajada» = lo que el cliente descarga (tx del router hacia él, `tx-byte`);
«subida» = lo que el cliente envía (`rx-byte`).
"""

from __future__ import annotations

import re
import statistics
import threading
import time
from typing import Callable

NAME_RE = re.compile(r"^<pppoe-(.+)>$")
Q_BUCKET_S = 900
H_BUCKET_S = 3600
Q_KEEP_S = 3 * 86400
H_KEEP_S = 35 * 86400
MAX_BYTES_PER_S = 12.5e9            # 100 Gbps: por encima de eso la lectura es basura
WINDOW_HOURS = (6, 24, 72, 168)
GB = 1024 ** 3

UPSERT = ("INSERT INTO {t}(router_id, user, ts, down_b, up_b) VALUES(?,?,?,?,?) "
          "ON CONFLICT(router_id, user, ts) DO UPDATE SET down_b = down_b + excluded.down_b, up_b = up_b + excluded.up_b")


def user_of(name: str) -> str | None:
    m = NAME_RE.match(name or "")
    return m.group(1) if m else None


class UsageMonitor:
    def __init__(self, db, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock
        self._lock = threading.RLock()
        self._last: dict[tuple[int, str], tuple[int, int, float]] = {}   # (router, usuario) -> (rx, tx, instante)
        self._seen_routers: set[int] = set()

    # ------------------------------------------------------------- escritura

    def process(self, router_id: int, rows: list[dict], now: float | None = None) -> int:
        """Procesa una lectura de contadores; devuelve cuántos clientes sumaron consumo."""
        now = self.clock() if now is None else now
        batch_q, batch_h = [], []
        with self._lock:
            first_read = router_id not in self._seen_routers
            current: dict[str, tuple[int, int]] = {}
            for r in rows:
                user = user_of(str(r.get("name", "")))
                if user is None:
                    continue
                try:
                    current[user] = (int(r.get("rx-byte") or 0), int(r.get("tx-byte") or 0))
                except (TypeError, ValueError):
                    continue
            for user, (rx, tx) in current.items():
                prev = self._last.get((router_id, user))
                if prev is None:
                    # Sesión nueva: sus contadores arrancan en 0, así que todo lo leído es consumo.
                    # En la primera lectura tras arrancar MAGI no se sabe desde cuándo, solo se toma la base.
                    up, down, dt = (0, 0, 0.0) if first_read else (rx, tx, 0.0)
                else:
                    p_rx, p_tx, p_t = prev
                    dt = now - p_t
                    if dt <= 0:
                        continue
                    up = rx - p_rx if rx >= p_rx else rx        # contador menor: el cliente se reconectó
                    down = tx - p_tx if tx >= p_tx else tx
                self._last[(router_id, user)] = (rx, tx, now)
                if dt and (up + down) / dt > MAX_BYTES_PER_S:
                    continue
                if up or down:
                    qb, hb = int(now // Q_BUCKET_S) * Q_BUCKET_S, int(now // H_BUCKET_S) * H_BUCKET_S
                    batch_q.append((router_id, user, qb, down, up))
                    batch_h.append((router_id, user, hb, down, up))
            for key in [k for k in self._last if k[0] == router_id and k[1] not in current]:
                del self._last[key]
            self._seen_routers.add(router_id)
        if batch_q:
            self.db.executemany(UPSERT.format(t="usage_q"), batch_q)
            self.db.executemany(UPSERT.format(t="usage_h"), batch_h)
        return len(batch_q)

    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self.db.execute("DELETE FROM usage_q WHERE ts < ?", (now - Q_KEEP_S,))
        self.db.execute("DELETE FROM usage_h WHERE ts < ?", (now - H_KEEP_S,))

    def forget_router(self, router_id: int) -> None:
        with self._lock:
            for key in [k for k in self._last if k[0] == router_id]:
                del self._last[key]
            self._seen_routers.discard(router_id)
        for t in ("usage_q", "usage_h"):
            self.db.execute(f"DELETE FROM {t} WHERE router_id=?", (router_id,))

    # --------------------------------------------------------------- lectura

    @staticmethod
    def _flag(cur: float, prior: list[float], hours: int, factor: float, min_gb: float, min_prior: int):
        """(ratio, anormal): consumo de la ventana actual contra la mediana de las anteriores."""
        if len(prior) < min_prior:
            return None, False
        typical = statistics.median(prior)
        if typical <= 0:
            return None, cur >= min_gb * GB * hours / 24
        ratio = cur / typical
        return ratio, ratio >= factor and cur >= min_gb * GB * hours / 24

    def top(self, hours: int = 24, router_id: int | None = None, q: str = "", limit: int = 50,
            only_abnormal: bool = False) -> dict:
        hours = hours if hours in WINDOW_HOURS else 24
        now = self.clock()
        w = hours * 3600
        windows = max(2, min(8, int(H_KEEP_S // w)))
        s = self.db.get_settings()
        factor, min_gb = float(s["usage_factor"]), float(s["usage_min_gb"])
        min_prior = 3 if hours <= 24 else 2
        sql = ("SELECT router_id, user, k, SUM(down_b) d, SUM(up_b) u, MAX(down_b) pk FROM ("
               "SELECT router_id, user, down_b, up_b, CAST((? - ts) / ? AS INTEGER) AS k FROM usage_h WHERE ts > ?")
        params: list = [now, w, now - windows * w]
        if router_id:
            sql += " AND router_id=?"
            params.append(router_id)
        if q:
            sql += " AND user LIKE ? ESCAPE '\\'"
            params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql += ") GROUP BY router_id, user, k"
        per: dict[tuple[int, str], dict] = {}
        for r in self.db.query(sql, params):
            e = per.setdefault((r["router_id"], r["user"]), {"cur": None, "prior": []})
            if r["k"] == 0:
                e["cur"] = r
            else:
                e["prior"].append(r["d"] + r["u"])
        names = {r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM routers")}
        rows = []
        for (rid, user), e in per.items():
            c = e["cur"]
            if c is None:
                continue
            total = c["d"] + c["u"]
            ratio, abnormal = self._flag(total, e["prior"], hours, factor, min_gb, min_prior)
            rows.append({
                "router_id": rid, "router": names.get(rid, "?"), "user": user,
                "down_b": c["d"], "up_b": c["u"], "total_b": total,
                "avg_down_mbps": round(c["d"] * 8 / w / 1e6, 3), "avg_up_mbps": round(c["u"] * 8 / w / 1e6, 3),
                "peak_down_mbps": round(c["pk"] * 8 / H_BUCKET_S / 1e6, 2),
                "ratio": round(ratio, 2) if ratio is not None else None, "abnormal": abnormal,
                "history_windows": len(e["prior"]),
            })
        rows.sort(key=lambda r: (not r["abnormal"], -r["total_b"]))
        abnormal_n = sum(1 for r in rows if r["abnormal"])
        if only_abnormal:
            rows = [r for r in rows if r["abnormal"]]
        return {"now": now, "hours": hours, "total": len(rows), "abnormal": abnormal_n,
                "factor": factor, "min_gb": min_gb, "rows": rows[:max(1, min(limit, 500))]}

    def client(self, router_id: int, user: str, hours: int = 24) -> dict:
        hours = max(1, min(hours, 35 * 24))
        now = self.clock()
        fine = hours <= 72
        table, bucket = ("usage_q", Q_BUCKET_S) if fine else ("usage_h", H_BUCKET_S)
        since = now - hours * 3600
        pts = self.db.query(
            f"SELECT ts, down_b, up_b FROM {table} WHERE router_id=? AND user=? AND ts > ? ORDER BY ts",
            (router_id, user, since - bucket))
        points = []
        for p in pts:
            d, u = p["down_b"] * 8 / bucket, p["up_b"] * 8 / bucket
            points.append([p["ts"], d, u, d, u])
        # Totales por día (ventanas de 24 h hacia atrás) y comparación con lo habitual.
        days = self.db.query(
            "SELECT CAST((? - ts) / 86400 AS INTEGER) AS k, SUM(down_b) d, SUM(up_b) u FROM usage_h "
            "WHERE router_id=? AND user=? AND ts > ? GROUP BY k ORDER BY k",
            (now, router_id, user, now - 14 * 86400))
        s = self.db.get_settings()
        today = next((d for d in days if d["k"] == 0), {"d": 0, "u": 0})
        prior = [d["d"] + d["u"] for d in days if d["k"] != 0]
        ratio, abnormal = self._flag(today["d"] + today["u"], prior, 24, float(s["usage_factor"]),
                                     float(s["usage_min_gb"]), 3)
        total = self.db.one(
            f"SELECT COALESCE(SUM(down_b),0) d, COALESCE(SUM(up_b),0) u, COALESCE(MAX(down_b),0) pd FROM {table} "
            "WHERE router_id=? AND user=? AND ts > ?", (router_id, user, since - bucket))
        name = self.db.one("SELECT name FROM routers WHERE id=?", (router_id,))
        return {
            "now": now, "router_id": router_id, "router": name["name"] if name else "?", "user": user,
            "hours": hours, "bucket_s": bucket, "points": points,
            "down_b": total["d"], "up_b": total["u"],
            "peak_down_mbps": round(total["pd"] * 8 / bucket / 1e6, 2),
            "daily": [{"days_ago": d["k"], "down_b": d["d"], "up_b": d["u"]} for d in days],
            "typical_b": statistics.median(prior) if len(prior) >= 3 else None,
            "ratio": round(ratio, 2) if ratio is not None else None, "abnormal": abnormal,
            "factor": float(s["usage_factor"]),
        }
