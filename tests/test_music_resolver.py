import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from music_resolver import (
    CacheStore,
    LibraryIndex,
    Resolution,
    is_live_track,
    normalize_query,
    parse_live_intent,
    parse_sonos_didl,
    strip_accents,
)


class MusicResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music.sqlite"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normalize_query_collapses_spacing_and_case(self):
        self.assertEqual(normalize_query("  Bohemian   RHAPSODY  "), "bohemian rhapsody")

    def test_strip_accents_removes_spanish_diacritics(self):
        self.assertEqual(strip_accents("canción Álbum corazón"), "cancion Album corazon")
        self.assertEqual(normalize_query("  Canción   DEL   Corazón  "), "cancion del corazon")

    def test_parse_sonos_didl_preserves_escaped_ampersand(self):
        didl = '''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="S://server/music/a.mp3"><res>x-file-cifs://server/music/a.mp3</res><dc:title>MYTH &amp; ROID - A beginning</dc:title><dc:creator>MYTH &amp; ROID</dc:creator><upnp:album>eYe's</upnp:album><upnp:originalTrackNumber>7</upnp:originalTrackNumber></item></DIDL-Lite>'''

        tracks = parse_sonos_didl(didl)

        self.assertEqual(tracks[0]["title"], "MYTH & ROID - A beginning")
        self.assertEqual(tracks[0]["artist"], "MYTH & ROID")
        self.assertEqual(tracks[0]["track_number"], 7)

    def test_parse_sonos_didl_derives_track_number_from_uri_when_missing(self):
        didl = '''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="S://server/music/a.flac"><res>x-file-cifs://server/music/Rammstein/Mutter%20(2001)/CD%2001/Rammstein%20-%20Mutter%20-%2007%20-%20Spieluhr.flac</res><dc:title>Spieluhr</dc:title><dc:creator>Rammstein</dc:creator><upnp:album>Mutter</upnp:album></item></DIDL-Lite>'''

        tracks = parse_sonos_didl(didl)

        self.assertEqual(tracks[0]["track_number"], 7)

    def test_parse_sonos_didl_derives_track_number_from_various_filename_formats(self):
        didl_1 = '''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="1"><res>x-file-cifs://server/music/03%20-%20Track.mp3</res><dc:title>Track</dc:title></item></DIDL-Lite>'''
        didl_2 = '''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="2"><res>x-file-cifs://server/music/04.%20Track.flac</res><dc:title>Track</dc:title></item></DIDL-Lite>'''
        didl_3 = '''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="3"><res>x-file-cifs://server/music/05%20Track.m4a</res><dc:title>Track</dc:title></item></DIDL-Lite>'''

        self.assertEqual(parse_sonos_didl(didl_1)[0]["track_number"], 3)
        self.assertEqual(parse_sonos_didl(didl_2)[0]["track_number"], 4)
        self.assertEqual(parse_sonos_didl(didl_3)[0]["track_number"], 5)

    def test_library_index_deduplicates_repeated_sonos_uris(self):
        index = LibraryIndex(self.db_path)
        track = {"uri": "x-file-cifs://server/music/repeated.mp3", "title": "Repeated", "artist": "Artist", "album": "Album"}

        index.replace_tracks([track, track])

        self.assertEqual(index.count(), 1)

    def test_library_search_prioritizes_title_and_artist(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/queen-live.mp3", "title": "Bohemian Rhapsody (Live)", "artist": "Queen", "album": "Live at Wembley"},
                {"uri": "x-file-cifs://server/music/queen-studio.mp3", "title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"},
            ]
        )

        match = index.search("Bohemian Rhapsody Queen")

        self.assertTrue(match["uri"].endswith("queen-studio.mp3"))
        self.assertEqual(match["title"], "Bohemian Rhapsody")

    def test_library_search_ignores_spanish_carrier_words(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/tren.mp3", "title": "Tren al Sur", "artist": "Los Prisioneros", "album": "Corazones"},
            ]
        )

        match = index.search("cancion tren al sur")
        self.assertIsNotNone(match)
        self.assertEqual(match["title"], "Tren al Sur")

        match_disco = index.search("disco corazones")
        self.assertIsNotNone(match_disco)
        self.assertEqual(match_disco["album"], "Corazones")

    def test_library_search_matches_with_and_without_accents(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/corazon.mp3", "title": "Corazón Espinado", "artist": "Santana", "album": "Supernatural"},
            ]
        )

        # Search without accent matches title with accent
        match1 = index.search("corazon espinado")
        self.assertIsNotNone(match1)
        self.assertEqual(match1["title"], "Corazón Espinado")

        # Search with accent matches
        match2 = index.search("corazón espinado")
        self.assertIsNotNone(match2)
        self.assertEqual(match2["title"], "Corazón Espinado")

    def test_library_search_matches_album_metadata(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/mutter-01.flac", "title": "Mein Herz brennt", "artist": "Rammstein", "album": "Mutter"},
                {"uri": "x-file-cifs://server/music/mutter-02.flac", "title": "Links 2 3 4", "artist": "Rammstein", "album": "Mutter"},
            ]
        )

        matches = index.search_many("Mutter", limit=10)

        self.assertEqual({track["title"] for track in matches}, {"Links 2 3 4", "Mein Herz brennt"})

    def test_library_album_search_orders_by_track_number(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/Mutter/CD%2001/mutter-03.flac", "title": "Sonne", "artist": "Rammstein", "album": "Mutter", "track_number": 3},
                {"uri": "x-file-cifs://server/music/Mutter/CD%2001/mutter-01.flac", "title": "Mein Herz brennt", "artist": "Rammstein", "album": "Mutter", "track_number": 1},
                {"uri": "x-file-cifs://server/music/Mutter/CD%2001/mutter-02.flac", "title": "Links 2 3 4", "artist": "Rammstein", "album": "Mutter", "track_number": 2},
                {"uri": "x-file-cifs://server/music/Mutter/CD%2002/bonus-01.flac", "title": "Ich will (Live)", "artist": "Rammstein", "album": "Mutter", "track_number": 1},
            ]
        )

        tracks = index.search_album("Mutter", artist="Rammstein")

        self.assertEqual([track["title"] for track in tracks], ["Mein Herz brennt", "Links 2 3 4", "Sonne", "Ich will (Live)"])

    def test_library_album_search_flexible_matches(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/dark-01.mp3", "title": "Speak to Me", "artist": "Pink Floyd", "album": "The Dark Side of the Moon", "track_number": 1},
                {"uri": "x-file-cifs://server/music/dark-02.mp3", "title": "Breathe", "artist": "Pink Floyd", "album": "The Dark Side of the Moon", "track_number": 2},
                {"uri": "x-file-cifs://server/music/queen-01.mp3", "title": "Under Pressure", "artist": "Queen & David Bowie", "album": "Hot Space (Deluxe Edition)", "track_number": 1},
            ]
        )

        # Matches album omitting leading 'The'
        tracks1 = index.search_album("Dark Side of the Moon")
        self.assertEqual(len(tracks1), 2)
        self.assertEqual(tracks1[0]["album"], "The Dark Side of the Moon")

        # Matches album omitting suffix '(Deluxe Edition)' and partial artist
        tracks2 = index.search_album("Hot Space", artist="Queen")
        self.assertEqual(len(tracks2), 1)
        self.assertEqual(tracks2[0]["album"], "Hot Space (Deluxe Edition)")

    def test_library_album_search_multidisc_support(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/Album/Disc%202/01.flac", "title": "D2 Track 1", "artist": "Artist", "album": "Double", "track_number": 1},
                {"uri": "x-file-cifs://server/music/Album/Disc%201/01.flac", "title": "D1 Track 1", "artist": "Artist", "album": "Double", "track_number": 1},
                {"uri": "x-file-cifs://server/music/Album/Disc%201/02.flac", "title": "D1 Track 2", "artist": "Artist", "album": "Double", "track_number": 2},
            ]
        )

        tracks = index.search_album("Double")
        self.assertEqual([t["title"] for t in tracks], ["D1 Track 1", "D1 Track 2", "D2 Track 1"])

    def test_cache_returns_unexpired_resolution_and_removes_expired_rows(self):
        cache = CacheStore(self.db_path)
        fresh = Resolution(provider="samba", kind="uri", target="x-file-cifs://server/a.mp3", title="A")
        expired = Resolution(provider="youtube", kind="url", target="https://www.youtube.com/watch?v=expired", title="Old")
        cache.put("Fresh Song", fresh, ttl_seconds=60, now=1_000)
        cache.put("Old Song", expired, ttl_seconds=1, now=1_000)

        self.assertEqual(cache.get("fresh song", now=1_010), fresh)
        self.assertIsNone(cache.get("old song", now=1_010))
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0], 1)

    def test_resolver_uses_samba_before_youtube_music_url_and_external_youtube(self):
        from music_resolver import Resolver

        library = LibraryIndex(self.db_path)
        library.replace_tracks([{"uri": "x-file-cifs://server/music/local.mp3", "title": "Local Song", "artist": "Artist", "album": "Album"}])
        resolver = Resolver(CacheStore(self.db_path), library, lambda query: Resolution("youtube", "url", "https://www.youtube.com/watch?v=fallback", query))

        local = resolver.resolve("Local Song", youtube_music_url="https://music.youtube.com/watch?v=other", now=1_000)
        remote = resolver.resolve("Not Local", youtube_music_url="https://music.youtube.com/watch?v=other", now=1_000)
        fallback = resolver.resolve("No URL", now=1_000)

        self.assertEqual(local.provider, "samba")
        self.assertEqual(remote.provider, "youtube_music")
        self.assertEqual(fallback.provider, "youtube")

    def test_resolver_accepts_youtube_and_youtube_music_urls(self):
        from music_resolver import Resolver

        resolver = Resolver(CacheStore(self.db_path), LibraryIndex(self.db_path), lambda query: Resolution("youtube", "url", "https://www.youtube.com/watch?v=fallback", query))

        res_yt = resolver.resolve("Missing", youtube_music_url="https://www.youtube.com/watch?v=abc1234", now=1_000)
        self.assertEqual(res_yt.provider, "youtube_music")
        self.assertEqual(res_yt.target, "https://www.youtube.com/watch?v=abc1234")

        res_short = resolver.resolve("Missing2", youtube_music_url="https://youtu.be/abc1234", now=1_000)
        self.assertEqual(res_short.provider, "youtube_music")

    def test_resolver_rejects_non_youtube_urls(self):
        from music_resolver import Resolver

        resolver = Resolver(CacheStore(self.db_path), LibraryIndex(self.db_path), lambda query: Resolution("youtube", "url", "https://www.youtube.com/watch?v=fallback", query))

        with self.assertRaisesRegex(ValueError, "YouTube"):
            resolver.resolve("Missing", youtube_music_url="https://example.com/track", now=1_000)

    def test_library_artist_search_finds_tracks_and_shuffles(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/queen-1.mp3", "title": "Radio Ga Ga", "artist": "Queen", "album": "The Works"},
                {"uri": "x-file-cifs://server/music/queen-2.mp3", "title": "Hammer to Fall", "artist": "Queen", "album": "The Works"},
                {"uri": "x-file-cifs://server/music/queen-3.mp3", "title": "I Want to Break Free", "artist": "Queen", "album": "The Works"},
                {"uri": "x-file-cifs://server/music/soda-1.mp3", "title": "Persiana Americana", "artist": "Soda Stereo", "album": "Signos"},
            ]
        )

        tracks = index.search_artist("Queen", limit=10, shuffle=False)
        self.assertEqual(len(tracks), 3)
        self.assertTrue(all(t["artist"] == "Queen" for t in tracks))

        limited = index.search_artist("Queen", limit=2, shuffle=False)
        self.assertEqual(len(limited), 2)

        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/cerati-1.mp3", "title": "Crimen", "artist": "Gustavo Cerati", "album": "Ahí Vamos"},
            ]
        )
        cerati = index.search_artist("gustavo cerati", limit=5)
        self.assertEqual(len(cerati), 1)
        self.assertEqual(cerati[0]["title"], "Crimen")

    def test_library_search_with_artist_filter(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/sanz.mp3", "title": "Silencio", "artist": "Alejandro Sanz", "album": "No Es Lo Mismo"},
                {"uri": "x-file-cifs://server/music/u2-one.mp3", "title": "One", "artist": "U2", "album": "Achtung Baby"},
                {"uri": "x-file-cifs://server/music/metallica-one.mp3", "title": "One", "artist": "Metallica", "album": "...And Justice for All"},
            ]
        )

        # Matching artist
        sanz = index.search("Silencio", artist="Alejandro Sanz")
        self.assertIsNotNone(sanz)
        self.assertEqual(sanz["artist"], "Alejandro Sanz")

        # Non-matching artist must return None
        u2_silencio = index.search("Silencio", artist="U2")
        self.assertIsNone(u2_silencio)

        # Disambiguates identical titles by artist
        u2_one = index.search("One", artist="U2")
        self.assertIsNotNone(u2_one)
        self.assertEqual(u2_one["artist"], "U2")

        met_one = index.search("One", artist="Metallica")
        self.assertIsNotNone(met_one)
        self.assertEqual(met_one["artist"], "Metallica")

    def test_resolver_with_artist_filter_and_fallback(self):
        from music_resolver import Resolver

        library = LibraryIndex(self.db_path)
        library.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/sanz.mp3", "title": "Silencio", "artist": "Alejandro Sanz", "album": "Album"},
            ]
        )
        called_yt = []
        def mock_yt(q):
            called_yt.append(q)
            return Resolution("youtube", "url", f"https://www.youtube.com/watch?v=mock_{q}", q, artist="U2")

        resolver = Resolver(CacheStore(self.db_path), library, mock_yt)

        # Auto mode with non-matching Samba artist should NOT return Samba, must fallback to YouTube with artist
        res = resolver.resolve("Silencio", artist="U2", now=1_000)
        self.assertEqual(res.provider, "youtube")
        self.assertIn("Silencio U2", called_yt)

        # Provider="samba" with non-matching artist must raise RuntimeError
        with self.assertRaisesRegex(RuntimeError, "No local track found for: Silencio by U2"):
            resolver.resolve("Silencio", artist="U2", provider="samba", bypass_cache=True, now=1_000)

        # Matching artist in Samba returns Samba resolution
        samba_res = resolver.resolve("Silencio", artist="Alejandro Sanz", now=1_000)
        self.assertEqual(samba_res.provider, "samba")
        self.assertEqual(samba_res.artist, "Alejandro Sanz")

    def test_parse_live_intent(self):
        cleaned, live = parse_live_intent("paint it black de estudio")
        self.assertEqual(cleaned, "paint it black")
        self.assertIs(live, False)

        cleaned, live = parse_live_intent("paint it black version estudio")
        self.assertEqual(cleaned, "paint it black")
        self.assertIs(live, False)

        cleaned, live = parse_live_intent("hotel california en vivo")
        self.assertEqual(cleaned, "hotel california")
        self.assertIs(live, True)

        cleaned, live = parse_live_intent("hotel california live")
        self.assertEqual(cleaned, "hotel california")
        self.assertIs(live, True)

        cleaned, live = parse_live_intent("paint it black")
        self.assertEqual(cleaned, "paint it black")
        self.assertIsNone(live)

    def test_is_live_track(self):
        self.assertTrue(is_live_track("Paint It, Black (Live)", "Flashpoint"))
        self.assertTrue(is_live_track("Paint It, Black", "Flashpoint"))
        self.assertTrue(is_live_track("Paint It, Black", "Live at Leeds"))
        self.assertTrue(is_live_track("Comfortably Numb (Live at Pompeii)", "The Wall"))
        self.assertFalse(is_live_track("Paint It, Black", "Aftermath"))
        self.assertFalse(is_live_track("Comfortably Numb", "The Wall"))

    def test_library_search_with_album_and_live_filter(self):
        index = LibraryIndex(self.db_path)
        index.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/stones-studio.mp3", "title": "Paint It, Black", "artist": "The Rolling Stones", "album": "Aftermath"},
                {"uri": "x-file-cifs://server/music/stones-live.mp3", "title": "Paint It, Black (Live)", "artist": "The Rolling Stones", "album": "Flashpoint"},
            ]
        )

        # live=False must return Aftermath studio version
        studio = index.search("Paint It, Black", live=False)
        self.assertIsNotNone(studio)
        self.assertEqual(studio["album"], "Aftermath")

        # live=True must return Flashpoint live version
        live = index.search("Paint It, Black", live=True)
        self.assertIsNotNone(live)
        self.assertEqual(live["album"], "Flashpoint")

        # album filter
        aftermath = index.search("Paint It, Black", album="Aftermath")
        self.assertIsNotNone(aftermath)
        self.assertEqual(aftermath["album"], "Aftermath")

        flashpoint = index.search("Paint It, Black", album="Flashpoint")
        self.assertIsNotNone(flashpoint)
        self.assertEqual(flashpoint["album"], "Flashpoint")

        # Non-matching album returns None
        non_matching = index.search("Paint It, Black", album="Beggars Banquet")
        self.assertIsNone(non_matching)

    def test_resolver_skips_live_samba_when_studio_requested(self):
        from music_resolver import Resolver

        # Only Flashpoint (live) is in Samba
        library = LibraryIndex(self.db_path)
        library.replace_tracks(
            [
                {"uri": "x-file-cifs://server/music/stones-live.mp3", "title": "Paint It, Black (Live)", "artist": "The Rolling Stones", "album": "Flashpoint"},
            ]
        )

        called_yt = []
        def mock_yt(q):
            called_yt.append(q)
            return Resolution("youtube", "url", f"https://www.youtube.com/watch?v=mock_{q}", "Paint It, Black", artist="The Rolling Stones")

        resolver = Resolver(CacheStore(self.db_path), library, mock_yt)

        # Request studio version explicitly: live=False
        res = resolver.resolve("Paint It, Black", live=False, now=1_000)
        self.assertEqual(res.provider, "youtube")
        self.assertIn("Paint It, Black studio version", called_yt)

        # Request studio version via natural language query: "paint it black de estudio"
        called_yt.clear()
        res_nl = resolver.resolve("Paint It, Black de estudio", bypass_cache=True, now=1_000)
        self.assertEqual(res_nl.provider, "youtube")
        self.assertIn("Paint It, Black studio version", called_yt)

        # Request specific album not in Samba: album="Aftermath"
        called_yt.clear()
        res_album = resolver.resolve("Paint It, Black", album="Aftermath", bypass_cache=True, now=1_000)
        self.assertEqual(res_album.provider, "youtube")
        self.assertIn("Paint It, Black Aftermath", called_yt)


if __name__ == "__main__":
    unittest.main()

