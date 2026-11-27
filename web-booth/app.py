import asyncio
import base64
import io
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import qrcode
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel
from guest_download import SESSIONS_DIR, prepare_download_url, router as guest_router
from sony_usb import SonyUSB
from usb_video import USBVideoCapture

if os.name == "nt":
    from comtypes import CoInitialize, CoUninitialize
    from pygrabber.dshow_graph import FilterGraph

app = FastAPI()
log = logging.getLogger(__name__)
session_lock = asyncio.Lock()
sony = SonyUSB()
sony_preview_state = "idle"

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_PATH = BASE_DIR / "template" / "Template_photobooth_transparan.png"
SESSION_ID = re.compile(r"\d{8}_\d{6}_(?:[0-9a-f]{4}|[0-9a-f]{32})")
CAMERA_BACKEND = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_V4L2
CAPTURE_SIZE = (1920, 1080)
PREVIEW_SIZE = (1280, 720)

SESSIONS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(guest_router)

CANVAS_SIZE = (1080, 1920)
PHOTO_SLOTS = [
    (48, 272, 558, 505),   # Top-left window
    (498, 722, 540, 488),  # Middle-right window
    (85, 1185, 542, 488),  # Bottom-left window
]


class SharedCamera:
    def __init__(self, index: int = 0):
        self.index = index
        self.lock = threading.Lock()
        self.frame_ready = threading.Condition(self.lock)
        self.frame_number = 0
        self.latest_frame = None
        self.running = True
        self.cap = None
        self._open_cap(index)
        threading.Thread(target=self._loop, daemon=True).start()

    def _open_cap(self, index: int):
        if self.cap is not None:
            self.cap.release()
        # Media Foundation delivers substantially more 1080p frames on the
        # built-in webcam. Keep DirectShow for the other picker indices.
        device = next((cam for cam in detect_cameras() if cam["index"] == index), None) if os.name == "nt" else None
        if device and device["label"] == f"[{index}] USB Video":
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                try:
                    fast_cap = USBVideoCapture(ffmpeg, CAPTURE_SIZE)
                    ready, frame = fast_cap.read(timeout=2)
                    if ready:
                        self.cap = fast_cap
                        self.index = index
                        self.latest_frame = frame
                        self.frame_number += 1
                        return
                    fast_cap.release()
                except OSError:
                    log.exception("Could not open USB Video in MJPEG mode")
            log.warning("USB Video MJPEG unavailable; falling back to OpenCV")

        if os.name == "nt" and index == 0:
            cap = self._configure_cap(index, cv2.CAP_MSMF)
            if (not cap.isOpened() or
                    (cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) != CAPTURE_SIZE):
                cap.release()
                cap = self._configure_cap(index, CAMERA_BACKEND)
        else:
            cap = self._configure_cap(index, CAMERA_BACKEND)
        self.cap = cap
        self.index = index
        self.latest_frame = None

    @staticmethod
    def _configure_cap(index: int, backend: int):
        cap = cv2.VideoCapture(index, backend)
        if os.name == "nt" and cap.isOpened():
            # Uncompressed 1080p can saturate a USB 2.0 capture device.
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_SIZE[1])
        if os.name == "nt" and cap.isOpened():
            cap.set(cv2.CAP_PROP_FPS, 30)
        return cap

    def switch_camera(self, index: int) -> bool:
        with self.lock:
            previous = self.index
            if index == previous and self.cap is not None and self.cap.isOpened():
                return True
            self._open_cap(index)
            if self.cap.isOpened():
                return True
            self._open_cap(previous)
            return False

    def _loop(self):
        while self.running:
            received = False
            with self.lock:
                cap = self.cap
                if cap is not None and cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        self.latest_frame = frame
                        self.frame_number += 1
                        self.frame_ready.notify_all()
                        received = True
            # Let a pending camera switch take the lock between reads.
            time.sleep(0.001 if received else 0.05)

    def get_preview_frame(self, after: int) -> tuple[int, bytes | None]:
        with self.frame_ready:
            if not self.frame_ready.wait_for(
                lambda: self.latest_frame is not None and self.frame_number != after, timeout=1
            ):
                return after, None
            frame = self.latest_frame
            number = self.frame_number
        preview = frame if frame.shape[1::-1] == PREVIEW_SIZE else cv2.resize(frame, PREVIEW_SIZE)
        ret, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return number, buf.tobytes() if ret else None

    def save_full_res(self, save_path: Path) -> bool:
        with self.lock:
            if self.latest_frame is None:
                return False
            unmirrored = cv2.flip(self.latest_frame, 1)
            return cv2.imwrite(str(save_path), unmirrored, [cv2.IMWRITE_JPEG_QUALITY, 95])


