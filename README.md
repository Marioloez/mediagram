# tgdl — Telegram Media Downloader (CLI)

CLI para descargar medios (fotos, videos, audios, voz, documentos) de chats, canales y grupos de Telegram usando la API oficial MTProto como cliente de usuario. Funciona con cualquier chat al que tengas acceso — públicos, privados, DMs, Saved Messages — incluidos los que tienen descargas deshabilitadas.

## Requisitos

- Python 3.10+
- Credenciales de API de Telegram: regístrate en https://my.telegram.org → *API development tools* y obtén tu `api_id` y `api_hash`.

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Copia `.env.example` a `.env` y rellena tus credenciales:

```env
TG_API_ID=1234567
TG_API_HASH=abcdef0123456789abcdef0123456789

# Opcional — directorio donde se guardan las descargas
# Si NO se define, se usa ./downloads (relativo al directorio actual)
TG_OUTPUT_DIR=D:\mantt
```

### Dónde aterrizan los archivos descargados

Orden de prioridad:

1. **Flag `-o / --output`** en el comando (gana sobre todo)
2. **Variable `TG_OUTPUT_DIR`** en `.env`
3. **Default `./downloads`** — carpeta `downloads/` creada **donde ejecutas el comando** (no donde vive el código)

Dentro del directorio elegido se crea una subcarpeta por chat: `<output>/<nombre_del_chat>/<id>_<archivo>`.

## Uso

### 1. Primer login

```bash
python -m tgdl login
```

Te pedirá tu número de teléfono y el código que llega por Telegram (y 2FA si lo tienes activado). La sesión se guarda en `tgdl.session` — no tendrás que volver a autenticarte.

### 2. Listar tus chats (para obtener IDs)

```bash
python -m tgdl chats
python -m tgdl chats --limit 200
```

### 3. Descargar

```bash
# Todo el media de un canal público
python -m tgdl download @canal_publico

# Por ID numérico (OJO: los IDs negativos requieren "--" para que no los confunda con flags)
python -m tgdl download -- -1001234567890

# Mensajes guardados
python -m tgdl download me

# Solo videos, últimos 100 mensajes
python -m tgdl download @canal -t video -n 100

# Varios tipos
python -m tgdl download @canal -t photo -t video

# Rango de fechas
python -m tgdl download @canal --from 2024-01-01 --to 2024-06-30

# Mensajes específicos
python -m tgdl download @canal -m 1234 -m 1235

# Directorio de salida personalizado
python -m tgdl download @canal -o ./mis_descargas
```

## Flags de `download`

| Flag | Descripción |
|------|-------------|
| `-o, --output DIR` | Carpeta de salida (override del `.env`; default: `TG_OUTPUT_DIR` o `./downloads`) |
| `-t, --type TYPE` | `photo`, `video`, `audio`, `voice`, `document` (repetible) |
| `-n, --limit N` | Máximo de mensajes a escanear |
| `--from YYYY-MM-DD` | Solo mensajes después de esta fecha |
| `--to YYYY-MM-DD` | Solo mensajes antes de esta fecha |
| `-m, --message-id ID` | ID de mensaje específico (repetible) |
| `-c, --connections N` | Máx conexiones MTProto paralelas por archivo (default: 4) |
| `-C, --concurrency N` | Cuántos archivos descargar en paralelo (default: 3) |
| `--fast / --no-fast` | Modo rápido con paralelismo (default: activado) |

## Notas

- **IDs negativos**: al pasar un ID como `-1001234567890`, añade `--` antes para que el parser no lo confunda con una flag:
  ```bash
  python -m tgdl list-media -- -1001234567890
  python -m tgdl download -- -1001234567890 -t video
  ```
- Los archivos se guardan en `<output>/<nombre_del_chat>/<id>_<nombre_original>`.
- Si un archivo ya existe con el tamaño correcto, se omite (reanudación básica).
- Respeta los límites de rate de Telegram automáticamente (Telethon gestiona los `FloodWait`).

## Licencia

[MIT](LICENSE) © Marioloez
