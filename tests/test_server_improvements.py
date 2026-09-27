import importlib.util
import json
import sys
import tempfile
import types
import unittest
from unittest import mock
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


class ServerImprovementsTests(unittest.TestCase):
    def test_get_protocol_info_detects_audio_extensions(self):
        server = load_server()
        self.assertEqual(server.get_protocol_info("x-file-cifs://server/song.flac"), "x-file-cifs:*:audio/flac:*")
        self.assertEqual(server.get_protocol_info("x-file-cifs://server/song.m4a"), "x-file-cifs:*:audio/mp4:*")
        self.assertEqual(server.get_protocol_info("x-file-cifs://server/song.mp4"), "x-file-cifs:*:audio/mp4:*")
        self.assertEqual(server.get_protocol_info("x-file-cifs://server/song.wav"), "x-file-cifs:*:audio/x-wav:*")
        self.assertEqual(server.get_protocol_info("x-file-cifs://server/song.ogg"), "x-file-cifs:*:audio/ogg:*")
        self.assertEqual(server.get_protocol_info("x-file-cifs://server/song.mp3"), "x-file-cifs:*:audio/mpeg:*")

    def test_is_youtube_url_recognizes_valid_hosts(self):
        server = load_server()
        self.assertTrue(server.is_youtube_url("https://www.youtube.com/watch?v=123456"))
        self.assertTrue(server.is_youtube_url("https://youtube.com/watch?v=123456"))
        self.assertTrue(server.is_youtube_url("https://youtu.be/123456"))
        self.assertTrue(server.is_youtube_url("https://music.youtube.com/watch?v=123456"))
        self.assertTrue(server.is_youtube_url("https://m.youtube.com/watch?v=123456"))
        self.assertFalse(server.is_youtube_url("https://spotify.com/track/123"))
        self.assertFalse(server.is_youtube_url("Just a song query"))
        self.assertFalse(server.is_youtube_url(""))

    def test_local_track_metadata_sets_correct_mime(self):
        server = load_server()
        track = {"uri": "x-file-cifs://server/song.flac", "title": "Song", "artist": "Artist", "album": "Album"}
        xml = server.local_track_metadata(track)
        self.assertIn('protocolInfo="x-file-cifs:*:audio/flac:*"', xml)
        self.assertIn("<dc:title>Song</dc:title>", xml)

    def test_select_youtube_music_artist_finds_tracks(self):
        server = load_server()
        search_url = "https://music.youtube.com/search?q=Queen"
        artist_browse_url = "https://music.youtube.com/browse/VL-queen-top"
        payloads = {
            search_url: {
                "entries": [
                    {"url": artist_browse_url},
                ]
            },
            artist_browse_url: {
                "title": "Queen",
                "entries": [
                    {"id": "queen01", "url": "https://music.youtube.com/watch?v=queen01", "title": "Bohemian Rhapsody", "channel": "Queen"},
                    {"id": "queen02", "url": "https://music.youtube.com/watch?v=queen02", "title": "Don't Stop Me Now", "channel": "Queen"},
                    {"id": "queen03", "url": "https://music.youtube.com/watch?v=queen03", "title": "Another One Bites the Dust", "channel": "Queen"},
                ],
            },
        }

        result = server.select_youtube_music_artist("Queen", limit=10, fetcher=lambda url: payloads[url])
        self.assertEqual(result["artist"], "Queen")
        self.assertEqual(len(result["tracks"]), 3)
        self.assertEqual(result["tracks"][0]["title"], "Bohemian Rhapsody")

    def test_resolve_public_host_autodetects_and_respects_env(self):
        server = load_server()
        detected = server.resolve_public_host()
        self.assertTrue(bool(detected))
        self.assertNotEqual(detected, "")

        import os
        old_env = os.environ.get("SONOS_API_HOST")
        try:
            os.environ["SONOS_API_HOST"] = "10.42.0.1"
            self.assertEqual(server.resolve_public_host(), "10.42.0.1")
        finally:
            if old_env is not None:
                os.environ["SONOS_API_HOST"] = old_env
            else:
                os.environ.pop("SONOS_API_HOST", None)


    def test_get_room_status_returns_dict(self):
        server = load_server()
        with mock.patch.object(server, "run_sonos", return_value={"stdout": '{"transport": {"State": "PLAYING"}}'}):
            status = server.get_room_status("Living")
            self.assertEqual(status.get("transport", {}).get("State"), "PLAYING")

    def test_get_room_status_returns_empty_on_error(self):
        server = load_server()
        with mock.patch.object(server, "run_sonos", side_effect=RuntimeError("Device unreachable")):
            status = server.get_room_status("Living")
            self.assertEqual(status, {})

    def test_image_cache_in_sqlite(self):
        server = load_server()
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        server.CACHE.put_image("test_key", "image/jpeg", fake_jpeg)
        retrieved = server.CACHE.get_image("test_key")
        self.assertIsNotNone(retrieved)
        mime, data = retrieved
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(data, fake_jpeg)

        img = server.get_image("test_key")
        self.assertEqual(img, (mime, data))

    def test_resolve_and_play_passes_artist_in_track_mode(self):
        server = load_server()
        with mock.patch.object(server, "resolve") as mock_resolve, mock.patch.object(server, "play_resolution") as mock_play:
            from music_resolver import Resolution
            mock_resolve.return_value = Resolution("youtube", "url", "https://youtube.com/watch?v=123", "Silencio", "U2")
            mock_play.return_value = {"ok": True}
            payload = server.resolve_and_play("Silencio", "Living", server.LIBRARY, mode="track", artist="U2")
            self.assertTrue(payload["ok"])
            mock_resolve.assert_called_once_with(
                "Silencio", "Living", None, False, None, False, artist="U2"
            )


if __name__ == "__main__":
    unittest.main()



