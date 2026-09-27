# Sonos API

API REST privada de resolución y control multimedia para sistemas **Sonos**. Diseñada como puente de integración para asistentes inteligentes, Home Assistant, scripts de automatización y agentes de IA.

Permite buscar y reproducir música de forma transparente combinando dos fuentes:
1. **Biblioteca Local (Samba / CIFS)** indexada directamente desde el servicio de música compartida de Sonos, incluyendo etiquetas de **género** (`<upnp:genre>`) y **año/década** (`<dc:date>` o inferido del directorio).
2. **YouTube & YouTube Music** con transcodificación en tiempo real a MP3 compatible con Sonos.

---

## 🏛️ Arquitectura del Sistema

```
                      ┌────────────────────────────────────────┐
                      │ Clientes (IA, Home Assistant, Scripts) │
                      └───────────────────┬────────────────────┘
                                          │ HTTP JSON (Puerto 39100)
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ sonos-api (Docker con network_mode: host)                                       │
│                                                                                 │
│   ┌─────────────────────┐    ┌──────────────────────┐    ┌──────────────────┐   │
│   │   music_resolver    │    │      server.py       │    │     sonoscli     │   │
│   │ - FTS5 SQLite       │    │ - Endpoints REST     │    │ - Control básico │   │
│   │ - Normalizador ES   │    │ - Proxy Streaming    │    │   play, pause,   │   │
│   │ - Jerarquía & Caché │    │ - SOAP UPnP / Queues │    │   volume, status │   │
│   │ - Género & Décadas  │    └──────────┬───────────┘    └─────────┬────────┘   │
│   └──────────┬──────────┘               │                          │            │
│              │                          │                          │            │
│              ▼                          ▼                          ▼            │
│   ┌─────────────────────┐    ┌──────────────────────┐    ┌──────────────────┐   │
│   │ SQLite (music-cache)│    │  ffmpeg + yt-dlp     │    │   Altavoces      │   │
│   │ - library_tracks    │    │ - Extracción audio   │    │   Sonos (Red     │   │
│   │ - query_cache       │    │ - Transcode MP3 320k │    │   Local LAN)     │   │
│   │ - yt_album_cache    │    │   en /stream/youtube │    │                  │   │
│   │ - image_cache (BLOB)│    │ - HTTP /image/<key>  │    │                  │   │
│   └─────────────────────┘    └──────────────────────┘    └──────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Componentes Internos
* **`sonoscli`**: Binario en Go compilado en la imagen Docker desde [steipete/sonoscli](https://github.com/steipete/sonoscli). Utilizado para el descubrimiento de altavoces y comandos básicos de transporte.
* **SOAP / UPnP Directo**: Llamadas SOAP a los servicios `AVTransport:1` y `ContentDirectory:1` de Sonos para controlar la cola nativa (`x-rincon-queue`), metadatos DIDL-Lite con MIME dinámico (`audio/flac`, `audio/mp4`, `audio/mpeg`) y sincronizar el índice de pistas Samba (`x-file-cifs:`).
* **Proxy de Streaming Transcodificado (`/stream/youtube/<token>`)**: Puesto que Sonos no puede reproducir directamente streams WebM/Opus de YouTube, el servidor resuelve el stream con `yt-dlp` y lo convierte al vuelo a **MP3 320 kbps** con `ffmpeg`, transmitiéndolo al altavoz por HTTP con la máxima fidelidad acústica.
* **SQLite con FTS5**: Base de datos local en `/data/music-cache.sqlite` con búsqueda de texto completo insensible a acentos (`tokenize='unicode61 remove_diacritics 2'`), ranking BM25 y caché de consultas con TTL.
* **Caché de Imágenes y Carátulas (`/image/<key>`)**: Almacenamiento persistente en SQLite de miniaturas (thumbnails de YouTube) y carátulas de álbumes (Samba local) como BLOBs con descarga bajo demanda (on-demand), evitando descargas durante la indexación y sirviendo imágenes ultra-rápidas a agentes de IA y Telegram.
* **Indexación de Género y Año**: Extracción automática de metadatos UPnP/DIDL (`<upnp:genre>`, `<dc:date>`) y año entre paréntesis en carpetas de álbumes (ej. `(1984)`).

---

## 🎯 Jerarquía de Resolución de Música

Cuando se envía una petición a `/resolve` o `/resolve-and-play`:

1. **Detección Directa de URL**: Si el campo `query` es una URL de YouTube (`youtube.com`, `youtu.be`, `music.youtube.com`):
   * Si es un video: se reproduce directamente sin buscar en Samba ni ejecutar búsquedas textuales en `yt-dlp`.
   * Si es una playlist o álbum (`/playlist?list=...` o `/browse/...`): se extraen directamente las pistas de la URL.
2. **Caché en SQLite (`query_cache`)**: Si la consulta ya fue resuelta previamente y no ha expirado (y `bypass_cache` es `false`), devuelve el resultado guardado.
3. **Biblioteca Local (Samba)**: Prioriza la búsqueda en los archivos locales indexados.
   * En modo `track`: busca por FTS5 ordenado por relevancia de título y artista.
   * En modo `album`: busca el álbum con tolerancia a tildes, prefijos (*"The "*, *"El "*) y sufijos (*"(Remastered)"*, *"[Deluxe]"*).
   * En modo `artist`: busca todas las canciones del artista en la biblioteca local, con opción de orden aleatorio (`shuffle`).
   * En modo `genre`: busca canciones por género musical y/o década/año (ej. *"rock de los 80"*, *"pop de los 90"*, *"clásica"*, o parámetros `genre` y `decade`).
4. **URL de YouTube Music**: Si se proporciona explícitamente en el parámetro `youtube_music_url`.
5. **Fallback Automático a YouTube**: Si no existe en la biblioteca local:
   * En modo `track`: busca en YouTube vía `yt-dlp`.
   * En modo `album`: busca el álbum en YouTube Music vía scraping de `/browse/`, validando que el artista coincida y respetando el orden del álbum.
   * En modo `artist`: busca las canciones principales / Top Songs del artista en YouTube Music.
   * En modo `genre`: busca una selección o mix del género y época en YouTube Music.
   * En modo `list`: busca las canciones en YouTube y las encola.

> **Nota sobre el parámetro `provider`**:
> * `null` (por defecto): Busca primero en Samba y hace fallback automático a YouTube si no existe en local.
> * `"samba"`: Fuerza la búsqueda exclusivamente en la biblioteca local (lanza error si no existe).
> * `"youtube"` o `"youtube_music"`: Fuerza la búsqueda directa en YouTube / YouTube Music.

---

## 🇪🇸 Optimización para Español y Comandos de Voz

El sistema está optimizado para procesar consultas en español:
* **Tildes y Diacríticos**: Búsqueda 100% tolerante. Buscar `"cancion"` encuentra `"Canción"`, y buscar `"corazón"` encuentra `"corazon"`.
* **Palabras de Invocación / Carrier Words**: Filtra automáticamente palabras habituales de comandos de voz (*"canción"*, *"cancion"*, *"disco"*, *"album"*, *"tema"*, *"pon"*, *"reproduce"*, *"musica"*) cuando acompañan a la consulta.
* **Detección Natural de Versión Estudio vs. En Vivo**: Interpreta frases conversacionales como *"Paint It, Black de estudio"*, *"versión de estudio"*, *"en vivo"*, *"en directo"* o *"live"*. Si el usuario pide versión de estudio y en la biblioteca local solo existe una versión en vivo (ej. *Flashpoint* de The Rolling Stones), el sistema evita esa pista y recurre automáticamente a la versión de estudio de YouTube Music.
* **Detección Natural de Género y Década**: Interpreta frases conversacionales como *"canciones de rock de los 80"*, *"música pop 90s"*, *"temas de los 70"*, *"rock 80s"*, extrayendo de forma transparente el género y el rango temporal sin necesidad de formatear parámetros especiales.
* **Preposición `" de "`**: Maneja de forma inteligente consultas como `"Canciones de amor"` frente a `"Thriller de Michael Jackson"`.
* **Prefijos de YouTube Music en Español**: Soporta `"Álbum - "`, `"Sencillo - "`, `"EP - "` y sus versiones en inglés.

---

## 🧠 Tolerancia a Errores de Tipeo (Fuzzy Matching)

El sistema incorpora un motor de rescate de errores ortográficos y tipográficos basado en la librería estándar `difflib`:
* **Cero sobrecosto en búsquedas normales:** Si la búsqueda exacta por FTS5 o coincidencia de subcadena tiene éxito, el fuzzy matching no se ejecuta (tiempo de respuesta < 1 ms).
* **Rescate automático en local:** Si una búsqueda arroja 0 resultados debido a un error de escritura (ej: *"peral jam"* en vez de *"pearl jam"*, *"yeld"* en vez de *"yield"*, o *"led zepelin"* en vez de *"led zeppelin"*):
  1. Compara la consulta o sus palabras contra los nombres únicos de artistas, álbumes y títulos de la biblioteca local.
  2. Si la similitud supera el umbral ($\ge 72\%$), autocorrige los términos y ejecuta la búsqueda local en 2 a 4 milisegundos.
  3. **Evita fallbacks innecesarios a la nube:** Impide que una consulta de música local falle y pase 40 segundos consultando infructuosamente en YouTube Music por un error tipográfico.
* **Cobertura integral:** Activo en todos los modos: pistas individuales (`track`), listas (`list`), discos completos (`album`) y artistas (`artist`).

---

## 📡 Referencia de la API HTTP

Todos los endpoints que reciben datos esperan `Content-Type: application/json`.

### Endpoints de Consulta y Control

#### `GET /health`
Estado del servicio, versión y configuración de cron.
```json
{
  "ok": true,
  "service": "sonos-api",
  "version": "2.2.0",
  "scheduled_reindex": "0 4 * * *",
  "timezone": "America/Santiago"
}
```

#### `GET /status?room=<NombreHabitacion>`
Estado de reproducción, volumen y metadatos actuales del altavoz en la habitación.

#### `GET /library/status`
Cantidad de canciones locales indexadas y marca de tiempo de la última sincronización.
```json
{
  "ok": true,
  "library": {
    "tracks": 8420,
    "last_sync": 1727452800,
    "sonos_total": 8420,
    "room": "Living"
  }
}
```

#### `GET /search` / `POST /search`
Búsqueda y exploración de candidatos de canciones (modo dry-run sin reproducir). Devuelve opciones coincidentes en la biblioteca local Samba y/o YouTube con metadatos (`title`, `artist`, `album`, `year`, `is_live`, `provider`, `target`). Permite explorar y seleccionar la versión deseada (estudio vs en vivo, álbum original vs directo).

* **Parámetros (`query params` en GET o `JSON body` en POST):**
  * `query` o `q` (`string`, obligatorio): Título o término de búsqueda.
  * `artist` (`string`, opcional): Filtro de artista.
  * `album` (`string`, opcional): Filtro de álbum específico.
  * `live` (`bool`, opcional): `false` para versión de estudio, `true` para versión en vivo.
  * `provider` (`string`, opcional): `"auto"` (Samba + YouTube), `"samba"` (solo local) o `"youtube"`.
  * `limit` (`int`, opcional): Máximo de resultados a devolver (1 a 50, por defecto 5).

```json
// Petición POST /search
{
  "query": "Paint It Black",
  "artist": "The Rolling Stones",
  "live": false,
  "limit": 5
}

