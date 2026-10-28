#!/usr/bin/env bash
# Instala (o actualiza) MAGI en un LXC/servidor Debian o Ubuntu.
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
apt-get install -y -qq python3 python3-venv python3-pip curl iproute2 wireguard-tools >/dev/null

python3 - <<'PY' || die "Hace falta Python 3.10 o más nuevo (Debian 12 / Ubuntu 22.04 o superior)."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

say "Copiando la aplicación a $APP_DIR"
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/app" "$APP_DIR/routeros" "$APP_DIR/scripts"
cp -a "$SRC/app" "$SRC/routeros" "$SRC/scripts" "$SRC/requirements.txt" "$APP_DIR/"
chmod 755 "$APP_DIR/scripts/wg-apply.py"
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
mkdir -p "$DATA_DIR/wg"
chown "$SVC_USER":"$SVC_USER" "$DATA_DIR/wg"

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

install -m 755 "$SRC/update.sh" /usr/local/bin/magi-update

say "Preparando la conexión remota (WireGuard)"
mkdir -p "$CONF_DIR/wg"
chmod 755 "$CONF_DIR" "$CONF_DIR/wg"
if [ ! -f "$CONF_DIR/wg/server.key" ]; then
  umask 077
  wg genkey > "$CONF_DIR/wg/server.key"
  umask 022
fi
wg pubkey < "$CONF_DIR/wg/server.key" > "$CONF_DIR/wg/server.pub"
chmod 600 "$CONF_DIR/wg/server.key"
chmod 644 "$CONF_DIR/wg/server.pub"
grep -q '^MKMON_WG_PORT=' "$ENV_FILE" || printf '\n# Conexión remota (WireGuard)\nMKMON_WG_NETWORK=10.77.0.0/24\nMKMON_WG_PORT=51820\n' >> "$ENV_FILE"

say "Instalando los servicios de systemd"
cp "$SRC/systemd/mk-monitor.service" /etc/systemd/system/mk-monitor.service
cp "$SRC/systemd/mk-monitor-wg.service" "$SRC/systemd/mk-monitor-wg.timer" "$SRC/systemd/mk-monitor-wg.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable mk-monitor >/dev/null 2>&1
systemctl restart mk-monitor
WG_OK=0
if ip link add magi-test type wireguard 2>/dev/null; then
  ip link del magi-test
  WG_OK=1
  systemctl enable --now mk-monitor-wg.path mk-monitor-wg.timer >/dev/null 2>&1 || true
  systemctl start mk-monitor-wg.service || true
fi

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
printf '\n\033[1;32mListo. MAGI está corriendo.\033[0m\n\n'
printf '  Panel:      http://%s:%s\n' "$IP" "$PORT"
printf '  Log PPPoE:  UDP %s (abrilo en el firewall si lo usás)\n' "$(grep -E '^MKMON_SYSLOG_PORT=' "$ENV_FILE" | cut -d= -f2 | grep . || echo 5140)"
if [ "$FIRST_INSTALL" -eq 1 ]; then
  printf '  Usuario:    admin\n  Contraseña: %s\n\n' "$ADMIN_PASS"
  printf '  (Quedan guardados en %s)\n' "$ENV_FILE"
else
  printf '  Usuario y contraseña: los de %s\n' "$ENV_FILE"
fi
printf '\nPara actualizar más adelante, desde cualquier carpeta:  magi-update\n'
printf '\nSiguiente paso: entrá al panel -> Configuración -> "Conectar un MikroTik nuevo"\n'
printf '(mismo sitio que el servidor) o "Router REMOTO" (por Internet, con túnel WireGuard).\n'
if [ "$WG_OK" -eq 1 ]; then
  printf '  Router remoto: abrí UDP %s hacia este servidor (reenvío de puerto en tu router).\n\n' "$(grep -E '^MKMON_WG_PORT=' "$ENV_FILE" | cut -d= -f2 | grep . || echo 51820)"
else
  printf '\n\033[1;33mAviso:\033[0m este equipo no puede crear interfaces WireGuard (en un LXC suele faltar el módulo en el host).\n'
  printf 'La conexión de routers REMOTOS no va a funcionar hasta habilitarlo; ver README, sección "Router remoto".\n\n'
fi
