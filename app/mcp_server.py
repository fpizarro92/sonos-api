import json
import logging
import queue
import re
import sys
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("sonos_mcp")

try:
    from _version import __version__
except ImportError:
    try:
        from app._version import __version__
    except ImportError:
        from ._version import __version__

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "sonos-api-mcp"
SERVER_VERSION = __version__

TOOL_DEFINITIONS = [
    {
        "name": "sonos_play_music",
        "description": "Resuelve y reproduce música en un altavoz Sonos utilizando la biblioteca local Samba (SMB) o YouTube / YouTube Music. Soporta búsquedas por canción, álbum, artista o URLs directas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Texto de búsqueda (título de canción, álbum o artista) o URL directa de YouTube / YouTube Music. Opcional si se especifica 'genre'."
                },
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos (ej. 'Sala de estar', 'Living', 'Oficina'). Si se omite, se autodescubre el altavoz activo."
                },
                "mode": {
                    "type": "string",
                    "enum": ["track", "album", "artist", "genre", "list"],
                    "description": "Modo de reproducción: 'track' (canción individual), 'album' (disco completo), 'artist' (discografía/radio del artista), 'genre' (género musical y/o década, ej. rock de los 80), 'list' (cola con múltiples temas). Por defecto: 'track'."
                },
                "provider": {
                    "type": "string",
                    "enum": ["auto", "samba", "youtube", "youtube_music"],
                    "description": "Proveedor de música: 'auto' (prioriza biblioteca Samba y cae a YouTube si no hay resultados locales), 'samba' (fuerza únicamente la biblioteca local Samba SMB), 'youtube' o 'youtube_music' (fuerza búsqueda y reproducción exclusivamente desde YouTube). Por defecto: 'auto'."
                },
                "limit": {
                    "type": "integer",
                    "description": "Número de canciones a encolar cuando se reproduce un artista, género o lista. Por defecto: 10."
                },
                "artist": {
                    "type": "string",
                    "description": "Filtro opcional para precisar el artista cuando hay nombres ambiguos."
                },
                "album": {
                    "type": "string",
                    "description": "Filtro opcional para precisar el álbum específico (ej. 'Aftermath') y evitar versiones de otros discos."
                },
                "live": {
                    "type": "boolean",
                    "description": "Filtro opcional: True para forzar versiones en vivo, False para forzar versiones de estudio (excluyendo o penalizando álbumes en vivo)."
                },
                "genre": {
                    "type": "string",
                    "description": "Género musical (ej. 'Rock', 'Pop', 'Jazz', 'Heavy Metal') cuando se busca por estilo."
                },
                "decade": {
                    "type": "integer",
                    "description": "Década o año para filtrar la música (ej. 1980 o 80 para los años 80, 1990 o 90 para los 90)."
                },
                "shuffle": {
                    "type": "boolean",
                    "description": "En modo 'artist', 'genre' o 'list', si es true mezcla aleatoriamente las pistas (tipo radio). Por defecto: true."
                },
                "bypass_cache": {
                    "type": "boolean",
                    "description": "Si es true, ignora la caché de resoluciones previas y fuerza una consulta fresca. Por defecto: false."
                }
            }
        }
    },
    {
        "name": "sonos_search_music",
        "description": "Busca canciones y devuelve una lista de opciones coincidentes con metadatos (título, artista, álbum, año, proveedor Samba/YouTube, versión en vivo o de estudio) SIN reproducir. Permite al usuario o asistente elegir entre varias versiones (ej. estudio vs en vivo, remaster vs original).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Nombre de la canción o término de búsqueda."
                },
                "artist": {
                    "type": "string",
                    "description": "Filtro opcional para precisar el artista o banda."
                },
                "album": {
                    "type": "string",
                    "description": "Filtro opcional para precisar el álbum específico (ej. 'Aftermath')."
                },
                "live": {
                    "type": "boolean",
                    "description": "True para buscar solo versiones en vivo, False para forzar versiones de estudio."
                },
                "provider": {
                    "type": "string",
                    "enum": ["auto", "samba", "youtube"],
                    "description": "Fuente de música: 'auto' (Samba con fallback a YouTube), 'samba' (solo biblioteca local) o 'youtube'. Por defecto: 'auto'."
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de resultados a devolver. Por defecto: 5."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "sonos_pause",
        "description": "Pausa la reproducción de música en el altavoz Sonos especificado (o en el altavoz activo por defecto).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            }
        }
    },
    {
        "name": "sonos_resume",
        "description": "Reanuda la reproducción de música en el altavoz Sonos especificado (o en el altavoz activo por defecto).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            }
        }
    },
    {
        "name": "sonos_stop",
        "description": "Detiene por completo la reproducción en el altavoz Sonos especificado (o en el altavoz activo por defecto).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            }
        }
    },
    {
        "name": "sonos_next",
        "description": "Avanza a la pista siguiente en la cola de reproducción del altavoz Sonos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            }
        }
    },
    {
        "name": "sonos_previous",
        "description": "Retrocede a la pista anterior en la cola de reproducción del altavoz Sonos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            }
        }
    },
    {
        "name": "sonos_set_volume",
        "description": "Ajusta el volumen de un altavoz Sonos a un nivel específico entre 0 y 100.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Nivel de volumen deseado (número entero entre 0 y 100)."
                },
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            },
            "required": ["level"]
        }
    },
    {
        "name": "sonos_get_status",
        "description": "Consulta el estado actual de un altavoz Sonos: estado de transporte (PLAYING, PAUSED_PLAYBACK), volumen, si está en silencio y metadatos de la canción que suena.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación o altavoz Sonos. Si se omite, se autodescubre el altavoz activo."
                }
            }
        }
    },
    {
        "name": "sonos_list_rooms",
        "description": "Descubre y lista todos los altavoces y habitaciones Sonos disponibles en la red local.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "sonos_reindex_library",
        "description": "Fuerza la reindexación de la biblioteca local de música de Sonos (Samba/CIFS) para detectar nuevos archivos o cambios en el disco.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "Nombre de la habitación Sonos. Si se omite, se autodescubre cualquier altavoz disponible."
                }
            }
        }
    }
]


