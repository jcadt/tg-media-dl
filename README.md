# TG Media Downloader

Descarga series y películas desde canales de Telegram, compara con tu biblioteca Jellyfin existente, renombra automáticamente y copia solo lo que falta.

## Funcionamiento

1. Conectas tu cuenta de Telegram (sin ser bot)
2. Seleccionas un canal
3. Buscas una serie o película por nombre
4. La app escanea el canal y compara con tu biblioteca
5. Te muestra qué episodios ya existen y cuáles son nuevos
6. Descargas solo los que faltan
7. Se renombran automáticamente al formato de tu biblioteca y se copian

## Requisitos

- Docker y Docker Compose
- API ID + API Hash de Telegram (sácalos de [my.telegram.org](https://my.telegram.org/api))

## Uso rápido

```bash
# Clonar
git clone git@github.com:jcadt/tg-media-dl.git
cd tg-media-dl

# Ajustar rutas en docker-compose.yml y arrancar
TG_API_ID=12345 TG_API_HASH=abc123 docker compose up -d
```

Abrir `http://localhost:8000` y seguir el asistente.

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

## Configuración vía docker-compose

| Variable | Defecto | Descripción |
|---|---|---|
| `TG_API_ID` | — | API ID de Telegram |
| `TG_API_HASH` | — | API Hash de Telegram |
| `DOWNLOADS_DIR` | `/downloads` | Ruta temporal de descargas |
| `MEDIA_DIR` | `/media` | Ruta a la biblioteca Jellyfin |
| `DATA_DIR` | `/data` | Datos persistentes (sesión, BD) |

## Tecnología

- **FastAPI** + **HTMX** — Web UI reactiva sin JS frameworks
- **Telethon** — Cliente Telegram como usuario real
- **SQLite** — Jobs, descargas, historial
- **Docker** — Contenedor único