// Respuesta (HTTP 200)
{
  "ok": true,
  "query": "Paint It Black",
  "count": 2,
  "tracks": [
    {
      "title": "Paint It, Black",
      "artist": "The Rolling Stones",
      "album": "Aftermath",
      "genre": "Rock",
      "year": 1966,
      "provider": "samba",
      "kind": "uri",
      "target": "x-file-cifs://...",
      "is_live": false
    },
    {
      "title": "The Rolling Stones - Paint It, Black (Official Lyric Video)",
      "artist": "The Rolling Stones",
      "album": "",
      "genre": "",
      "year": null,
      "provider": "youtube",
      "kind": "url",
      "target": "https://www.youtube.com/watch?v=O4irXQhgMqg",
      "is_live": false
    }
  ]
}
```

#### `POST /play` / `POST /pause` / `POST /stop`
Reanudar, pausar o detener por completo la reproducción. Devuelven el estado actualizado del altavoz de inmediato para evitar consultas adicionales a `/status`. Soporta tanto la CLI de Sonos como fallback a UPnP SOAP AVTransport directo.
```json
// Petición
{ "room": "Sala de estar" }

// Respuesta (HTTP 200)
{
  "ok": true,
  "action": "pause",
  "room": "Sala de estar",
  "status": {
    "transport": { "State": "PAUSED_PLAYBACK", "Status": "OK" },
    "volume": 50,
    "nowPlaying": { "title": "Hickory Dichotomy", "artist": "Stone Temple Pilots" }
  }
}
```

#### `POST /next` / `POST /previous` (o `/prev`)
Avanzar a la siguiente pista o retroceder a la pista anterior en la cola de reproducción actual. Devuelve el estado actualizado del altavoz.
```json
// Petición
{ "room": "Sala de estar" }

