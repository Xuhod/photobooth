"""Open the Windows USB Video capture card in explicit 1080p MJPEG mode."""

import queue
import subprocess
import threading

import numpy as np


class USBVideoCapture:
    def __init__(self, ffmpeg, size):
        width, height = size
        self.shape = (height, width, 3)
        self.frame_bytes = width * height * 3
        self.frames = queue.Queue(maxsize=1)
        self.process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "dshow",
             "-video_size", f"{width}x{height}", "-framerate", "30", "-vcodec", "mjpeg",
             "-i", "video=USB Video", "-map", "0:v:0", "-pix_fmt", "bgr24",
             "-threads:v", "1", "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.reader = threading.Thread(target=self._read_pipe, daemon=True)
        self.reader.start()

    def _read_pipe(self):
        try:
            with self.process.stdout as pipe:
                while len(data := pipe.read(self.frame_bytes)) == self.frame_bytes:
                    try:
                        self.frames.get_nowait()
                    except queue.Empty:
                        pass
                    self.frames.put_nowait(np.frombuffer(data, dtype=np.uint8).reshape(self.shape))
        except OSError:
            pass

    def read(self, timeout=0.25):
        try:
            return True, self.frames.get(timeout=timeout)
        except queue.Empty:
            return False, None

    def isOpened(self):
        return self.process.poll() is None

    def release(self):
        if self.isOpened():
            self.process.kill()
        self.process.wait(timeout=2)
        self.reader.join(timeout=1)
