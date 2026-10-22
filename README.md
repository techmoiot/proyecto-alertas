# MK Monitor

Panel web de alertas para MikroTik (RouterOS 7). Lee las **interfaces físicas**
(`ether1`, `sfp-sfpplus1`, etc.) de uno o varios routers por la API y avisa en
pantalla, con sonido y color, cuando:

| Alerta | Cuándo salta |
|---|---|
| **Enlace caído** | una interfaz que ya estuvo arriba pierde el enlace |
| **Tope superado** | la entrada (RX) o salida (TX) pasa un tope fijo que configurás por interfaz |
| **Consumo anormal** | una interfaz consume varias veces lo que consume habitualmente (aprende el promedio sola) |
| **Router sin respuesta** | el MikroTik deja de contestar por la API |

Es liviano (Python + SQLite, sin Docker ni servicios externos): medido en pruebas,
usa unos 55 MB de RAM con dos routers, así que entra en un LXC chico.

> **Etapa 1 (esto):** interfaces físicas. **Etapa 2 (pendiente):** clientes PPPoE
> (desconexiones por log, consumo por cliente). Ver [Próximos pasos](#próximos-pasos).

## Instalación en el LXC

Requisitos: Debian 12 / Ubuntu 22.04 o más nuevo (Python 3.10+), acceso root y
que el LXC llegue por red a los MikroTik (puerto 8728).

```bash
git clone https://github.com/techmoiot/proyecto-alertas
cd proyecto-alertas
sudo ./install.sh
```

Al terminar muestra la URL del panel y la contraseña de `admin`. Para actualizar
más adelante: `git pull && sudo ./install.sh` (conserva configuración y datos).

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
de consumo y una caída de enlace que se repiten cada 6 minutos.

## Configuración

Variables en `/etc/mk-monitor/mk-monitor.env` (ver `mk-monitor.env.example`):
`MKMON_PORT`, `MKMON_ADMIN_USER`, `MKMON_ADMIN_PASSWORD`, `MKMON_REGISTER_TOKEN`,
`MKMON_PUBLIC_VIEW`, `MKMON_TELEGRAM_*`. Los datos viven en `/var/lib/mk-monitor/`
(SQLite) y se guarda 7 días de historial de tráfico (configurable).

## Seguridad

- Usá el panel en una red de confianza (LAN / VLAN de gestión). Usa HTTP Basic sobre
  HTTP: la contraseña viaja sin cifrar. Si lo exponés fuera, ponelo detrás de un
  proxy con HTTPS.
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

Cubren el motor de alertas, la API y la lectura contra un servidor falso del
protocolo API de RouterOS (login, filtro de interfaces, caída y recuperación de enlace).

## Próximos pasos

- **Clientes PPPoE:** alerta de desconexión leyendo `/ppp/active` y el log del
  MikroTik (syslog), y consumo anormal por cliente.
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
