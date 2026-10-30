"""Terminal events and bounded waiting when a ComfyUI worker fails."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import websocket
from core.comfyui import ComfyUIClient, wait_history_ws


class ComfyWaitTests(unittest.TestCase):
    def test_execution_error_is_saved_without_waiting_for_history(self):
        event = {"type": "execution_error", "data": {"prompt_id": "job", "node_id": "sampler",
                 "exception_message": "CUDA out of memory", "exception_type": "AcceleratorError"}}
        ws = Mock()
        ws.recv.return_value = json.dumps(event)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ComfyUIClient("http://localhost:8188", root)
            with patch("core.comfyui.websocket.WebSocket", return_value=ws), \
                 patch("core.comfyui.requests.get", side_effect=AssertionError("must not wait for history")), \
                 patch("core.comfyui.free_comfy_memory"), \
                 patch("core.comfyui.queue_prompt", return_value=("job", "client")), \
                 patch("core.comfyui.log"):
                with self.assertRaisesRegex(RuntimeError, "CUDA out of memory"):
                    client.run({}, root / "graph.json", root / "history.json", "test")
            saved = json.loads((root / "history.json").read_text())
            self.assertEqual(saved["source"], "websocket")
            self.assertEqual(saved["status"]["messages"][0][1], event["data"])
        ws.close.assert_called_once()

    def test_closed_websocket_fails_instead_of_spinning(self):
        for recv in ("", websocket.WebSocketConnectionClosedException("closed")):
            ws = Mock()
            if isinstance(recv, Exception):
                ws.recv.side_effect = recv
            else:
                ws.recv.return_value = recv
            with self.subTest(recv=recv), patch("core.comfyui.websocket.WebSocket", return_value=ws):
                with self.assertRaisesRegex(RuntimeError, "connection closed"):
                    wait_history_ws("job", "client", {}, "http://localhost:8188")
            ws.close.assert_called_once()

    def test_polling_recovers_completion_without_websocket_event(self):
        clock = [0]
        ws = Mock()
        def recv():
            clock[0] = 6
            raise websocket.WebSocketTimeoutException()
        ws.recv.side_effect = recv
        history = {"status": {"status_str": "success"}, "outputs": {}}
        with patch("core.comfyui.websocket.WebSocket", return_value=ws), \
             patch("core.comfyui.time.perf_counter", side_effect=lambda: clock[0]), \
             patch("core.comfyui.requests.get", return_value=Mock(json=Mock(return_value={"job": history}))):
            self.assertEqual(wait_history_ws("job", "client", {}, "http://localhost:8188"), history)

    def test_api_failure_during_silent_wait_is_reported(self):
        clock = [0]
        ws = Mock()
        def recv():
            clock[0] = 6
            raise websocket.WebSocketTimeoutException()
        ws.recv.side_effect = recv
        with patch("core.comfyui.websocket.WebSocket", return_value=ws), \
             patch("core.comfyui.time.perf_counter", side_effect=lambda: clock[0]), \
             patch("core.comfyui.requests.get", side_effect=requests.ConnectionError("server stopped")):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                wait_history_ws("job", "client", {}, "http://localhost:8188")

    def test_alive_server_with_dead_worker_has_finite_wait(self):
        ws = Mock()
        with patch("core.comfyui.websocket.WebSocket", return_value=ws), \
             patch("core.comfyui.time.perf_counter", side_effect=[0, 6, 6]):
            with self.assertRaisesRegex(RuntimeError, "exceeded 5s"):
                wait_history_ws("job", "client", {}, "http://localhost:8188", timeout_seconds=5)
        ws.recv.assert_not_called()
        ws.close.assert_called_once()

    def test_transport_failure_preserves_prompt_id_in_debug(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ComfyUIClient("http://localhost:8188", root)
            with patch("core.comfyui.free_comfy_memory"), \
                 patch("core.comfyui.queue_prompt", return_value=("job", "client")), \
                 patch("core.comfyui.wait_history", side_effect=RuntimeError("connection closed")), \
                 patch("core.comfyui.log"):
                with self.assertRaisesRegex(RuntimeError, "Debug saved to"):
                    client.run({}, root / "graph.json", root / "history.json", "test")
            self.assertEqual(json.loads((root / "history.json").read_text())["prompt_id"], "job")


if __name__ == "__main__":
    unittest.main()
