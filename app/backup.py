"""Respaldos: copia automática de la base de MAGI y de la configuración de los MikroTik.

- Base de MAGI: copia consistente con la API de respaldo de SQLite, comprimida, cada `backup_every_h`
  horas; se conservan las últimas `backup_keep`.
- MikroTik (opcional por router): `/export terse` por SSH. Solo se guarda una versión nueva si la
  configuración cambió desde la última, y se conservan las últimas `router_backup_keep`.

Todo queda en <carpeta de datos>/backups con permisos 600. Los archivos de la base contienen
contraseñas de routers y claves del túnel: se descargan solo con un usuario administrador.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import io
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .db import SCHEMA  # noqa: E402

log = logging.getLogger("mkmonitor.backup")

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
RETRY_AFTER_ERROR_S = 3600
SSH_TIMEOUT_S = 60
MAX_EXPORT_BYTES = 20 * 1024 * 1024
MAX_RESTORE_BYTES = 2 * 1024 ** 3
WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
REQUIRED_TABLES = {"routers", "settings", "users", "alerts"}


class BackupError(Exception):
    """Falla de un respaldo (el mensaje se muestra al usuario)."""


def stamp(ts: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "router"


def normalize_export(text: str) -> str:
    """Quita lo que cambia en cada exportación (la fecha de la primera línea) para comparar versiones."""
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    if lines and lines[0].startswith("#") and " by RouterOS" in lines[0]:
        lines = lines[1:]
    return "\n".join(lines).strip() + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(normalize_export(text).encode()).hexdigest()


# ------------------------------------------------------------------------- SSH


def ssh_export(db, router: dict, timeout: float = SSH_TIMEOUT_S) -> str:
    """Ejecuta `/export terse` en el router por SSH y devuelve el texto.

    La clave del servidor SSH se aprende la primera vez (TOFU) y se exige igual después: así un
    intermediario no puede quedarse con la contraseña del usuario de lectura.
    """
    import paramiko

    key_name = f"sshkey:{router['id']}"

    class _Tofu(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fp = f"{key.get_name()} {key.get_base64()}"
            row = db.one("SELECT value FROM settings WHERE key=?", (key_name,))
            if row is None:
                db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key_name, fp))
            elif row["value"] != fp:
                raise paramiko.SSHException(
                    "La clave SSH del router cambió (¿se reinstaló o es otro equipo?). "
                    "Si es normal, usá «Olvidar clave SSH» y probá de nuevo."
                )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_Tofu())
    try:
        client.connect(
            router["host"], port=int(router.get("ssh_port") or 22), username=router["username"],
            password=router["password"], timeout=timeout, auth_timeout=timeout, banner_timeout=timeout,
            look_for_keys=False, allow_agent=False,
        )
        _, out, err = client.exec_command("/export terse", timeout=timeout)
        data = out.read(MAX_EXPORT_BYTES + 1)
        errtxt = err.read(2000).decode("utf-8", "replace").strip()
    except paramiko.AuthenticationException:
        raise BackupError("El router rechazó el usuario/contraseña por SSH. El usuario necesita el permiso «ssh» "
                          "(en RouterOS: /user group set <grupo> policy=read,api,ssh).") from None
    except paramiko.SSHException as exc:
        raise BackupError(f"SSH: {exc}") from None
    except OSError as exc:
        raise BackupError(f"No se pudo conectar por SSH a {router['host']}:{router.get('ssh_port') or 22} ({exc}). "
                          "¿Está activo el servicio ssh y llega desde este servidor?") from None
    finally:
        client.close()
    if len(data) > MAX_EXPORT_BYTES:
        raise BackupError("La configuración exportada es demasiado grande")
    text = data.decode("utf-8", "replace")
    if not text.strip() or "#" not in text[:200] and "/" not in text:
        raise BackupError(errtxt or "El router no devolvió una configuración")
    if errtxt and len(text) < 200:
        raise BackupError(errtxt)
    return text


# ---------------------------------------------------------------------- manager


class BackupManager:
    def __init__(self, db, base_dir: str | Path | None, exporter: Callable | None = None,
                 clock: Callable[[], float] = time.time, wg_state_dir: str | Path | None = None):
        self.db = db
        self.wg_state_dir = Path(wg_state_dir) if wg_state_dir else None
        self.base = Path(base_dir) if base_dir else None
        self.exporter = exporter or (lambda router: ssh_export(self.db, router))
        self.clock = clock
        self._lock = threading.RLock()          # una operación de archivo a la vez
        self._thread: threading.Thread | None = None
        self._router_checked: dict[int, float] = {}
        self._errors: dict[int | str, tuple[float, str]] = {}   # "db" o router_id -> (cuándo, mensaje)
        self._db_failed_at = 0.0

    @property
    def enabled(self) -> bool:
        return self.base is not None

    # ------------------------------------------------------------- archivos

    def _ensure(self, *parts: str) -> Path:
        d = self.base.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    @staticmethod
    def _files(d: Path, pattern: str) -> list[Path]:
        if not d.is_dir():
            return []
        return sorted((p for p in d.glob(pattern) if p.is_file()), key=lambda p: p.name, reverse=True)   # el nombre lleva la fecha

    @staticmethod
    def _info(p: Path) -> dict:
        st = p.stat()
        return {"name": p.name, "size": st.st_size, "created": st.st_mtime}

    def _router_dir(self, router: dict, create: bool = False) -> Path | None:
        root = self.base / "routers"
        prefix = f"{router['id']}-"
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir() and d.name.startswith(prefix):
                    return d
        return self._ensure("routers", f"{prefix}{safe(router['name'])}") if create else None

    # ------------------------------------------------------------ base de MAGI

    def backup_db(self) -> dict:
        if not self.enabled:
            raise BackupError("Los respaldos no están disponibles en este modo")
        with self._lock:
            now = self.clock()
            d = self._ensure("db")
            tmp = d / f".tmp-{stamp(now)}.db"
            final = d / f"magi-{stamp(now)}.db.gz"
            dst = sqlite3.connect(tmp)
            try:
                with self.db._lock:
                    self.db._conn.backup(dst)
            finally:
                dst.close()
            try:
                with open(tmp, "rb") as src, gzip.open(final, "wb", compresslevel=6) as out:
                    shutil.copyfileobj(src, out)
                os.chmod(final, 0o600)
            finally:
                tmp.unlink(missing_ok=True)
            self._prune(d, "magi-*.db.gz", int(self.db.get_settings()["backup_keep"]))
            self._errors.pop("db", None)
            log.info("Respaldo de la base: %s", final.name)
            return self._info(final)

    def _prune(self, d: Path, pattern: str, keep: int) -> None:
        for p in self._files(d, pattern)[max(keep, 1):]:
            p.unlink(missing_ok=True)

    # ------------------------------------------------------- config de MikroTik

    def backup_router(self, router: dict, force: bool = False) -> dict:
        """Exporta la configuración; guarda un archivo nuevo solo si cambió (o si `force`)."""
        if not self.enabled:
            raise BackupError("Los respaldos no están disponibles en este modo")
        with self._lock:
            rid = router["id"]
            try:
                text = self.exporter(router)
            except BackupError as exc:
                self._errors[rid] = (self.clock(), str(exc))
                raise
            except Exception as exc:  # noqa: BLE001
                self._errors[rid] = (self.clock(), f"{type(exc).__name__}: {exc}")
                raise BackupError(f"{type(exc).__name__}: {exc}") from None
            self._errors.pop(rid, None)
            now = self.clock()
            self._router_checked[rid] = now
            d = self._router_dir(router, create=True)
            files = self._files(d, "config-*.rsc")
            if files and not force:
                try:
                    if digest(files[0].read_text(encoding="utf-8", errors="replace")) == digest(text):
                        return {"changed": False, **self._info(files[0])}
                except OSError:
                    pass
            final = d / f"config-{stamp(now)}.rsc"
            final.write_text(text, encoding="utf-8")
            os.chmod(final, 0o600)
            self._prune(d, "config-*.rsc", int(self.db.get_settings()["router_backup_keep"]))
            log.info("Respaldo de configuración de %s: %s", router["name"], final.name)
            return {"changed": True, **self._info(final)}

    # --------------------------------------------------------------- programa

    def tick(self, now: float | None = None) -> None:
        """Lo llama el supervisor: lanza en un hilo los respaldos que toquen."""
        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        now = self.clock() if now is None else now
        s = self.db.get_settings()
        if not s["backup_enabled"]:
            return
        every = max(s["backup_every_h"], 0.1) * 3600
        jobs: list[Callable[[], None]] = []
        last_db = max((p.stat().st_mtime for p in self._files(self.base / "db", "magi-*.db.gz")), default=0)
        if now - last_db >= every and now - self._db_failed_at >= RETRY_AFTER_ERROR_S:
            jobs.append(self._job_db)
        for r in self.db.query("SELECT * FROM routers WHERE enabled=1 AND cfg_backup=1"):
            rid = r["id"]
            if rid not in self._router_checked:
                d = self._router_dir(r)
                self._router_checked[rid] = max((p.stat().st_mtime for p in self._files(d, "config-*.rsc")), default=0) if d else 0
            failed = self._errors.get(rid)
            if now - self._router_checked[rid] >= every and not (failed and now - failed[0] < RETRY_AFTER_ERROR_S):
                jobs.append(lambda r=r: self._job_router(r))
        if jobs:
            def run():
                for j in jobs:
                    j()
            self._thread = threading.Thread(target=run, name="backup", daemon=True)
            self._thread.start()

    def _job_db(self) -> None:
        try:
            self.backup_db()
        except Exception as exc:  # noqa: BLE001
            self._db_failed_at = self.clock()
            self._errors["db"] = (self._db_failed_at, f"{type(exc).__name__}: {exc}")
            log.error("Falló el respaldo de la base: %s", exc)

    def _job_router(self, router: dict) -> None:
        try:
            self.backup_router(router)
        except Exception as exc:  # noqa: BLE001
            log.warning("Respaldo de %s falló: %s", router["name"], exc)

    def wait(self, timeout: float = 30) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    # ------------------------------------------------------------------- vistas

    def listing(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "dir": "", "db": [], "routers": [], "db_error": ""}
        routers = []
        for r in self.db.query("SELECT id, name, host, enabled, cfg_backup, ssh_port FROM routers ORDER BY name"):
            d = self._router_dir(r)
            files = [self._info(p) for p in self._files(d, "config-*.rsc")] if d else []
            err = self._errors.get(r["id"])
            routers.append({
                "router_id": r["id"], "router": r["name"], "host": r["host"], "cfg_backup": bool(r["cfg_backup"]),
                "ssh_port": r["ssh_port"], "files": files,
                "error": err[1] if err else "", "error_at": err[0] if err else None,
            })
        db_err = self._errors.get("db")
        return {
            "enabled": True, "dir": str(self.base),
            "db": [self._info(p) for p in self._files(self.base / "db", "magi-*.db.gz")],
            "db_error": db_err[1] if db_err else "", "routers": routers,
        }

    def path_for(self, kind: str, name: str, router_id: int | None = None) -> Path | None:
        """Ruta de un archivo de respaldo ya validada (sin salir de la carpeta de respaldos)."""
        if not self.enabled or not NAME_RE.match(name or ""):
            return None
        if kind == "db":
            p = self.base / "db" / name
        elif kind == "router" and router_id is not None:
            row = self.db.one("SELECT id, name FROM routers WHERE id=?", (router_id,))
            d = self._router_dir(row) if row else None
            if d is None:
                return None
            p = d / name
        else:
            return None
        return p if p.is_file() and p.resolve().parent == p.parent.resolve() else None

    # ------------------------------------------------- respaldo completo / restauración

    def _wg_key(self) -> str:
        """Clave privada del túnel WireGuard (la deja ahí el ayudante root); '' si no está."""
        if not self.wg_state_dir:
            return ""
        try:
            key = (self.wg_state_dir / "server.key.export").read_text().strip()
        except OSError:
            return ""
        return key if WG_KEY_RE.match(key) else ""

    def make_bundle(self) -> Path:
        """Archivo .tar.gz con la base completa + la clave del túnel. Quien lo pide debe borrarlo."""
        fd, name = tempfile.mkstemp(prefix="magi-completo-", suffix=".tar.gz")
        os.close(fd)
        out = Path(name)
        tmpdb = Path(name + ".db")
        try:
            dst = sqlite3.connect(tmpdb)
            try:
                with self.db._lock:
                    self.db._conn.backup(dst)
            finally:
                dst.close()
            key = self._wg_key()
            manifest = json.dumps({"app": "magi", "version": 1, "created": self.clock(), "wg_key": bool(key)}).encode()
            with tarfile.open(out, "w:gz", compresslevel=6) as tar:
                tar.add(tmpdb, arcname="mkmonitor.db")
                for arc, data in (("manifest.json", manifest), ("server.key", key.encode() if key else b"")):
                    if not data:
                        continue
                    info = tarfile.TarInfo(arc)
                    info.size, info.mtime, info.mode = len(data), int(self.clock()), 0o600
                    tar.addfile(info, io.BytesIO(data))
            os.chmod(out, 0o600)
            return out
        except Exception:
            out.unlink(missing_ok=True)
            raise
        finally:
            tmpdb.unlink(missing_ok=True)

    @staticmethod
    def _unpack(path: Path, workdir: Path) -> tuple[Path, str]:
        """Saca de un archivo subido la base (y la clave del túnel si viene). Acepta el respaldo completo
        (.tar.gz), una copia de la base (.db.gz) o la base sola (.db). No extrae nada por nombre."""
        with open(path, "rb") as fh:
            head = fh.read(512)
        dbfile, key = workdir / "restore.db", ""
        if head.startswith(b"SQLite format 3"):
            shutil.copyfile(path, dbfile)
        elif head[:2] == b"\x1f\x8b":
            try:
                with tarfile.open(path, "r:gz") as tar:
                    names = {m.name: m for m in tar.getmembers() if m.isfile()}
                    if "mkmonitor.db" not in names:
                        raise tarfile.ReadError("sin base")
                    with tar.extractfile(names["mkmonitor.db"]) as src, open(dbfile, "wb") as out:
                        shutil.copyfileobj(src, out)
                    if "server.key" in names and names["server.key"].size <= 200:
                        with tar.extractfile(names["server.key"]) as src:
                            key = src.read().decode("ascii", "ignore").strip()
            except tarfile.TarError:
                # no es un tar: probablemente un .db.gz suelto
                try:
                    with gzip.open(path, "rb") as src, open(dbfile, "wb") as out:
                        shutil.copyfileobj(src, out)
                except OSError:
                    raise BackupError("El archivo no es un respaldo de MAGI válido.") from None
            except (OSError, EOFError):
                raise BackupError("El archivo está dañado o incompleto.") from None
        else:
            raise BackupError("El archivo no es un respaldo de MAGI válido.")
        return dbfile, key if WG_KEY_RE.match(key) else ""

    def restore_bundle(self, path: Path) -> dict:
        """Reemplaza TODA la base en uso por la del archivo. Guarda antes una copia de la actual."""
        with tempfile.TemporaryDirectory(prefix="magi-restore-") as td:
            dbfile, key = self._unpack(Path(path), Path(td))
            try:
                src = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
            except sqlite3.Error:
                raise BackupError("El archivo no es una base de datos de MAGI válida.") from None
            try:
                try:
                    if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise BackupError("La base del respaldo está dañada.")
                    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                except sqlite3.DatabaseError:
                    raise BackupError("El archivo no es una base de datos de MAGI válida.") from None
                if not REQUIRED_TABLES <= tables:
                    raise BackupError("El archivo no parece una base de MAGI (faltan tablas).")
                if src.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] < 1:
                    raise BackupError("El respaldo no tiene ningún usuario administrador.")
                if self.enabled:
                    try:
                        self.backup_db()          # red de seguridad: la base actual queda en backups/db
                    except Exception as exc:      # noqa: BLE001
                        raise BackupError(f"No se pudo guardar la copia de seguridad previa: {exc}") from None
                with self.db._lock:
                    src.backup(self.db._conn)
                    self.db._conn.executescript(SCHEMA)
                    self.db._migrate()
            finally:
                src.close()
        wrote_key = False
        if key and self.wg_state_dir:
            try:
                tmp = self.wg_state_dir / ".server.key.import.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(key + "\n")
                os.replace(tmp, self.wg_state_dir / "server.key.import")
                wrote_key = True
            except OSError as exc:
                log.warning("No se pudo dejar la clave del túnel para importar: %s", exc)
        self._errors.clear()
        self._router_checked.clear()
        log.warning("Base restaurada desde un respaldo subido (clave del túnel: %s)", "sí" if wrote_key else "no")
        return {"ok": True, "wg_key": wrote_key, "had_wg_key": bool(key)}

    def forget_host_key(self, router_id: int) -> None:
        self.db.execute("DELETE FROM settings WHERE key=?", (f"sshkey:{router_id}",))
        self._errors.pop(router_id, None)

    def forget_router(self, router_id: int) -> None:
        self._router_checked.pop(router_id, None)
        self._errors.pop(router_id, None)
        self.db.execute("DELETE FROM settings WHERE key=?", (f"sshkey:{router_id}",))
