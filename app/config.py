"""Configuración del servidor, leída de variables de entorno (MKMON_*)."""

import os
import tempfile
from dataclasses import dataclass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    admin_user: str
    admin_password: str
    register_token: str
    public_view: bool
    demo: bool
    telegram_token: str
    telegram_chat: str
    syslog_port: int
    # Conexión remota por WireGuard (ver app/wg.py)
    backup_dir: str = ""                                    # por defecto, <carpeta de la base>/backups
    wg_network: str = "10.77.0.0/24"
    wg_port: int = 51821
    wg_endpoint: str = ""                                  # IP pública o dominio del servidor
    wg_state_dir: str = "/var/lib/mk-monitor/wg"           # donde la app deja la lista de peers
    wg_run_dir: str = "/run/mk-monitor-wg"                 # donde el ayudante root publica el estado
    wg_pub_path: str = "/etc/mk-monitor/wg/server.pub"     # clave pública del servidor


def load() -> Config:
    demo = _bool(os.environ.get("MKMON_DEMO"))
    default_db = ":memory:" if demo else "/var/lib/mk-monitor/mkmonitor.db"
    return Config(
        host=os.environ.get("MKMON_HOST", "0.0.0.0"),
        port=int(os.environ.get("MKMON_PORT", "8080")),
        db_path=os.environ.get("MKMON_DB", default_db),
        admin_user=os.environ.get("MKMON_ADMIN_USER", "admin"),
        admin_password=os.environ.get("MKMON_ADMIN_PASSWORD", ""),
        register_token=os.environ.get("MKMON_REGISTER_TOKEN", ""),
        public_view=_bool(os.environ.get("MKMON_PUBLIC_VIEW")),
        demo=demo,
        telegram_token=os.environ.get("MKMON_TELEGRAM_TOKEN", ""),
        telegram_chat=os.environ.get("MKMON_TELEGRAM_CHAT", ""),
        # 0 = no escuchar syslog. En modo demo no se abre ningún puerto salvo que se pida.
        syslog_port=int(os.environ.get("MKMON_SYSLOG_PORT", "0" if demo else "5140")),
        backup_dir=os.environ.get("MKMON_BACKUP_DIR", ""),
        wg_network=os.environ.get("MKMON_WG_NETWORK", "10.77.0.0/24"),
        wg_port=int(os.environ.get("MKMON_WG_PORT", "51821")),
        wg_endpoint=os.environ.get("MKMON_WG_ENDPOINT", ""),
        wg_state_dir=os.environ.get("MKMON_WG_STATE_DIR", tempfile.mkdtemp(prefix="magi-wg-") if demo else "/var/lib/mk-monitor/wg"),
        wg_run_dir=os.environ.get("MKMON_WG_RUN_DIR", "/run/mk-monitor-wg"),
        wg_pub_path=os.environ.get("MKMON_WG_PUB", "/etc/mk-monitor/wg/server.pub"),
    )