// Respuesta (HTTP 200)
{
  "ok": true,
  "action": "next",
  "room": "Sala de estar",
  "status": {
    "transport": { "State": "PLAYING", "Status": "OK" }
  }
}
```

#### `GET /image/<key>`
Sirve imágenes de carátula o miniaturas cacheadas en la base de datos SQLite local (`image_cache`).
- Las imágenes se descargan y guardan bajo demanda (on-demand) la primera vez que se reproduce una pista o se solicita el endpoint.
- Headers HTTP optimizados con `Cache-Control: public, max-age=2592000, immutable` (30 días) y soporte de CORS para clientes web.
- Claves generadas automáticamente: `yt_<videoId>` para YouTube y `samba_<hash>` para música local compartida.

#### `POST /volume`
Ajustar el volumen de un altavoz (0 a 100). Devuelve el estado resultante del altavoz.
```json
// Petición
{ "room": "Sala de estar", "level": 40 }

// Respuesta (HTTP 200)
{
  "ok": true,
  "action": "volume",
  "room": "Sala de estar",
  "level": 40,
  "status": { "volume": 40, "mute": false, "transport": { "State": "PLAYING" } }
}
```

#### `POST /play-url`
Reproducir una URL directa de YouTube / YouTube Music en un altavoz. Devuelve el estado inicial de reproducción.
```json
{ "room": "Sala de estar", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ" }
```

#### `POST /library/reindex`
Fuerza la reindexación de la biblioteca local de Sonos (autodescubre cualquier altavoz disponible si no se especifica `room`).
```json
{}
```
O especificando un altavoz concreto:
```json
{ "room": "Sala de estar" }
```

---

### Endpoints de Resolución y Reproducción

* **`POST /resolve`**: Resuelve la pista o consulta y devuelve los metadatos sin reproducir.
* **`POST /resolve-and-play`** (o `POST /search-and-play`): Resuelve y reproduce inmediatamente en el altavoz indicado.
* **`POST /resolve-list`**: Resuelve una lista o álbum y encola todas las canciones.

#### Parámetros del JSON Body:

| Campo | Tipo | Obligatorio | Por defecto | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `room` | `string` | Sí* | - | Nombre de la habitación Sonos (ej. `"Living"`, `"Oficina"`). *Obligatorio si se reproduce. |
| `query` | `string` | Sí* | - | Texto de búsqueda (canción, álbum, artista, género con década) o URL de YouTube. *Opcional si se pasa `genre`. |
| `mode` | `string` | No | `"track"` | Modo de resolución: `"track"` (canción), `"album"` (álbum completo), `"artist"` (discografía/radio del artista), `"genre"` (por género y/o década), `"list"` (lista de canciones). |
| `genre` | `string` | No | `""` | Filtro de género musical (ej. `"Rock"`, `"Pop"`, `"Heavy Metal"`). Se puede especificar aquí o directamente en `query`. |
| `decade` | `int` | No | `null` | Década o año de lanzamiento (ej. `1980` u `80`). Abarca el rango decenal (ej. 1980 a 1989). |
| `provider` | `string` | No | `null` | Proveedor: `null` (auto: Samba -> YouTube), `"samba"`, `"youtube"` o `"youtube_music"`. |
| `artist` | `string` | No | `""` | Filtro opcional de artista para precisar la búsqueda. |
| `album` | `string` | No | `""` | Filtro opcional de álbum para forzar la versión de un disco específico (ej. `"Aftermath"`). |
| `live` | `bool` | No | `null` | `false` para forzar versión de estudio (excluye o penaliza álbumes en vivo como *Flashpoint*), `true` para versión en vivo. Si es `null`, se autodetecta desde `query`. |
| `shuffle` | `bool` | No | `true` | En modo `"artist"` o `"genre"`, si es `true` mezcla las pistas aleatoriamente (tipo radio). Si es `false`, mantiene el orden cronológico / discográfico. |
| `limit` | `int` | No | `10` | Límite de resultados en modo `"list"`, `"artist"` o `"genre"` (1 a 50). |
| `bypass_cache` | `bool` | No | `false` | Si es `true`, ignora la caché SQLite y fuerza una nueva resolución. |
| `reindex` | `bool` | No | `false` | Si es `true`, reindexa la biblioteca Samba antes de resolver. |

#### Ejemplos de Petición

##### 1. Reproducir una canción (modo `track` con fallback automático):
```bash
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "query": "cancion tren al sur"
  }'
