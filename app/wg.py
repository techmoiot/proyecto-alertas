"""Conexión remota de MikroTik por túnel WireGuard.

El servidor MAGI y el router no están en la misma red: el router abre un túnel
WireGuard hacia el servidor (conexión saliente, funciona detrás de NAT) y toda la
comunicación (API de RouterOS y syslog) viaja por ese túnel con IP privadas.

Cómo se reparten las tareas:
  - Esta clase (usuario sin privilegios) genera las claves del router, reparte las IP
    del túnel y escribe la lista de peers en `peers.conf`.
  - `scripts/wg-apply.py` (root, lo dispara systemd) valida esa lista, arma la
    configuración de WireGuard y la aplica; además publica los últimos handshakes.
"""

import base64
import ipaddress
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

PRIVATE_USER = "mkmonitor"
TEMPLATE = Path(__file__).resolve().parent.parent / "routeros" / "conectar-remoto.rsc"
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

# ------------------------------------------------------------------ X25519 (RFC 7748)

_P = 2**255 - 19
_A24 = 121665


def x25519(k: bytes, u: bytes) -> bytes:
    """Multiplicación escalar de Curve25519 (lo que usa WireGuard para sus claves)."""
    kk = bytearray(k)
    kk[0] &= 248
    kk[31] &= 127
    kk[31] |= 64
    n = int.from_bytes(kk, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (n >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * ((da - cb) ** 2) % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def generate_keypair() -> tuple[str, str]:
    """(clave privada, clave pública) en base64, como las muestra WireGuard."""
    priv = bytearray(secrets.token_bytes(32))
    priv[0] &= 248
    priv[31] &= 127
    priv[31] |= 64
    pub = x25519(bytes(priv), (9).to_bytes(32, "little"))
    return base64.b64encode(bytes(priv)).decode(), base64.b64encode(pub).decode()


def public_from_private(priv_b64: str) -> str:
    return base64.b64encode(x25519(base64.b64decode(priv_b64), (9).to_bytes(32, "little"))).decode()


def valid_key(text: str) -> bool:
    try:
        return len(base64.b64decode(text, validate=True)) == 32 and len(text) == 44
    except Exception:  # noqa: BLE001
        return False


class RemoteError(ValueError):
    pass


class WGManager:
    def __init__(self, db, cfg, clock=time.time):
        self.db = db
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.RLock()
        self._last_tick = 0.0
        self.write_error = ""

    # ------------------------------------------------------------- datos del servidor

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(self.cfg.wg_network)

    @property
    def server_ip(self) -> str:
        return str(next(self.network.hosts()))

    def server_pub(self) -> str | None:
        try:
            text = Path(self.cfg.wg_pub_path).read_text().strip()
        except OSError:
            return None
        return text if valid_key(text) else None

    def endpoint(self) -> str:
        row = self.db.one("SELECT value FROM settings WHERE key='wg_endpoint'")
        return (row["value"] if row else "") or self.cfg.wg_endpoint

    def set_endpoint(self, host: str) -> str:
        host = host.strip()
        if host and not HOST_RE.match(host):
            raise RemoteError("Poné solo la IP pública o el dominio, sin http:// ni puerto")
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES('wg_endpoint', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (host,))
        return host

    # ------------------------------------------------------------------------ peers

    def peer(self, router_id: int) -> dict | None:
        return self.db.one("SELECT * FROM wg_peers WHERE router_id=?", (router_id,))

    def _allocate_ip(self) -> str:
        used = {r["ip"] for r in self.db.query("SELECT ip FROM wg_peers")}
        for ip in list(self.network.hosts())[1:]:
            if str(ip) not in used:
                return str(ip)
        raise RemoteError("No quedan direcciones libres en la red del túnel")

    def create_peer(self, router_id: int) -> dict:
        with self._lock:
            ip = self._allocate_ip()
            priv, pub = generate_keypair()
            self.db.execute(
                "INSERT INTO wg_peers(router_id, private_key, public_key, ip, activated, created_at) "
                "VALUES(?,?,?,?,0,?)", (router_id, priv, pub, ip, self.clock()))
            self.write_files()
            return self.peer(router_id)

    def remove_peer(self, router_id: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM wg_peers WHERE router_id=?", (router_id,))
            self.write_files()

    def write_files(self) -> None:
        """Escribe la lista de peers para el ayudante root y le avisa (systemd la observa)."""
        lines = []
        for r in self.db.query("SELECT p.public_key, p.ip, r.name FROM wg_peers p "
                               "LEFT JOIN routers r ON r.id = p.router_id ORDER BY p.ip"):
            lines += ["[Peer]", f"# {re.sub(r'[^A-Za-z0-9 ._-]', '_', r['name'] or '?')}",
                      f"PublicKey = {r['public_key']}", f"AllowedIPs = {r['ip']}/32", ""]
        state = Path(self.cfg.wg_state_dir)
        try:
            state.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=state, prefix=".peers-")
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(lines))
            os.replace(tmp, state / "peers.conf")
            (state / "apply.trigger").write_text(str(self.clock()))
            self.write_error = ""
        except OSError as exc:
            self.write_error = f"No se pudo escribir {state}: {exc}"

    # ----------------------------------------------------------------------- estado

    def handshakes(self) -> dict[str, float] | None:
        """{clave pública: instante del último handshake}; None si el ayudante no publicó nada."""
        path = Path(self.cfg.wg_run_dir) / "handshakes.txt"
        try:
            text = path.read_text()
        except OSError:
            return None
        out = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and valid_key(parts[0]):
                try:
                    out[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        return out

    def status(self) -> dict:
        hs = self.handshakes()
        now = self.clock()
        peers = []
        for p in self.db.query(
            "SELECT p.router_id, p.public_key, p.ip, p.activated, r.name, r.enabled FROM wg_peers p "
            "LEFT JOIN routers r ON r.id = p.router_id ORDER BY r.name"
        ):
            ts = (hs or {}).get(p["public_key"], 0)
            peers.append({
                "router_id": p["router_id"], "name": p["name"], "ip": p["ip"],
                "connected": bool(ts and now - ts < 180),
                "last_handshake": ts or None,
                "activated": bool(p["activated"]),
            })
        return {
            "server_ready": self.server_pub() is not None,
            "helper_running": hs is not None,
            "endpoint": self.endpoint(), "port": self.cfg.wg_port,
            "network": str(self.network), "server_ip": self.server_ip,
            "write_error": self.write_error, "peers": peers,
        }

    def tick(self) -> None:
        """Activa el monitoreo de un router remoto cuando su túnel conecta por primera vez."""
        now = self.clock()
        if now - self._last_tick < 10:
            return
        self._last_tick = now
        hs = self.handshakes()
        if not hs:
            return
        for p in self.db.query("SELECT router_id, public_key FROM wg_peers WHERE activated=0"):
            if hs.get(p["public_key"], 0) > 0:
                self.db.execute("UPDATE routers SET enabled=1, updated_at=? WHERE id=?", (now, p["router_id"]))
                self.db.execute("UPDATE wg_peers SET activated=1 WHERE router_id=?", (p["router_id"],))

    # ------------------------------------------------------------------------ script

    def render_script(self, router: dict, peer: dict, syslog_port: int) -> str:
        pub = self.server_pub()
        if not pub:
            raise RemoteError("El servidor todavía no tiene WireGuard instalado (corré ./install.sh de nuevo)")
        endpoint = self.endpoint()
        if not endpoint:
            raise RemoteError("Falta la IP pública o el dominio del servidor")
        mask = self.network.prefixlen
        text = TEMPLATE.read_text(encoding="utf-8")
        values = {
            "__SRV_PUB__": pub, "__ENDPOINT__": endpoint, "__WG_PORT__": str(self.cfg.wg_port),
            "__SRV_IP__": self.server_ip, "__MY_PRIV__": peer["private_key"],
            "__MY_ADDR__": f"{peer['ip']}/{mask}", "__API_USER__": router["username"],
            "__API_PASS__": router["password"], "__API_PORT__": str(router["port"]),
            "__SYSLOG_PORT__": str(syslog_port), "__NAME__": re.sub(r"[^A-Za-z0-9 ._-]", "_", router["name"]),
        }
        for k, v in values.items():
            text = text.replace(k, v)
        return text
