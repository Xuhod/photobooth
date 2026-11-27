import asyncio
import base64
import io
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import qrcode
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

app = FastAPI()

BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_PATH = BASE_DIR / "template" / "Template_photobooth_transparan.png"

SESSIONS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
        self.latest_frame = None
        self.running = True
        self.cap = None
        self._open_cap(index)
        threading.Thread(target=self._loop, daemon=True).start()

    def _open_cap(self, index: int):
        if self.cap is not None:
            self.cap.release()
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        self.cap = cap
        self.index = index
        self.latest_frame = None

    def switch_camera(self, index: int):
        with self.lock:
            self._open_cap(index)

    def _loop(self):
        while self.running:
            with self.lock:
                cap = self.cap
            if cap is not None and cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    with self.lock:
                        self.latest_frame = frame
                else:
                    time.sleep(0.05)
            else:
                time.sleep(0.1)

    def get_jpeg_bytes(self) -> bytes | None:
        with self.lock:
            if self.latest_frame is None:
                return None
            preview = cv2.resize(self.latest_frame, (1280, 720))
            ret, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes() if ret else None

    def save_full_res(self, save_path: Path) -> bool:
        with self.lock:
            if self.latest_frame is None:
                return False
            unmirrored = cv2.flip(self.latest_frame, 1)
            cv2.imwrite(str(save_path), unmirrored, [cv2.IMWRITE_JPEG_QUALITY, 95])
            return True


def detect_cameras(max_idx: int = 6) -> list[dict]:
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


class CameraSelectPayload(BaseModel):
    index: int


@app.get("/cameras")
def list_cameras():
    cams = detect_cameras()
    return {"current": camera.index, "cameras": cams}


@app.post("/camera/select")
def select_camera(payload: CameraSelectPayload):
    camera.switch_camera(payload.index)
    return {"status": "ok", "current": camera.index}


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def compose_template(raw_paths: list[Path], output_path: Path):
    canvas = Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 255))
    for path, (x, y, w, h) in zip(raw_paths, PHOTO_SLOTS):
        with Image.open(path) as img:
            fitted = ImageOps.fit(img.convert("RGBA"), (w, h), Image.Resampling.LANCZOS)
            canvas.paste(fitted, (x, y))

    if TEMPLATE_PATH.exists():
        with Image.open(TEMPLATE_PATH) as tpl:
            tpl_resized = tpl.convert("RGBA").resize(CANVAS_SIZE, Image.Resampling.LANCZOS)
            canvas = Image.alpha_composite(canvas, tpl_resized)

    canvas.convert("RGB").save(output_path, "JPEG", quality=92)


def make_qr_base64(url: str) -> str:
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@app.get("/")
async def kiosk_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/video_feed")
def video_feed():
    def generate():
        while True:
            frame_bytes = camera.get_jpeg_bytes()
            if frame_bytes:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                )
            time.sleep(0.033)

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/s/{session_id}", response_class=HTMLResponse)
async def guest_download_page(session_id: str):
    session_dir = SESSIONS_DIR / session_id
    if not session_dir.exists():
        return HTMLResponse("<h2>Session not found.</h2>", status_code=404)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Dies Natalis Photobooth</title>
        <style>
            body {{ font-family: system-ui, sans-serif; background: #f8fafc; color: #0f172a; text-align: center; padding: 20px; margin: 0; }}
            img.preview {{ max-width: 90%; max-height: 60vh; border-radius: 12px; box-shadow: 0 8px 24px rgba(15,23,42,0.12); }}
            .btn {{ display: block; width: 85%; max-width: 340px; margin: 16px auto; padding: 14px; background: #2563eb; color: #fff; text-decoration: none; border-radius: 10px; font-weight: 700; }}
            .raw-grid {{ display: flex; gap: 14px; justify-content: center; margin-top: 20px; }}
            .raw-grid a {{ color: #2563eb; font-weight: 600; font-size: 14px; text-decoration: none; }}
        </style>
    </head>
    <body>
        <h2>Your Photobooth Strip</h2>
        <img class="preview" src="/files/{session_id}/composite.jpg" alt="Composite">
        <a class="btn" href="/files/{session_id}/composite.jpg" download="DiesNatalis_{session_id}.jpg">Download Framed Photo</a>
        <div class="raw-grid">
            <a href="/files/{session_id}/raw_1.jpg" download="raw_1.jpg">Raw 1</a>
            <a href="/files/{session_id}/raw_2.jpg" download="raw_2.jpg">Raw 2</a>
            <a href="/files/{session_id}/raw_3.jpg" download="raw_3.jpg">Raw 3</a>
        </div>
    </body>
    </html>
    """


@app.get("/files/{session_id}/{filename}")
async def serve_session_file(session_id: str, filename: str):
    file_path = SESSIONS_DIR / session_id / filename
    if not file_path.exists():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(file_path)


@app.websocket("/ws")
async def booth_ws(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "start":
                session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
                session_dir = SESSIONS_DIR / session_id
                session_dir.mkdir(parents=True, exist_ok=True)

                raw_paths = []
                for shot_num in range(1, 4):
                    wait_sec = 6 if shot_num == 1 else 4
                    for sec in range(wait_sec, 0, -1):
                        await ws.send_json({"state": "countdown", "shot": shot_num, "seconds": sec})
                        await asyncio.sleep(1)

                    raw_file = session_dir / f"raw_{shot_num}.jpg"
                    camera.save_full_res(raw_file)
                    raw_paths.append(raw_file)

                    await ws.send_json({
                        "state": "flash",
                        "shot": shot_num,
                        "thumb": f"/files/{session_id}/raw_{shot_num}.jpg"
                    })
                    await asyncio.sleep(0.6)

                await ws.send_json({"state": "processing"})
                comp_file = session_dir / "composite.jpg"
                await asyncio.to_thread(compose_template, raw_paths, comp_file)

                local_ip = get_local_ip()
                share_url = f"http://{local_ip}:8000/s/{session_id}"
                qr_b64 = make_qr_base64(share_url)

                for sec in range(30, 0, -1):
                    await ws.send_json({
                        "state": "showcase",
                        "seconds": sec,
                        "qr": qr_b64,
                        "url": share_url,
                        "preview": f"/files/{session_id}/composite.jpg",
                    })
                    await asyncio.sleep(1)

                await ws.send_json({"state": "idle"})
    except WebSocketDisconnect:
        pass