def _load_server():
    """Importa el módulo server de forma robusta soportando ejecución directa, como módulo o como paquete."""
    import sys
    from pathlib import Path

    app_dir = str(Path(__file__).resolve().parent)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    try:
        import server
        return server
    except (ImportError, ValueError):
        pass

    try:
        from app import server
        return server
    except (ImportError, ValueError):
        pass

    from . import server
    return server


def extract_youtube_video_id(url: Optional[str]) -> Optional[str]:
    """Extrae el ID de 11 caracteres de cualquier URL de YouTube o YouTube Music."""
    if not url:
        return None
    patterns = [
        r"(?:v=|vi=|v%3D|vi%3D)([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/live/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, str(url))
        if match:
            return match.group(1)
    return None


def _find_album_art_in_dict(data: Any) -> Optional[str]:
    """Busca recursivamente campos de carátula en diccionarios de estado."""
    if not isinstance(data, dict):
        return None
    for key in ("albumArtURI", "album_art_uri", "albumArtUri", "artUri", "image_url", "imageUrl"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for v in data.values():
        if isinstance(v, dict):
            found = _find_album_art_in_dict(v)
            if found:
                return found
    return None


def enrich_playback_payload(
    payload: Dict[str, Any],
    room: str,
    server_module: Any = None
) -> Dict[str, Any]:
    """Enriquece la respuesta de reproducción o estado con metadatos claros y URL de imagen/thumbnail."""
    result = dict(payload)
    title = result.get("title")
    artist = result.get("artist")
    album = result.get("album")
    source = result.get("source") or result.get("provider")
    source_uri = result.get("source_uri")
    image_url = result.get("image_url")
    speaker_ip = None

    # Metadatos desde 'resolution'
    resolution = result.get("resolution")
    if isinstance(resolution, dict):
        title = title or resolution.get("title")
        artist = artist or resolution.get("artist")
        album = album or resolution.get("album")
        source = source or resolution.get("provider")
        source_uri = source_uri or resolution.get("target")

    # Metadatos desde 'tracks' (si es álbum, lista, artista o género)
    tracks = result.get("tracks")
    if isinstance(tracks, list) and tracks:
        first_track = tracks[0]
        if isinstance(first_track, dict):
            title = title or first_track.get("title")
            artist = artist or first_track.get("artist")
            album = album or first_track.get("album")
            source_uri = source_uri or first_track.get("url") or first_track.get("uri")

    # Obtener IP del altavoz para expandir URLs relativas de Sonos
    if server_module and hasattr(server_module, "discover_room"):
        try:
            speaker_ip = server_module.discover_room(room)
        except Exception:
            speaker_ip = None

    # Metadatos y arte desde el bloque 'status'
    status = result.get("status")
    if isinstance(status, dict):
        for sub in ("item", "track", "media"):
            sub_dict = status.get(sub)
            if isinstance(sub_dict, dict):
                title = title or sub_dict.get("title")
                artist = artist or sub_dict.get("artist")
                album = album or sub_dict.get("album")
                source_uri = source_uri or sub_dict.get("uri")
        raw_art = _find_album_art_in_dict(status)
        if raw_art and not image_url:
            if raw_art.startswith("http://") or raw_art.startswith("https://"):
                image_url = raw_art
            elif raw_art.startswith("/") and speaker_ip:
                image_url = f"http://{speaker_ip}:1400{raw_art}"
            elif speaker_ip:
                image_url = f"http://{speaker_ip}:1400/{raw_art.lstrip('/')}"

    import hashlib

    # Determinar clave de caché única y URL origen
    cache_key = None
    source_image_url = None

    yt_id = extract_youtube_video_id(source_uri)
    if yt_id:
        cache_key = f"yt_{yt_id}"
        source_image_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
        source = "youtube"
    elif source in {"youtube", "youtube_music"} and source_uri:
        yt_id = extract_youtube_video_id(source_uri)
        if yt_id:
            cache_key = f"yt_{yt_id}"
            source_image_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
    elif source_uri and str(source_uri).startswith("x-file-cifs:"):
        cifs_hash = hashlib.md5(str(source_uri).encode("utf-8")).hexdigest()[:16]
        cache_key = f"samba_{cifs_hash}"
        if not image_url and speaker_ip:
            source_image_url = f"http://{speaker_ip}:1400/getaa?u={quote(str(source_uri), safe='')}&v=0"
        elif image_url:
            source_image_url = image_url
        source = source or "samba"
    elif image_url:
        img_hash = hashlib.md5(str(image_url).encode("utf-8")).hexdigest()[:16]
        cache_key = f"img_{img_hash}"
        source_image_url = image_url

    # Registrar en el servidor y precargar en caché SQLite bajo demanda
    local_proxy_url = None
    if cache_key and server_module:
        public_host = server_module.resolve_public_host(speaker_ip) if hasattr(server_module, "resolve_public_host") else getattr(server_module, "PUBLIC_HOST", "127.0.0.1")
        bind_port = getattr(server_module, "BIND_PORT", 39100)
        local_proxy_url = f"http://{public_host}:{bind_port}/image/{cache_key}"

        if hasattr(server_module, "IMAGE_SOURCES") and source_image_url:
            server_module.IMAGE_SOURCES[cache_key] = source_image_url

        if hasattr(server_module, "fetch_and_cache_image") and source_image_url:
            threading.Thread(
                target=server_module.fetch_and_cache_image,
                args=(cache_key, source_image_url),
                daemon=True,
            ).start()

    result["room"] = room
    if title:
        result["title"] = title
    if artist:
        result["artist"] = artist
    if album is not None:
        result["album"] = album
    if source:
        result["source"] = source
    if source_uri:
        result["source_uri"] = source_uri
    if cache_key:
        result["image_key"] = cache_key
    if local_proxy_url:
        result["image_url"] = local_proxy_url
    elif source_image_url:
        result["image_url"] = source_image_url
    elif image_url:
        result["image_url"] = image_url

    if source == "youtube" and source_image_url:
        result["external_image_url"] = source_image_url
    elif source_image_url and source_image_url.startswith("http://") and ":1400/" in source_image_url:
        result["local_art_uri"] = source_image_url

    return result


class DefaultBackend:
    """Backend por defecto que invoca las funciones del núcleo de server.py."""

    def _server(self):
        return _load_server()

    def get_room(self, room: Optional[str] = None) -> str:
        if room and room.strip():
            return room.strip()
        srv = self._server()
        discovered = srv.discover_any_room()
        if isinstance(discovered, (tuple, list)):
            return discovered[0]
        return str(discovered)

    def play_music(
        self,
        query: str,
        room: Optional[str] = None,
        mode: str = "track",
        artist: Optional[str] = None,
        album: Optional[str] = None,
        live: Optional[bool] = None,
        genre: Optional[str] = None,
        decade: Optional[int] = None,
        shuffle: bool = True,
        provider: Optional[str] = None,
        limit: int = 10,
        bypass_cache: bool = False,
    ) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        provider_val = provider.strip().lower() if provider and provider.strip().lower() != "auto" else None
        resolved = srv.resolve_and_play(
            query=query,
            room=target_room,
            library=srv.LIBRARY,
            mode=mode,
            artist=artist or "",
            genre=genre,
            decade=decade,
            shuffle=shuffle,
            provider=provider_val,
            limit=limit or 10,
            bypass_cache=bypass_cache,
            youtube_music_url=None,
            album=album,
            live=live,
        )
        resolved["status"] = srv.get_room_status(target_room)
        return enrich_playback_payload(resolved, target_room, server_module=srv)

    def search_music(
        self,
        query: str,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        live: Optional[bool] = None,
        provider: Optional[str] = None,
        limit: int = 5,
    ) -> Dict[str, Any]:
        srv = self._server()
        provider_val = provider.strip().lower() if provider and provider.strip().lower() != "auto" else None
        results = srv.search_tracks(
            query=query,
            artist=artist,
            album=album,
            live=live,
            provider=provider_val,
            limit=limit or 5,
        )
        return {
            "ok": True,
            "query": query,
            "count": len(results),
            "tracks": results,
        }

    def pause(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        result = srv.run_sonos("pause", "--name", target_room)
        payload = {"action": "pause", "room": target_room, "status": srv.get_room_status(target_room), "result": result}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def resume(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        result = srv.run_sonos("play", "--name", target_room)
        payload = {"action": "play", "room": target_room, "status": srv.get_room_status(target_room), "result": result}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def stop(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        result = srv.stop_playback(target_room)
        payload = {"action": "stop", "room": target_room, "status": srv.get_room_status(target_room), "result": result}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def next_track(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        result = srv.next_track(target_room)
        payload = {"action": "next", "room": target_room, "status": srv.get_room_status(target_room), "result": result}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def previous_track(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        result = srv.previous_track(target_room)
        payload = {"action": "previous", "room": target_room, "status": srv.get_room_status(target_room), "result": result}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def set_volume(self, level: int, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        result = srv.run_sonos("volume", "set", "--name", target_room, str(level))
        payload = {"action": "volume", "room": target_room, "level": level, "status": srv.get_room_status(target_room), "result": result}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def get_status(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = self.get_room(room)
        payload = {"room": target_room, "status": srv.get_room_status(target_room)}
        return enrich_playback_payload(payload, target_room, server_module=srv)

    def list_rooms(self) -> Dict[str, Any]:
        srv = self._server()
        result = srv.run_sonos("discover", "--format", "json")
        try:
            parsed = json.loads(result.get("stdout", "[]"))
            rooms = [item.get("name") for item in parsed if isinstance(item, dict) and item.get("name")]
            return {"rooms": rooms, "devices": parsed}
        except Exception:
            return {"raw": result.get("stdout", "")}

    def reindex_library(self, room: Optional[str] = None) -> Dict[str, Any]:
        srv = self._server()
        target_room = room.strip() if room and room.strip() else None
        return {"library": srv.sync_library(target_room, srv.LIBRARY)}


class MCPServer:
    """Implementación del servidor Model Context Protocol (MCP) v2024-11-05."""

    def __init__(self, backend=None):
        self.backend = backend or DefaultBackend()

    def handle_jsonrpc(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if not method or not isinstance(method, str):
            return self._make_error(req_id, -32600, "Invalid Request: missing method")

        # Handlers
        if method == "initialize":
            return self._handle_initialize(req_id, params)
        elif method in {"notifications/initialized", "initialized"}:
            return None  # Las notificaciones no devuelven respuesta
        elif method == "ping":
            return self._make_result(req_id, {})
        elif method == "tools/list":
            return self._handle_tools_list(req_id)
        elif method == "tools/call":
            return self._handle_tools_call(req_id, params)
        else:
            return self._make_error(req_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self, req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._make_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION
                }
            }
        )

    def _handle_tools_list(self, req_id: Any) -> Dict[str, Any]:
        return self._make_result(req_id, {"tools": TOOL_DEFINITIONS})

    def _handle_tools_call(self, req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments", {})
        logger.info("MCP Tool Call: %s(%s)", name, json.dumps(args, ensure_ascii=False))

        try:
            if name == "sonos_play_music":
                query = args.get("query")
                genre = args.get("genre")
                if not query and not genre:
                    raise ValueError("El parámetro 'query' (o 'genre') es obligatorio.")
                if not query:
                    query = genre or ""
                result = self.backend.play_music(
                    query=query,
                    room=args.get("room"),
                    mode=args.get("mode", "track"),
                    artist=args.get("artist"),
                    album=args.get("album"),
                    live=args.get("live"),
                    genre=genre,
                    decade=args.get("decade"),
                    shuffle=args.get("shuffle", True),
                    provider=args.get("provider"),
                    limit=args.get("limit", 10),
                    bypass_cache=args.get("bypass_cache", False),
                )
            elif name == "sonos_search_music":
                query = args.get("query")
                if not query:
                    raise ValueError("El parámetro 'query' es obligatorio.")
                result = self.backend.search_music(
                    query=query,
                    artist=args.get("artist"),
                    album=args.get("album"),
                    live=args.get("live"),
                    provider=args.get("provider"),
                    limit=args.get("limit", 5),
                )
            elif name == "sonos_pause":
                result = self.backend.pause(room=args.get("room"))
            elif name == "sonos_resume":
                result = self.backend.resume(room=args.get("room"))
            elif name == "sonos_stop":
                result = self.backend.stop(room=args.get("room"))
            elif name == "sonos_next":
                result = self.backend.next_track(room=args.get("room"))
            elif name == "sonos_previous":
                result = self.backend.previous_track(room=args.get("room"))
            elif name == "sonos_set_volume":
                level = args.get("level")
                if level is None or not isinstance(level, int) or not 0 <= level <= 100:
                    raise ValueError("El parámetro 'level' debe ser un número entero entre 0 y 100.")
                result = self.backend.set_volume(level=level, room=args.get("room"))
            elif name == "sonos_get_status":
                result = self.backend.get_status(room=args.get("room"))
            elif name == "sonos_list_rooms":
                result = self.backend.list_rooms()
            elif name == "sonos_reindex_library":
                result = self.backend.reindex_library(room=args.get("room"))
            else:
                return self._make_error(req_id, -32601, f"Unknown tool: {name}")

            return self._make_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False, indent=2)
                        }
                    ],
                    "isError": False
                }
            )
        except Exception as error:
            logger.error("MCP Tool '%s' failed: %s", name, error)
            return self._make_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error ejecutando {name}: {str(error)}"
                        }
                    ],
                    "isError": True
                }
            )

    def _make_result(self, req_id: Any, result: Any) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result
        }

    def _make_error(self, req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message
            }
        }


