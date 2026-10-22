#!/usr/bin/env bash
# Instala (o actualiza) MK Monitor en un LXC/servidor Debian o Ubuntu.
# Uso:  sudo ./install.sh
set -euo pipefail

APP_DIR=/opt/mk-monitor
CONF_DIR=/etc/mk-monitor
DATA_DIR=/var/lib/mk-monitor
ENV_FILE="$CONF_DIR/mk-monitor.env"
SVC_USER=mkmonitor
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Ejecutalo como root:  sudo ./install.sh"
command -v apt-get >/dev/null || die "Este instalador es para Debian/Ubuntu (usa apt)."
command -v systemctl >/dev/null || die "Hace falta systemd."

say "Instalando paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null

python3 - <<'PY' || die "Hace falta Python 3.10 o más nuevo (Debian 12 / Ubuntu 22.04 o superior)."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

say "Copiando la aplicación a $APP_DIR"
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/app" "$APP_DIR/routeros"
cp -a "$SRC/app" "$SRC/routeros" "$SRC/requirements.txt" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

say "Preparando el entorno de Python"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "Usuario de servicio y permisos"
id -u "$SVC_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

FIRST_INSTALL=0
if [ ! -f "$ENV_FILE" ]; then
  FIRST_INSTALL=1
  say "Creando la configuración en $ENV_FILE"
  gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(12))'; }
  ADMIN_PASS="$(gen)"
  REG_TOKEN="$(gen)"
  sed -e "s|^MKMON_ADMIN_PASSWORD=.*|MKMON_ADMIN_PASSWORD=$ADMIN_PASS|" \
      -e "s|^MKMON_REGISTER_TOKEN=.*|MKMON_REGISTER_TOKEN=$REG_TOKEN|" \
      "$SRC/mk-monitor.env.example" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  say "Se conserva la configuración existente ($ENV_FILE)"
fi

say "Instalando el servicio de systemd"
cp "$SRC/systemd/mk-monitor.service" /etc/systemd/system/mk-monitor.service
systemctl daemon-reload
systemctl enable mk-monitor >/dev/null 2>&1
systemctl restart mk-monitor

PORT="$(grep -E '^MKMON_PORT=' "$ENV_FILE" | cut -d= -f2)"
PORT="${PORT:-8080}"

say "Esperando que el servicio responda"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  journalctl -u mk-monitor -n 30 --no-pager || true
  die "El servicio no arrancó. Mirá el log con:  journalctl -u mk-monitor -e"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-IP-DEL-SERVIDOR}"
printf '\n\033[1;32mListo. MK Monitor está corriendo.\033[0m\n\n'
printf '  Panel:      http://%s:%s\n' "$IP" "$PORT"
printf '  Log PPPoE:  UDP %s (abrilo en el firewall si lo usás)\n' "$(grep -E '^MKMON_SYSLOG_PORT=' "$ENV_FILE" | cut -d= -f2 | grep . || echo 5140)"
if [ "$FIRST_INSTALL" -eq 1 ]; then
  printf '  Usuario:    admin\n  Contraseña: %s\n\n' "$ADMIN_PASS"
  printf '  (Quedan guardados en %s)\n' "$ENV_FILE"
else
  printf '  Usuario y contraseña: los de %s\n' "$ENV_FILE"
fi
printf '\nSiguiente paso: entrá al panel -> Configuración -> "Conectar un MikroTik nuevo"\n'
printf 'y pegá el script en el MikroTik.\n\n'