```

##### 2. Reproducir un artista en modo radio/aleatorio (modo `artist`):
```bash
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "query": "Queen",
    "mode": "artist",
    "shuffle": true,
    "limit": 30
  }'
```

##### 3. Reproducir música por género y década (modo `genre` o frase natural):
```bash
# Mediante frase natural en español:
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "query": "canciones de rock de los 80",
    "mode": "genre",
    "limit": 25,
    "shuffle": true
  }'

# O mediante parámetros estructurados explícitos:
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "genre": "Rock",
    "decade": 1980,
    "mode": "genre"
  }'
```

##### 4. Reproducir un álbum completo (modo `album`):
```bash
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "query": "Cancion Animal",
    "artist": "Soda Stereo",
    "mode": "album"
  }'
```

##### 5. Reproducir una URL directa de YouTube:
```bash
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "query": "https://www.youtube.com/watch?v=kJQP7kiw5Fk"
  }'
```

##### 6. Reproducir una playlist directa de YouTube Music:
```bash
curl -X POST http://localhost:39100/resolve-and-play \
  -H "Content-Type: application/json" \
  -d '{
    "room": "Living",
    "query": "https://music.youtube.com/playlist?list=PLrAlG_poNf1_1v...",
    "mode": "album"
  }'
```

##### 5. Respuesta típica de éxito:
```json
{
  "ok": true,
  "provider": "samba",
  "mode": "album",
  "album": "Canción Animal",
  "artist": "Soda Stereo",
  "count": 10,
  "tracks": [
    {
      "uri": "x-file-cifs://nas/musica/Soda/01.flac",
      "title": "(En) El Séptimo Día",
      "artist": "Soda Stereo",
      "album": "Canción Animal",
      "track_number": 1
    }
  ],
  "result": {
    "queued": 10
  },
  "status": {
    "transport": { "State": "PLAYING", "Status": "OK" },
    "volume": 40,
    "nowPlaying": { "title": "(En) El Séptimo Día", "artist": "Soda Stereo", "album": "Canción Animal" }
  }
}
```

---

## 🤖 Integración MCP (Model Context Protocol) para IA

El servicio incluye un servidor **MCP integrado** que permite a asistentes de Inteligencia Artificial (como **Hermes**, Claude Desktop, Cursor, etc.) controlar tu música y altavoces de forma nativa sin que tengas que programar integraciones HTTP manuales.

La API REST tradicional sigue operando en paralelo en el mismo puerto (`:39100`) sin interferencias.

### 1. Conexión desde Hermes / Clientes MCP

#### Opción A: Por Red HTTP / SSE (Recomendada)
Agrega el servidor a la configuración de herramientas MCP de tu cliente (ej. `mcp_config.json` o configuración de Hermes):

```json
{
  "mcpServers": {
    "sonos": {
      "url": "http://<IP_HOST>:39100/sse"
    }
  }
}
```

#### Opción B: Por `stdio` vía Docker Exec
Si el cliente corre en el mismo host y tiene acceso a Docker:

```json
{
  "mcpServers": {
    "sonos": {
      "command": "docker",
      "args": ["exec", "-i", "sonos-api", "python3", "-m", "app.mcp_server"]
    }
  }
}
```

### 2. Herramientas Disponibles (*Tools*)

Cualquier modelo de IA conectado tendrá acceso directo a las siguientes funciones:

| Herramienta | Parámetros | Descripción |
| :--- | :--- | :--- |
| `sonos_play_music` | `query` *(opcional si se indica `genre`)*, `room`, `provider` (`"auto"`, `"samba"`, `"youtube"`), `mode` (`"track"`, `"album"`, `"artist"`, `"genre"`, `"list"`), `genre`, `decade`, `artist`, `album`, `live`, `shuffle`, `limit`, `bypass_cache` | Resuelve y reproduce música en Sonos desde Samba o YouTube Music. Soporta búsquedas por canción, disco, discografía o género/década (ej. *"rock de los 80"* o parámetros explícitos `genre="Rock"` y `decade=1980`). Con `album` precisa el disco específico, y con `live=false` o `live=true` fuerza versión de estudio o en vivo (evitando álbumes en vivo como *Flashpoint* si se pide estudio). Con `provider="samba"` fuerza solo música local sin fallback a YouTube, y con `provider="youtube"` busca directamente en YouTube. Si no se indica `room`, autodescubre el altavoz activo. **Devuelve metadatos enriquecidos con `image_url` listo para previsualización o envío en Telegram**. |
| `sonos_search_music` | `query` *(obligatorio)*, `artist`, `album`, `live`, `provider` (`"auto"`, `"samba"`, `"youtube"`), `limit` | Busca y devuelve una lista de canciones coincidentes con metadatos (`title`, `artist`, `album`, `year`, `provider`, `is_live`) **sin reproducir**. Permite a la IA inspeccionar candidatos y dar a elegir al usuario entre diferentes versiones (estudio vs en vivo, álbumes distintos). |
| `sonos_pause` | `room` *(opcional)* | Pausa la música en la habitación indicada (o altavoz activo). |
| `sonos_resume` | `room` *(opcional)* | Reanuda la reproducción. |
| `sonos_stop` | `room` *(opcional)* | Detiene por completo la reproducción en el altavoz Sonos especificado. |
| `sonos_next` | `room` *(opcional)* | Avanza a la siguiente pista en la cola de reproducción del altavoz Sonos. |
| `sonos_previous` | `room` *(opcional)* | Retrocede a la pista anterior en la cola de reproducción del altavoz Sonos. |
| `sonos_set_volume` | `level` *(0 a 100, obligatorio)*, `room` *(opcional)* | Cambia el volumen del altavoz. |
| `sonos_get_status` | `room` *(opcional)* | Devuelve el estado de reproducción, volumen y la canción/artista en reproducción con `image_url`. |
| `sonos_list_rooms` | *(ninguno)* | Descubre y devuelve la lista de altavoces Sonos disponibles en la red local. |
| `sonos_reindex_library` | `room` *(opcional)* | Fuerza la actualización de la biblioteca local Samba/CIFS. |

---

## ⚙️ Variables de Entorno

| Variable | Valor por defecto | Descripción |
| :--- | :--- | :--- |
| `SONOS_API_HOST` | *(Autodetectado)* | **Opcional**. IP del host accesible por los altavoces Sonos para solicitar streams HTTP (`/stream/youtube/`). Si no se define, el sistema la autoresuelve dinámicamente consultando la tabla de rutas hacia el altavoz. |
| `SONOS_STREAM_BITRATE` | `"320k"` | **Opcional**. Bitrate de codificación MP3 para streams de YouTube (ej. `"320k"`, `"256k"`). |
| `SONOS_REINDEX_CRON` | `""` | **Opcional**. Expresión cron diaria para sincronización automática (ej. `"0 4 * * *"` para reindexar a las 4:00 AM). |
| `SONOS_REINDEX_ROOM` | *(Autodetectado)* | **Opcional**. Altavoz Sonos específico para reindexar. Si no se define, se autodescubre cualquier altavoz disponible en el hogar. |
| `TZ` | `"UTC"` | Zona horaria del contenedor para evaluar el cron programado (ej. `"America/Santiago"`). |
| `LOG_LEVEL` | `"INFO"` | **Opcional**. Nivel de detalle de logs (`"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`). |