class SSEManager:
    """Gestiona sesiones concurrentes de Server-Sent Events (SSE) para clientes MCP."""

    def __init__(self, mcp_server: Optional[MCPServer] = None):
        self.mcp_server = mcp_server or MCPServer()
        self.sessions: Dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        with self._lock:
            self.sessions[session_id] = queue.Queue()
        return session_id

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            self.sessions.pop(session_id, None)

    def send_message(self, session_id: str, message: Dict[str, Any]) -> bool:
        with self._lock:
            q = self.sessions.get(session_id)
            if q is not None:
                q.put(message)
                return True
        return False

    def handle_sse(self, handler) -> None:
        """Mantiene abierta la conexión SSE con el cliente HTTP."""
        session_id = self.create_session()
        q = self.sessions[session_id]

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache, no-transform")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()

        # Emitir el evento de endpoint inicial requerido por el protocolo MCP SSE
        try:
            endpoint_event = f"event: endpoint\r\ndata: /messages?session_id={session_id}\r\n\r\n"
            handler.wfile.write(endpoint_event.encode("utf-8"))
            handler.wfile.flush()
        except Exception:
            self.remove_session(session_id)
            return

        try:
            while True:
                try:
                    message = q.get(timeout=15.0)
                    body = json.dumps(message, ensure_ascii=False)
                    payload = f"event: message\r\ndata: {body}\r\n\r\n"
                    handler.wfile.write(payload.encode("utf-8"))
                    handler.wfile.flush()
                except queue.Empty:
                    # Ping / keep-alive para evitar timeouts de proxies/clientes
                    handler.wfile.write(b": ping\r\n\r\n")
                    handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.remove_session(session_id)


def run_stdio():
    """Ejecuta el servidor MCP en modo standard I/O (stdio) para clientes CLI / docker exec."""
    server = MCPServer()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
            response = server.handle_jsonrpc(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as error:
            err_res = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(error)}"}
            }
            sys.stdout.write(json.dumps(err_res, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
