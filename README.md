# MAGI

*Sistema de monitoreo de red (nombre inspirado en las tres computadoras MAGI de Evangelion).*

Panel web de alertas para MikroTik (RouterOS 7). Lee las **interfaces físicas**
(`ether1`, `sfp-sfpplus1`, etc.) de uno o varios routers por la API y avisa en
pantalla, con sonido y color, cuando:

| Alerta | Cuándo salta |
|---|---|
| **Enlace caído** | una interfaz que ya estuvo arriba pierde el enlace |
| **Tope superado** | la entrada (RX) o salida (TX) pasa un tope fijo que configurás por interfaz |
| **Consumo anormal** | una interfaz consume varias veces lo que consume habitualmente (aprende el promedio sola) |
| **Router sin respuesta** | el MikroTik deja de contestar por la API |
| **Cliente PPPoE desconectado** | un cliente que estaba conectado desaparece de `/ppp/active` (con el motivo, si llega el log) |
| **Caída masiva PPPoE** | se caen muchos clientes a la vez: UNA alerta crítica en vez de cientos |
| **Sector caído (PON)** | la mayoría de los clientes de un PON/VLAN se caen juntos: se ve en el mapa de la red |

Es liviano (Python + SQLite, sin Docker ni servicios externos): medido en pruebas,
usa unos 55 MB de RAM con dos routers, así que entra en un LXC chico.

