"""Receptor de syslog (UDP) para los logs de PPPoE del MikroTik.

El log solo se usa para conocer el MOTIVO de cada desconexión. El formato exacto
de los mensajes puede variar entre versiones de RouterOS, por eso los patrones son
tolerantes (se buscan en cualquier parte de la línea) y lo que no se entiende se
guarda igual en el buffer de diagnóstico para poder ajustarlo.
"""

import logging
import re
import socket
import threading
import time
from collections import deque

log = logging.getLogger("mkmonitor.syslog")

_USER = r"<(?:pppoe-)?(?P<user>[^<>\s]+)>"

# "<pppoe-juan>: terminating... - peer is not responding"
RE_TERMINATING = re.compile(_USER + r":\s*terminating\.\.\.\s*-\s*(?P<reason>.+?)\s*$", re.I)
# "<pppoe-juan>: disconnected"
RE_DISCONNECTED = re.compile(_USER + r":\s*disconnected", re.I)
# "juan logged out, 3600 1234 5678 ..."
RE_LOGGED_OUT = re.compile(r"(?P<user>\S+) logged out\b", re.I)
# "juan logged in, 10.0.0.5 from AA:BB:CC:DD:EE:FF"
RE_LOGGED_IN = re.compile(r"(?P<user>\S+) logged in\b", re.I)
# "<pppoe-juan>: authentication failed"
RE_AUTH = re.compile(_USER + r":\s*(?:authentication failed|login failed)", re.I)


def parse_line(text: str) -> dict | None:
    """Devuelve {'event': 'disconnect'|'connect'|'auth_failed', 'user', 'reason'} o None."""
    m = RE_TERMINATING.search(text)
    if m:
        return {"event": "disconnect", "user": m["user"], "reason": m["reason"]}
    m = RE_DISCONNECTED.search(text)
    if m:
        return {"event": "disconnect", "user": m["user"], "reason": ""}
    m = RE_LOGGED_OUT.search(text)
    if m:
        return {"event": "disconnect", "user": m["user"], "reason": ""}
    m = RE_AUTH.search(text)
    if m:
        return {"event": "auth_failed", "user": m["user"], "reason": ""}
    m = RE_LOGGED_IN.search(text)
    if m:
        return {"event": "connect", "user": m["user"], "reason": ""}
    return None


class SyslogReceiver:
    def __init__(self, port: int, handler, host: str = "0.0.0.0"):
        self.port = port
        self.host = host
        self.handler = handler          # handler(ip, text) -> bool (¿se entendió y se aplicó?)
        self.error: str | None = None
        self.received = 0
        self.parsed = 0
        self.lines: deque[dict] = deque(maxlen=200)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = False

    def start(self) -> None:
        if not self.port:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            self.error = str(exc)
            log.warning("No se pudo abrir el syslog UDP %s: %s", self.port, exc)
            return
        sock.settimeout(1.0)
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name="syslog", daemon=True)
        self._thread.start()
        log.info("Escuchando syslog UDP en %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop:
            try:
                data, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self.handle(addr[0], data.decode("utf-8", "replace"))

    def handle(self, ip: str, text: str) -> None:
        text = text.strip("\x00\r\n ")
        if not text:
            return
        self.received += 1
        ok = False
        try:
            ok = bool(self.handler(ip, text))
        except Exception:  # noqa: BLE001
            log.exception("Error procesando línea de syslog")
        if ok:
            self.parsed += 1
        self.lines.append({"ts": time.time(), "ip": ip, "text": text[:400], "parsed": ok})

    def snapshot(self) -> dict:
        return {
            "port": self.port,
            "listening": self._sock is not None and not self._stop,
            "error": self.error,
            "received": self.received,
            "parsed": self.parsed,
            "lines": list(self.lines)[::-1][:100],
        }
