#!/usr/bin/env bash
# Actualiza MAGI a la última versión (o lo instala si no estaba). Se puede correr desde cualquier carpeta.
# Después de la primera vez queda disponible como:  magi-update
set -euo pipefail

REPO="${MAGI_REPO:-https://github.com/techmoiot/proyecto-alertas}"
DIR=/opt/magi-src

[ "$(id -u)" -eq 0 ] || { echo "Ejecutalo como root:  sudo magi-update" >&2; exit 1; }
command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin main
  git -C "$DIR" reset -q --hard origin/main
else
  rm -rf "$DIR"
  git clone -q "$REPO" "$DIR"
fi
cd "$DIR"
./install.sh
