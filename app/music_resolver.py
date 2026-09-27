from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
SPANISH_CARRIER_WORDS = {"pon", "reproduce", "cancion", "canciones", "tema", "temas", "disco", "album", "musica"}
ALLOWED_MEDIA_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")


def normalize_query(value: str) -> str:
    return strip_accents(" ".join(value.casefold().split()))


def _tokens(value: str) -> set[str]:
    return set(TOKEN_RE.findall(normalize_query(value)))


def parse_sonos_didl(didl: str) -> list[dict[str, str]]:
    if not didl.strip():
        return []
    root = ET.fromstring(didl)
    namespaces = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
        "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    }
    tracks = []
    for item in root.findall("didl:item", namespaces):
        resource = item.find("didl:res", namespaces)
        uri = (resource.text or "").strip() if resource is not None else ""
        if not uri.startswith("x-file-cifs:"):
            continue
        title = (item.findtext("dc:title", default="", namespaces=namespaces) or "").strip()
        if not title:
            continue
        track_number_text = (item.findtext("upnp:originalTrackNumber", default="", namespaces=namespaces) or "").strip()
        track_number = int(track_number_text) if track_number_text.isdigit() else 0
        if not track_number:
            decoded_uri = unquote(uri)
            filename = decoded_uri.rsplit("/", 1)[-1]
            match = re.search(r"(?:^|[\s\-_])(\d{1,2})[\s.\-_]", filename)
            track_number = int(match.group(1)) if match else 0
        raw_date = (item.findtext("dc:date", default="", namespaces=namespaces) or "").strip()
        year = 0
        if raw_date:
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", raw_date)
            if year_match:
                year = int(year_match.group(1))
        if year == 0:
            uri_match = re.search(r"[\(\[\-_/](19\d{2}|20\d{2})[\)\]\-_/]", unquote(uri))
            if uri_match:
                year = int(uri_match.group(1))

        genre = (item.findtext("upnp:genre", default="", namespaces=namespaces) or "").strip()

        tracks.append({
            "uri": uri,
            "title": title,
            "artist": (item.findtext("dc:creator", default="", namespaces=namespaces) or "").strip(),
            "album": (item.findtext("upnp:album", default="", namespaces=namespaces) or "").strip(),
            "genre": genre,
            "year": year,
            "track_number": track_number,
        })
    return tracks


