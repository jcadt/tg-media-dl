# TG Media Downloader

Descarga series y películas desde canales de Telegram, compara con tu biblioteca Jellyfin existente, renombra automáticamente y copia solo lo que falta.

## Funcionamiento

- **Admin**: Se conecta a Telegram, busca contenido, ve qué existe vs qué falta, descarga y gestiona solicitudes de usuarios.
- **Usuarios**: Buscan series/películas, ven si ya están en la biblioteca, y si no, envían solicitudes que el admin debe aprobar.

## Requisitos

- Docker y Docker Compose
- API ID + API Hash de Telegram (sácalos de [my.telegram.org](https://my.telegram.org/api))
- (Opcional) Servidor Authentik para autenticación multiusuario

## Uso rápido

```bash
git clone git@github.com:jcadt/tg-media-dl.git
cd tg-media-dl

# Ajustar rutas en docker-compose.yml y arrancar
TG_API_ID=12345 TG_API_HASH=abc123 docker compose up -d
```

Abrir `http://localhost:8000`.

## Modo multiusuario con Authentik

Para activar la autenticación vía Authentik:

1. Crea una aplicación OAuth2 en Authentik:
   - Nombre: `tg-media-dl`
   - Slug: `tg-media-dl`
   - Redirect URIs: `https://tu-dominio/auth/callback`
   - Scope: openid, email, profile
   - Client type: Confidential

2. Arranca con las variables de autenticación:

```bash
AUTH_ENABLED=1 \
AUTHENTIK_DOMAIN=https://auth.tudominio.com \
AUTHENTIK_CLIENT_ID=... \
AUTHENTIK_CLIENT_SECRET=... \
ADMIN_EMAILS=admin@email.com \
APP_URL=https://tg-media-dl.tudominio.com \
SECRET_KEY=genera-una-clave-segura-aqui \
TG_API_ID=12345 TG_API_HASH=abc123 \
docker compose up -d
```

O usa un archivo `.env`:

```env
AUTH_ENABLED=1
AUTHENTIK_DOMAIN=https://auth.perdigans.es
AUTHENTIK_CLIENT_ID=tg-media-dl
AUTHENTIK_CLIENT_SECRET=...
ADMIN_EMAILS=jcadt@perdigans.es
APP_URL=https://tg-media-dl.perdigans.es
SECRET_KEY=...
TG_API_ID=12345
TG_API_HASH=abc123
```

### Roles

| Rol | Qué puede hacer |
|-----|----------------|
| **admin** (definido en `ADMIN_EMAILS`) | Conectar Telegram, buscar en canales, descargar, aprobar/rechazar solicitudes |
| **user** (cualquier otro usuario) | Buscar en la biblioteca, ver si existe, enviar solicitudes |

### Flujo para usuarios

1. El usuario busca una serie/película por nombre
2. La app comprueba si **ya está en la biblioteca** → ✅ mensaje "ya existe"
3. Si no está → botón **"Solicitar descarga"**
4. El admin recibe la solicitud y puede **aprobar** (crea un job de descarga) o **rechazar** (con mensaje opcional)
5. El usuario ve el estado de sus solicitudes en el panel

## Configuración vía docker-compose

| Variable | Defecto | Descripción |
|---|---|---|
| `TG_API_ID` | — | API ID de Telegram |
| `TG_API_HASH` | — | API Hash de Telegram |
| `AUTH_ENABLED` | `0` | Activar autenticación multiusuario |
| `AUTHENTIK_DOMAIN` | — | URL del servidor Authentik |
| `AUTHENTIK_CLIENT_ID` | — | Client ID OAuth2 |
| `AUTHENTIK_CLIENT_SECRET` | — | Client Secret OAuth2 |
| `ADMIN_EMAILS` | — | Emails admin separados por coma |
| `APP_URL` | `http://localhost:8000` | URL pública de la app |
| `SECRET_KEY` | `change-me` | Clave para firmar sesiones |
| `DOWNLOADS_DIR` | `/downloads` | Ruta temporal de descargas |
| `MEDIA_DIR` | `/media` | Ruta a la biblioteca Jellyfin |
| `DATA_DIR` | `/data` | Datos persistentes |

## Estructura de biblioteca esperada

```
/media/
├── Series/
│   └── Nombre Serie (2020)/
│       ├── Nombre Serie - S01E01 - Título.mkv
│       └── Nombre Serie - S01E02 - Título.mkv
├── Peliculas/
│   └── Nombre Película (2020)/
│       └── Nombre Película (2020).mkv
├── Animacion/
├── Peliculas-Anime/
└── Series-animacion/
```

## Tecnología

- **FastAPI** + **HTMX** — Web UI reactiva sin JS frameworks
- **Telethon** — Cliente Telegram como usuario real
- **Authentik OIDC** — Autenticación multiusuario
- **SQLite** — Jobs, solicitudes, usuarios, descargas
- **Docker** — Contenedor único
