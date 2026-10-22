#!/usr/bin/env bash
# Desinstala MK Monitor. Conserva la base de datos y la configuración salvo que
# se pase --purge.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Ejecutalo como root: sudo ./uninstall.sh" >&2; exit 1; }

systemctl disable --now mk-monitor 2>/dev/null || true
rm -f /etc/systemd/system/mk-monitor.service
systemctl daemon-reload
rm -rf /opt/mk-monitor

if [ "${1:-}" = "--purge" ]; then
  rm -rf /etc/mk-monitor /var/lib/mk-monitor
  userdel mkmonitor 2>/dev/null || true
  echo "MK Monitor desinstalado y datos borrados."
else
  echo "MK Monitor desinstalado. Se conservaron /etc/mk-monitor y /var/lib/mk-monitor"
  echo "(usá --purge para borrarlos)."
fi
