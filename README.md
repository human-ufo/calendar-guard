# calendar-guard

Alertas de escritorio para eventos próximos de Google Calendar, en todas tus
cuentas a la vez. Pensado para no olvidarte jamás de una entrevista cuando
manejás varias cuentas de Gmail.

## Cómo funciona

- [gogcli](https://github.com/openclaw/gogcli) (`gog`) maneja la
  autenticación OAuth multi-cuenta y el acceso a la API de Calendar.
- `calguard.py` es un daemon que cada `poll_seconds` consulta los eventos de
  las próximas `window_hours` en cada cuenta autenticada.
- Cuando un evento cruza uno de los umbrales configurados (por defecto 60 y
  15 minutos antes), dispara una notificación de escritorio con
  `notify-send` (título, cuenta, hora y link de Meet si existe).
- Un SQLite local recuerda qué alertas ya se emitieron: no hay duplicados,
  y si un evento se reagenda (cambia su hora) vuelve a alertar.

Solo usa la biblioteca estándar de Python 3.11+.

## Requisitos

- Python 3.11+
- [gogcli](https://github.com/openclaw/gogcli) (`gog`) instalado
- Un escritorio con `notify-send` (GNOME/KDE/etc.)
- Un proyecto de Google Cloud con un OAuth client de escritorio (setup abajo)

## Setup 1: proyecto de Google Cloud (una sola vez)

Sirve para todas tus cuentas. Elegí cualquier cuenta tuya como "dueña" del
proyecto; no gana ningún acceso especial sobre las demás.

1. En [console.cloud.google.com](https://console.cloud.google.com), creá un
   proyecto (por ejemplo `calendar-guard`).
2. Activá la **Google Calendar API** (APIs & Services → Library).
3. OAuth consent screen: tipo **External**, estado **Testing**.
4. Agregá **cada una de tus cuentas Gmail como test users** (límite: 100).
5. Credentials → Create credentials → **OAuth client ID**, tipo
   **Desktop app**. Descargá el JSON (`client_secret_*.json`).

Nota: para apps en modo Testing, los tokens de los test users son de larga
duración. Si un token muere (por ejemplo, si cambiás el consent screen),
`calguard` lo detecta y te pide re-autorizar solo esa cuenta.

## Setup 2: cuentas en gog

```bash
# cliente OAuth (una sola vez)
gog auth credentials set ~/Downloads/client_secret_*.json

# una vez por cuenta (abre el browser, elegís la cuenta, autorizás)
gog auth add tu.personal@gmail.com --services calendar --readonly
gog auth add otra.cuenta@gmail.com --services calendar --readonly

# verificación
gog auth doctor --check
```

Los refresh tokens quedan en el keyring del sistema.

## Instalación

```bash
git clone https://github.com/human-ufo/calendar-guard.git ~/calendar-guard

# config a gusto (opcional; sin config usa defaults)
mkdir -p ~/.config/calguard
cp ~/calendar-guard/config.example.toml ~/.config/calguard/config.toml

# servicio de usuario
mkdir -p ~/.config/systemd/user
cp ~/calendar-guard/systemd/calguard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now calguard
```

## Configuración

`~/.config/calguard/config.toml` (ver `config.example.toml`):

| Clave                | Default  | Descripción                                        |
| -------------------- | -------- | -------------------------------------------------- |
| `window_hours`       | `24`     | Ventana hacia adelante que se mira                 |
| `poll_seconds`       | `60`     | Frecuencia del daemon                              |
| `accounts`           | `[]`     | Cuentas a monitorear; vacío = todas las de gog     |
| `thresholds_minutes` | `[60, 15]` | Umbrales de alerta, en minutos antes del evento  |
| `only_keywords`      | `[]`     | Solo alerta títulos que contienen alguna palabra   |
| `skip_keywords`      | `[]`     | Nunca alerta títulos que contienen alguna palabra  |
| `skip_all_day`       | `true`   | Ignorar eventos de día completo                    |
| `notify`             | `true`   | `false` silencia (el daemon sigue logueando)       |

Para alertar solo entrevistas:

```toml
only_keywords = ["entrevista", "interview"]
```

## Uso

```bash
python3 calguard.py run           # daemon (lo que corre en systemd)
python3 calguard.py once          # un solo ciclo, para probar
python3 calguard.py once --dry-run --debug
python3 calguard.py accounts      # cuentas que se van a monitorear
python3 calguard.py test-notify   # notificación de prueba
```

Tests:

```bash
python3 -m unittest discover -s tests -v
```

## Comportamiento

- Por cada evento se dispara **una notificación por umbral cruzado**. Si el
  daemon arranca tarde y el evento ya cruzó varios umbrales, dispara solo el
  más urgente (uno solo).
- Urgencia `critical` cuando faltan ≤ 15 minutos; `normal` antes.
- Si un evento cambia de horario, la clave de deduplicación cambia y vuelve
  a alertar.
- Eventos de día completo se ignoran por defecto (`skip_all_day`).
- Los registros van a stdout (con systemd: `journalctl --user -u calguard`).

## Solución de problemas

- `cuenta X necesita re-auth`: correr de nuevo `gog auth add <cuenta> --services calendar --readonly`.
- `notify-send falló`: verificar que el servicio corre dentro de la sesión
  gráfica (`systemctl --user status graphical-session.target`) y que
  `DBUS_SESSION_BUS_ADDRESS` está disponible en el entorno del servicio.
- Sin cuentas visibles: `python3 calguard.py accounts` y `gog auth tokens list`.
