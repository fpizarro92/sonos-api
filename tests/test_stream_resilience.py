import importlib.util
import json
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

    def test_clean_youtube_url_with_mix_radio_strips_list(self):
        # Shortlink with algorithmic radio/mix
        short_url = "https://youtu.be/NUI9nqWX_EI?list=RDNUI9nqWX_EI"
        self.assertEqual(self.server.clean_youtube_url(short_url), "https://www.youtube.com/watch?v=NUI9nqWX_EI")

        # Full watch URL with algorithmic radio/mix
        full_url = "https://www.youtube.com/watch?v=NUI9nqWX_EI&list=RDNUI9nqWX_EI"
        self.assertEqual(self.server.clean_youtube_url(full_url), "https://www.youtube.com/watch?v=NUI9nqWX_EI")

    def test_is_youtube_playlist_url_detection(self):
        self.assertTrue(self.server.is_youtube_playlist_url("https://www.youtube.com/playlist?list=PL12345678"))
        self.assertTrue(self.server.is_youtube_playlist_url("https://music.youtube.com/playlist?list=OLAK5uy_abc"))
        self.assertTrue(self.server.is_youtube_playlist_url("https://music.youtube.com/browse/VL-playlist-id"))
        self.assertFalse(self.server.is_youtube_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(self.server.is_youtube_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL12345678"))
        self.assertFalse(self.server.is_youtube_playlist_url("https://youtu.be/NUI9nqWX_EI?list=RDNUI9nqWX_EI"))
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

    def test_youtube_json_applies_playlist_end_flag(self):
        fake_completed = mock.Mock(returncode=0, stdout='{"entries": []}', stderr="")
        with mock.patch("subprocess.run", return_value=fake_completed) as mock_run:
            self.server.youtube_json("https://www.youtube.com/playlist?list=PL123", max_items=25)
            self.assertTrue(mock_run.called)
            args = mock_run.call_args[0][0]
            self.assertIn("--playlist-end", args)
            self.assertEqual(args[args.index("--playlist-end") + 1], "25")

    def test_play_youtube_list_truncation_preserves_tokens(self):
        # Generate 1500 mock tracks (exceeding MAX_ACTIVE_STREAMS = 1000)
        huge_tracks = [
            {"url": f"https://www.youtube.com/watch?v=track{i:04d}", "title": f"Track {i}", "artist": "Artist", "album": "Album"}
            for i in range(1500)
        ]
        with mock.patch.object(self.server, "discover_room", return_value="192.168.1.50"),              mock.patch.object(self.server, "resolve_public_host", return_value="192.168.1.10"),              mock.patch.object(self.server, "resolve_youtube_stream", return_value="http://stream"),              mock.patch.object(self.server, "play_local_list", return_value=None),              mock.patch.object(self.server, "wait_for_playback_state", return_value="PLAYING"):

            self.server.YOUTUBE_STREAMS.clear()
            queued = self.server.play_youtube_list("Living", huge_tracks, max_queue=20)

            # Should be truncated to max_queue (20)
            self.assertEqual(len(queued), 20)
            # YOUTUBE_STREAMS should contain exactly 20 tokens, not overflow/evict
            self.assertEqual(len(self.server.YOUTUBE_STREAMS), 20)
            # Track 0 token must still be in YOUTUBE_STREAMS
            first_token = queued[0]["uri"].split("/")[-1]
            self.assertIn(first_token, self.server.YOUTUBE_STREAMS)

    def test_play_local_list_starts_playback_on_first_track_before_rest(self):
        mock_tracks = [
            {"uri": f"http://test/{i}", "title": f"T{i}", "artist": "A", "album": "Alb"}
            for i in range(3)
        ]
        calls = []
        def mock_soap(ip, service, action, args):
            calls.append((action, args))
            return ""

        with mock.patch.object(self.server, "discover_room", return_value="192.168.1.50"),              mock.patch.object(self.server, "run_sonos", return_value={"stdout": json.dumps([{"name": "Living", "udn": "RINCON_123"}])}),              mock.patch.object(self.server, "soap_action", side_effect=mock_soap):

            self.server.play_local_list("Living", mock_tracks)

            actions = [c[0] for c in calls]
            # Order must be: RemoveAllTracksFromQueue, AddURIToQueue (track 0), SetAVTransportURI, Play, AddURIToQueue (tracks 1, 2)
            self.assertEqual(actions[0], "RemoveAllTracksFromQueue")
            self.assertEqual(actions[1], "AddURIToQueue")
            self.assertIn("http://test/0", calls[1][1])
            self.assertEqual(actions[2], "SetAVTransportURI")
            self.assertEqual(actions[3], "Play")
            self.assertEqual(actions[4], "AddURIToQueue")
            self.assertIn("http://test/1", calls[4][1])
            self.assertEqual(actions[5], "AddURIToQueue")
            self.assertIn("http://test/2", calls[5][1])


    def test_queue_feeder_appends_tracks_when_nearing_queue_end(self):
        feeder_url = "https://www.youtube.com/playlist?list=PLinfinite"
        self.server.register_queue_feeder("Living", feeder_url, next_start=21, batch_size=15)
        feeder = self.server.ACTIVE_FEEDERS["Living"]

        mock_new_tracks = [
            {"url": f"https://www.youtube.com/watch?v=batch{i}", "title": f"Batch {i}", "artist": "Art", "album": "Alb"}
            for i in range(15)
        ]

        with mock.patch.object(self.server, "get_transport_state", return_value="PLAYING"),              mock.patch.object(self.server, "get_queue_position", return_value=(18, 20)),              mock.patch.object(self.server, "fetch_playlist_batch", return_value=mock_new_tracks) as mock_fetch,              mock.patch.object(self.server, "append_tracks_to_queue", return_value=15) as mock_append:

            self.server.feed_queue_if_needed("Living")

            mock_fetch.assert_called_once_with(feeder_url, 21, 15)
            mock_append.assert_called_once_with("Living", mock_new_tracks)
            self.assertEqual(feeder.next_start, 36)

    def test_stop_playback_stops_queue_feeder(self):
        self.server.register_queue_feeder("Living", "https://youtube.com/playlist?list=PL123")
        self.assertIn("Living", self.server.ACTIVE_FEEDERS)

        with mock.patch.object(self.server, "run_sonos", return_value={"exit_code": 0}):
            self.server.stop_playback("Living")

        self.assertNotIn("Living", self.server.ACTIVE_FEEDERS)

    def test_save_youtube_stream_persists_across_memory_flush(self):
        # 1. Save stream token in server
        token = "persistent_token_123"
        url = "https://www.youtube.com/watch?v=NUI9nqWX_EI"
        self.server.save_youtube_stream(token, url)

        # 2. Simulate complete RAM wipe (container restart / different process)
        self.server.YOUTUBE_STREAMS.clear()
        self.assertNotIn(token, self.server.YOUTUBE_STREAMS)

        # 3. get_youtube_stream_source must recover it from SQLite
        recovered_url = self.server.get_youtube_stream_source(token)
        self.assertEqual(recovered_url, url)
        self.assertIn(token, self.server.YOUTUBE_STREAMS)

        # 4. HEAD request to stream endpoint must return 200 OK, not 404
        class DummyHandler(self.server.Handler):
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

        # Clear RAM again before HEAD request
        self.server.YOUTUBE_STREAMS.clear()
        handler = DummyHandler(f"/stream/youtube/{token}")
        handler.do_HEAD()
        self.assertEqual(handler.sent_status, 200)
        self.assertEqual(handler.sent_headers.get("Content-Type"), "audio/mpeg")


if __name__ == "__main__":
    unittest.main()
