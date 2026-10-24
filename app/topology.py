"""Topología OLT → PON → pools (Internet / TV) → clientes, y alerta de sector caído.

Cada PON tiene su VLAN y dos pools (dos /25 de la misma red: Internet y TV).
Un cliente se ubica en su PON por la IP que le dio el PPPoE: no hace falta
configurar nada por cliente. Los clientes se leen de `PPPoEMonitor`.

Un "sector caído" es un PON donde una parte grande de los clientes conocidos
está sin conexión a la vez (corte de fibra, puerto PON, VLAN, splitter...).
"""

import bisect
import ipaddress
import threading
import time
from typing import Any

KIND = "pon_down"
OLT_ROUTER_ID = 0        # las alertas de sector no pertenecen a un router; se guardan con id 0
POOL_NAMES = ("inet", "tv")


class ConfigError(ValueError):
    pass


def _parse_cidr(text: str, label: str) -> ipaddress.IPv4Network | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise ConfigError(f"{label}: «{text}» no es una red válida (ejemplo: 10.10.1.0/25)") from None
    if net.version != 4:
        raise ConfigError(f"{label}: solo se admite IPv4")
    if net.num_addresses < 4:
        raise ConfigError(f"{label}: la red es demasiado chica")
    return net


def validate_pons(pons: list[dict]) -> list[dict]:
    """Normaliza los PON de una OLT y verifica que los pools no se pisen."""
    if not pons:
        raise ConfigError("La OLT necesita al menos un PON")
    if len(pons) > 128:
        raise ConfigError("Máximo 128 PON por OLT")
    out, nets = [], []
    for n, p in enumerate(pons, start=1):
        name = str(p.get("name") or f"PON {n}").strip()[:40] or f"PON {n}"
        try:
            vlan = int(p.get("vlan") or 0)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: la VLAN debe ser un número") from None
        if not 0 <= vlan <= 4094:
            raise ConfigError(f"{name}: la VLAN debe estar entre 1 y 4094")
        row = {"name": name, "vlan": vlan, "inet_cidr": "", "tv_cidr": ""}
        for key, label in (("inet_cidr", "Internet"), ("tv_cidr", "TV")):
            net = _parse_cidr(p.get(key, ""), f"{name} / {label}")
            if net is not None:
                row[key] = str(net)
                nets.append((net, f"{name} / {label}"))
        out.append(row)
    names = [r["name"].lower() for r in out]
    if len(set(names)) != len(names):
        raise ConfigError("Hay dos PON con el mismo nombre")
    for i, (a, la) in enumerate(nets):
        for b, lb in nets[i + 1:]:
            if a.overlaps(b):
                raise ConfigError(f"Los pools se pisan: {la} ({a}) y {lb} ({b})")
    return out


def detect_pons(vlans: list[dict], addresses: list[dict],
                vlan_from: int | None = None, vlan_to: int | None = None) -> dict:
    """Arma la lista de PON a partir de las VLAN y direcciones IP que tiene un MikroTik.

    Cada interfaz VLAN con una IPv4 pasa a ser un PON; su red se parte en dos mitades
    (Internet y TV) si es /24 o más grande. Es una propuesta: se revisa antes de guardar.
    """
    by_if: dict[str, ipaddress.IPv4Interface] = {}
    for a in addresses:
        if a.get("disabled") or not a.get("interface"):
            continue
        try:
            ifc = ipaddress.ip_interface(str(a.get("address", "")).strip())
        except ValueError:
            continue
        if ifc.version == 4 and a["interface"] not in by_if:
            by_if[a["interface"]] = ifc
    pons, skipped = [], []
    usable = []
    for v in vlans:
        try:
            vid = int(v.get("vlan-id"))
        except (TypeError, ValueError):
            continue
        if v.get("disabled"):
            continue
        if vlan_from is not None and vid < vlan_from:
            continue
        if vlan_to is not None and vid > vlan_to:
            continue
        usable.append((vid, v))
    for vid, v in sorted(usable, key=lambda t: t[0]):
        name = v.get("name", f"vlan{vid}")
        ifc = by_if.get(name)
        if ifc is None:
            skipped.append(f"{name} (VLAN {vid}): no tiene dirección IPv4")
            continue
        net = ifc.network
        if net.prefixlen < 16 or net.prefixlen > 29:
            skipped.append(f"{name} (VLAN {vid}): la red {net} no es de un tamaño razonable")
            continue
        if net.prefixlen <= 24:
            first, second = list(net.subnets(prefixlen_diff=1))
            inet, tv = str(first), str(second)
        else:
            inet, tv = str(net), ""
        pons.append({"name": f"PON {len(pons) + 1}", "vlan": vid, "inet_cidr": inet, "tv_cidr": tv,
                     "from": f"{name} · {ifc.with_prefixlen}"})
    return {"pons": pons, "skipped": skipped}


