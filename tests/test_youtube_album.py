import importlib.util
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
warnings.filterwarnings("ignore", category=ResourceWarning)


def load_server():
    resolver_spec = importlib.util.spec_from_file_location("music_resolver", APP / "music_resolver.py")
    resolver = importlib.util.module_from_spec(resolver_spec)
    sys.modules["music_resolver"] = resolver
    resolver_spec.loader.exec_module(resolver)
    temporary_data_dir = Path(tempfile.mkdtemp())
    source = (APP / "server.py").read_text().replace(
        'DATA_DIR = Path("/data")', f"DATA_DIR = Path({str(temporary_data_dir)!r})"
    )
    server = types.ModuleType("sonos_server")
    server.__file__ = str(APP / "server.py")
    exec(compile(source, server.__file__, "exec"), server.__dict__)
    return server


class YouTubeAlbumResolutionTests(unittest.TestCase):
    def test_selects_exact_album_and_preserves_its_track_order(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Pearl%20Jam%20No%20Code"
        wrong_url = "https://music.youtube.com/browse/MPRE-wrong"
        right_url = "https://music.youtube.com/browse/MPRE-right"
        payloads = {
            search_url: {
                "entries": [
                    {"url": wrong_url, "ie_key": "YoutubeTab"},
                    {"url": right_url, "ie_key": "YoutubeTab"},
                ]
            },
            wrong_url: {
                "title": "Album - Yield",
                "entries": [{"id": "wrong1", "url": "https://music.youtube.com/watch?v=wrong1", "title": "Brain of J."}],
            },
            right_url: {
                "title": "Album - No Code",
                "entries": [
                    {"id": "first01", "url": "https://music.youtube.com/watch?v=first01", "title": "Sometimes", "channel": "Pearl Jam"},
                    {"id": "second02", "url": "https://music.youtube.com/watch?v=second02", "title": "Hail, Hail", "channel": "Pearl Jam"},
                ],
            },
        }

        album = server.select_youtube_music_album("No Code", "Pearl Jam", lambda url: payloads[url])

        self.assertEqual(album["album"], "No Code")
        self.assertEqual(album["artist"], "Pearl Jam")
        self.assertEqual([track["title"] for track in album["tracks"]], ["Sometimes", "Hail, Hail"])
        self.assertEqual(album["tracks"][0]["url"], "https://www.youtube.com/watch?v=first01")

    def test_selects_album_with_spanish_prefix(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Soda%20Stereo%20Cancion%20Animal"
        browse_url = "https://music.youtube.com/browse/MPRE-soda"
        payloads = {
            search_url: {"entries": [{"url": browse_url}]},
            browse_url: {
                "title": "Álbum - Canción Animal",
                "entries": [
                    {"id": "track01", "url": "https://music.youtube.com/watch?v=track01", "title": "(En) El Séptimo Día", "channel": "Soda Stereo"},
                    {"id": "track02", "url": "https://music.youtube.com/watch?v=track02", "title": "De Música Ligera", "channel": "Soda Stereo"},
                ],
            },
        }

        album = server.select_youtube_music_album("Cancion Animal", "Soda Stereo", lambda url: payloads[url])

        self.assertEqual(album["album"], "Canción Animal")
        self.assertEqual(len(album["tracks"]), 2)

    def test_rejects_homonymous_album_from_wrong_artist(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Pearl%20Jam%20Ten"
        wrong_artist_url = "https://music.youtube.com/browse/MPRE-wrong-artist"
        right_artist_url = "https://music.youtube.com/browse/MPRE-right-artist"
        payloads = {
            search_url: {
                "entries": [
                    {"url": wrong_artist_url},
                    {"url": right_artist_url},
                ]
            },
            wrong_artist_url: {
                "title": "Album - Ten",
                "entries": [
                    {"id": "othertrack01", "url": "https://music.youtube.com/watch?v=othertrack01", "title": "Song A", "channel": "Other Band"},
                    {"id": "othertrack02", "url": "https://music.youtube.com/watch?v=othertrack02", "title": "Song B", "channel": "Other Band"},
                ],
            },
            right_artist_url: {
                "title": "Album - Ten",
                "entries": [
                    {"id": "pjtrack01", "url": "https://music.youtube.com/watch?v=pjtrack01", "title": "Once", "channel": "Pearl Jam"},
                    {"id": "pjtrack02", "url": "https://music.youtube.com/watch?v=pjtrack02", "title": "Even Flow", "channel": "Pearl Jam"},
                ],
            },
        }

        album = server.select_youtube_music_album("Ten", "Pearl Jam", lambda url: payloads[url])

        self.assertEqual(album["artist"], "Pearl Jam")
        self.assertEqual(album["tracks"][0]["title"], "Once")

    def test_direct_youtube_playlist_url_resolution(self):
        server = load_server()
        playlist_url = "https://music.youtube.com/playlist?list=PL12345678"
        payloads = {
            playlist_url: {
                "title": "My Favorite Playlist",
                "entries": [
                    {"id": "song01", "url": "https://music.youtube.com/watch?v=song01", "title": "Song 1", "channel": "Artist 1"},
                    {"id": "song02", "url": "https://music.youtube.com/watch?v=song02", "title": "Song 2", "channel": "Artist 2"},
                ],
            }
        }

        album = server.select_youtube_music_album(playlist_url, "", lambda url: payloads[url])

        self.assertEqual(album["album"], "My Favorite Playlist")
        self.assertEqual(len(album["tracks"]), 2)
        self.assertEqual(album["tracks"][0]["url"], "https://www.youtube.com/watch?v=song01")

    def test_accepts_a_playlist_title_that_includes_artist_and_exact_album(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Pearl%20Jam%20No%20Code"
        playlist_url = "https://music.youtube.com/browse/VL-no-code"
        payloads = {
            search_url: {"entries": [{"url": playlist_url, "ie_key": "YoutubeTab"}]},
            playlist_url: {
                "title": "Pearl Jam – No Code",
                "entries": [
                    {"id": "first01", "url": "https://music.youtube.com/watch?v=first01", "title": "Sometimes", "channel": "Pearl Jam"},
                    {"id": "second02", "url": "https://music.youtube.com/watch?v=second02", "title": "Hail, Hail", "channel": "Pearl Jam"},
                ],
            },
        }

        album = server.select_youtube_music_album("No Code", "Pearl Jam", lambda url: payloads[url])

        self.assertEqual(album["album"], "No Code")
        self.assertEqual(len(album["tracks"]), 2)

    def test_prefers_numbered_album_playlist_when_multiple_titles_match(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Pearl%20Jam%20No%20Code"
        unnumbered_url = "https://music.youtube.com/browse/VL-unnumbered"
        numbered_url = "https://music.youtube.com/browse/VL-numbered"
        payloads = {
            search_url: {"entries": [{"url": unnumbered_url}, {"url": numbered_url}]},
            unnumbered_url: {
                "title": "Pearl Jam-No Code",
                "entries": [
                    {"id": "first01", "title": "I'm Open by Pearl Jam", "channel": "Pearl Jam"},
                    {"id": "second02", "title": "Sometimes by Pearl Jam", "channel": "Pearl Jam"},
                ],
            },
            numbered_url: {
                "title": "Pearl Jam – No Code",
                "entries": [
                    {"id": "third03", "title": "1-Pearl Jam-Sometimes", "channel": "Pearl Jam"},
                    {"id": "fourth04", "title": "2-Pearl Jam-Hail, Hail", "channel": "Pearl Jam"},
                    {"id": "fifth05", "title": "Pearl Jam – No Code Full Album", "channel": "Pearl Jam"},
                ],
            },
        }

        album = server.select_youtube_music_album("No Code", "Pearl Jam", lambda url: payloads[url])

        self.assertEqual(album["tracks"][0]["title"], "1-Pearl Jam-Sometimes")
        self.assertEqual(len(album["tracks"]), 2)

    def test_caches_youtube_album_tracks_by_album_and_artist(self):
        database = Path(tempfile.mkdtemp()) / "cache.sqlite"
        resolver_spec = importlib.util.spec_from_file_location("cache_resolver", APP / "music_resolver.py")
        resolver = importlib.util.module_from_spec(resolver_spec)
        sys.modules["cache_resolver"] = resolver
        resolver_spec.loader.exec_module(resolver)
        cache = resolver.CacheStore(database)
        tracks = [{"url": "https://www.youtube.com/watch?v=first01", "title": "Sometimes", "artist": "Pearl Jam", "album": "No Code"}]

        cache.put_album("No Code", "Pearl Jam", tracks, ttl_seconds=60, now=100)

        self.assertEqual(cache.get_album("No Code", "Pearl Jam", now=101), tracks)

    def test_ignores_artist_channel_urls_and_does_not_fetch_them(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Balatro%20OST"
        channel_url = "https://music.youtube.com/browse/UC9ecwl3FTG66jIKA9JRDtmg"
        channel_url2 = "https://music.youtube.com/channel/UC1234567890abcdef"
        album_url = "https://music.youtube.com/browse/MPREb_balatro"
        fetched_urls = []

        payloads = {
            search_url: {
                "entries": [
                    {"url": channel_url},
                    {"url": channel_url2},
                    {"url": album_url},
                ]
            },
            album_url: {
                "title": "Album - Balatro OST",
                "channel": "LouisF",
                "entries": [
                    {"id": "balatro01", "url": "https://music.youtube.com/watch?v=balatro01", "title": "Main Theme", "channel": "LouisF"},
                    {"id": "balatro02", "url": "https://music.youtube.com/watch?v=balatro02", "title": "Shop Theme", "channel": "LouisF"},
                ],
            },
        }

        def mock_fetcher(url, timeout=15):
            fetched_urls.append(url)
            if url in payloads:
                return payloads[url]
            raise RuntimeError(f"Unexpected fetch for {url}")

        album = server.select_youtube_music_album("Balatro OST", "", mock_fetcher)

        self.assertNotIn(channel_url, fetched_urls)
        self.assertNotIn(channel_url2, fetched_urls)
        self.assertIn(album_url, fetched_urls)
        self.assertEqual(album["album"], "Balatro OST")
        self.assertEqual(len(album["tracks"]), 2)

    def test_recovers_when_first_candidate_fails_or_times_out(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Test%20Band%20Greatest%20Hits"
        failing_url = "https://music.youtube.com/browse/MPREb_failing"
        working_url = "https://music.youtube.com/browse/MPREb_working"

        def mock_fetcher(url, timeout=15):
            if url == search_url:
                return {"entries": [{"url": failing_url}, {"url": working_url}]}
            if url == failing_url:
                raise TimeoutError("yt-dlp timed out")
            if url == working_url:
                return {
                    "title": "Album - Greatest Hits",
                    "channel": "Test Band",
                    "entries": [
                        {"id": "track01", "url": "https://music.youtube.com/watch?v=track01", "title": "Track 1", "channel": "Test Band"},
                        {"id": "track02", "url": "https://music.youtube.com/watch?v=track02", "title": "Track 2", "channel": "Test Band"},
                    ],
                }
            raise RuntimeError(f"Unknown URL {url}")

        album = server.select_youtube_music_album("Greatest Hits", "Test Band", mock_fetcher)
        self.assertEqual(album["album"], "Greatest Hits")
        self.assertEqual(len(album["tracks"]), 2)

    def test_prioritizes_mpre_official_releases_over_playlists(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Artist%20Album"
        playlist_url = "https://music.youtube.com/browse/VL-playlist"
        official_url = "https://music.youtube.com/browse/MPREb_official"
        fetched_order = []

        payloads = {
            search_url: {
                "entries": [
                    {"url": playlist_url},
                    {"url": official_url},
                ]
            },
            official_url: {
                "title": "Album - Album",
                "channel": "Artist",
                "entries": [
                    {"id": "offtrack01", "url": "https://music.youtube.com/watch?v=offtrack01", "title": "Track 1", "channel": "Artist"},
                    {"id": "offtrack02", "url": "https://music.youtube.com/watch?v=offtrack02", "title": "Track 2", "channel": "Artist"},
                ],
            },
            playlist_url: {
                "title": "Artist - Album",
                "channel": "Artist",
                "entries": [
                    {"id": "playtrack01", "url": "https://music.youtube.com/watch?v=playtrack01", "title": "Track 1", "channel": "Artist"},
                    {"id": "playtrack02", "url": "https://music.youtube.com/watch?v=playtrack02", "title": "Track 2", "channel": "Artist"},
                ],
            },
        }

        def mock_fetcher(url, timeout=15):
            fetched_order.append(url)
            return payloads[url]

        album = server.select_youtube_music_album("Album", "Artist", mock_fetcher)
        # official_url should be evaluated before playlist_url
        self.assertEqual(fetched_order[1], official_url)
        self.assertEqual(album["album"], "Album")

    def test_fallback_to_search_youtube_many_when_no_youtube_music_album_found(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Unknown%20Artist%20Rare%20Bootleg"

        def mock_fetcher(url, timeout=15):
            return {"entries": []}

        original_search_youtube_many = server.search_youtube_many
        try:
            server.search_youtube_many = lambda query, limit: [
                {"url": "https://www.youtube.com/watch?v=fallback1", "title": "Track 1", "artist": "Unknown Artist", "album": ""},
                {"url": "https://www.youtube.com/watch?v=fallback2", "title": "Track 2", "artist": "Unknown Artist", "album": ""},
            ]
            album = server.select_youtube_music_album("Rare Bootleg", "Unknown Artist", mock_fetcher)
            self.assertEqual(album["album"], "Rare Bootleg")
            self.assertEqual(album["artist"], "Unknown Artist")
            self.assertEqual(len(album["tracks"]), 2)
            self.assertEqual(album["tracks"][0]["url"], "https://www.youtube.com/watch?v=fallback1")
        finally:
            server.search_youtube_many = original_search_youtube_many


if __name__ == "__main__":
    unittest.main()