> **Etapa 1:** interfaces físicas. **Etapa 2 (esto):** desconexiones de clientes PPPoE.
> El consumo por cliente todavía no está. Ver [Próximos pasos](#próximos-pasos).

## Instalación en el LXC

Requisitos: Debian 12 / Ubuntu 22.04 o más nuevo (Python 3.10+), acceso root y
que el LXC llegue por red a los MikroTik (puerto 8728).

```bash
git clone https://github.com/techmoiot/proyecto-alertas
cd proyecto-alertas
sudo ./install.sh
```

Al terminar muestra la URL del panel y la contraseña de `admin`. Para actualizar
más adelante, desde cualquier carpeta: **`magi-update`** (conserva configuración y datos).
La primera vez, si todavía no existe ese comando:
`curl -fsSL https://raw.githubusercontent.com/techmoiot/proyecto-alertas/main/update.sh | bash`.

Queda como servicio de systemd (`mk-monitor`):

```bash
systemctl status mk-monitor
journalctl -u mk-monitor -f        # log en vivo
```

## Conectar un MikroTik

1. Entrá al panel → **Configuración** → **Conectar un MikroTik nuevo**.
2. Copiá el script y pegalo en una terminal del MikroTik (Winbox → New Terminal).
3. En unos segundos el router aparece en el **Panel**.

El script crea el usuario `mkmonitor` con permisos **solo de lectura**, limita la
API a la IP del servidor y registra el router en el panel. Sirve igual para el
primer MikroTik que para los siguientes: es el mismo paso para todos.

> El script reemplaza la lista de IPs permitidas del servicio `api` por la del
> servidor. Si usás la API del router desde otro lado, ajustalo después en
> `IP → Services`.

**Alternativa manual:** en el MikroTik, `/user group add name=monitor-ro policy=read,api`
y `/user add name=mkmonitor group=monitor-ro password=...`; después cargalo en
**Configuración → Agregar manualmente** y usá **Probar conexión**.

En RouterOS 7 los puertos SFP/SFP+ son interfaces de tipo `ether`, así que con la
configuración por defecto se leen todos. Las interfaces PPPoE de clientes **no se
leen** (el filtro se hace en el router, no se bajan las ~1500 en cada lectura).
Si querés incluir otra por nombre (`bridge1`, `bonding1`), agregala en
*Interfaces extra por nombre*.

## Router REMOTO (el servidor no está junto al MikroTik)

Si el router está en **otra red** (otro sitio, detrás de NAT/CGNAT), se conecta por un túnel
**WireGuard**: el router llama al servidor (conexión saliente) y todo —API de RouterOS y log
de PPPoE— viaja cifrado por ese túnel con IP privadas (`10.77.0.0/24`). La API del MikroTik
**no se abre a Internet**.

1. En el panel: **Configuración → Router REMOTO**. Cargá la **IP pública o dominio** del
   servidor y guardá.
2. En el router de tu sitio (donde está el servidor), reenviá el **UDP 51821** (o el que muestre el panel; el instalador evita el 51820 si ya lo usa otro WireGuard) hacia el servidor.
3. Escribí el nombre del router remoto y **Crear router remoto**. Se genera un script con
   todo adentro (claves del túnel, usuario de solo lectura, firewall, log): no hace falta
   token ni aviso.
4. Pegalo en una terminal del MikroTik. En segundos el estado pasa a **CONECTADO** y el
   router empieza a monitorearse solo (hasta entonces queda pausado, sin alertas falsas).

El script se puede volver a ver desde la tabla «Estado de los túneles» (**Ver script**).
Para quitarlo, eliminá el router: se libera su IP del túnel.

> **Requisito en LXC / Proxmox:** el *host* debe tener el módulo WireGuard
> (`modprobe wireguard`, y para que cargue siempre `echo wireguard >> /etc/modules`).
> `install.sh` lo comprueba y te avisa si no se puede. Si tu CT es *unprivileged* y falla,
> usá un CT *privileged* o corré WireGuard en el host. El servicio queda así: la app
> (sin privilegios) escribe la lista de routers y un ayudante root de systemd
> (`mk-monitor-wg`) valida cada entrada y arma la configuración de WireGuard.
>
> **Sin probar contra un RouterOS real** (el script y el túnel): si algo falla en el
> router, pegame el mensaje que muestra la terminal.

## Cómo se alerta

- **Velocidad:** se calcula con los contadores `rx-byte`/`tx-byte` cada 10 s (configurable).
- **Enlace caído:** solo si la interfaz estuvo arriba alguna vez, así los puertos
  vacíos no generan alertas. Las interfaces deshabilitadas en el router tampoco.
- **Tope fijo:** por interfaz (o un tope global por defecto). Alerta si se
  mantiene 30 s sobre el tope y se resuelve al bajar del 90 %.
- **Consumo anormal:** cada interfaz tiene un promedio habitual que se actualiza
  solo con tráfico normal (se congela durante un pico, para que el pico no se
  vuelva "lo normal"). Alerta si el consumo es ≥ 3× lo habitual, ≥ 50 Mbps y se
  sostiene 60 s. Las interfaces nuevas aprenden 30 min antes de poder alertar.
- Las alertas se **reconocen** desde el panel (deja de sonar, sigue visible hasta que
  se resuelva) y quedan en el **Historial**.

Todos los umbrales se cambian en **Configuración → Umbrales y detección**, y cada
interfaz puede tener su etiqueta, sus topes y su propio on/off.

## Clientes PPPoE (desconexiones)

Cada ~15 s se lee `/ppp/active` de cada router y se compara con la lectura anterior.
Eso es lo que **decide** si un cliente está caído; el log del MikroTik solo agrega el
**motivo**. Así, si el log llega en un formato raro, las alertas siguen funcionando.

- **Gracia (60 s):** si el cliente vuelve antes, no se alerta (en PPPoE es habitual).
- **Caída masiva:** si se caen 20 o más clientes en 60 s, sale una sola alerta crítica
  que se va actualizando («quedan N sin volver») y no se crean alertas individuales.
- **Vencimiento (24 h):** los clientes que no vuelven (bajas, cortes) dejan de alertar
  pasado ese tiempo, pero siguen listados en la pestaña **PPPoE**.
- Al reconectarse, la alerta se resuelve sola y sin mensaje de Telegram.
- La pestaña **PPPoE** muestra clientes en línea por router, los caídos ahora con su
  motivo, un buscador (usuario / IP / MAC) y las últimas desconexiones.
- En el panel principal solo se muestran los 5 más recientes y un «Ver todos».

**Motivo de la desconexión:** el script de conexión configura el MikroTik para enviar
el log de PPPoE por syslog (UDP) a este servidor, puerto `5140` (`MKMON_SYSLOG_PORT`,
`0` = apagado). Si hay firewall en el LXC, abrí **UDP 5140**. En la pestaña PPPoE hay
un cuadro «Log recibido (diagnóstico)» que muestra lo que llega y si se entendió.

> **Sin probar contra un RouterOS real:** el formato exacto de las líneas de log puede
> variar. Si el motivo no aparece, copiá 3–4 líneas del cuadro de diagnóstico y se ajusta
> el intérprete.

Los routers que ya estaban conectados necesitan volver a correr el script (o agregar a
mano el envío de log) para mandar el syslog; la lectura de `/ppp/active` funciona sin eso,
con el mismo usuario de solo lectura.

## Mapa de la red (OLT → PON → pools → clientes)

Pestaña **Red**: un grafo estilo Obsidian con la OLT como punto central, un nodo por
PON (una VLAN por PON), los dos pools de cada VLAN —**Internet** (azul) y **TV** (violeta)—
y cada cliente como un punto unido a su pool. Verde = conectado, ámbar = en espera,
rojo = caído. Si un sector se cae, el PON se pone rojo y pulsa, aparece un halo rojo
sobre todo su grupo y se genera una alerta crítica «Sector caído».

- **Carga:** *Configuración → OLT y PON*. Poné el nombre, la cantidad de PON (16, o los
  puertos que tenga tu OLT), la VLAN del primero y la red de cada VLAN, por ejemplo
  `10.10.{n}.0/24` (`{n}` = número de PON, `{v}` = VLAN). Cada red se parte en dos /25:
  la primera mitad es Internet y la segunda TV. Después podés corregir cada fila a mano.
- **Detectar desde el MikroTik:** en el mismo formulario, «Detectar VLAN y redes» lee del
  router elegido las interfaces VLAN y sus IP (`/interface/vlan` y `/ip/address`, con el
  usuario de solo lectura) y arma la tabla: un PON por VLAN, con su red partida en dos
  mitades (Internet / TV). Se puede limitar por rango de VLAN. Es una propuesta: revisala,
  quitá las VLAN que no son PON (gestión, etc.) y recién ahí guardás. Si una VLAN tiene una
  red más chica que /24, entra entera como Internet.
- **Cómo ubica a los clientes:** por la **IP** que les da el PPPoE (`/ppp/active`). No hay
  que cargar nada por cliente; si la IP cae en el pool de un PON, el cliente va ahí.
  Los que no caen en ningún pool se cuentan aparte («fuera de las VLAN»).
- **Alerta de sector:** se dispara cuando al menos el 50 % de los clientes conocidos de un
  PON están caídos a la vez (mínimo 4 clientes, sostenido 30 s). Se resuelve cuando bajan
  de la mitad de ese umbral. Los tres valores se cambian en *Umbrales y detección*.
  Es independiente de la «caída masiva» y de las alertas por cliente: te dice **qué sector**
  es, no solo cuántos clientes.
- **Uso:** tocá un PON para ver su detalle (ocupación de cada pool y clientes caídos),
  doble toque para acercar, rueda o pellizco para el zoom, arrastrá para mover. Hay botón
  de pantalla completa para la TV.
- **Limitación:** solo se ven los clientes que aparecen en `/ppp/active` (PPPoE). Si los
  decodificadores de TV no hacen PPPoE (IPoE/DHCP), el pool de TV figura vacío.

## Pantalla / TV

Abrí el panel en el navegador de la pantalla. El sonido se activa con el botón
**Sonido** (el navegador exige un clic la primera vez). Para que la pantalla no
pida contraseña, poné `MKMON_PUBLIC_VIEW=1` en `/etc/mk-monitor/mk-monitor.env`:
cualquiera en la red podrá **ver**, pero reconocer alertas y configurar sigue
pidiendo usuario y contraseña.

## Telegram (opcional)

Creá un bot con @BotFather y agregá a `/etc/mk-monitor/mk-monitor.env`:

```
MKMON_TELEGRAM_TOKEN=123456:ABC...
MKMON_TELEGRAM_CHAT=-1001234567890
```

Después `sudo systemctl restart mk-monitor`. Avisa cuando salta y cuando se resuelve cada alerta.

## Probarlo sin MikroTik (modo demo)

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
MKMON_DEMO=1 MKMON_ADMIN_PASSWORD=demo venv/bin/python -m app
```

Abrí http://localhost:8080 (usuario `admin`). Simula dos routers con tráfico, un pico
de consumo, una caída de enlace, caídas de clientes PPPoE (individuales y una masiva)
y una OLT de 6 PON con sectores que se caen, todo repetido cada 6 minutos.

## Configuración

Variables en `/etc/mk-monitor/mk-monitor.env` (ver `mk-monitor.env.example`):
`MKMON_PORT`, `MKMON_ADMIN_USER`, `MKMON_ADMIN_PASSWORD`, `MKMON_REGISTER_TOKEN`,
`MKMON_PUBLIC_VIEW`, `MKMON_SYSLOG_PORT`, `MKMON_WG_PORT`, `MKMON_WG_NETWORK`, `MKMON_TELEGRAM_*`. Los datos viven en `/var/lib/mk-monitor/`
(SQLite) y se guarda 7 días de historial de tráfico (configurable).

## Seguridad

- El acceso es una **pantalla de login** con sesión por cookie (HttpOnly, firmada; 12 h, o
  30 días con «mantener la sesión»). Cambiar la contraseña cierra todas las sesiones, y tras
  6 intentos fallidos desde una IP se bloquea 5 minutos. Los scripts y `curl` pueden seguir
  usando usuario y contraseña HTTP (Basic).
- El panel va por **HTTP**: la contraseña y la cookie viajan sin cifrar. Usalo en una red de
  confianza o ponelo detrás de un proxy con HTTPS (con HTTPS la cookie se marca `Secure`
  sola si el proxy manda `X-Forwarded-Proto: https`).
- El usuario de la API en el MikroTik es de **solo lectura** y está limitado a la IP del servidor.
- Las contraseñas de los routers se guardan en la base (`/var/lib/mk-monitor`, solo
  accesible por el usuario de servicio). Por eso el usuario es de solo lectura.
- El token de registro permite agregar routers: no lo compartas.
- API con SSL (puerto 8729): se puede activar por router, pero no se valida el
  certificado. Con red de gestión propia, la API normal alcanza.

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest
```

Cubren el motor de alertas, la API, el monitor PPPoE (gracia, caída masiva, vencimiento,
reinicio del servidor), el receptor syslog por UDP real y la lectura contra un servidor
falso del protocolo API de RouterOS (login, interfaces, `/ppp/active`, caída y recuperación).

## Próximos pasos

- **Consumo de las interfaces de la OLT / ONT** (hoy el mapa muestra conectado/caído).
- **Consumo anormal por cliente PPPoE** (queues o contadores por sesión).
- Alertas por caída anormal de tráfico (enlace arriba pero sin tráfico).
- Más de un usuario / usuarios de solo lectura.

## Estructura

```
app/            backend (API, motor de alertas, lectura RouterOS) y panel web (app/static)
routeros/       script para pegar en cada MikroTik
systemd/        unidad del servicio
tests/          pruebas automáticas
install.sh      instalador para el LXC  ·  uninstall.sh
```


## Usuarios y contraseñas

En **Configuración → Usuarios y contraseñas** (solo administradores) podés agregar usuarios, cambiarles la
contraseña, darles permiso de **Administrador** (cambia todo) o **Solo ver** (mira el panel y el mapa, ideal
para la pantalla/TV) y eliminarlos. Cada usuario puede cambiar su propia contraseña con la actual. Las
contraseñas se guardan con scrypt (nunca en claro) y, al cambiarlas, las sesiones abiertas de ese usuario se cierran.
Siempre queda al menos un administrador.

El usuario inicial (`admin`) se crea en la primera instalación con la contraseña que muestra el instalador;
desde ahí manda la base de datos (`MKMON_ADMIN_PASSWORD` solo siembra el primer arranque).
**Si olvidás la contraseña**, en el servidor: `magi-passwd` (o `magi-passwd otrousuario`) pide una nueva y deja
a ese usuario como administrador.

## Tarjetas de la OLT

Al agregar o editar una OLT elegís **cuántas tarjetas** tiene y **cuántos puertos PON por tarjeta**
(típico 8 o 16). «Generar» crea todos los PON (`PON 2/3` = tarjeta 2, puerto 3) y cada fila tiene su
selector de tarjeta para corregir a mano; «Detectar» desde el MikroTik los reparte entre las tarjetas.
En el mapa, los PON se agrupan por tarjeta con una banda que se pone roja/naranja si caen sus PON.

## Registrados vs. conectados (PPPoE)

Además de `/ppp/active` (los conectados), MAGI lee cada minuto `/ppp/secret` (los usuarios creados en el router).
En la pestaña **PPPoE** cada router muestra «N en línea de M registrados» y la tarjeta **Registrados sin conexión**
lista a los que no tienen sesión: su última desconexión (`last-logged-out` del router), el motivo
(`last-disconnect-reason`, o el del log si llega por syslog), perfil y nota/comentario. Distingue entre *sin conexión*,
*deshabilitado* (suspendido) y *nunca se conectó*. Esta lista no genera alertas. Si el router usa RADIUS no tiene
Secrets y la lista queda vacía.
