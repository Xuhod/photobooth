import queue
import threading
import unittest
from unittest.mock import Mock

from starlette.requests import ClientDisconnect

from sony_preview import SonyPreviewResponse, start_preview


class SonyPreviewTest(unittest.IsolatedAsyncioTestCase):
    def test_missing_window_never_starts_capture(self):
        with self.assertRaises(OSError):
            start_preview(None)

    async def test_disconnect_while_sending_frame_stops_capture(self):
        control = Mock()
        control.is_finished.return_value = False
        frames = queue.Queue(maxsize=1)
        frames.put_nowait(b"frame-data")
        response = SonyPreviewResponse((object(), control, frames, threading.Event()))

        async def receive():
            self.fail("ASGI 2.4 detects disconnects via send")

        async def send(message):
            if message["type"] == "http.response.body":
                self.assertIn(b"Content-Length: 10\r\n\r\nframe-data", message["body"])
                raise OSError("browser disconnected")

        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        control.stop.assert_called_once_with()
