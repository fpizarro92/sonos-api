import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
warnings.filterwarnings("ignore", category=ResourceWarning)

from music_resolver import (
    LibraryIndex,
    parse_genre_and_decade,
    parse_sonos_didl,
)


class GenreDecadeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_music.db"
        self.index = LibraryIndex(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_sonos_didl_extracts_genre_and_date(self):
        didl = '''<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">
            <item id="1">
                <res>x-file-cifs://nas/music/ACDC/BackInBlack/01.flac</res>
                <dc:title>Hells Bells</dc:title>
                <dc:creator>AC/DC</dc:creator>
                <upnp:album>Back in Black</upnp:album>
                <upnp:genre>Hard Rock</upnp:genre>
                <dc:date>1980-07-25</dc:date>
                <upnp:originalTrackNumber>1</upnp:originalTrackNumber>
            </item>
            <item id="2">
                <res>x-file-cifs://nas/music/SodaStereo/(1990)%20Cancion%20Animal/01.flac</res>
                <dc:title>(En) El Séptimo Día</dc:title>
                <dc:creator>Soda Stereo</dc:creator>
                <upnp:album>Canción Animal</upnp:album>
                <upnp:genre>Rock en Español</upnp:genre>
            </item>
        </DIDL-Lite>'''
        tracks = parse_sonos_didl(didl)
        self.assertEqual(len(tracks), 2)

        self.assertEqual(tracks[0]["genre"], "Hard Rock")
        self.assertEqual(tracks[0]["year"], 1980)

        # Track 2 extracts year 1990 from folder name in URI
        self.assertEqual(tracks[1]["genre"], "Rock en Español")
        self.assertEqual(tracks[1]["year"], 1990)

    def test_parse_genre_and_decade(self):
        genre, decade = parse_genre_and_decade("canciones de rock de los 80")
        self.assertEqual(genre, "rock")
        self.assertEqual(decade, 1980)

        genre, decade = parse_genre_and_decade("rock 80s")
        self.assertEqual(genre, "rock")
        self.assertEqual(decade, 1980)

        genre, decade = parse_genre_and_decade("pop de los 90s")
        self.assertEqual(genre, "pop")
        self.assertEqual(decade, 1990)

        genre, decade = parse_genre_and_decade("musica de los 70")
        self.assertEqual(genre, "")
        self.assertEqual(decade, 1970)

        genre, decade = parse_genre_and_decade("heavy metal")
        self.assertEqual(genre, "heavy metal")
        self.assertIsNone(decade)

    def test_search_genre_and_decade_filtering(self):
        sample_tracks = [
            {
                "uri": "x-file-cifs://nas/music/1.flac",
                "title": "Back in Black",
                "artist": "AC/DC",
                "album": "Back in Black",
                "genre": "Rock",
                "year": 1980,
                "track_number": 1,
            },
            {
                "uri": "x-file-cifs://nas/music/2.flac",
                "title": "Billie Jean",
                "artist": "Michael Jackson",
                "album": "Thriller",
                "genre": "Pop",
                "year": 1982,
                "track_number": 2,
            },
            {
                "uri": "x-file-cifs://nas/music/3.flac",
                "title": "Smells Like Teen Spirit",
                "artist": "Nirvana",
                "album": "Nevermind",
                "genre": "Grunge",
                "year": 1991,
                "track_number": 1,
            },
            {
                "uri": "x-file-cifs://nas/music/4.flac",
                "title": "Bohemian Rhapsody",
                "artist": "Queen",
                "album": "A Night at the Opera",
                "genre": "Classic Rock",
                "year": 1975,
                "track_number": 11,
            },
        ]
        self.index.replace_tracks(sample_tracks)

        # 1. Search Rock in the 80s (decade 1980 or 80)
        results = self.index.search_genre("Rock", decade=1980)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Back in Black")

        # 2. Search all 80s music (no genre filter)
        results = self.index.search_genre(decade=80)
        self.assertEqual(len(results), 2)
        titles = {r["title"] for r in results}
        self.assertEqual(titles, {"Back in Black", "Billie Jean"})

        # 3. Search 90s music
        results = self.index.search_genre(decade=1990)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Smells Like Teen Spirit")

        # 4. Search all Rock (any year)
        results = self.index.search_genre("Rock", shuffle=False)
        self.assertEqual(len(results), 2)
        titles = {r["title"] for r in results}
        self.assertEqual(titles, {"Back in Black", "Bohemian Rhapsody"})

        # 5. Fuzzy genre match: "grunje" -> matches "Grunge"
        results = self.index.search_genre("grunje")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Smells Like Teen Spirit")

    def test_search_fallback_detects_genre_phrase(self):
        sample_tracks = [
            {
                "uri": "x-file-cifs://nas/music/1.flac",
                "title": "You Shook Me All Night Long",
                "artist": "AC/DC",
                "album": "Back in Black",
                "genre": "Rock",
                "year": 1980,
                "track_number": 7,
            },
        ]
        self.index.replace_tracks(sample_tracks)

        # When searching for a natural phrase like "canciones de rock de los 80"
        match = self.index.search("canciones de rock de los 80")
        self.assertIsNotNone(match)
        self.assertEqual(match["title"], "You Shook Me All Night Long")

    def test_resolve_and_play_mode_genre(self):
        sample_tracks = [
            {
                "uri": "x-file-cifs://nas/music/1.flac",
                "title": "Back in Black",
                "artist": "AC/DC",
                "album": "Back in Black",
                "genre": "Rock",
                "year": 1980,
                "track_number": 1,
            },
        ]
        self.index.replace_tracks(sample_tracks)

        import importlib.util
        import types
        temporary_data_dir = Path(self.temp_dir.name) / "server_data"
        temporary_data_dir.mkdir(parents=True, exist_ok=True)
        source = (APP / "server.py").read_text().replace(
            'DATA_DIR = Path("/data")', f"DATA_DIR = Path({str(temporary_data_dir)!r})"
        )
        server = types.ModuleType("sonos_server")
        server.__file__ = str(APP / "server.py")
        exec(compile(source, server.__file__, "exec"), server.__dict__)

        with mock.patch.object(server, "play_local_list", return_value=None):
            payload = server.resolve_and_play(
                query="rock de los 80",
                room="Living",
                library=self.index,
                mode="genre",
                action="resolve",
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["provider"], "samba")
            self.assertEqual(payload["mode"], "genre")
            self.assertEqual(payload["genre"], "rock")
            self.assertEqual(payload["decade"], 1980)
            self.assertEqual(payload["count"], 1)


if __name__ == "__main__":
    unittest.main()

