#!/usr/bin/env python3
"""Private loopback resolver API for sonoscli."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from music_resolver import (
    CacheStore,
    LibraryIndex,
    Resolution,
    Resolver,
    clean_youtube_url,
    normalize_query,
    parse_genre_and_decade,
    parse_sonos_didl,
)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sonos-api")

BIND_HOST = "0.0.0.0"
BIND_PORT = 39100


def resolve_public_host(target_hint: str | None = None) -> str:
    env_host = os.getenv("SONOS_API_HOST", "").strip()
    if env_host:
        return env_host
    targets = []
    if target_hint:
        targets.append((target_hint, 1400))
    targets.extend([("1.1.1.1", 80), ("10.255.255.255", 1)])
    for host, port in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, port))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except Exception:
            continue
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


PUBLIC_HOST = resolve_public_host()
STREAM_BITRATE = os.getenv("SONOS_STREAM_BITRATE", "320k")
YOUTUBE_STREAMS: dict[str, str] = {}
STREAM_URL_CACHE: dict[str, tuple[str, float]] = {}
STREAM_URL_CACHE_TTL = 7200  # 2 hours
MAX_ACTIVE_STREAMS = 1000
IMAGE_SOURCES: dict[str, str] = {}
DATA_DIR = Path("/data")
DATABASE_PATH = DATA_DIR / "music-cache.sqlite"
LIBRARY_STATUS_PATH = DATA_DIR / "library-sync.json"
MAX_QUERY_LENGTH = 180
MAX_LIBRARY_TRACKS = 50_000
LIBRARY_PAGE_SIZE = 500
ALLOWED_MEDIA_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}
LIBRARY_LOCK = threading.Lock()
REINDEX_CRON = os.getenv("SONOS_REINDEX_CRON", "").strip()
REINDEX_ROOM = os.getenv("SONOS_REINDEX_ROOM", "").strip()
REINDEX_TIMEZONE = os.getenv("TZ", "UTC").strip() or "UTC"


def fail(message: str, status: int = HTTPStatus.BAD_REQUEST) -> tuple[int, dict]:
    return status, {"ok": False, "error": message}


def validate_room(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError("room is required and must be at most 100 characters")
    if any(char in value for char in "\x00\r\n"):
        raise ValueError("room contains invalid characters")
    return value.strip()


def validate_query(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_LENGTH:
        raise ValueError(f"query is required and must be at most {MAX_QUERY_LENGTH} characters")
    if any(char in value for char in "\x00\r\n"):
        raise ValueError("query contains invalid characters")
    return value.strip()


def is_youtube_url(value: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host in ALLOWED_MEDIA_HOSTS


def validate_youtube_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("url is required")
    cleaned = clean_youtube_url(value)
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_MEDIA_HOSTS:
        raise ValueError("url must be an HTTPS YouTube or YouTube Music URL")
    return cleaned


def run_sonos(*args: str, timeout: int = 45) -> dict:
    completed = subprocess.run(
        ["sonos", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    result = {"exit_code": completed.returncode, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    if completed.returncode != 0:
        raise RuntimeError(result["stderr"] or result["stdout"] or "sonos command failed")
    return result


def search_youtube(query: str) -> Resolution:
    completed = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", f"ytsearch1:{query}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "YouTube search failed")
    result = json.loads(completed.stdout)
    entries = result.get("entries") or []
    if not entries:
        raise RuntimeError("No YouTube result found")
    entry = entries[0]
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise RuntimeError("YouTube returned an invalid video identifier")
    return Resolution(
        provider="youtube", kind="url", target=f"https://www.youtube.com/watch?v={video_id}",
        title=str(entry.get("title") or query), artist=str(entry.get("channel") or entry.get("uploader") or ""),
    )


def search_youtube_many(query: str, limit: int) -> list[dict[str, str]]:
    completed = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", f"ytsearch{limit}:{query}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "YouTube search failed")
    entries = json.loads(completed.stdout).get("entries") or []
    tracks = []
    for entry in entries:
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
            continue
        tracks.append({
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": str(entry.get("title") or query),
            "artist": str(entry.get("channel") or entry.get("uploader") or ""),
            "album": "",
        })
    return tracks


def youtube_json(url: str) -> dict:
    completed = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", url],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "YouTube Music lookup failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("YouTube Music returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("YouTube Music returned an invalid result")
    return payload


def youtube_watch_url(entry: dict) -> str | None:
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def select_youtube_music_album(query: str, artist: str, fetcher=youtube_json) -> dict:
    if is_youtube_url(query):
        payload = fetcher(query)
        title = str(payload.get("title") or "Album").strip()
        for prefix in ["Album - ", "Álbum - ", "Sencillo - ", "Single - ", "EP - "]:
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                break
        tracks = []
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            watch_url = youtube_watch_url(entry)
            track_title = str(entry.get("title") or "").strip()
            if not watch_url or not track_title:
                continue
            tracks.append({
                "url": watch_url,
                "title": track_title,
                "artist": str(entry.get("channel") or entry.get("uploader") or artist).strip(),
                "album": title,
            })
        if len(tracks) >= 1:
            return {
                "album": title,
                "artist": artist or (tracks[0]["artist"] if tracks else ""),
                "tracks": tracks,
            }
        raise RuntimeError("No tracks found in the provided YouTube album/playlist URL")

    expected_album = normalize_query(query)
    expected_artist = normalize_query(artist)
    search_url = "https://music.youtube.com/search?q=" + quote(f"{artist} {query}".strip())
    candidates = fetcher(search_url).get("entries") or []
    best: tuple[int, dict] | None = None
    for candidate in candidates[:30]:
        if not isinstance(candidate, dict):
            continue
        candidate_url = candidate.get("url")
        parsed = urlparse(candidate_url) if isinstance(candidate_url, str) else None
        if not parsed or parsed.hostname != "music.youtube.com" or not (parsed.path.startswith("/browse/") or parsed.path.startswith("/playlist")):
            continue
        payload = fetcher(candidate_url)
        title = str(payload.get("title") or "").strip()
        album = title
        for prefix in ["Album - ", "Álbum - ", "Sencillo - ", "Single - ", "EP - "]:
            if album.startswith(prefix):
                album = album[len(prefix):].strip()
                break
        normalized_title = normalize_query(title)
        normalized_album = normalize_query(album)
        exact_album = normalized_album and normalized_album == expected_album
        title_names_artist_and_album = expected_album in normalized_title and (not expected_artist or expected_artist in normalized_title)
        if not exact_album and not title_names_artist_and_album:
            continue

        candidate_artist = str(payload.get("channel") or payload.get("uploader") or "").strip()
        if not candidate_artist and payload.get("entries"):
            for e in payload["entries"]:
                if isinstance(e, dict) and (e.get("channel") or e.get("uploader")):
                    candidate_artist = str(e.get("channel") or e.get("uploader")).strip()
                    break
        norm_candidate_artist = normalize_query(candidate_artist)
        if expected_artist:
            artist_match = (
                expected_artist in norm_candidate_artist
                or norm_candidate_artist in expected_artist
                or expected_artist in normalized_title
            )
        else:
            artist_match = True

        if not artist_match:
            continue

        if not exact_album:
            album = query
        tracks = []
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            watch_url = youtube_watch_url(entry)
            track_title = str(entry.get("title") or "").strip()
            if not watch_url or not track_title:
                continue
            tracks.append({
                "url": watch_url,
                "title": track_title,
                "artist": str(entry.get("channel") or entry.get("uploader") or artist).strip(),
                "album": album,
            })
        numbered_tracks = []
        for track in tracks:
            match = re.match(r"\s*(\d{1,2})\s*[-.)]", track["title"])
            if match:
                numbered_tracks.append((int(match.group(1)), track))
        if len(numbered_tracks) >= 2:
            tracks = [track for _, track in sorted(numbered_tracks, key=lambda item: item[0])]
        if len(tracks) < 2:
            continue
        score = 100 if exact_album else 10
        score += len(numbered_tracks)
        resolved = {"album": album, "artist": artist or tracks[0]["artist"], "tracks": tracks}
        if best is None or score > best[0]:
            best = (score, resolved)
    if best is not None:
        return best[1]
    raise RuntimeError("No exact YouTube Music album found")


def select_youtube_music_artist(artist: str, limit: int = 25, fetcher=youtube_json) -> dict:
    if is_youtube_url(artist):
        payload = fetcher(artist)
        title = str(payload.get("title") or "Artist").strip()
        tracks = []
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            watch_url = youtube_watch_url(entry)
            track_title = str(entry.get("title") or "").strip()
            if not watch_url or not track_title:
                continue
            tracks.append({
                "url": watch_url,
                "title": track_title,
                "artist": str(entry.get("channel") or entry.get("uploader") or title).strip(),
                "album": title,
            })
        if len(tracks) >= 1:
            return {"artist": title, "tracks": tracks[:limit]}
        raise RuntimeError("No tracks found in the provided YouTube artist URL")

    search_url = "https://music.youtube.com/search?q=" + quote(f"{artist}".strip())
    payload = fetcher(search_url)
    candidates = payload.get("entries") or []

    tracks = []
    norm_artist = normalize_query(artist)
    for cand in candidates[:20]:
        if not isinstance(cand, dict):
            continue
        c_url = cand.get("url")
        if not isinstance(c_url, str):
            continue
        parsed = urlparse(c_url)
        if parsed.hostname != "music.youtube.com":
            continue

        if parsed.path.startswith("/browse/") or parsed.path.startswith("/playlist") or parsed.path.startswith("/channel/"):
            cand_payload = fetcher(c_url)
            cand_title = str(cand_payload.get("title") or "").strip()
            norm_title = normalize_query(cand_title)

            if norm_artist in norm_title or norm_title in norm_artist:
                for entry in cand_payload.get("entries") or []:
                    if not isinstance(entry, dict):
                        continue
                    w_url = youtube_watch_url(entry)
                    t_title = str(entry.get("title") or "").strip()
                    if not w_url or not t_title:
                        continue
                    tracks.append({
                        "url": w_url,
                        "title": t_title,
                        "artist": str(entry.get("channel") or entry.get("uploader") or artist).strip(),
                        "album": cand_title,
                    })
                if len(tracks) >= 3:
                    break

    if len(tracks) < 3:
        search_query = f"{artist} songs"
        direct_tracks = search_youtube_many(search_query, limit=limit)
        if direct_tracks:
            return {"artist": artist, "tracks": direct_tracks}

    if tracks:
        return {"artist": artist, "tracks": tracks[:limit]}
    raise RuntimeError(f"No YouTube Music tracks found for artist: {artist}")


def resolve_youtube_stream(url: str, force_fresh: bool = False) -> str:
    clean_url = clean_youtube_url(url)
    now = time.time()
    if not force_fresh and clean_url in STREAM_URL_CACHE:
        cached_stream, cached_at = STREAM_URL_CACHE[clean_url]
        if now - cached_at < STREAM_URL_CACHE_TTL:
            logger.debug("Using cached YouTube stream for %s", clean_url)
            return cached_stream

    completed = subprocess.run(
        [
            "yt-dlp",
            "--no-playlist",
            "-f",
            "bestaudio[ext=m4a]/bestaudio[ext=aac]/bestaudio[ext=mp3]/bestaudio/best",
            "--extract-flat",
            "false",
            "-g",
            clean_url,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(completed.stderr.strip() or "Could not resolve YouTube audio")
    stream_url = completed.stdout.strip().splitlines()[0]
    STREAM_URL_CACHE[clean_url] = (stream_url, now)
    return stream_url


def discover_room(room: str) -> str:
    result = run_sonos("discover", "--format", "json")
    devices = json.loads(result["stdout"])
    for device in devices:
        if device.get("name") == room and isinstance(device.get("ip"), str):
            return device["ip"]
    raise RuntimeError(f"Sonos room not found: {room}")


def discover_any_room() -> tuple[str, str]:
    result = run_sonos("discover", "--format", "json")
    devices = json.loads(result["stdout"])
    for device in devices:
        name = device.get("name")
        ip = device.get("ip")
        if name and isinstance(name, str) and ip and isinstance(ip, str):
            return name, ip
    raise RuntimeError("No active Sonos speakers discovered on local network")


def get_room_status(room: str) -> dict:
    try:
        result = run_sonos("status", "--name", room, "--format", "json")
        return json.loads(result["stdout"])
    except Exception:
        return {}


def get_transport_state(room: str) -> str | None:
    # 1. Fast direct SOAP AVTransport query (typically 5-15ms)
    try:
        ip = discover_room(room)
        res_xml = soap_action(
            ip,
            "urn:schemas-upnp-org:service:AVTransport:1",
            "GetTransportInfo",
            "<InstanceID>0</InstanceID>",
        )
        match = re.search(r"<CurrentTransportState>([^<]+)</CurrentTransportState>", res_xml)
        if match:
            return match.group(1).strip().upper()
    except Exception:
        pass

    # 2. Fallback to sonos status
    try:
        status = get_room_status(room)
        state = status.get("transport", {}).get("State")
        if state:
            return str(state).strip().upper()
    except Exception:
        pass
    return None


def wait_for_playback_state(
    room: str,
    target_states: set[str] | tuple[str, ...] = ("PLAYING",),
    timeout: float = 4.0,
    poll_interval: float = 0.2,
) -> str | None:
    targets = set(target_states)
    start = time.time()
    last_state = None
    unreachable_count = 0
    while time.time() - start < timeout:
        state = get_transport_state(room)
        if state:
            last_state = state
            if state in targets:
                return state
        else:
            unreachable_count += 1
            if unreachable_count >= 2:
                break
        time.sleep(poll_interval)
    return last_state


def soap_browse(ip: str, start_index: int, count: int) -> tuple[int, int, str]:
    body = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
    <ObjectID>A:TRACKS</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter>
    <StartingIndex>{start_index}</StartingIndex><RequestedCount>{count}</RequestedCount><SortCriteria></SortCriteria>
  </u:Browse></s:Body>
</s:Envelope>'''.encode("utf-8")
    request = Request(
        f"http://{ip}:1400/MediaServer/ContentDirectory/Control", data=body, method="POST",
        headers={
            "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
            "Content-Type": "text/xml; charset=utf-8",
        },
    )
    with urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())
    values = {element.tag.rsplit("}", 1)[-1]: element.text or "" for element in root.iter()}
    return int(values.get("NumberReturned", "0")), int(values.get("TotalMatches", "0")), values.get("Result", "")


def soap_action(ip: str, service: str, action: str, arguments: str) -> str:
    envelope = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>
<u:{action} xmlns:u="{service}">{arguments}</u:{action}>
</s:Body></s:Envelope>'''.encode("utf-8")
    request = Request(
        f"http://{ip}:1400/MediaRenderer/AVTransport/Control", data=envelope, method="POST",
        headers={"SOAPACTION": f'"{service}#{action}"', "Content-Type": "text/xml; charset=utf-8"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def get_protocol_info(uri: str, default_protocol: str = "x-file-cifs") -> str:
    lower = uri.lower()
    if lower.endswith(".flac"):
        mime = "audio/flac"
    elif lower.endswith((".m4a", ".mp4", ".aac")):
        mime = "audio/mp4"
    elif lower.endswith(".wav"):
        mime = "audio/x-wav"
    elif lower.endswith(".ogg"):
        mime = "audio/ogg"
    else:
        mime = "audio/mpeg"
    return f"{escape(default_protocol)}:*:{mime}:*"


def local_track_metadata(track: dict[str, str]) -> str:
    title = escape(track["title"])
    artist = escape(track.get("artist", ""))
    album = escape(track.get("album", ""))
    genre = escape(str(track.get("genre", "")))
    genre_tag = f"<upnp:genre>{genre}</upnp:genre>" if genre else ""
    protocol = track.get("protocol", "x-file-cifs")
    protocol_info = get_protocol_info(track["uri"], protocol)
    return f'''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><res protocolInfo="{protocol_info}"><![CDATA[{track["uri"]}]]></res><dc:title>{title}</dc:title><dc:creator>{artist}</dc:creator><upnp:album>{album}</upnp:album>{genre_tag}<upnp:class>object.item.audioItem.musicTrack</upnp:class></item></DIDL-Lite>'''


def play_local_list(room: str, tracks: list[dict[str, str]]) -> None:
    if not tracks:
        raise RuntimeError("No local tracks found")
    ip = discover_room(room)
    device = run_sonos("discover", "--format", "json")["stdout"]
    udn = next(item["udn"] for item in json.loads(device) if item.get("name") == room)
    avtransport = "urn:schemas-upnp-org:service:AVTransport:1"
    soap_action(ip, avtransport, "RemoveAllTracksFromQueue", "<InstanceID>0</InstanceID>")
    for track in tracks:
        args = (
            "<InstanceID>0</InstanceID><EnqueuedURI>" + escape(track["uri"]) + "</EnqueuedURI>"
            "<EnqueuedURIMetaData>" + escape(local_track_metadata(track)) + "</EnqueuedURIMetaData>"
            "<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>"
        )
        soap_action(ip, avtransport, "AddURIToQueue", args)
    queue_uri = f"x-rincon-queue:{udn}#0"
    soap_action(ip, avtransport, "SetAVTransportURI", f"<InstanceID>0</InstanceID><CurrentURI>{escape(queue_uri)}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>")
    soap_action(ip, avtransport, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")


def stop_playback(room: str) -> dict:
    try:
        return run_sonos("stop", "--name", room)
    except Exception:
        ip = discover_room(room)
        soap_action(ip, "urn:schemas-upnp-org:service:AVTransport:1", "Stop", "<InstanceID>0</InstanceID>")
        return {"exit_code": 0, "stdout": "", "stderr": ""}


def next_track(room: str) -> dict:
    try:
        return run_sonos("next", "--name", room)
    except Exception:
        ip = discover_room(room)
        soap_action(ip, "urn:schemas-upnp-org:service:AVTransport:1", "Next", "<InstanceID>0</InstanceID>")
        return {"exit_code": 0, "stdout": "", "stderr": ""}


def previous_track(room: str) -> dict:
    try:
        return run_sonos("previous", "--name", room)
    except Exception:
        try:
            return run_sonos("prev", "--name", room)
        except Exception:
            ip = discover_room(room)
            soap_action(ip, "urn:schemas-upnp-org:service:AVTransport:1", "Previous", "<InstanceID>0</InstanceID>")
            return {"exit_code": 0, "stdout": "", "stderr": ""}



def play_youtube_list(room: str, tracks: list[dict[str, str]]) -> list[dict[str, str]]:
    speaker_ip = discover_room(room)
    public_host = resolve_public_host(speaker_ip)
    queued = []
    for track in tracks:
        token = uuid.uuid4().hex
        cleaned_url = clean_youtube_url(track["url"])
        if len(YOUTUBE_STREAMS) >= MAX_ACTIVE_STREAMS:
            oldest_key = next(iter(YOUTUBE_STREAMS))
            YOUTUBE_STREAMS.pop(oldest_key, None)
        YOUTUBE_STREAMS[token] = cleaned_url
        queued.append({**track, "url": cleaned_url, "uri": f"http://{public_host}:{BIND_PORT}/stream/youtube/{token}", "protocol": "http-get"})

    # Pre-warm stream for the first track so Sonos gets audio immediately without timeout
    if queued:
        try:
            resolve_youtube_stream(queued[0]["url"])
        except Exception as error:
            logger.warning("Stream pre-warm failed for playlist first track: %s", error)

    play_local_list(room, queued)

    # Confirm playback state and retry play once if STOPPED
    state = wait_for_playback_state(room, target_states={"PLAYING"}, timeout=4.0, poll_interval=0.2)
    if state == "STOPPED":
        logger.warning("Sonos room '%s' is STOPPED after queueing playlist; attempting retry Play...", room)
        try:
            run_sonos("play", "--name", room)
            wait_for_playback_state(room, target_states={"PLAYING"}, timeout=3.0, poll_interval=0.2)
        except Exception as error:
            logger.warning("Retry play failed: %s", error)

    return queued


def didl_tracks(didl: str) -> list[dict[str, str]]:
    return parse_sonos_didl(didl)


def write_library_status(payload: dict) -> None:
    temporary = LIBRARY_STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(temporary, LIBRARY_STATUS_PATH)


def library_status(index: LibraryIndex) -> dict:
    payload = {"tracks": index.count(), "last_sync": None}
    if LIBRARY_STATUS_PATH.exists():
        try:
            payload.update(json.loads(LIBRARY_STATUS_PATH.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return payload


def sync_library(room: str | None, index: LibraryIndex) -> dict:
    target_room = (room or "").strip() or REINDEX_ROOM
    with LIBRARY_LOCK:
        if not target_room:
            target_room, _ = discover_any_room()
        logger.info("Synchronizing music library from Sonos room '%s'...", target_room)
        ip = discover_room(target_room)
        tracks: list[dict[str, str]] = []
        start = 0
        total = None
        while total is None or start < total:
            returned, total_matches, didl = soap_browse(ip, start, LIBRARY_PAGE_SIZE)
            total = min(total_matches, MAX_LIBRARY_TRACKS)
            batch = didl_tracks(didl)
            tracks.extend(batch)
            start += returned
            if returned == 0:
                break
        index.replace_tracks(tracks)
        payload = {"tracks": len(tracks), "sonos_total": total or 0, "last_sync": int(time.time()), "room": target_room}
        write_library_status(payload)
        logger.info("Library sync complete: %d tracks indexed from '%s'", len(tracks), target_room)
        return payload


def parse_daily_reindex_cron(value: str) -> tuple[int, int]:
    fields = value.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ValueError("SONOS_REINDEX_CRON must use daily form: '<minute> <hour> * * *'")
    try:
        minute, hour = (int(fields[0]), int(fields[1]))
    except ValueError as error:
        raise ValueError("SONOS_REINDEX_CRON minute and hour must be integers") from error
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ValueError("SONOS_REINDEX_CRON minute must be 0-59 and hour 0-23")
    return hour, minute


def seconds_until_next_daily_run(hour: int, minute: int, timezone: ZoneInfo, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


def start_reindex_scheduler() -> None:
    if not REINDEX_CRON:
        return
    hour, minute = parse_daily_reindex_cron(REINDEX_CRON)
    try:
        timezone = ZoneInfo(REINDEX_TIMEZONE)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"TZ is not a valid IANA timezone: {REINDEX_TIMEZONE}") from error

    logger.info("Scheduled library reindex cron active: '%s' (timezone: %s)", REINDEX_CRON, timezone)

    def run() -> None:
        while True:
            time.sleep(seconds_until_next_daily_run(hour, minute, timezone))
            try:
                sync_library(REINDEX_ROOM or None, LIBRARY)
            except Exception as error:
                logger.error("Scheduled library reindex failed: %s", error)

    threading.Thread(target=run, name="scheduled-library-reindex", daemon=True).start()


CACHE = CacheStore(DATABASE_PATH)
LIBRARY = LibraryIndex(DATABASE_PATH)
RESOLVER = Resolver(CACHE, LIBRARY, search_youtube)


def fetch_and_cache_image(key: str, source_url: str) -> tuple[str, bytes] | None:
    if not source_url:
        return None
    try:
        req = Request(source_url, headers={"User-Agent": "SonosAPI/2.0"})
        with urlopen(req, timeout=10) as resp:
            data = resp.read()
            raw_content_type = resp.headers.get("Content-Type", "image/jpeg")
            mime = raw_content_type.split(";")[0].strip() if raw_content_type else "image/jpeg"
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            CACHE.put_image(key, mime, data)
            logger.info("Cached image '%s' (%d bytes) from %s", key, len(data), source_url)
            return mime, data
    except Exception as error:
        logger.warning("Failed to fetch image '%s' from %s: %s", key, source_url, error)
        return None


def get_image(key: str) -> tuple[str, bytes] | None:
    cached = CACHE.get_image(key)
    if cached:
        return cached
    source_url = IMAGE_SOURCES.get(key)
    if source_url:
        return fetch_and_cache_image(key, source_url)
    return None


def resolve(
    query: str,
    room: str,
    youtube_music_url: str | None = None,
    reindex: bool = False,
    provider: str | None = None,
    bypass_cache: bool = False,
    artist: str | None = None,
) -> Resolution:
    if reindex or LIBRARY.count() == 0:
        sync_library(room, LIBRARY)
    return RESOLVER.resolve(query, youtube_music_url=youtube_music_url, provider=provider, bypass_cache=bypass_cache, artist=artist)


def resolve_youtube_album(query: str, artist: str, bypass_cache: bool = False) -> dict:
    tracks = None if bypass_cache else CACHE.get_album(query, artist)
    if tracks is None:
        resolved = select_youtube_music_album(query, artist)
        tracks = resolved["tracks"]
        CACHE.put_album(query, artist, tracks, ttl_seconds=30 * 24 * 60 * 60)
        return resolved
    if not tracks:
        raise RuntimeError("Cached YouTube Music album contains no tracks")
    return {
        "album": str(tracks[0].get("album") or query),
        "artist": artist or str(tracks[0].get("artist") or ""),
        "tracks": tracks,
    }


def resolve_youtube_artist(artist: str, limit: int = 25, bypass_cache: bool = False) -> dict:
    tracks = None if bypass_cache else CACHE.get_album(artist, "__artist__")
    if tracks is None:
        resolved = select_youtube_music_artist(artist, limit=limit)
        tracks = resolved["tracks"]
        CACHE.put_album(artist, "__artist__", tracks, ttl_seconds=14 * 24 * 60 * 60)
        return resolved
    return {"artist": artist, "tracks": tracks}


def play_url_with_retry(room: str, url: str, max_retries: int = 1) -> dict:
    clean_url = clean_youtube_url(url)

    # Pre-warm: pre-resolve stream URL to validate format and cache direct URL
    try:
        resolve_youtube_stream(clean_url)
    except Exception as error:
        logger.warning("Stream pre-warm failed for %s: %s", clean_url, error)

    last_result = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(
                "Playback in room '%s' was STOPPED or unconfirmed; re-resolving fresh stream and retrying (%d/%d)...",
                room, attempt, max_retries,
            )
            STREAM_URL_CACHE.pop(clean_url, None)
            try:
                resolve_youtube_stream(clean_url, force_fresh=True)
            except Exception:
                pass
            time.sleep(0.5)

        last_result = run_sonos("play-url", "--name", room, clean_url, timeout=90)

        # Wait up to 4s for confirmed PLAYING
        state = wait_for_playback_state(room, target_states={"PLAYING"}, timeout=4.0, poll_interval=0.2)
        if state == "PLAYING":
            logger.info("Room '%s' confirmed PLAYING on attempt %d", room, attempt + 1)
            break
        elif state == "STOPPED" and attempt < max_retries:
            logger.warning("Room '%s' is STOPPED after play-url; attempting automatic retry...", room)
            continue

    return last_result


def play_resolution(room: str, resolution: Resolution) -> dict:
    if resolution.kind == "uri":
        res = run_sonos("play-uri", "--name", room, resolution.target, "--title", resolution.title, timeout=90)
        wait_for_playback_state(room, target_states={"PLAYING"}, timeout=3.0, poll_interval=0.2)
        return res
    return play_url_with_retry(room, resolution.target)


def resolve_and_play(
    query: str,
    room: str,
    library: LibraryIndex,
    mode: str = "track",
    artist: str = "",
    genre: str | None = None,
    decade: int | None = None,
    shuffle: bool = True,
    provider: str | None = None,
    limit: int = 10,
    bypass_cache: bool = False,
    youtube_music_url: str | None = None,
    action: str = "play",
) -> dict:
    if mode not in {"track", "list", "album", "artist", "genre"}:
        raise ValueError("mode must be track, list, album, artist, or genre")
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    if provider is not None and provider not in {"samba", "youtube", "youtube_music"}:
        raise ValueError("provider must be samba, youtube, or youtube_music")

    logger.info(
        "resolve_and_play: mode=%s, room=%s, query=%r, artist=%r, genre=%r, decade=%s, provider=%s",
        mode, room, query, artist, genre, decade, provider or "auto"
    )
    is_url = is_youtube_url(query)

    # 1. Mode: Artist
    if mode == "artist":
        target_artist = artist.strip() if artist else query.strip()
        if provider in {"youtube", "youtube_music"} or is_url:
            resolved_artist = resolve_youtube_artist(target_artist, limit=limit, bypass_cache=bypass_cache)
            tracks = list(resolved_artist["tracks"])
            if shuffle:
                import random
                random.shuffle(tracks)
            payload = {
                "ok": True,
                "provider": "youtube",
                "mode": "artist",
                "artist": resolved_artist["artist"],
                "count": len(tracks),
                "tracks": tracks,
            }
            if action == "play":
                payload["result"] = {"queued": len(play_youtube_list(room, tracks))}
            return payload

        if library.count() == 0:
            sync_library(room, library)
        tracks = library.search_artist(target_artist, limit=limit, shuffle=shuffle)
        if not tracks:
            if provider == "samba":
                raise RuntimeError(f"No local tracks found for artist: {target_artist}")
            resolved_artist = resolve_youtube_artist(target_artist, limit=limit, bypass_cache=bypass_cache)
            tracks = list(resolved_artist["tracks"])
            if shuffle:
                import random
                random.shuffle(tracks)
            payload = {
                "ok": True,
                "provider": "youtube",
                "mode": "artist",
                "artist": resolved_artist["artist"],
                "count": len(tracks),
                "tracks": tracks,
            }
            if action == "play":
                payload["result"] = {"queued": len(play_youtube_list(room, tracks))}
            return payload

        payload = {
            "ok": True,
            "provider": "samba",
            "mode": "artist",
            "artist": tracks[0]["artist"],
            "count": len(tracks),
            "tracks": tracks,
        }
        if action == "play":
            play_local_list(room, tracks)
            payload["result"] = {"queued": len(tracks)}
        return payload

    # 2. Mode: Genre
    if mode == "genre":
        raw_genre = (genre or "").strip()
        target_decade = decade
        if not raw_genre and target_decade is None:
            p_genre, p_decade = parse_genre_and_decade(query)
            raw_genre = p_genre
            target_decade = p_decade
        if not raw_genre and target_decade is None:
            raw_genre = query

        if provider in {"youtube", "youtube_music"} or is_url:
            search_term = f"{raw_genre} {f'{target_decade}s' if target_decade else ''} playlist".strip()
            tracks = search_youtube_many(search_term, limit)
            if not tracks:
                raise RuntimeError(f"No YouTube tracks found for genre: {query}")
            payload = {
                "ok": True,
                "provider": "youtube",
                "mode": "genre",
                "genre": raw_genre,
                "decade": target_decade,
                "count": len(tracks),
                "tracks": tracks,
            }
            if action == "play":
                payload["result"] = {"queued": len(play_youtube_list(room, tracks))}
            return payload

        if library.count() == 0:
            sync_library(room, library)
        tracks = library.search_genre(genre=raw_genre or None, decade=target_decade, limit=limit, shuffle=shuffle)
        if not tracks:
            if provider == "samba":
                desc = f"'{raw_genre}'" if raw_genre else ""
                if target_decade:
                    desc += f" de los {target_decade}s"
                raise RuntimeError(f"No local tracks found for genre {desc.strip()}")
            search_term = f"{raw_genre} {f'{target_decade}s' if target_decade else ''} canciones".strip()
            tracks = search_youtube_many(search_term, limit)
            if not tracks:
                raise RuntimeError(f"No tracks found for genre: {query}")
            payload = {
                "ok": True,
                "provider": "youtube",
                "mode": "genre",
                "genre": raw_genre,
                "decade": target_decade,
                "count": len(tracks),
                "tracks": tracks,
            }
            if action == "play":
                payload["result"] = {"queued": len(play_youtube_list(room, tracks))}
            return payload

        payload = {
            "ok": True,
            "provider": "samba",
            "mode": "genre",
            "genre": raw_genre,
            "decade": target_decade,
            "count": len(tracks),
            "tracks": tracks,
        }
        if action == "play":
            play_local_list(room, tracks)
            payload["result"] = {"queued": len(tracks)}
        return payload

    # 3. Mode: Album
    if mode == "album":
        album = query
        album_artist = artist.strip() if artist else ""
        if not album_artist and not is_url:
            parts = re.split(r"\s+de\s+", query, flags=re.IGNORECASE)
            if len(parts) >= 2:
                album, album_artist = " de ".join(parts[:-1]).strip(), parts[-1].strip()

        if provider in {"youtube", "youtube_music"} or is_url:
            resolved_album = resolve_youtube_album(album, album_artist, bypass_cache=bypass_cache)
            tracks = resolved_album["tracks"]
            payload = {
                "ok": True,
                "provider": "youtube",
                "mode": "album",
                "album": resolved_album["album"],
                "artist": resolved_album["artist"],
                "count": len(tracks),
                "tracks": tracks,
            }
            if action == "play":
                payload["result"] = {"queued": len(play_youtube_list(room, tracks))}
            return payload

        if library.count() == 0:
            sync_library(room, library)
        tracks = library.search_album(album, artist=album_artist or None)
        if not tracks:
            if provider == "samba":
                raise RuntimeError("No local tracks found for the album")
            resolved_album = resolve_youtube_album(album, album_artist, bypass_cache=bypass_cache)
            tracks = resolved_album["tracks"]
            payload = {
                "ok": True,
                "provider": "youtube",
                "mode": "album",
                "album": resolved_album["album"],
                "artist": resolved_album["artist"],
                "count": len(tracks),
                "tracks": tracks,
            }
            if action == "play":
                payload["result"] = {"queued": len(play_youtube_list(room, tracks))}
            return payload

        payload = {
            "ok": True,
            "provider": "samba",
            "mode": "album",
            "album": tracks[0]["album"],
            "artist": tracks[0]["artist"],
            "count": len(tracks),
            "tracks": tracks,
        }
        if action == "play":
            play_local_list(room, tracks)
            payload["result"] = {"queued": len(tracks)}
        return payload

    # 4. Mode: List
    if mode == "list":
        clean_artist = artist.strip() if artist else None
        yt_query = f"{query} {clean_artist}".strip() if clean_artist else query
        if provider in {"youtube", "youtube_music"} or is_url:
            tracks = search_youtube_many(yt_query, limit)
            if not tracks:
                raise RuntimeError("No YouTube tracks found for the list")
            resolved_tracks = play_youtube_list(room, tracks) if action == "play" else tracks
            payload = {"ok": True, "provider": "youtube", "count": len(tracks), "tracks": tracks}
            if resolved_tracks is not tracks:
                payload["result"] = {"queued": len(resolved_tracks)}
            return payload

        if library.count() == 0:
            sync_library(room, library)
        tracks = library.search_many(query, limit=limit, artist=clean_artist)
        if not tracks and not clean_artist:
            p_genre, p_decade = parse_genre_and_decade(query)
            if p_genre or p_decade:
                tracks = library.search_genre(genre=p_genre or None, decade=p_decade, limit=limit, shuffle=True)
        if not tracks:
            if provider == "samba":
                err_msg = "No local tracks found for the list"
                if clean_artist:
                    err_msg += f" by {clean_artist}"
                raise RuntimeError(err_msg)
            tracks = search_youtube_many(yt_query, limit)
            if not tracks:
                raise RuntimeError("No YouTube tracks found for the list")
            resolved_tracks = play_youtube_list(room, tracks) if action == "play" else tracks
            payload = {"ok": True, "provider": "youtube", "count": len(tracks), "tracks": tracks}
            if resolved_tracks is not tracks:
                payload["result"] = {"queued": len(resolved_tracks)}
            return payload

        payload = {"ok": True, "provider": "samba", "count": len(tracks), "tracks": tracks}
        if action == "play":
            play_local_list(room, tracks)
            payload["result"] = {"queued": len(tracks)}
        return payload

    # 5. Mode: Track (default)
    if is_url:
        resolution = Resolution("youtube", "url", query, "YouTube Track")
        payload = {"ok": True, "resolution": asdict(resolution)}
        if action == "play":
            payload["result"] = play_resolution(room, resolution)
        return payload

    clean_artist = artist.strip() if artist else None
    resolution = resolve(query, room, youtube_music_url, False, provider, bypass_cache, artist=clean_artist)
    payload = {"ok": True, "resolution": asdict(resolution)}
    if action == "play":
        payload["result"] = play_resolution(room, resolution)
    return payload


try:
    from _version import __version__
except ImportError:
    try:
        from app._version import __version__
    except ImportError:
        from ._version import __version__

try:
    from mcp_server import MCPServer, SSEManager
except ImportError:
    try:
        from app.mcp_server import MCPServer, SSEManager
    except ImportError:
        from .mcp_server import MCPServer, SSEManager

MCP_SERVER = MCPServer()
MCP_SSE_MANAGER = SSEManager(MCP_SERVER)


class Handler(BaseHTTPRequestHandler):
    server_version = f"SonosPrivateAPI/{__version__}"

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self, "path", "").startswith("/health"):
            logger.debug("%s - - %s", self.address_string(), format % args)
            return
        logger.info("%s - - %s", self.address_string(), format % args)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/stream/youtube/"):
            token = parsed.path.rsplit("/", 1)[-1]
            if token not in YOUTUBE_STREAMS:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            return
        if parsed.path.startswith("/image/"):
            key = parsed.path.split("/image/", 1)[-1].strip()
            img = get_image(key)
            if img:
                mime_type, data = img
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=2592000")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path in {"/health", "/status", "/library/status"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def respond(self, status: int, payload: dict) -> None:
        current_room = getattr(self, "_current_room", None)
        if current_room and self.path in {"/resolve-and-play", "/search-and-play"} and payload.get("ok") and "status" not in payload:
            payload["status"] = get_room_status(current_room)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 4096:
            raise ValueError("request body must be between 1 and 4096 bytes")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/sse":
            MCP_SSE_MANAGER.handle_sse(self)
            return
        if parsed.path.startswith("/stream/youtube/"):
            token = parsed.path.rsplit("/", 1)[-1]
            source_url = YOUTUBE_STREAMS.get(token)
            if not source_url:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                stream_url = resolve_youtube_stream(source_url)
            except RuntimeError as error:
                logger.warning("Stream resolution failed for token %s (%s): %s", token, source_url, error)
                self.send_error(HTTPStatus.BAD_GATEWAY)
                return
            process = subprocess.Popen([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", stream_url,
                "-vn", "-f", "mp3",
                "-acodec", "libmp3lame",
                "-b:a", STREAM_BITRATE,
                "-ar", "44100",
                "-ac", "2",
                "pipe:1"
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Connection", "close")
                self.send_header("Accept-Ranges", "none")
                self.end_headers()
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                logger.debug("Sonos client closed stream connection for token %s", token)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            return
        if parsed.path.startswith("/image/"):
            key = parsed.path.split("/image/", 1)[-1].strip()
            img = get_image(key)
            if img:
                mime_type, data = img
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=2592000")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Image not found")
            return
        try:
            if parsed.path == "/health":
                self.respond(HTTPStatus.OK, {
                    "ok": True,
                    "service": "sonos-api",
                    "version": __version__,
                    "mcp": True,
                    "scheduled_reindex": REINDEX_CRON or None,
                    "timezone": REINDEX_TIMEZONE if REINDEX_CRON else None,
                })
                return
            if parsed.path == "/status":
                room = validate_room(parse_qs(parsed.query).get("room", [None])[0])
                self.respond(HTTPStatus.OK, {"ok": True, "status": get_room_status(room)})
                return
            if parsed.path == "/library/status":
                self.respond(HTTPStatus.OK, {"ok": True, "library": library_status(LIBRARY)})
                return
            self.respond(*fail("not found", HTTPStatus.NOT_FOUND))
        except (ValueError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, ET.ParseError) as error:
            logger.warning("[%s %s] Error: %s", self.command, getattr(self, "path", ""), error)
            self.respond(*fail(str(error)))
        except Exception as error:
            logger.error("[%s %s] Unhandled exception: %s", self.command, getattr(self, "path", ""), error, exc_info=True)
            self.respond(*fail(str(error), HTTPStatus.INTERNAL_SERVER_ERROR))

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/messages":
                session_id = parse_qs(parsed.query).get("session_id", [None])[0]
                try:
                    rpc_req = self.read_json()
                except Exception as e:
                    self.respond(HTTPStatus.BAD_REQUEST, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})
                    return
                rpc_res = MCP_SERVER.handle_jsonrpc(rpc_req)
                if rpc_res is not None:
                    sent = False
                    if session_id:
                        sent = MCP_SSE_MANAGER.send_message(session_id, rpc_res)
                    if not sent:
                        self.respond(HTTPStatus.OK, rpc_res)
                        return
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            data = self.read_json()
            if self.path == "/library/reindex":
                raw_room = data.get("room")
                target_room = validate_room(raw_room) if raw_room else None
                self.respond(HTTPStatus.OK, {"ok": True, "library": sync_library(target_room, LIBRARY)})
                return
            room = validate_room(data.get("room"))
            self._current_room = room
            if self.path == "/pause":
                result = run_sonos("pause", "--name", room)
                self.respond(HTTPStatus.OK, {"ok": True, "action": "pause", "room": room, "status": get_room_status(room), "result": result})
                return
            if self.path == "/stop":
                result = stop_playback(room)
                self.respond(HTTPStatus.OK, {"ok": True, "action": "stop", "room": room, "status": get_room_status(room), "result": result})
                return
            if self.path == "/next":
                result = next_track(room)
                self.respond(HTTPStatus.OK, {"ok": True, "action": "next", "room": room, "status": get_room_status(room), "result": result})
                return
            if self.path in {"/previous", "/prev"}:
                result = previous_track(room)
                self.respond(HTTPStatus.OK, {"ok": True, "action": "previous", "room": room, "status": get_room_status(room), "result": result})
                return
            if self.path == "/play":
                result = run_sonos("play", "--name", room)
                wait_for_playback_state(room, target_states={"PLAYING"}, timeout=3.0, poll_interval=0.2)
                self.respond(HTTPStatus.OK, {"ok": True, "action": "play", "room": room, "status": get_room_status(room), "result": result})
                return
            if self.path == "/volume":
                level = data.get("level")
                if not isinstance(level, int) or not 0 <= level <= 100:
                    raise ValueError("level must be an integer from 0 to 100")
                result = run_sonos("volume", "set", "--name", room, str(level))
                self.respond(HTTPStatus.OK, {"ok": True, "action": "volume", "room": room, "level": level, "status": get_room_status(room), "result": result})
                return
            if self.path == "/play-url":
                url = validate_youtube_url(data.get("url"))
                result = play_url_with_retry(room, url)
                self.respond(HTTPStatus.OK, {"ok": True, "action": "play-url", "room": room, "url": url, "status": get_room_status(room), "result": result})
                return
            if self.path in {"/resolve", "/resolve-and-play", "/search-and-play", "/resolve-list"}:
                query = validate_query(data.get("query"))
                youtube_music_url = data.get("youtube_music_url")
                provider = data.get("provider")
                bypass_cache = data.get("bypass_cache", False)
                mode = data.get("mode", "track")
                limit = data.get("limit", 10)
                artist = data.get("artist") or ""
                genre = data.get("genre")
                decade = data.get("decade")
                shuffle = data.get("shuffle", True)

                if youtube_music_url is not None and not isinstance(youtube_music_url, str):
                    raise ValueError("youtube_music_url must be a string")
                if provider is not None and not isinstance(provider, str):
                    raise ValueError("provider must be a string")
                if artist is not None and not isinstance(artist, str):
                    raise ValueError("artist must be a string")
                if genre is not None and not isinstance(genre, str):
                    raise ValueError("genre must be a string")
                if decade is not None and not isinstance(decade, int):
                    raise ValueError("decade must be an integer")
                if not isinstance(bypass_cache, bool):
                    raise ValueError("bypass_cache must be boolean")
                if not isinstance(shuffle, bool):
                    raise ValueError("shuffle must be boolean")

                if bool(data.get("reindex", False)):
                    sync_library(room, LIBRARY)

                action = "play" if self.path in {"/resolve-and-play", "/search-and-play", "/resolve-list"} else "resolve"
                payload = resolve_and_play(
                    query=query,
                    room=room,
                    library=LIBRARY,
                    mode=mode,
                    artist=artist,
                    genre=genre,
                    decade=decade,
                    shuffle=shuffle,
                    provider=provider,
                    limit=limit,
                    bypass_cache=bypass_cache,
                    youtube_music_url=youtube_music_url,
                    action=action,
                )
                self.respond(HTTPStatus.OK, payload)
                return
            self.respond(*fail("not found", HTTPStatus.NOT_FOUND))
        except (ValueError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, ET.ParseError) as error:
            self.respond(*fail(str(error)))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("==================================================")
    logger.info("Sonos API 2.0 starting...")
    logger.info("Listening on http://%s:%d", BIND_HOST, BIND_PORT)
    logger.info("Data directory: %s", DATA_DIR)
    logger.info("Stream bitrate: %s", STREAM_BITRATE)
    if REINDEX_CRON:
        logger.info("Scheduled reindex cron: '%s' (TZ: %s)", REINDEX_CRON, REINDEX_TIMEZONE)
    logger.info("==================================================")
    start_reindex_scheduler()
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Sonos API...")
        server.server_close()
