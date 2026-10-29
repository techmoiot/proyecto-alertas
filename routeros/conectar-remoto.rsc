# ============================================================================
#  MAGI - script de conexión REMOTA para MikroTik (RouterOS 7)
#
#  Para un router que NO está en la misma red que el servidor. Pegá TODO este
#  bloque (desde "{" hasta "}") en una terminal del MikroTik: Winbox -> New Terminal.
#
#  Qué hace:
#   1. Crea un túnel WireGuard (interfaz "magi-wg") hacia el servidor. El router
#      es quien llama, así que funciona aunque esté detrás de NAT / CGNAT.
#   2. Abre en el firewall SOLO la API (y ping) para la IP del servidor dentro
#      del túnel. No se abre nada hacia Internet.
#   3. Crea el usuario "__API_USER__" de SOLO lectura y limita la API a esa IP.
#   4. Envía el log de PPPoE al servidor por el túnel (motivo de las desconexiones).
#
#  El servidor ya conoce este router: no hace falta ningún aviso ni token.
#  Este script contiene claves privadas: no lo compartas ni lo guardes en chats.
# ============================================================================
{
:local srvPub "__SRV_PUB__"
:local srvHost "__ENDPOINT__"
:local srvPort __WG_PORT__
:local srvIp "__SRV_IP__"
:local myPriv "__MY_PRIV__"
:local myAddr "__MY_ADDR__"
:local apiUser "__API_USER__"
:local apiPass "__API_PASS__"
:local apiPort __API_PORT__
:local syslogPort __SYSLOG_PORT__
:local wg "magi-wg"

# 1) Túnel WireGuard hacia el servidor
# Puerto local libre: si el router ya tiene otros WireGuard, no se puede repetir el 13231.
:local lp 13240
:while ([:len [/interface wireguard find where listen-port=$lp name!=$wg]] > 0) do={ :set lp ($lp + 1) }
:if ([:len [/interface wireguard find name=$wg]] = 0) do={
  /interface wireguard add name=$wg private-key=$myPriv listen-port=$lp comment="MAGI"
} else={
  /interface wireguard set [find name=$wg] private-key=$myPriv listen-port=$lp
}
/interface wireguard enable [find name=$wg]
/interface wireguard peers remove [find interface=$wg]
/interface wireguard peers add interface=$wg public-key=$srvPub endpoint-address=$srvHost endpoint-port=$srvPort allowed-address=($srvIp . "/32") persistent-keepalive=25s comment="MAGI"
/ip address remove [find interface=$wg]
/ip address add address=$myAddr interface=$wg comment="MAGI"

# 2) Firewall: solo API y ping desde el servidor, por el túnel
:if ([:len [/ip firewall filter find comment="MAGI api"]] = 0) do={
  :do {
    /ip firewall filter add chain=input action=accept in-interface=$wg src-address=($srvIp . "/32") protocol=tcp dst-port=$apiPort comment="MAGI api" place-before=0
    /ip firewall filter add chain=input action=accept in-interface=$wg src-address=($srvIp . "/32") protocol=icmp comment="MAGI ping" place-before=0
    /ip firewall filter add chain=input action=accept in-interface=$wg src-address=($srvIp . "/32") protocol=tcp dst-port=22 comment="MAGI ssh (respaldo de config)" place-before=0
  } on-error={
    /ip firewall filter add chain=input action=accept in-interface=$wg src-address=($srvIp . "/32") protocol=tcp dst-port=$apiPort comment="MAGI api"
    /ip firewall filter add chain=input action=accept in-interface=$wg src-address=($srvIp . "/32") protocol=icmp comment="MAGI ping"
    /ip firewall filter add chain=input action=accept in-interface=$wg src-address=($srvIp . "/32") protocol=tcp dst-port=22 comment="MAGI ssh (respaldo de config)"
  }
}

# 3) Usuario de solo lectura y API limitada al servidor
:if ([:len [/user group find name="monitor-ro"]] = 0) do={
  /user group add name="monitor-ro" policy=read,api,ssh comment="MAGI: solo lectura"
} else={
  /user group set [find name="monitor-ro"] policy=read,api,ssh
}
:if ([:len [/user find name=$apiUser]] = 0) do={
  /user add name=$apiUser group="monitor-ro" password=$apiPass comment="MAGI"
} else={
  /user set [find name=$apiUser] group="monitor-ro" password=$apiPass
}
/user set [find name=$apiUser] address=($srvIp . "/32")
/ip service set api disabled=no port=$apiPort address=($srvIp . "/32")

# 4) Log de PPPoE hacia el servidor (por el túnel)
:if ($syslogPort > 0) do={
  :if ([:len [/system logging action find name="magi"]] = 0) do={
    /system logging action add name="magi" target=remote remote=$srvIp remote-port=$syslogPort
  } else={
    /system logging action set [find name="magi"] remote=$srvIp remote-port=$syslogPort
  }
  :if ([:len [/system logging find action="magi"]] = 0) do={
    /system logging add topics=pppoe action="magi"
    /system logging add topics=account action="magi"
  }
}

:put "MAGI: túnel configurado para __NAME__. En el panel debería aparecer CONECTADO en unos segundos."
:put "Si no conecta: /interface wireguard peers print  (mirá last-handshake) y revisá que el servidor tenga abierto el puerto UDP __WG_PORT__."
}
