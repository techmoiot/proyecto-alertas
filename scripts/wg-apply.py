#!/usr/bin/env python3
"""Ayudante ROOT de MAGI para WireGuard (lo ejecuta systemd, no la aplicación).

La aplicación corre sin privilegios y deja la lista de peers en
/var/lib/mk-monitor/wg/peers.conf. Como ese archivo lo controla un usuario sin
privilegios, acá NUNCA se copia tal cual a la configuración de WireGuard (una línea
PostUp= sería ejecución de comandos como root): se leen solo los pares
PublicKey/AllowedIPs con formato estricto y se arma una configuración propia.

Uso:  wg-apply.py            aplica los peers y publica el estado
"""

import base64
import ipaddress
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path("/etc/mk-monitor/mk-monitor.env")
WG_DIR = Path("/etc/mk-monitor/wg")
STATE_DIR = Path("/var/lib/mk-monitor/wg")
RUN_DIR = Path("/run/mk-monitor-wg")
IFACE = "magi0"
CONF = Path(f"/etc/wireguard/{IFACE}.conf")
KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def read_env() -> dict[str, str]:
    out = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def safe_read(path: Path, limit: int = 1_000_000) -> str:
    """Lee un archivo del usuario sin privilegios sin seguir enlaces simbólicos."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("no es un archivo regular")
        return os.read(fd, limit).decode("utf-8", "replace")
    finally:
        os.close(fd)


def parse_peers(text: str, net: ipaddress.IPv4Network, server_ip: ipaddress.IPv4Address):
    peers, key, ip = [], None, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("PublicKey = "):
            key = line[12:]
            if not KEY_RE.match(key) or len(base64.b64decode(key)) != 32:
                key = None
        elif line.startswith("AllowedIPs = "):
            m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})/32", line[13:])
            try:
                cand = ipaddress.ip_address(m.group(1)) if m else None
            except ValueError:
                cand = None
            ip = cand if cand is not None and cand in net and cand != server_ip else None
        elif line == "" and key and ip:
            peers.append((key, str(ip)))
            key = ip = None
    if key and ip:
        peers.append((key, str(ip)))
    return peers


def main() -> int:
    env = read_env()
    net = ipaddress.ip_network(env.get("MKMON_WG_NETWORK", "10.77.0.0/24"))
    port = int(env.get("MKMON_WG_PORT", "51820"))
    server_ip = next(net.hosts())
    key_file = WG_DIR / "server.key"
    if not key_file.exists():
        print("Falta el par de claves del servidor (corré ./install.sh)", file=sys.stderr)
        return 1
    try:
        peers = parse_peers(safe_read(STATE_DIR / "peers.conf"), net, server_ip)
    except OSError:
        peers = []

    conf = [
        "[Interface]",
        f"Address = {server_ip}/{net.prefixlen}",
        f"ListenPort = {port}",
        f"PrivateKey = {key_file.read_text().strip()}",
        "",
    ]
    for key, ip in peers:
        conf += ["[Peer]", f"PublicKey = {key}", f"AllowedIPs = {ip}/32", ""]
    text = "\n".join(conf)

    CONF.parent.mkdir(parents=True, exist_ok=True)
    old = CONF.read_text() if CONF.exists() else None
    if old != text:
        tmp = CONF.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, CONF)

    up = subprocess.run(["ip", "link", "show", IFACE], capture_output=True).returncode == 0
    if not up:
        subprocess.run(["systemctl", "start", f"wg-quick@{IFACE}"], capture_output=True)
    elif old != text:
        strip = subprocess.run(["wg-quick", "strip", IFACE], capture_output=True, text=True)
        if strip.returncode == 0:
            subprocess.run(["wg", "syncconf", IFACE, "/dev/stdin"], input=strip.stdout, text=True,
                           capture_output=True)

    # Estado para el panel: último handshake de cada peer (lectura pública, sin claves privadas)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o755)
    res = subprocess.run(["wg", "show", IFACE, "latest-handshakes"], capture_output=True, text=True)
    if res.returncode == 0:
        out = RUN_DIR / "handshakes.txt.tmp"
        out.write_text(res.stdout)
        os.chmod(out, 0o644)
        os.replace(out, RUN_DIR / "handshakes.txt")
    else:
        (RUN_DIR / "handshakes.txt").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
