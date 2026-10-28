"""Usuarios del panel: contraseñas con scrypt y dos roles (admin: todo; viewer: solo mira)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{2,40}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 256

_N, _R, _P = 2 ** 14, 8, 1


class UserError(ValueError):
    """Dato inválido (el mensaje se muestra al usuario)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_hash(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(digest) // 2)
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise UserError(f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres")
    if len(password) > MAX_PASSWORD:
        raise UserError("La contraseña es demasiado larga")


def fingerprint(stored_hash: str) -> str:
    """Huella corta del hash: las sesiones firmadas con ella mueren al cambiar la contraseña."""
    return hashlib.sha256(stored_hash.encode()).hexdigest()[:16]


class Users:
    def __init__(self, db):
        self.db = db
        # Para igualar el tiempo de respuesta cuando el usuario no existe.
        self._dummy = hash_password(secrets.token_urlsafe(8))

    # -------------------------------------------------------------- consulta

    def count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM users")["n"]

    def get(self, username: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE lower(username)=lower(?)", (username,))

    def get_id(self, uid: int) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE id=?", (uid,))

    def list(self) -> list[dict]:
        return self.db.query("SELECT id, username, role, created_at, last_login FROM users ORDER BY lower(username)")

    def _admins(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM users WHERE role='admin'")["n"]

    # ----------------------------------------------------------- autenticación

    def authenticate(self, username: str, password: str) -> dict | None:
        row = self.get(username) if username else None
        if row is None:
            verify_hash(password, self._dummy)
            return None
        return row if verify_hash(password, row["pw_hash"]) else None

    def touch_login(self, uid: int) -> None:
        self.db.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), uid))

    # ------------------------------------------------------------ modificación

    def create(self, username: str, password: str, role: str = "viewer", *, enforce: bool = True) -> int:
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise UserError("Usuario: 2 a 40 caracteres (letras, números, punto, guion, guion bajo o @)")
        if role not in ROLES:
            raise UserError("Rol inválido")
        if enforce:
            check_password_rules(password)
        if self.get(username):
            raise UserError(f"Ya existe el usuario «{username}»")
        return self.db.execute(
            "INSERT INTO users(username, pw_hash, role, created_at) VALUES(?,?,?,?)",
            (username, hash_password(password), role, time.time()),
        ).lastrowid

    def set_password(self, uid: int, password: str) -> None:
        check_password_rules(password)
        self.db.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), uid))
        # La contraseña inicial autogenerada ya no vale: no se deja en claro en la base.
        self.db.execute("DELETE FROM settings WHERE key='admin_password'")

    def set_role(self, uid: int, role: str) -> None:
        if role not in ROLES:
            raise UserError("Rol inválido")
        row = self.get_id(uid)
        if row is None:
            raise KeyError(uid)
        if row["role"] == "admin" and role != "admin" and self._admins() <= 1:
            raise UserError("Tiene que quedar al menos un administrador")
        self.db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))

    def delete(self, uid: int) -> None:
        row = self.get_id(uid)
        if row is None:
            raise KeyError(uid)
        if row["role"] == "admin" and self._admins() <= 1:
            raise UserError("Tiene que quedar al menos un administrador")
        self.db.execute("DELETE FROM users WHERE id=?", (uid,))
