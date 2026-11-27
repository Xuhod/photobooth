"""Stream only the Sony Remote window using Windows Graphics Capture."""

import asyncio
import queue
import threading
import time

import anyio
import cv2
from starlette.responses import StreamingResponse


def start_preview(hwnd):
    if not hwnd:
        raise OSError("Sony Remote window is unavailable.")
    from windows_capture import WindowsCapture

    capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=False)
    frames = queue.Queue(maxsize=1)
    closed = threading.Event()
    last_frame = 0.0

    @capture.event
    def on_frame_arrived(frame, _control):
        nonlocal last_frame
        now = time.monotonic()
        if now - last_frame < 1 / 20:
            return
        last_frame = now
        bgr = frame.frame_buffer[:, :, :3]
        height, width = bgr.shape[:2]
        if width > 1280:
            bgr = cv2.resize(bgr, (1280, max(1, round(height * 1280 / width))),
                             interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        try:
            frames.get_nowait()  # Prefer the latest frame over accumulating lag.
        except queue.Empty:
            pass
        frames.put_nowait(encoded.tobytes())

    @capture.event
    def on_closed():
        closed.set()

    control = capture.start_free_threaded()
    return capture, control, frames, closed


class SonyPreviewResponse(StreamingResponse):
    """Own capture for the lifetime of the HTTP response, including disconnects."""

    def __init__(self, session):
        self.session = session
        super().__init__(
            self._stream(), media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"},
        )

    async def _stream(self):
        _capture, control, frames, closed = self.session
        while not closed.is_set() and not control.is_finished():
            try:
                jpeg = await asyncio.to_thread(frames.get, True, 1)
            except queue.Empty:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii") + jpeg + b"\r\n")

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                control = self.session[1]
                if not control.is_finished():
                    await anyio.to_thread.run_sync(control.stop)
