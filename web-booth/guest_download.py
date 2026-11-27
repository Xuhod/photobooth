"""Guest-facing photo downloads; no camera, raw photos, or kiosk controls."""

import re
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"
GUEST_ID = re.compile(r"[0-9]{8}_[0-9]{6}_[0-9a-f]{32}")
SHARE_MARKER = ".guest-share"
router = APIRouter()
HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff"}


def get_strip(session_id: str, *, shared_only: bool = False) -> Path:
    if not GUEST_ID.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Photo not found")
    path = SESSIONS_DIR / session_id / "composite.jpg"
    if (not path.is_file() or path.resolve() != SESSIONS_DIR.resolve() / session_id / "composite.jpg"
            or (shared_only and not path.with_name(SHARE_MARKER).is_file())):
        raise HTTPException(status_code=404, detail="Photo not found")
    return path


def prepare_download_url(session_id: str, share_base_url: str | None) -> str:
    strip = get_strip(session_id)
    if share_base_url:
        strip.with_name(SHARE_MARKER).touch()
    base_url = share_base_url or "http://127.0.0.1:8000"
    return f"{base_url.rstrip('/')}/s/{session_id}/strip.jpg?download=true"


def download_filename(session_id: str) -> str:
    date, time, suffix = session_id.split("_")
    return f"photobooth-strip-{date}-{time}-{suffix[:8]}.jpg"


@router.get("/s/{session_id}", response_class=HTMLResponse)
def guest_page(session_id: str):
    get_strip(session_id)
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Photobooth Strip</title>
<style>body{{font:1rem system-ui;text-align:center;margin:1rem}}
img{{max-width:100%;max-height:70vh}}a{{display:block;padding:1rem}}</style>
<h1>Your Photobooth Strip</h1>
<img src="/s/{session_id}/strip.jpg" alt="Your photobooth strip">
<a href="/s/{session_id}/strip.jpg?download=true">Download photo</a></html>""", headers=HEADERS)


@router.get("/s/{session_id}/strip.jpg")
def guest_strip(session_id: str, download: bool = False):
    return FileResponse(get_strip(session_id), media_type="image/jpeg", headers=HEADERS,
                        filename=download_filename(session_id) if download else None)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.router.redirect_slashes = False


@app.get("/s/{session_id}/strip.jpg", response_class=FileResponse)
def public_strip(session_id: str):
    return FileResponse(get_strip(session_id, shared_only=True), media_type="image/jpeg",
                        headers=HEADERS, filename=download_filename(session_id))
