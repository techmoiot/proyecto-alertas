# ============================================================================
#  MK Monitor - script de conexión para MikroTik (RouterOS 7)
#
#  Pegá TODO este bloque (desde "{" hasta "}") en una terminal del MikroTik:
#  Winbox -> New Terminal. No hace falta subir archivos.
#
#  Qué hace:
#   1. Crea el grupo "monitor-ro" y el usuario "mkmonitor" con permiso
#      SOLO de lectura (no puede cambiar nada del router).
#   2. Habilita la API (puerto 8728) y la limita a la IP del servidor.
#      OJO: si ya limitabas la API a otras IPs, esa lista se reemplaza.
#   3. Le avisa al servidor de MK Monitor para que empiece a monitorear.
#
#  La contraseña del usuario se genera al azar y viaja una sola vez al servidor
#  por la red de gestión. Usalo en una red de confianza (LAN / VLAN de gestión).
# ============================================================================
{
# --- Datos del servidor (ya vienen cargados desde el panel) ---
:local serverUrl "__SERVER_URL__"
:local serverIp "__SERVER_IP__"
:local token "__TOKEN__"

# --- Opcional: cambiar solo si hace falta ---
:local apiUser "mkmonitor"
:local apiPort 8728
# IP de ESTE router con la que el servidor debe conectarse. Vacío = el servidor
# usa la IP desde la que llega este aviso.
:local mgmtIp ""

:local pass [:rndstr length=20 from="abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"]

# 1) Grupo y usuario de solo lectura
:if ([:len [/user group find name="monitor-ro"]] = 0) do={
  /user group add name="monitor-ro" policy=read,api comment="MK Monitor: solo lectura"
}
:if ([:len [/user find name=$apiUser]] = 0) do={
  /user add name=$apiUser group="monitor-ro" password=$pass comment="MK Monitor"
} else={
  /user set [find name=$apiUser] group="monitor-ro" password=$pass
}

# 2) API habilitada y limitada al servidor
/ip service set api disabled=no port=$apiPort
:if ($serverIp != "") do={
  /user set [find name=$apiUser] address=($serverIp . "/32")
  /ip service set api address=($serverIp . "/32")
}

# 3) Avisar al servidor
:local ident [/system identity get name]
:local hostPart ""
:if ($mgmtIp != "") do={ :set hostPart (",\"host\":\"" . $mgmtIp . "\"") }
:local body ("{\"name\":\"" . $ident . "\",\"port\":" . $apiPort . ",\"username\":\"" . $apiUser . "\",\"password\":\"" . $pass . "\"" . $hostPart . "}")
:do {
  :local res [/tool fetch url=($serverUrl . "/api/register") http-method=post http-header-field=("Content-Type: application/json,X-Register-Token: " . $token) http-data=$body output=user as-value]
  :put ("MK Monitor: router registrado. Respuesta del servidor: " . ($res->"data"))
} on-error={
  :put "MK Monitor: ERROR al avisar al servidor. Revisá serverUrl y el token, y que este router llegue al servidor."
}
}
