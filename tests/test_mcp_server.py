import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from mcp_server import MCPServer, SSEManager, TOOL_DEFINITIONS, run_stdio


class MockBackend:
    def __init__(self):
        self.calls = []

    def get_room(self, room=None):
        return room or "Sala de estar"

    def play_music(self, query, room=None, mode="track", artist=None, genre=None, decade=None, shuffle=True, **kwargs):
        self.calls.append(("play_music", query, room, mode, artist, genre, decade, shuffle))
        return {
            "ok": True,
            "query": query,
            "room": room or "Sala de estar",
            "mode": mode,
            "status": {"transport": {"State": "PLAYING"}}
        }

    def pause(self, room=None):
        self.calls.append(("pause", room))
        return {"action": "pause", "room": room or "Sala de estar", "status": {"transport": {"State": "PAUSED_PLAYBACK"}}}

    def resume(self, room=None):
        self.calls.append(("resume", room))
        return {"action": "play", "room": room or "Sala de estar", "status": {"transport": {"State": "PLAYING"}}}

    def stop(self, room=None):
        self.calls.append(("stop", room))
        return {"action": "stop", "room": room or "Sala de estar", "status": {"transport": {"State": "STOPPED"}}}

    def next_track(self, room=None):
        self.calls.append(("next", room))
        return {"action": "next", "room": room or "Sala de estar", "status": {"transport": {"State": "PLAYING"}}}

    def previous_track(self, room=None):
        self.calls.append(("previous", room))
        return {"action": "previous", "room": room or "Sala de estar", "status": {"transport": {"State": "PLAYING"}}}

    def set_volume(self, level, room=None):
        self.calls.append(("set_volume", level, room))
        return {"action": "volume", "room": room or "Sala de estar", "level": level, "status": {"volume": level}}

    def get_status(self, room=None):
        self.calls.append(("get_status", room))
        return {"room": room or "Sala de estar", "status": {"transport": {"State": "PLAYING"}, "volume": 40}}

    def list_rooms(self):
        self.calls.append(("list_rooms",))
        return {"rooms": ["Sala de estar", "Dormitorio"], "devices": []}

    def reindex_library(self, room=None):
        self.calls.append(("reindex_library", room))
        return {"library": {"tracks": 1500, "status": "indexed"}}


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend()
        self.server = MCPServer(backend=self.backend)

    def test_initialize_handshake(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-agent", "version": "1.0"}
            }
        }
        res = self.server.handle_jsonrpc(req)
        self.assertEqual(res["jsonrpc"], "2.0")
        self.assertEqual(res["id"], 1)
        self.assertEqual(res["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", res["result"]["capabilities"])
        self.assertEqual(res["result"]["serverInfo"]["name"], "sonos-api-mcp")

    def test_notifications_initialized_returns_none(self):
        req = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
        res = self.server.handle_jsonrpc(req)
        self.assertIsNone(res)

    def test_ping(self):
        req = {"jsonrpc": "2.0", "id": "p1", "method": "ping"}
        res = self.server.handle_jsonrpc(req)
        self.assertEqual(res["result"], {})

    def test_tools_list_returns_all_tools(self):
        req = {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}
        res = self.server.handle_jsonrpc(req)
        tools = res["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        expected = {
            "sonos_play_music",
            "sonos_pause",
            "sonos_resume",
            "sonos_stop",
            "sonos_next",
            "sonos_previous",
            "sonos_set_volume",
            "sonos_get_status",
            "sonos_list_rooms",
            "sonos_reindex_library",
        }
        self.assertTrue(expected.issubset(tool_names))
        for tool in tools:
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_call_sonos_play_music(self):
        req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "sonos_play_music",
                "arguments": {
                    "query": "Stone Temple Pilots",
                    "room": "Sala de estar",
                    "mode": "artist",
                    "provider": "youtube",
                    "limit": 5
                }
            }
        }
        res = self.server.handle_jsonrpc(req)
        self.assertFalse(res["result"]["isError"])
        content = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(content["query"], "Stone Temple Pilots")
        self.assertEqual(content["room"], "Sala de estar")
        self.assertEqual(content["mode"], "artist")
        self.assertEqual(len(self.backend.calls), 1)

    def test_call_sonos_play_music_genre_only(self):
        req = {
            "jsonrpc": "2.0",
            "id": 111,
            "method": "tools/call",
            "params": {
                "name": "sonos_play_music",
                "arguments": {
                    "genre": "Grunge",
                    "provider": "samba"
                }
            }
        }
        res = self.server.handle_jsonrpc(req)
        self.assertFalse(res["result"]["isError"])
        content = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(content["query"], "Grunge")

    def test_call_sonos_play_music_missing_query(self):
        req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "sonos_play_music",
                "arguments": {}
            }
        }
        res = self.server.handle_jsonrpc(req)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("obligatorio", res["result"]["content"][0]["text"])

    def test_call_sonos_pause_and_resume(self):
        req_pause = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "sonos_pause", "arguments": {"room": "Sala de estar"}}
        }
        res_pause = self.server.handle_jsonrpc(req_pause)
        self.assertFalse(res_pause["result"]["isError"])
        parsed_pause = json.loads(res_pause["result"]["content"][0]["text"])
        self.assertEqual(parsed_pause["action"], "pause")

        req_resume = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "sonos_resume", "arguments": {}}
        }
        res_resume = self.server.handle_jsonrpc(req_resume)
        self.assertFalse(res_resume["result"]["isError"])
        parsed_resume = json.loads(res_resume["result"]["content"][0]["text"])
        self.assertEqual(parsed_resume["action"], "play")

    def test_call_sonos_stop_next_previous(self):
        req_stop = {
            "jsonrpc": "2.0",
            "id": 131,
            "method": "tools/call",
            "params": {"name": "sonos_stop", "arguments": {"room": "Sala de estar"}}
        }
        res_stop = self.server.handle_jsonrpc(req_stop)
        self.assertFalse(res_stop["result"]["isError"])
        parsed_stop = json.loads(res_stop["result"]["content"][0]["text"])
        self.assertEqual(parsed_stop["action"], "stop")

        req_next = {
            "jsonrpc": "2.0",
            "id": 132,
            "method": "tools/call",
            "params": {"name": "sonos_next", "arguments": {}}
        }
        res_next = self.server.handle_jsonrpc(req_next)
        self.assertFalse(res_next["result"]["isError"])
        parsed_next = json.loads(res_next["result"]["content"][0]["text"])
        self.assertEqual(parsed_next["action"], "next")

        req_prev = {
            "jsonrpc": "2.0",
            "id": 133,
            "method": "tools/call",
            "params": {"name": "sonos_previous", "arguments": {}}
        }
        res_prev = self.server.handle_jsonrpc(req_prev)
        self.assertFalse(res_prev["result"]["isError"])
        parsed_prev = json.loads(res_prev["result"]["content"][0]["text"])
        self.assertEqual(parsed_prev["action"], "previous")

    def test_call_sonos_set_volume(self):
        req = {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {"name": "sonos_set_volume", "arguments": {"level": 35, "room": "Sala de estar"}}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertFalse(res["result"]["isError"])
        parsed = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(parsed["level"], 35)

    def test_call_sonos_set_volume_out_of_range(self):
        req = {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {"name": "sonos_set_volume", "arguments": {"level": 150}}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("0 y 100", res["result"]["content"][0]["text"])

    def test_call_sonos_get_status(self):
        req = {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "tools/call",
            "params": {"name": "sonos_get_status", "arguments": {"room": "Sala de estar"}}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertFalse(res["result"]["isError"])
        parsed = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(parsed["room"], "Sala de estar")
        self.assertEqual(parsed["status"]["transport"]["State"], "PLAYING")

    def test_call_sonos_list_rooms(self):
        req = {
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/call",
            "params": {"name": "sonos_list_rooms", "arguments": {}}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertFalse(res["result"]["isError"])
        parsed = json.loads(res["result"]["content"][0]["text"])
        self.assertIn("Sala de estar", parsed["rooms"])

    def test_call_sonos_reindex_library(self):
        req = {
            "jsonrpc": "2.0",
            "id": 19,
            "method": "tools/call",
            "params": {"name": "sonos_reindex_library", "arguments": {}}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertFalse(res["result"]["isError"])
        parsed = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(parsed["library"]["tracks"], 1500)

    def test_unknown_tool_returns_error(self):
        req = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "non_existent_tool", "arguments": {}}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], -32601)

    def test_unknown_method_returns_error(self):
        req = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "unknown/method",
            "params": {}
        }
        res = self.server.handle_jsonrpc(req)
        self.assertEqual(res["error"]["code"], -32601)

    def test_sse_manager_session_routing(self):
        manager = SSEManager(self.server)
        session_id = manager.create_session()
        self.assertIn(session_id, manager.sessions)

        msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        sent = manager.send_message(session_id, msg)
        self.assertTrue(sent)

        q = manager.sessions[session_id]
        received = q.get_nowait()
        self.assertEqual(received, msg)

        manager.remove_session(session_id)
        self.assertNotIn(session_id, manager.sessions)
        self.assertFalse(manager.send_message(session_id, msg))

    def test_stdio_mode(self):
        input_data = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        fake_stdin = io.StringIO(input_data)
        fake_stdout = io.StringIO()

        with mock.patch("sys.stdin", fake_stdin), mock.patch("sys.stdout", fake_stdout):
            run_stdio()

        output_lines = [line.strip() for line in fake_stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(output_lines), 2)
        res1 = json.loads(output_lines[0])
        self.assertEqual(res1["id"], 1)
        self.assertEqual(res1["result"]["serverInfo"]["name"], "sonos-api-mcp")

        res2 = json.loads(output_lines[1])
        self.assertEqual(res2["id"], 2)
        self.assertGreater(len(res2["result"]["tools"]), 5)

    def test_default_backend_loads_server_and_dispatches(self):
        from mcp_server import DefaultBackend
        mock_server = mock.MagicMock()
        mock_server.discover_any_room.return_value = "Living"
        mock_server.resolve_and_play.return_value = {"ok": True}
        mock_server.get_room_status.return_value = {"transport": {"State": "PLAYING"}}
        mock_server.run_sonos.return_value = {"returncode": 0}
        mock_server.sync_library.return_value = {"tracks": 10}

        backend = DefaultBackend()
        with mock.patch("mcp_server._load_server", return_value=mock_server):
            res_play = backend.play_music("Queen", room="Living")
            self.assertTrue(res_play["ok"])
            mock_server.resolve_and_play.assert_called_once()

            res_pause = backend.pause("Living")
            self.assertEqual(res_pause["action"], "pause")

            res_resume = backend.resume("Living")
            self.assertEqual(res_resume["action"], "play")

            mock_server.stop_playback.return_value = {"exit_code": 0}
            res_stop = backend.stop("Living")
            self.assertEqual(res_stop["action"], "stop")
            mock_server.stop_playback.assert_called_once_with("Living")

            mock_server.next_track.return_value = {"exit_code": 0}
            res_next = backend.next_track("Living")
            self.assertEqual(res_next["action"], "next")
            mock_server.next_track.assert_called_once_with("Living")

            mock_server.previous_track.return_value = {"exit_code": 0}
            res_prev = backend.previous_track("Living")
            self.assertEqual(res_prev["action"], "previous")
            mock_server.previous_track.assert_called_once_with("Living")

            res_vol = backend.set_volume(25, "Living")
            self.assertEqual(res_vol["level"], 25)

            res_status = backend.get_status("Living")
            self.assertEqual(res_status["room"], "Living")

            res_reindex = backend.reindex_library("Living")
            self.assertIn("library", res_reindex)

    def test_thumbnail_extraction_youtube(self):
        from mcp_server import extract_youtube_video_id, enrich_playback_payload
        vid = extract_youtube_video_id("https://www.youtube.com/watch?v=e3YzmjmAGoI")
        self.assertEqual(vid, "e3YzmjmAGoI")

        payload = {
            "ok": True,
            "resolution": {
                "title": "Hell March",
                "artist": "Frank Klepacki",
                "provider": "youtube",
                "target": "https://www.youtube.com/watch?v=e3YzmjmAGoI"
            }
        }
        enriched = enrich_playback_payload(payload, "Living")
        self.assertEqual(enriched["title"], "Hell March")
        self.assertEqual(enriched["artist"], "Frank Klepacki")
        self.assertEqual(enriched["source"], "youtube")
        self.assertEqual(enriched["image_url"], "https://img.youtube.com/vi/e3YzmjmAGoI/hqdefault.jpg")

    def test_album_art_samba(self):
        from mcp_server import enrich_playback_payload
        mock_server = mock.MagicMock()
        mock_server.discover_room.return_value = "192.168.1.100"
        mock_server.resolve_public_host.return_value = "192.168.1.50"
        mock_server.BIND_PORT = 39100

        # Case 1: albumArtURI in status (relative)
        payload = {
            "status": {
                "track": {
                    "title": "Smells Like Teen Spirit",
                    "artist": "Nirvana",
                    "album": "Nevermind",
                    "albumArtURI": "/getaa?u=x-file-cifs%3A%2F%2Fmusic%2Fsong.flac"
                }
            }
        }
        enriched = enrich_playback_payload(payload, "Living", server_module=mock_server)
        self.assertEqual(enriched["title"], "Smells Like Teen Spirit")
        self.assertEqual(enriched["artist"], "Nirvana")
        self.assertTrue(enriched["image_url"].startswith("http://192.168.1.50:39100/image/"))
        self.assertEqual(enriched["local_art_uri"], "http://192.168.1.100:1400/getaa?u=x-file-cifs%3A%2F%2Fmusic%2Fsong.flac")

        # Case 2: CIFS source_uri without albumArtURI in status
        payload2 = {
            "source": "samba",
            "source_uri": "x-file-cifs://nas/music/Nirvana/song.mp3",
            "title": "Come As You Are"
        }
        enriched2 = enrich_playback_payload(payload2, "Living", server_module=mock_server)
        self.assertTrue(enriched2["image_url"].startswith("http://192.168.1.50:39100/image/samba_"))
        self.assertIn("http://192.168.1.100:1400/getaa?u=", enriched2["local_art_uri"])


if __name__ == "__main__":
    unittest.main()


