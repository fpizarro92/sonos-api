import importlib.util
import io
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


class StreamResilienceTests(unittest.TestCase):
    def setUp(self):
        self.server = load_server()

    def test_clean_youtube_url_music(self):
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=tracking123&feature=shared"
        cleaned = self.server.clean_youtube_url(url)
        self.assertEqual(cleaned, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_clean_youtube_url_short(self):
        url = "https://youtu.be/dQw4w9WgXcQ?si=tracking123"
        cleaned = self.server.clean_youtube_url(url)
        self.assertEqual(cleaned, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_clean_youtube_url_shorts_and_embed(self):
        self.assertEqual(
            self.server.clean_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            self.server.clean_youtube_url("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_clean_youtube_url_playlist(self):
        url = "https://music.youtube.com/playlist?list=OLAK5uy_abc123&si=xyz"
        cleaned = self.server.clean_youtube_url(url)
        self.assertEqual(cleaned, "https://www.youtube.com/playlist?list=OLAK5uy_abc123")

    def test_is_youtube_playlist_url_detection(self):
        self.assertTrue(self.server.is_youtube_playlist_url("https://www.youtube.com/playlist?list=PL12345678"))
        self.assertTrue(self.server.is_youtube_playlist_url("https://music.youtube.com/playlist?list=OLAK5uy_abc"))
        self.assertTrue(self.server.is_youtube_playlist_url("https://music.youtube.com/browse/VL-playlist-id"))
        self.assertFalse(self.server.is_youtube_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(self.server.is_youtube_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL12345678"))
        self.assertFalse(self.server.is_youtube_playlist_url("pearl jam no code"))
        self.assertFalse(self.server.is_youtube_playlist_url(""))

    def test_validate_youtube_url_normalizes(self):
        raw = "https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=foo123"
        validated = self.server.validate_youtube_url(raw)
        self.assertEqual(validated, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_resolve_youtube_stream_caches_and_force_fresh(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.server.STREAM_URL_CACHE.clear()

        fake_completed = mock.Mock(returncode=0, stdout="https://googlevideo.com/stream1\n", stderr="")
        with mock.patch("subprocess.run", return_value=fake_completed) as mock_run:
            stream1 = self.server.resolve_youtube_stream(url)
            self.assertEqual(stream1, "https://googlevideo.com/stream1")
            self.assertEqual(mock_run.call_count, 1)

            # Second call should use cache
            stream2 = self.server.resolve_youtube_stream(url)
            self.assertEqual(stream2, "https://googlevideo.com/stream1")
            self.assertEqual(mock_run.call_count, 1)

            # Force fresh should re-run yt-dlp
            fake_completed.stdout = "https://googlevideo.com/stream2\n"
            stream3 = self.server.resolve_youtube_stream(url, force_fresh=True)
            self.assertEqual(stream3, "https://googlevideo.com/stream2")
            self.assertEqual(mock_run.call_count, 2)

    def test_get_transport_state_soap(self):
        with mock.patch.object(self.server, "discover_room", return_value="192.168.1.50"):
            soap_resp = "<CurrentTransportState>PLAYING</CurrentTransportState>"
            with mock.patch.object(self.server, "soap_action", return_value=soap_resp):
                state = self.server.get_transport_state("Living")
                self.assertEqual(state, "PLAYING")

    def test_get_transport_state_fallback(self):
        with mock.patch.object(self.server, "discover_room", side_effect=RuntimeError("no room")):
            with mock.patch.object(self.server, "get_room_status", return_value={"transport": {"State": "STOPPED"}}):
                state = self.server.get_transport_state("Living")
                self.assertEqual(state, "STOPPED")

    def test_wait_for_playback_state_immediate(self):
        with mock.patch.object(self.server, "get_transport_state", return_value="PLAYING"):
            state = self.server.wait_for_playback_state("Living", target_states={"PLAYING"}, timeout=1.0)
            self.assertEqual(state, "PLAYING")

    def test_play_url_with_retry_success(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with mock.patch.object(self.server, "resolve_youtube_stream", return_value="http://googlevideo.com/s"):
            with mock.patch.object(self.server, "run_sonos", return_value={"exit_code": 0}) as mock_sonos:
                with mock.patch.object(self.server, "wait_for_playback_state", return_value="PLAYING"):
                    res = self.server.play_url_with_retry("Living", url, max_retries=1)
                    self.assertEqual(res, {"exit_code": 0})
                    self.assertEqual(mock_sonos.call_count, 1)

    def test_play_url_with_retry_retries_on_stopped(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with mock.patch.object(self.server, "resolve_youtube_stream", return_value="http://googlevideo.com/s"):
            with mock.patch.object(self.server, "run_sonos", return_value={"exit_code": 0}) as mock_sonos:
                # First attempt STOPPED, second attempt PLAYING
                with mock.patch.object(self.server, "wait_for_playback_state", side_effect=["STOPPED", "PLAYING"]):
                    with mock.patch("time.sleep", return_value=None):
                        res = self.server.play_url_with_retry("Living", url, max_retries=1)
                        self.assertEqual(res, {"exit_code": 0})
                        self.assertEqual(mock_sonos.call_count, 2)

    def test_do_head_handler(self):
        server = self.server
        token = "test_token_123"
        server.YOUTUBE_STREAMS[token] = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

        class DummyHandler(server.Handler):
            def __init__(self, path):
                self.path = path
                self.command = "HEAD"
                self.headers = {}
                self.rfile = io.BytesIO()
                self.wfile = io.BytesIO()
                self.sent_status = None
                self.sent_headers = {}

            def send_response(self, status, message=None):
                self.sent_status = status

            def send_header(self, key, value):
                self.sent_headers[key] = value

            def end_headers(self):
                pass

            def send_error(self, code, message=None, explain=None):
                self.sent_status = code

            def log_message(self, format, *args):
                pass

        # Valid stream token HEAD request
        handler = DummyHandler(f"/stream/youtube/{token}")
        handler.do_HEAD()
        self.assertEqual(handler.sent_status, 200)
        self.assertEqual(handler.sent_headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(handler.sent_headers.get("Accept-Ranges"), "none")

        # Invalid token HEAD request
        handler_404 = DummyHandler("/stream/youtube/non_existent_token")
        handler_404.do_HEAD()
        self.assertEqual(handler_404.sent_status, 404)


if __name__ == "__main__":
    unittest.main()
