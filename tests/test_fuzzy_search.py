from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.music_resolver import LibraryIndex


class TestFuzzySearch(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_library.db"
        self.index = LibraryIndex(self.db_path)

        tracks = [
            # Pearl Jam - Yield
            {"uri": "x-file-cifs://nas/Pearl%20Jam/Yield/01-Brain_of_J.mp3", "title": "Brain of J.", "artist": "Pearl Jam", "album": "Yield", "track_number": 1},
            {"uri": "x-file-cifs://nas/Pearl%20Jam/Yield/02-Faithfull.mp3", "title": "Faithfull", "artist": "Pearl Jam", "album": "Yield", "track_number": 2},
            {"uri": "x-file-cifs://nas/Pearl%20Jam/Yield/03-No_Way.mp3", "title": "No Way", "artist": "Pearl Jam", "album": "Yield", "track_number": 3},
            # Pearl Jam - No Code
            {"uri": "x-file-cifs://nas/Pearl%20Jam/No%20Code/01-Sometimes.mp3", "title": "Sometimes", "artist": "Pearl Jam", "album": "No Code", "track_number": 1},
            # Metallica - Master of Puppets
            {"uri": "x-file-cifs://nas/Metallica/Master/01-Battery.mp3", "title": "Battery", "artist": "Metallica", "album": "Master of Puppets", "track_number": 1},
            {"uri": "x-file-cifs://nas/Metallica/Master/02-Master_of_Puppets.mp3", "title": "Master of Puppets", "artist": "Metallica", "album": "Master of Puppets", "track_number": 2},
            # Led Zeppelin - IV
            {"uri": "x-file-cifs://nas/Led%20Zeppelin/IV/01-Black_Dog.mp3", "title": "Black Dog", "artist": "Led Zeppelin", "album": "Led Zeppelin IV", "track_number": 1},
            {"uri": "x-file-cifs://nas/Led%20Zeppelin/IV/04-Stairway_to_Heaven.mp3", "title": "Stairway to Heaven", "artist": "Led Zeppelin", "album": "Led Zeppelin IV", "track_number": 4},
        ]
        self.index.replace_tracks(tracks)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fuzzy_match_artist_helper(self) -> None:
        self.assertEqual(self.index.fuzzy_match_artist("peral jam"), "Pearl Jam")
        self.assertEqual(self.index.fuzzy_match_artist("mettalica"), "Metallica")
        self.assertEqual(self.index.fuzzy_match_artist("led zepelin"), "Led Zeppelin")
        self.assertIsNone(self.index.fuzzy_match_artist("random non-existent artist 12345"))

    def test_search_artist_with_typo(self) -> None:
        # "peral jam" should resolve to Pearl Jam tracks
        tracks = self.index.search_artist("peral jam", limit=10, shuffle=False)
        self.assertGreater(len(tracks), 0)
        for t in tracks:
            self.assertEqual(t["artist"], "Pearl Jam")

        # "mettalica" should resolve to Metallica tracks
        tracks_met = self.index.search_artist("mettalica", limit=10, shuffle=False)
        self.assertGreater(len(tracks_met), 0)
        self.assertEqual(tracks_met[0]["artist"], "Metallica")

    def test_search_album_with_typo_in_artist(self) -> None:
        # Artist typo: "peral jam", exact album: "Yield"
        tracks = self.index.search_album("Yield", artist="peral jam")
        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[0]["title"], "Brain of J.")
        self.assertEqual(tracks[1]["title"], "Faithfull")
        self.assertEqual(tracks[2]["title"], "No Way")

    def test_search_album_with_typo_in_album_and_artist(self) -> None:
        # Both album and artist with typo: "yeld", "peral jam"
        tracks = self.index.search_album("yeld", artist="peral jam")
        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[0]["album"], "Yield")

    def test_search_many_with_typo_in_artist_name(self) -> None:
        # Query with typo in artist and correct title: "peral jam brain of j"
        tracks = self.index.search_many("peral jam brain of j", limit=5)
        self.assertGreater(len(tracks), 0)
        self.assertEqual(tracks[0]["title"], "Brain of J.")
        self.assertEqual(tracks[0]["artist"], "Pearl Jam")

    def test_search_many_with_typo_in_title(self) -> None:
        # Query with typo in song title: "stairwey to heaven"
        tracks = self.index.search_many("stairwey to heaven", limit=5)
        self.assertGreater(len(tracks), 0)
        self.assertEqual(tracks[0]["title"], "Stairway to Heaven")

    def test_nonsense_query_returns_empty(self) -> None:
        # Complete gibberish should return empty list, allowing YouTube fallback
        tracks = self.index.search_many("xyzqwertyuiopasdfghjkl", limit=5)
        self.assertEqual(tracks, [])


if __name__ == "__main__":
    unittest.main()