def detect_cameras(max_idx: int = 6) -> list[dict]:
    if os.name == "nt":
        # Enumerate the same DirectShow category as OpenCV, without opening devices.
        CoInitialize()
        try:
            names = FilterGraph().get_input_devices()
        finally:
            CoUninitialize()
        return [{"index": idx, "label": f"[{idx}] {name}"} for idx, name in enumerate(names)]
    available = []
    for idx in range(max_idx):
        dev_path = Path(f"/dev/video{idx}")
        if not dev_path.exists():
            continue
        # Avoid locking the active camera index during probe
        if "camera" in globals() and camera.index == idx:
            name_file = Path(f"/sys/class/video4linux/video{idx}/name")
            label = name_file.read_text().strip() if name_file.exists() else f"Camera {idx}"
            available.append({"index": idx, "label": f"[{idx}] {label}"})
            continue

        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                name_file = Path(f"/sys/class/video4linux/video{idx}/name")
                label = name_file.read_text().strip() if name_file.exists() else f"Camera {idx}"
                available.append({"index": idx, "label": f"[{idx}] {label}"})
            cap.release()
    return available


camera = SharedCamera(0)


@app.on_event("shutdown")
def release_camera():
    camera.running = False
    with camera.lock:
        if camera.cap is not None:
            camera.cap.release()


class CameraSelectPayload(BaseModel):
    index: int


@app.get("/cameras")
async def list_cameras():
    cams = await asyncio.to_thread(detect_cameras)
    try:
        connected = await asyncio.wait_for(sony.call("available"), 2)
    except (TimeoutError, ImportError, OSError):
        connected = False
    return {"current": camera.index, "cameras": cams, "sony_usb": os.name == "nt",
            "sony_connected": connected}


@app.post("/camera/select")
def select_camera(payload: CameraSelectPayload):
    if payload.index < 0 or not camera.switch_camera(payload.index):
        raise HTTPException(status_code=409, detail="Camera could not be opened.")
    return {"status": "ok", "current": camera.index}


def compose_template(raw_paths: list[Path], output_path: Path):
    canvas = Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 255))
    for path, (x, y, w, h) in zip(raw_paths, PHOTO_SLOTS):
        with Image.open(path) as img:
            fitted = ImageOps.fit(ImageOps.exif_transpose(img).convert("RGBA"), (w, h), Image.Resampling.LANCZOS)
            canvas.paste(fitted, (x, y))

    if TEMPLATE_PATH.exists():
        with Image.open(TEMPLATE_PATH) as tpl:
            tpl_resized = tpl.convert("RGBA").resize(CANVAS_SIZE, Image.Resampling.LANCZOS)
            canvas = Image.alpha_composite(canvas, tpl_resized)

    canvas.convert("RGB").save(output_path, "JPEG", quality=92)


@app.get("/")
async def kiosk_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/video_feed")
def video_feed():
    def generate():
        number = -1
        while True:
            number, frame_bytes = camera.get_preview_frame(number)
            if frame_bytes:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame_bytes)}\r\n\r\n".encode("ascii")
                    + frame_bytes + b"\r\n"
                )

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")


def require_local_sony(request: Request):
    if (os.name != "nt" or not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or request.headers.get("sec-fetch-site") not in (None, "same-origin")):
        raise HTTPException(status_code=404)


@app.get("/sony_status")
async def sony_status(request: Request):
    require_local_sony(request)
    return {"preview": sony_preview_state}