---

## 🧪 Pruebas Unitarias Locales

Para ejecutar la suite completa de pruebas unitarias sin necesidad de Docker ni dependencias externas:

```bash
python3 -m unittest discover tests
```

Los tests cubren:
* Servidor MCP integrado (handshake `initialize`, listado de herramientas, llamadas a tools, sesiones SSE y transporte `stdio`).
* Normalización y eliminación de diacríticos/tildes en español.
* Filtrado de carrier words de voz (*"canción"*, *"disco"*, *"pon"*).
* Búsqueda flexible de álbumes locales (sin prefijos/sufijos y soporte de artistas en colaboraciones).
* Detección robusta de números de pista y carpetas multi-disco (`/Disc 1/`, `/CD 02/`).
* Selección de álbumes en YouTube Music con prefijos en español y validación estricta de artista.
* Detección y resolución directa de URLs de YouTube y YouTube Music.
* Generación dinámica de MIME types para UPnP (`audio/flac`, `audio/mp4`, `audio/mpeg`, etc.).
* Rescate de errores tipográficos (Fuzzy Matching) en artistas, discos y canciones.
* Indexación de géneros y año/década, consultas en lenguaje natural en español y modo `"genre"`.

---

## 🐳 Despliegue con Docker Compose

```yaml
services:
  sonos-api:
    build:
      context: .
      dockerfile: Dockerfile
    image: local/sonos-api:latest
    container_name: sonos-api
    network_mode: host
    restart: unless-stopped
    environment:
      - SONOS_REINDEX_CRON=0 4 * * *
      - TZ=America/Santiago
    volumes:
      - sonos-api-data:/data
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  sonos-api-data:
```

Para construir y levantar:
```bash
docker compose up -d --build
```
*(Nota: Requiere `network_mode: host` para que el servicio pueda descubrir y comunicarse por SSDP/UPnP con los altavoces Sonos en la subred local).*
