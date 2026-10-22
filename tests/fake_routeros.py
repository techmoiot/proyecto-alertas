"""Servidor falso del protocolo API de RouterOS (puerto 8728), solo para pruebas.

Implementa el formato de palabras/oraciones del protocolo por su cuenta (sin usar
librouteros), así las pruebas validan de verdad la conversación por socket:
login, /interface/print con .proplist y consultas (?=k=v, ?#|, ?#&, ?#!).
"""

import socket
import threading


def enc_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    raise ValueError(n)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("cerrado")
        buf += chunk
    return buf


def read_len(sock: socket.socket) -> int:
    b = recv_exact(sock, 1)[0]
    if b < 0x80:
        return b
    if b < 0xC0:
        return ((b & 0x3F) << 8) | recv_exact(sock, 1)[0]
    if b < 0xE0:
        return ((b & 0x1F) << 16) | int.from_bytes(recv_exact(sock, 2), "big")
    if b < 0xF0:
        return ((b & 0x0F) << 24) | int.from_bytes(recv_exact(sock, 3), "big")
    raise ValueError("longitud no soportada")


def read_sentence(sock: socket.socket) -> list[str]:
    words = []
    while True:
        n = read_len(sock)
        if n == 0:
            return words
        words.append(recv_exact(sock, n).decode("utf-8"))


def write_sentence(sock: socket.socket, words: list[str]) -> None:
    out = b"".join(enc_len(len(w.encode())) + w.encode() for w in words) + b"\x00"
    sock.sendall(out)


def wire(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def default_interfaces() -> list[dict]:
    ifs = [
        dict(name="ether1", type="ether", running=True, disabled=False, comment="Upstream A"),
        dict(name="sfp-sfpplus1", type="ether", running=True, disabled=False, comment="Upstream B"),
        dict(name="ether2", type="ether", running=False, disabled=False),
        dict(name="bridge1", type="bridge", running=True, disabled=False),
    ]
    ifs += [
        dict(name=f"<pppoe-user{i}>", type="pppoe-in", running=True, disabled=False)
        for i in range(300)
    ]
    for n, i in enumerate(ifs, start=1):
        i["rx-byte"] = 0
        i["tx-byte"] = 0
        i[".id"] = f"*{n:X}"
    return ifs


def eval_query(words: list[str], item: dict) -> bool:
    stack: list[bool] = []
    for w in words:
        if w.startswith("?#"):
            op = w[2:]
            if op == "!":
                stack.append(not stack.pop())
            elif op in ("|", "&"):
                b, a = stack.pop(), stack.pop()
                stack.append((a or b) if op == "|" else (a and b))
            else:
                raise ValueError(w)
        elif w.startswith("?="):
            key, _, val = w[2:].partition("=")
            stack.append(wire(item.get(key)) == val)
        elif w.startswith("?>") or w.startswith("?<"):
            key, _, val = w[2:].partition("=")
            cur = item.get(key)
            stack.append(cur is not None and (cur > int(val) if w[1] == ">" else cur < int(val)))
        else:
            raise ValueError(w)
    return all(stack)


class FakeRouterOS:
    def __init__(self, user="mkmonitor", password="secret", interfaces=None, bytes_per_request=0,
                 reject_queries=False):
        self.user, self.password = user, password
        self.interfaces = interfaces if interfaces is not None else default_interfaces()
        self.bytes_per_request = bytes_per_request
        self.reject_queries = reject_queries
        self.requests: list[list[str]] = []
        self.ppp_active: list[dict] = []     # sesiones de /ppp/active (se editan desde la prueba)
        self.ppp_error: str | None = None    # si se pone, /ppp/active/print contesta con un error
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        try:
            # shutdown despierta el accept() bloqueado; solo con close() el puerto seguiría abierto.
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        authed = False
        try:
            while True:
                words = read_sentence(conn)
                self.requests.append(words)
                cmd, args = words[0], words[1:]
                if cmd == "/login":
                    attrs = dict(a[1:].split("=", 1) for a in args if a.startswith("="))
                    if attrs.get("name") == self.user and attrs.get("password") == self.password:
                        authed = True
                        write_sentence(conn, ["!done"])
                    else:
                        write_sentence(conn, ["!trap", "=message=invalid user name or password (6)"])
                        write_sentence(conn, ["!done"])
                elif not authed:
                    write_sentence(conn, ["!trap", "=message=not logged in"])
                    write_sentence(conn, ["!done"])
                elif cmd == "/interface/print":
                    self._interface_print(conn, args)
                elif cmd == "/ppp/active/print":
                    self._ppp_active_print(conn, args)
                else:
                    write_sentence(conn, ["!trap", "=message=no such command prefix"])
                    write_sentence(conn, ["!done"])
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    def _interface_print(self, conn: socket.socket, args: list[str]) -> None:
        proplist = None
        query = []
        for a in args:
            if a.startswith("=.proplist="):
                proplist = a.split("=", 2)[2].split(",")
            elif a.startswith("?"):
                query.append(a)
        if query and self.reject_queries:
            write_sentence(conn, ["!trap", "=message=bad query"])
            write_sentence(conn, ["!done"])
            return
        with self._lock:
            for i in self.interfaces:
                if i["running"]:
                    i["rx-byte"] += self.bytes_per_request
                    i["tx-byte"] += self.bytes_per_request // 4
            snapshot = [dict(i) for i in self.interfaces]
        for item in snapshot:
            if not eval_query(query, item):
                continue
            keys = [".id"] + (proplist if proplist else [k for k in item if k != ".id"])
            write_sentence(conn, ["!re"] + [f"={k}={wire(item[k])}" for k in keys if k in item])
        write_sentence(conn, ["!done"])

    def _ppp_active_print(self, conn: socket.socket, args: list[str]) -> None:
        if self.ppp_error:
            write_sentence(conn, ["!trap", f"=message={self.ppp_error}"])
            write_sentence(conn, ["!done"])
            return
        proplist = None
        for a in args:
            if a.startswith("=.proplist="):
                proplist = a.split("=", 2)[2].split(",")
        with self._lock:
            rows = [dict(r) for r in self.ppp_active]
        for n, item in enumerate(rows, start=1):
            item[".id"] = f"*{n:X}"
            keys = [".id"] + (proplist if proplist else [k for k in item if k != ".id"])
            write_sentence(conn, ["!re"] + [f"={k}={wire(item[k])}" for k in keys if k in item])
        write_sentence(conn, ["!done"])
