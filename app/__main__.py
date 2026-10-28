"""Arranque: python -m app   ·   Recuperar acceso: python -m app passwd [usuario]"""

import getpass
import logging
import sys

import uvicorn

from .config import load


def passwd(username: str | None) -> int:
    """Pone (o crea) un administrador con la contraseña que se escriba; sirve si se olvidó la clave."""
    from .db import DB
    from .users import UserError, Users

    cfg = load()
    users = Users(DB(cfg.db_path))
    username = username or cfg.admin_user
    first = getpass.getpass(f"Nueva contraseña para «{username}»: ")
    if first != getpass.getpass("Repetila: "):
        print("No coinciden.", file=sys.stderr)
        return 1
    try:
        row = users.get(username)
        if row:
            users.set_password(row["id"], first)
            users.set_role(row["id"], "admin")
        else:
            users.create(username, first, "admin")
    except UserError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Listo: «{username}» ya puede entrar (administrador).")
    return 0


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "passwd":
        sys.exit(passwd(sys.argv[2] if len(sys.argv) > 2 else None))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load()
    uvicorn.run("app.main:create_app", factory=True, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