def parse_genre_and_decade(text: str) -> tuple[str, int | None]:
    norm = normalize_query(text)
    if not norm:
        return "", None

    decade = None
    decade_match = re.search(r"(?:anos|años|de los|decada(?: del?)?)\s*(\d{2,4})s?\b", norm)
    if not decade_match:
        decade_match = re.search(r"\b(\d{2,4})s\b", norm)
    if not decade_match:
        decade_match = re.search(r"\b(19\d{2}|20\d{2})\b", norm)

    if decade_match:
        raw_num = int(decade_match.group(1))
        decade = raw_num if raw_num >= 1000 else (1900 + raw_num if raw_num >= 30 else 2000 + raw_num)
        decade = (decade // 10) * 10
        cleaned = norm[:decade_match.start()] + " " + norm[decade_match.end():]
    else:
        cleaned = norm

    cleaned = re.sub(r"\b(?:canciones|cancion|musica|temas|tema|exitos|exito|clasicos|clasico|de|del|los|las|la|el)\b", " ", cleaned)
    genre = " ".join(cleaned.split()).strip()
    return genre, decade



@dataclass(frozen=True)
class Resolution:
    provider: str
    kind: str
    target: str
    title: str
    artist: str = ""
    album: str = ""


class _Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_tracks (
                    uri TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    genre TEXT NOT NULL DEFAULT '',
                    year INTEGER NOT NULL DEFAULT 0,
                    track_number INTEGER NOT NULL DEFAULT 0,
                    normalized TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS library_tracks_fts USING fts5(
                    title, artist, album, genre,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS query_cache (
                    query TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    expires_at INTEGER NOT NULL,
                    last_used INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS query_cache_expiry ON query_cache(expires_at);
                CREATE TABLE IF NOT EXISTS youtube_album_cache (
                    query TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    tracks_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_used INTEGER NOT NULL,
                    PRIMARY KEY(query, artist)
                );
                CREATE INDEX IF NOT EXISTS youtube_album_cache_expiry ON youtube_album_cache(expires_at);
                CREATE TABLE IF NOT EXISTS image_cache (
                    key TEXT PRIMARY KEY,
                    mime_type TEXT NOT NULL,
                    data BLOB NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(library_tracks)")}
            if "track_number" not in columns:
                connection.execute("ALTER TABLE library_tracks ADD COLUMN track_number INTEGER NOT NULL DEFAULT 0")
            if "genre" not in columns:
                connection.execute("ALTER TABLE library_tracks ADD COLUMN genre TEXT NOT NULL DEFAULT ''")
            if "year" not in columns:
                connection.execute("ALTER TABLE library_tracks ADD COLUMN year INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS library_tracks_album_artist_track ON library_tracks(album COLLATE NOCASE, artist COLLATE NOCASE, track_number)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS library_tracks_genre ON library_tracks(genre COLLATE NOCASE)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS library_tracks_year ON library_tracks(year)"
            )
            fts_columns = {row[1] for row in connection.execute("PRAGMA table_info(library_tracks_fts)")}
            if "genre" not in fts_columns:
                connection.execute("DROP TABLE IF EXISTS library_tracks_fts")
                connection.execute(
                    """CREATE VIRTUAL TABLE library_tracks_fts USING fts5(
                        title, artist, album, genre,
                        tokenize='unicode61 remove_diacritics 2'
                    )"""
                )
            library_count = int(connection.execute("SELECT COUNT(*) FROM library_tracks").fetchone()[0])
            fts_count = int(connection.execute("SELECT COUNT(*) FROM library_tracks_fts").fetchone()[0])
            if library_count != fts_count:
                connection.execute("DELETE FROM library_tracks_fts")
                connection.execute(
                    "INSERT INTO library_tracks_fts(rowid, title, artist, album, genre) SELECT rowid, title, artist, album, genre FROM library_tracks"
                )
        connection.close()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class LibraryIndex:
    def __init__(self, path: str | Path):
        self.database = _Database(path)
        self._cached_artists: list[str] | None = None
        self._cached_albums: list[tuple[str, str]] | None = None
        self._cached_titles: list[str] | None = None

    def _invalidate_cache(self) -> None:
        self._cached_artists = None
        self._cached_albums = None
        self._cached_titles = None

    def _get_artists(self) -> list[str]:
        if self._cached_artists is None:
            with self.database.connect() as connection:
                rows = connection.execute("SELECT DISTINCT artist FROM library_tracks WHERE artist != ''").fetchall()
                self._cached_artists = [str(r["artist"]).strip() for r in rows if str(r["artist"]).strip()]
        return self._cached_artists

    def _get_albums(self) -> list[tuple[str, str]]:
        if self._cached_albums is None:
            with self.database.connect() as connection:
                rows = connection.execute("SELECT DISTINCT album, artist FROM library_tracks WHERE album != ''").fetchall()
                self._cached_albums = [
                    (str(r["album"]).strip(), str(r["artist"]).strip())
                    for r in rows if str(r["album"]).strip()
                ]
        return self._cached_albums

    def _get_titles(self) -> list[str]:
        if self._cached_titles is None:
            with self.database.connect() as connection:
                rows = connection.execute("SELECT DISTINCT title FROM library_tracks WHERE title != ''").fetchall()
                self._cached_titles = [str(r["title"]).strip() for r in rows if str(r["title"]).strip()]
        return self._cached_titles

    def fuzzy_match_artist(self, query: str, cutoff: float = 0.72) -> str | None:
        norm_query = normalize_query(query)
        if not norm_query:
            return None
        artists = self._get_artists()
        if not artists:
            return None
        norm_map = {normalize_query(a): a for a in artists}
        matches = difflib.get_close_matches(norm_query, norm_map.keys(), n=1, cutoff=cutoff)
        if matches:
            return norm_map[matches[0]]
        return None

    def fuzzy_match_album(self, album_query: str, artist_query: str | None = None, cutoff: float = 0.72) -> tuple[str, str] | None:
        norm_album = normalize_query(album_query)
        if not norm_album:
            return None
        albums = self._get_albums()
        if not albums:
            return None

        target_artist = None
        if artist_query:
            target_artist = self.fuzzy_match_artist(artist_query, cutoff=cutoff) or artist_query

        if target_artist:
            norm_target_artist = normalize_query(target_artist)
            filtered = [
                (alb, art) for alb, art in albums
                if norm_target_artist == normalize_query(art) or norm_target_artist in normalize_query(art)
            ]
            if filtered:
                albums = filtered

        norm_map = {normalize_query(alb): (alb, art) for alb, art in albums}
        matches = difflib.get_close_matches(norm_album, norm_map.keys(), n=1, cutoff=cutoff)
        if matches:
            return norm_map[matches[0]]
        return None

    def replace_tracks(self, tracks: list[dict[str, str]]) -> None:
        self._invalidate_cache()
        rows = []
        seen_uris: set[str] = set()
        for track in tracks:
            uri = str(track.get("uri", "")).strip()
            title = str(track.get("title", "")).strip()
            if not uri or not title or uri in seen_uris:
                continue
            seen_uris.add(uri)
            artist = str(track.get("artist", "")).strip()
            album = str(track.get("album", "")).strip()
            genre = str(track.get("genre", "")).strip()
            raw_track_number = track.get("track_number", 0)
            track_number = raw_track_number if isinstance(raw_track_number, int) and raw_track_number > 0 else 0
            raw_year = track.get("year", 0)
            year = raw_year if isinstance(raw_year, int) and 1900 <= raw_year <= 2100 else 0
            rows.append((uri, title, artist, album, genre, year, track_number, normalize_query(f"{title} {artist} {album} {genre} {year if year else ''}")))
        with self.database.connect() as connection:
            connection.execute("DELETE FROM library_tracks_fts")
            connection.execute("DELETE FROM library_tracks")
            connection.executemany(
                "INSERT INTO library_tracks(uri, title, artist, album, genre, year, track_number, normalized) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            connection.execute(
                "INSERT INTO library_tracks_fts(rowid, title, artist, album, genre) SELECT rowid, title, artist, album, genre FROM library_tracks"
            )

    def search_many(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        tokens = TOKEN_RE.findall(normalize_query(query))
        if not tokens:
            return []
        filtered_tokens = [t for t in tokens if t not in SPANISH_CARRIER_WORDS]
        if filtered_tokens:
            tokens = filtered_tokens
        match_query = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        wanted = set(tokens)
        candidate_limit = max(limit * 8, 100)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT library_tracks.uri, library_tracks.title, library_tracks.artist, library_tracks.album, library_tracks.genre, library_tracks.year
                FROM library_tracks_fts
                JOIN library_tracks ON library_tracks.rowid = library_tracks_fts.rowid
                WHERE library_tracks_fts MATCH ?
                ORDER BY bm25(library_tracks_fts, 3.0, 2.0, 1.0, 1.5)
                LIMIT ?
                """,
                (match_query, candidate_limit),
            ).fetchall()
        ranked: list[tuple[int, sqlite3.Row]] = []
        normalized_query = normalize_query(query)
        for row in rows:
            score = (
                len(wanted & _tokens(row["title"])) * 3
                + len(wanted & _tokens(row["artist"])) * 2
                + len(wanted & _tokens(row["album"]))
                + len(wanted & _tokens(row["genre"])) * 2
            )
            if normalized_query in normalize_query(row["title"]):
                score += 10
            ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], item[1]["title"], item[1]["artist"]))
        results = [dict(row) for _, row in ranked[:limit]]
        if results:
            return results

        # --- Typo tolerance / Fuzzy rescue ---
        raw_tokens = [t for t in tokens if t not in SPANISH_CARRIER_WORDS]
        if raw_tokens:
            artists = self._get_artists()
            if artists:
                norm_art_map = {normalize_query(a): a for a in artists}
                corrected_query = query
                for length in range(min(4, len(raw_tokens)), 0, -1):
                    for i in range(len(raw_tokens) - length + 1):
                        window = " ".join(raw_tokens[i : i + length])
                        if length == 1 and len(window) < 4:
                            continue
                        norm_w = normalize_query(window)
                        cutoff = 0.78 if length == 1 else 0.72
                        matches = difflib.get_close_matches(norm_w, norm_art_map.keys(), n=1, cutoff=cutoff)
                        if matches:
                            matched_artist = norm_art_map[matches[0]]
                            if normalize_query(matched_artist) != norm_w:
                                corrected_query = re.sub(re.escape(window), matched_artist, query, flags=re.IGNORECASE)
                                break
                    if corrected_query != query:
                        break
                if corrected_query != query:
                    fuzzy_results = self.search_many(corrected_query, limit=limit)
                    if fuzzy_results:
                        return fuzzy_results

        titles = self._get_titles()
        if titles:
            norm_title_map = {normalize_query(t): t for t in titles}
            title_matches = difflib.get_close_matches(normalized_query, norm_title_map.keys(), n=limit, cutoff=0.68)
            if title_matches:
                matched_titles = [norm_title_map[tm] for tm in title_matches]
                placeholders = ", ".join("?" for _ in matched_titles)
                with self.database.connect() as connection:
                    fuzzy_rows = connection.execute(
                        f"""
                        SELECT uri, title, artist, album
                        FROM library_tracks
                        WHERE title IN ({placeholders})
                        LIMIT ?
                        """,
                        [*matched_titles, limit],
                    ).fetchall()
                if fuzzy_rows:
                    return [dict(r) for r in fuzzy_rows]

        return []

    def search_album(self, album: str, artist: str | None = None) -> list[dict[str, str]]:
        album_name = album.strip()
        artist_name = artist.strip() if artist else ""
        if not album_name:
            return []

        disc_ordering = """
            CASE
                WHEN instr(lower(uri), '/cd%20') > 0 THEN substr(uri, instr(lower(uri), '/cd%20'), 8)
                WHEN instr(lower(uri), '/cd') > 0 THEN substr(uri, instr(lower(uri), '/cd'), 6)
                WHEN instr(lower(uri), '/disc%20') > 0 THEN substr(uri, instr(lower(uri), '/disc%20'), 10)
                WHEN instr(lower(uri), '/disc') > 0 THEN substr(uri, instr(lower(uri), '/disc'), 8)
                WHEN instr(lower(uri), '/disco%20') > 0 THEN substr(uri, instr(lower(uri), '/disco%20'), 11)
                WHEN instr(lower(uri), '/disco') > 0 THEN substr(uri, instr(lower(uri), '/disco'), 9)
                ELSE ''
            END
        """

        def fetch_by_exact_names(a_name: str, art_name: str | None) -> list[dict[str, str]]:
            where = "album = ? COLLATE NOCASE"
            parameters: list[object] = [a_name]
            if art_name:
                where += " AND artist = ? COLLATE NOCASE"
                parameters.append(art_name)
            with self.database.connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT uri, title, artist, album, track_number
                    FROM library_tracks
                    WHERE {where}
                    ORDER BY
                        {disc_ordering},
                        CASE WHEN track_number > 0 THEN 0 ELSE 1 END,
                        track_number,
                        title,
                        uri
                    """,
                    parameters,
                ).fetchall()
            return [dict(row) for row in rows]

        # 1. Direct exact match
        direct = fetch_by_exact_names(album_name, artist_name or None)
        if direct:
            return direct

        # 2. Tolerant matching across distinct album/artist candidates
        norm_album = normalize_query(album_name)
        clean_album = re.sub(
            r"\s*[\(\[][^\)\]]*(?:remaster|deluxe|edition|version|live|vivo|anniversary)[^\)\]]*[\)\]]",
            "",
            norm_album,
            flags=re.IGNORECASE,
        ).strip()
        norm_artist = normalize_query(artist_name) if artist_name else ""

        with self.database.connect() as connection:
            candidates = connection.execute("SELECT DISTINCT album, artist FROM library_tracks").fetchall()

        best_candidate = None
        best_score = 0
        for cand in candidates:
            c_album = str(cand["album"] or "").strip()
            c_artist = str(cand["artist"] or "").strip()
            if not c_album:
                continue
            c_norm_album = normalize_query(c_album)
            c_clean_album = re.sub(
                r"\s*[\(\[][^\)\]]*(?:remaster|deluxe|edition|version|live|vivo|anniversary)[^\)\]]*[\)\]]",
                "",
                c_norm_album,
                flags=re.IGNORECASE,
            ).strip()
            c_norm_artist = normalize_query(c_artist)

            score = 0
            if norm_album == c_norm_album:
                score += 100
            elif clean_album and clean_album == c_clean_album:
                score += 90
            elif clean_album and (clean_album in c_norm_album or c_clean_album in norm_album):
                score += 65
            elif norm_album.removeprefix("the ").strip() == c_norm_album.removeprefix("the ").strip():
                score += 85
            elif norm_album.removeprefix("el ").strip() == c_norm_album.removeprefix("el ").strip():
                score += 85
            elif norm_album.removeprefix("la ").strip() == c_norm_album.removeprefix("la ").strip():
                score += 85
            elif norm_album.removeprefix("los ").strip() == c_norm_album.removeprefix("los ").strip():
                score += 85
            else:
                album_ratio = difflib.SequenceMatcher(None, clean_album or norm_album, c_clean_album or c_norm_album).ratio()
                if album_ratio >= 0.72:
                    score += int(album_ratio * 80)

            if score > 0 and norm_artist:
                if norm_artist == c_norm_artist:
                    score += 50
                elif norm_artist in c_norm_artist or c_norm_artist in norm_artist:
                    score += 30
                else:
                    artist_ratio = difflib.SequenceMatcher(None, norm_artist, c_norm_artist).ratio()
                    if artist_ratio >= 0.72:
                        score += int(artist_ratio * 40)
                    else:
                        continue

            if score > best_score:
                best_score = score
                best_candidate = (c_album, c_artist)

        if best_candidate and best_score >= 50:
            return fetch_by_exact_names(best_candidate[0], best_candidate[1] if artist_name else None)

        return []

    def search_artist(self, artist: str, limit: int = 50, shuffle: bool = True) -> list[dict[str, str]]:
        artist_name = artist.strip()
        if not artist_name:
            return []
        norm_artist = normalize_query(artist_name)

        with self.database.connect() as connection:
            rows = connection.execute("SELECT DISTINCT artist FROM library_tracks").fetchall()

        matching_artists = []
        for row in rows:
            raw_a = str(row["artist"] or "").strip()
            if not raw_a:
                continue
            norm_a = normalize_query(raw_a)
            if norm_a == norm_artist:
                matching_artists.append((raw_a, 100))
            elif norm_artist in norm_a or norm_a in norm_artist:
                matching_artists.append((raw_a, 50))

        matching_artists.sort(key=lambda x: -x[1])
        if not matching_artists:
            fuzzy_artist = self.fuzzy_match_artist(artist_name, cutoff=0.72)
            if fuzzy_artist:
                matching_artists.append((fuzzy_artist, 90))
            else:
                return self.search_many(artist, limit=limit)

        top_score = matching_artists[0][1]
        target_artists = [a for a, s in matching_artists if s == top_score]
        placeholders = ", ".join("?" for _ in target_artists)
        order_clause = "RANDOM()" if shuffle else "album COLLATE NOCASE, track_number, title"

        with self.database.connect() as connection:
            tracks = connection.execute(
                f"""
                SELECT uri, title, artist, album, track_number
                FROM library_tracks
                WHERE artist IN ({placeholders})
                ORDER BY {order_clause}
                LIMIT ?
                """,
                [*target_artists, limit],
            ).fetchall()
        return [dict(t) for t in tracks]

    def search_genre(self, genre: str | None = None, decade: int | None = None, limit: int = 50, shuffle: bool = True) -> list[dict[str, Any]]:
        genre_name = (genre or "").strip()
        if not genre_name and decade is None:
            return []

        where_clauses = []
        params: list[Any] = []

        if genre_name:
            norm_genre = normalize_query(genre_name)
            with self.database.connect() as connection:
                genre_rows = connection.execute("SELECT DISTINCT genre FROM library_tracks WHERE genre != ''").fetchall()
            all_genres = [str(r["genre"]).strip() for r in genre_rows if str(r["genre"]).strip()]

            matched_genres = [g for g in all_genres if norm_genre in normalize_query(g) or normalize_query(g) in norm_genre]
            if not matched_genres and all_genres:
                close = difflib.get_close_matches(norm_genre, [normalize_query(g) for g in all_genres], n=3, cutoff=0.68)
                norm_to_orig = {normalize_query(g): g for g in all_genres}
                matched_genres = [norm_to_orig[c] for c in close if c in norm_to_orig]

            if matched_genres:
                placeholders = ", ".join("?" for _ in matched_genres)
                where_clauses.append(f"genre IN ({placeholders})")
                params.extend(matched_genres)
            else:
                where_clauses.append("genre LIKE ? COLLATE NOCASE")
                params.append(f"%{genre_name}%")

        if decade is not None:
            start_year = decade
            if 0 <= decade <= 99:
                start_year = 1900 + decade if decade >= 30 else 2000 + decade
            end_year = start_year + 9
            where_clauses.append("year >= ? AND year <= ?")
            params.extend([start_year, end_year])

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        order_sql = "ORDER BY RANDOM()" if shuffle else "ORDER BY artist COLLATE NOCASE, year, album COLLATE NOCASE, track_number"

        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT uri, title, artist, album, genre, year, track_number
                FROM library_tracks
                WHERE {where_sql}
                {order_sql}
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        with self.database.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM library_tracks").fetchone()[0])

    def search(self, query: str) -> dict[str, str] | None:
        matches = self.search_many(query, limit=1)
        if matches:
            return matches[0]
        p_genre, p_decade = parse_genre_and_decade(query)
        if p_genre or p_decade:
            genre_matches = self.search_genre(genre=p_genre, decade=p_decade, limit=1, shuffle=True)
            if genre_matches:
                return genre_matches[0]
        return None


class CacheStore:
    def __init__(self, path: str | Path):
        self.database = _Database(path)

    def get(self, query: str, now: int | None = None, provider: str | None = None) -> Resolution | None:
        now = int(time.time()) if now is None else now
        key = normalize_query(query)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM query_cache WHERE expires_at <= ?", (now,))
            if provider is None:
                row = connection.execute("SELECT * FROM query_cache WHERE query = ?", (key,)).fetchone()
            else:
                row = connection.execute("SELECT * FROM query_cache WHERE query = ? AND provider = ?", (key, provider)).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE query_cache SET last_used = ? WHERE query = ?", (now, key))
        return Resolution(row["provider"], row["kind"], row["target"], row["title"], row["artist"], row["album"])

    def put(self, query: str, resolution: Resolution, ttl_seconds: int, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        key = normalize_query(query)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO query_cache(query, provider, kind, target, title, artist, album, expires_at, last_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    provider=excluded.provider, kind=excluded.kind, target=excluded.target,
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    expires_at=excluded.expires_at, last_used=excluded.last_used
                """,
                (key, resolution.provider, resolution.kind, resolution.target, resolution.title, resolution.artist, resolution.album, now + ttl_seconds, now),
            )

    def get_album(self, query: str, artist: str, now: int | None = None) -> list[dict[str, str]] | None:
        now = int(time.time()) if now is None else now
        key = normalize_query(query)
        artist_key = normalize_query(artist)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM youtube_album_cache WHERE expires_at <= ?", (now,))
            row = connection.execute(
                "SELECT tracks_json FROM youtube_album_cache WHERE query = ? AND artist = ?", (key, artist_key)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE youtube_album_cache SET last_used = ? WHERE query = ? AND artist = ?", (now, key, artist_key)
            )
        try:
            tracks = json.loads(row["tracks_json"])
        except json.JSONDecodeError:
            return None
        return tracks if isinstance(tracks, list) and all(isinstance(track, dict) for track in tracks) else None

    def put_album(
        self, query: str, artist: str, tracks: list[dict[str, str]], ttl_seconds: int, now: int | None = None
    ) -> None:
        now = int(time.time()) if now is None else now
        key = normalize_query(query)
        artist_key = normalize_query(artist)
        payload = json.dumps(tracks, ensure_ascii=False, separators=(",", ":"))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO youtube_album_cache(query, artist, tracks_json, expires_at, last_used)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(query, artist) DO UPDATE SET
                    tracks_json=excluded.tracks_json, expires_at=excluded.expires_at, last_used=excluded.last_used
                """,
                (key, artist_key, payload, now + ttl_seconds, now),
            )

    def get_image(self, key: str) -> tuple[str, bytes] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT mime_type, data FROM image_cache WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return row["mime_type"], bytes(row["data"])
        return None

    def put_image(self, key: str, mime_type: str, data: bytes) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO image_cache(key, mime_type, data, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    mime_type=excluded.mime_type,
                    data=excluded.data,
                    created_at=excluded.created_at
                """,
                (key, mime_type, data, int(time.time())),
            )


class Resolver:
    def __init__(self, cache: CacheStore, library: LibraryIndex, youtube_search):
        self.cache = cache
        self.library = library
        self.youtube_search = youtube_search

    def resolve(
        self,
        query: str,
        youtube_music_url: str | None = None,
        provider: str | None = None,
        bypass_cache: bool = False,
        now: int | None = None,
    ) -> Resolution:
        normalized = normalize_query(query)
        if not normalized:
            raise ValueError("query is required")
        cache_provider = provider if provider in {"samba", "youtube", "youtube_music"} else None
        cached = None if bypass_cache else self.cache.get(normalized, now=now, provider=cache_provider)
        if cached is not None:
            return cached
        if provider == "youtube":
            resolution = self.youtube_search(normalized)
            self.cache.put(normalized, resolution, ttl_seconds=7 * 24 * 60 * 60, now=now)
            return resolution
        if provider not in {None, "samba", "youtube_music"}:
            raise ValueError("provider must be samba, youtube, or youtube_music")
        local = None if provider == "youtube_music" else self.library.search(normalized)
        if local is not None:
            resolution = Resolution("samba", "uri", local["uri"], local["title"], local["artist"], local["album"])
            self.cache.put(normalized, resolution, ttl_seconds=90 * 24 * 60 * 60, now=now)
            return resolution
        if youtube_music_url:
            parsed = urlparse(youtube_music_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or host not in ALLOWED_MEDIA_HOSTS:
                raise ValueError("youtube_music_url must be an HTTPS YouTube or YouTube Music URL")
            resolution = Resolution("youtube_music", "url", youtube_music_url, normalized)
            self.cache.put(normalized, resolution, ttl_seconds=14 * 24 * 60 * 60, now=now)
            return resolution
        resolution = self.youtube_search(normalized)
        self.cache.put(normalized, resolution, ttl_seconds=7 * 24 * 60 * 60, now=now)
        return resolution