@app.get("/sony_preview")
async def sony_preview(request: Request):
    global sony_preview_state
    require_local_sony(request)
    if sony_preview_state == "connecting":
        raise HTTPException(status_code=503, detail="Sony USB is already connecting.")
    sony_preview_state = "connecting"
    try:
        first = await asyncio.wait_for(sony.call("preview"), 6)
        if not first:
            sony_preview_state = "no_frame"
            raise HTTPException(status_code=503, detail="Sony USB did not return a live view frame.")
    except asyncio.CancelledError:
        sony_preview_state = "idle"
        raise
    except (TimeoutError, ImportError, OSError, RuntimeError) as exc:
        sony_preview_state = "timeout" if isinstance(exc, TimeoutError) else "usb_error"
        log.warning("Sony USB preview unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Sony USB live view is unavailable.") from exc
    sony_preview_state = "ready"

    async def frames():
        global sony_preview_state
        jpeg = first
        misses = 0
        while misses < 10:
            if jpeg:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                       + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii") + jpeg + b"\r\n")
                misses = 0
            else:
                misses += 1
            await asyncio.sleep(0.12)
            try:
                jpeg = await sony.call("preview")
            except (OSError, RuntimeError):
                sony_preview_state = "usb_error"
                return
        sony_preview_state = "no_frame"

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"})


@app.get("/files/{session_id}/{filename}")
async def serve_session_file(session_id: str, filename: str):
    if not SESSION_ID.fullmatch(session_id) or filename not in {
        "composite.jpg", "raw_1.jpg", "raw_2.jpg", "raw_3.jpg"
    }:
        raise HTTPException(status_code=404, detail="Not found")
    file_path = SESSIONS_DIR / session_id / filename
    if not file_path.exists():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(file_path)


async def show_download_qr(ws: WebSocket, session_id: str, url: str):
    preview = f"/files/{session_id}/composite.jpg"
    qr = io.BytesIO()
    qrcode.make(url).save(qr, format="PNG")
    qr_b64 = base64.b64encode(qr.getvalue()).decode("ascii")

    for sec in range(30, 0, -1):
        await ws.send_json({"state": "showcase", "seconds": sec, "qr": qr_b64,
                            "url": url, "preview": preview})
        await asyncio.sleep(1)
    await ws.send_json({"state": "idle"})


async def run_session(ws: WebSocket, *, use_sony: bool = False):
    if use_sony and (not sony.last_frame or time.monotonic() - sony.last_frame > 3):
        await ws.send_json({"state": "sony_not_ready"})
        return
    session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    raw_paths = []
    for shot_num in range(1, 4):
        wait_sec = 6 if shot_num == 1 else 4
        for sec in range(wait_sec, 0, -1):
            await ws.send_json({"state": "countdown", "shot": shot_num, "seconds": sec})
            await asyncio.sleep(1)

        raw_file = session_dir / f"raw_{shot_num}.jpg"
        try:
            if use_sony:
                await ws.send_json({"state": "sony_transfer"})
                await sony.call("capture", raw_file)
            elif not await asyncio.to_thread(camera.save_full_res, raw_file):
                raise OSError("No camera frame could be saved.")
        except (OSError, cv2.error) as exc:
            log.warning("Capture failed (%s)", type(exc).__name__)
            await ws.send_json({"state": "capture_failed", "sony": use_sony})
            return
        raw_paths.append(raw_file)

        await ws.send_json({"state": "flash", "shot": shot_num,
                            "thumb": f"/files/{session_id}/raw_{shot_num}.jpg"})
        await asyncio.sleep(0.6)

    await ws.send_json({"state": "processing"})
    try:
        await asyncio.to_thread(compose_template, raw_paths, session_dir / "composite.jpg")
        url = prepare_download_url(session_id, os.environ.get("PHOTOBOOTH_SHARE_BASE_URL"))
    except (OSError, ValueError, HTTPException) as exc:
        log.warning("Could not prepare strip (%s)", type(exc).__name__)
        await ws.send_json({"state": "capture_failed"})
        return
    await show_download_qr(ws, session_id, url)


def sony_session_allowed(ws: WebSocket) -> bool:
    try:
        origin = urlsplit(ws.headers.get("origin", ""))
        return bool(os.name == "nt" and ws.client and ws.client.host in {"127.0.0.1", "::1"}
                    and origin.scheme in {"http", "https"}
                    and origin.hostname in {"localhost", "127.0.0.1", "::1"}
                    and origin.netloc == ws.headers.get("host"))
    except ValueError:
        return False


@app.websocket("/ws")
async def booth_ws(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("action") != "start":
                continue
            source = msg.get("source", "video")
            if source == "sony" and not sony_session_allowed(ws):
                await ws.close(code=1008)
                return
            if source not in ("video", "sony"):
                continue
            if session_lock.locked():
                await ws.send_json({"state": "busy"})
                continue
            async with session_lock:
                await run_session(ws, use_sony=source == "sony")
    except WebSocketDisconnect:
        pass
