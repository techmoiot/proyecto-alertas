#!/usr/bin/env bash
# Desinstala MAGI. Conserva la base de datos y la configuración salvo que
# se pase --purge.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Ejecutalo como root: sudo ./uninstall.sh" >&2; exit 1; }

systemctl disable --now mk-monitor mk-monitor-wg.path mk-monitor-wg.timer 2>/dev/null || true
systemctl disable --now wg-quick@magi0 2>/dev/null || true
rm -f /etc/systemd/system/mk-monitor.service /etc/systemd/system/mk-monitor-wg.* /etc/wireguard/magi0.conf
systemctl daemon-reload
rm -rf /opt/mk-monitor /opt/magi-src
rm -f /usr/local/bin/magi-update /usr/local/bin/magi-passwd /usr/local/bin/magi-restore

if [ "${1:-}" = "--purge" ]; then
  rm -rf /etc/mk-monitor /var/lib/mk-monitor
  userdel mkmonitor 2>/dev/null || true
  echo "MAGI desinstalado y datos borrados."
else
  echo "MAGI desinstalado. Se conservaron /etc/mk-monitor y /var/lib/mk-monitor"
  echo "(usá --purge para borrarlos)."
fi