class Topology:
    def __init__(self, db, engine, pppoe, clock=time.time):
        self.db = db
        self.engine = engine
        self.pppoe = pppoe
        self.clock = clock
        self._lock = threading.RLock()
        self._index: tuple[list[int], list[tuple[int, int, int, int]]] | None = None
        # pon_id -> {"bad_since", "good_since", "active"}
        self._track: dict[int, dict[str, Any]] = {}
        self._load_alerts()

    # ----------------------------------------------------------- configuración

    def olts(self) -> list[dict]:
        out = []
        for o in self.db.query("SELECT id, name FROM olts ORDER BY name"):
            pons = self.db.query(
                "SELECT id, name, vlan, inet_cidr, tv_cidr FROM pons WHERE olt_id=? ORDER BY pos, id",
                (o["id"],),
            )
            out.append({"id": o["id"], "name": o["name"], "pons": pons})
        return out

    def save_olt(self, name: str, pons: list[dict], olt_id: int | None = None) -> int:
        name = (name or "").strip()[:40]
        if not name:
            raise ConfigError("Poné un nombre para la OLT")
        pons = validate_pons(pons)
        dup = self.db.query("SELECT id FROM olts WHERE lower(name)=lower(?)", (name,))
        if dup and dup[0]["id"] != olt_id:
            raise ConfigError(f"Ya existe una OLT llamada «{name}»")
        # Los pools de esta OLT no pueden pisar los de otras.
        mine = [(ipaddress.ip_network(c), p["name"]) for p in pons for c in (p["inet_cidr"], p["tv_cidr"]) if c]
        for o in self.olts():
            if o["id"] == olt_id:
                continue
            for p in o["pons"]:
                for c in (p["inet_cidr"], p["tv_cidr"]):
                    if not c:
                        continue
                    other = ipaddress.ip_network(c)
                    for net, pname in mine:
                        if net.overlaps(other):
                            raise ConfigError(
                                f"{pname} ({net}) se pisa con {p['name']} ({other}) de la OLT «{o['name']}»"
                            )
        with self._lock:
            if olt_id is None:
                olt_id = self.db.execute("INSERT INTO olts(name) VALUES(?)", (name,)).lastrowid
            else:
                if not self.db.query("SELECT 1 FROM olts WHERE id=?", (olt_id,)):
                    raise KeyError(olt_id)
                self.db.execute("UPDATE olts SET name=? WHERE id=?", (name, olt_id))
                self._drop_olt_state(olt_id)
                self.db.execute("DELETE FROM pons WHERE olt_id=?", (olt_id,))
            self.db.executemany(
                "INSERT INTO pons(olt_id, pos, name, vlan, inet_cidr, tv_cidr) VALUES(?,?,?,?,?,?)",
                [(olt_id, i, p["name"], p["vlan"], p["inet_cidr"], p["tv_cidr"]) for i, p in enumerate(pons)],
            )
            self._index = None
        return olt_id

    def delete_olt(self, olt_id: int) -> bool:
        with self._lock:
            if not self.db.query("SELECT 1 FROM olts WHERE id=?", (olt_id,)):
                return False
            self._drop_olt_state(olt_id)
            self.db.execute("DELETE FROM pons WHERE olt_id=?", (olt_id,))
            self.db.execute("DELETE FROM olts WHERE id=?", (olt_id,))
            self._index = None
        return True

    def _drop_olt_state(self, olt_id: int) -> None:
        """Resuelve sin avisar las alertas de sector de esta OLT (se re-crean si siguen mal)."""
        ids = [r["id"] for r in self.db.query("SELECT id FROM pons WHERE olt_id=?", (olt_id,))]
        olt = self.db.query("SELECT name FROM olts WHERE id=?", (olt_id,))
        prefix = f"{olt[0]['name']} · " if olt else None
        if prefix:
            for key in [k for k in list(self.engine.active) if k[0] == OLT_ROUTER_ID
                        and k[2] == KIND and k[1].startswith(prefix)]:
                self.engine.clear_alert(key[0], key[1], key[2], quiet=True)
        for i in ids:
            self._track.pop(i, None)

    # ------------------------------------------------------------- asignación

    def _build_index(self):
        starts, spans = [], []
        rows = []
        for o in self.olts():
            for p in o["pons"]:
                for pool, cidr in ((0, p["inet_cidr"]), (1, p["tv_cidr"])):
                    if cidr:
                        net = ipaddress.ip_network(cidr)
                        rows.append((int(net.network_address), int(net.broadcast_address), p["id"], pool))
        rows.sort()
        for a, b, pid, pool in rows:
            starts.append(a)
            spans.append((a, b, pid, pool))
        return starts, spans

    def locate(self, ip: str) -> tuple[int, int] | None:
        """(pon_id, pool) al que pertenece una IP, o None."""
        with self._lock:
            if self._index is None:
                self._index = self._build_index()
            starts, spans = self._index
        try:
            n = int(ipaddress.IPv4Address(ip))
        except ValueError:
            return None
        i = bisect.bisect_right(starts, n) - 1
        if i >= 0 and spans[i][0] <= n <= spans[i][1]:
            return spans[i][2], spans[i][3]
        return None

    # --------------------------------------------------------------- instantánea

    def snapshot(self, now: float | None = None) -> dict:
        """Estructura completa para dibujar el grafo."""
        now = self.clock() if now is None else now
        cfg = self.olts()
        names = {r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM routers")}
        # pon_id -> pool -> lista de clientes [nombre, estado, ip, router]
        buckets: dict[int, list[list[list]]] = {}
        unassigned = 0
        for rid, se, status in self.pppoe.all_sessions():
            loc = self.locate(se.address) if se.address else None
            if loc is None:
                unassigned += 1
                continue
            pid, pool = loc
            buckets.setdefault(pid, [[], []])[pool].append([se.name, status, se.address, names.get(rid, "?")])
        settings = self.db.get_settings()
        out = []
        for o in cfg:
            pons = []
            for p in o["pons"]:
                pools = buckets.get(p["id"], [[], []])
                pools_out = []
                for pool, label in enumerate(POOL_NAMES):
                    cidr = p["inet_cidr"] if pool == 0 else p["tv_cidr"]
                    clients = sorted(pools[pool], key=lambda c: c[0])
                    cap = 0
                    if cidr:
                        net = ipaddress.ip_network(cidr)
                        cap = max(net.num_addresses - 2, 0)
                    pools_out.append({
                        "kind": label, "cidr": cidr, "capacity": cap,
                        "up": sum(1 for c in clients if c[1] == "up"),
                        "down": sum(1 for c in clients if c[1] != "up"),
                        "clients": clients,
                    })
                m = self._metrics(pools)
                pons.append({
                    "id": p["id"], "name": p["name"], "vlan": p["vlan"], "pools": pools_out,
                    "total": m["total"], "down": m["down"],
                    "pct": round(m["pct"], 1), "alert": self._is_active(p["id"]),
                    "status": self._status(m, p["id"], settings),
                })
            out.append({"id": o["id"], "name": o["name"], "pons": pons})
        return {"now": now, "olts": out, "unassigned": unassigned}

    @staticmethod
    def _metrics(pools, include_expired: bool = False) -> dict:
        up = down = expired = 0
        for pool in pools:
            for c in pool:
                if c[1] == "up":
                    up += 1
                elif c[1] == "exp":
                    expired += 1
                else:
                    down += 1
        if include_expired:
            down += expired
        total = up + down
        return {"up": up, "down": down, "total": total, "expired": expired,
                "pct": (100.0 * down / total) if total else 0.0}

    def _status(self, m: dict, pon_id: int, settings: dict) -> str:
        if self._is_active(pon_id):
            return "down"
        if m["total"] == 0:
            return "empty"
        if m["pct"] >= float(settings["pon_down_pct"]) / 2 and m["down"] >= 2:
            return "warn"
        return "ok"

    # ------------------------------------------------------------------- alertas

    def _is_active(self, pon_id: int) -> bool:
        return bool(self._track.get(pon_id, {}).get("active"))

    def _load_alerts(self) -> None:
        """Tras un reinicio, retoma las alertas de sector que seguían abiertas."""
        for key in self.engine.active:
            if key[0] == OLT_ROUTER_ID and key[2] == KIND:
                olt_name, _, pon_name = key[1].partition(" · ")
                rows = self.db.query(
                    "SELECT p.id FROM pons p JOIN olts o ON o.id = p.olt_id WHERE o.name=? AND p.name=?",
                    (olt_name, pon_name),
                )
                if rows:
                    self._track[rows[0]["id"]] = {"active": True, "bad_since": None, "good_since": None}

    def evaluate(self, now: float | None = None) -> None:
        """Abre o resuelve las alertas de sector caído. Se llama cada pocos segundos."""
        now = self.clock() if now is None else now
        settings = self.db.get_settings()
        pct = float(settings["pon_down_pct"])
        min_clients = int(settings["pon_min_clients"])
        sustain = float(settings["pon_down_after_s"])
        if pct <= 0:
            return
        snap = self.snapshot(now)
        # El snapshot ya calculó los totales; acá se decide con histéresis.
        for olt in snap["olts"]:
            for pon in olt["pons"]:
                tr = self._track.setdefault(pon["id"], {"active": False, "bad_since": None, "good_since": None})
                pools = [[c for c in pl["clients"]] for pl in pon["pools"]]
                # Mientras el sector está en alerta, los clientes vencidos siguen contando como caídos.
                m = self._metrics(pools, include_expired=tr["active"])
                bad = m["total"] >= min_clients and m["pct"] >= pct
                good = m["total"] < min_clients or m["pct"] < pct / 2
                key = f"{olt['name']} · {pon['name']}"
                if not tr["active"]:
                    tr["good_since"] = None
                    if bad:
                        tr["bad_since"] = tr["bad_since"] or now
                        if now - tr["bad_since"] >= sustain:
                            tr["active"] = True
                            tr["bad_since"] = None
                            self.engine.fire_alert(
                                {"id": OLT_ROUTER_ID, "name": "OLT"}, key, KIND, "critical",
                                self._message(olt["name"], pon, m), value=m["pct"], now=now,
                            )
                    else:
                        tr["bad_since"] = None
                else:
                    tr["bad_since"] = None
                    if bad:   # al recuperarse se conserva el último texto «caído»; no se reescribe con 0 clientes
                        self.engine.update_alert(OLT_ROUTER_ID, key, KIND, self._message(olt["name"], pon, m), m["pct"])
                    if good and not bad:
                        tr["good_since"] = tr["good_since"] or now
                        if now - tr["good_since"] >= sustain:
                            tr["active"] = False
                            tr["good_since"] = None
                            self.engine.clear_alert(OLT_ROUTER_ID, key, KIND, now)
                    else:
                        tr["good_since"] = None

    @staticmethod
    def _message(olt: str, pon: dict, m: dict) -> str:
        vlan = f" (VLAN {pon['vlan']})" if pon["vlan"] else ""
        return (f"Sector caído: {pon['name']}{vlan} de {olt} — {m['down']} de {m['total']} "
                f"clientes sin conexión ({m['pct']:.0f} %)")
