# photobooth-dies-natalis
A repository of Workshop Electro Polinema's 31 Dies Natalis official Photobooth Programs initiated by IT Division in the Organization

## Camera source (Windows)

For v1, select **USB Video** in the kiosk camera picker. With FFmpeg on `PATH`, the kiosk opens this source as 1920×1080 MJPEG at 30 FPS and renders a 1280×720 preview.

**Sony α7 II (USB)** is experimental and not verified for event use. It requires a USB-connected `ILCE-7M2` in **PC Remote**, with single-shot drive and **Large JPEG** enabled. This mode talks to the camera through Windows Image Acquisition, without Imaging Edge or a separate intake folder.

USB detection alone does not indicate readiness. The kiosk requires a recent Sony live-view frame before accepting a session. Each shot waits for a new camera JPEG and keeps the original bytes; the template is rendered separately. If direct USB fails, the booth reports it instead of falling back to the webcam or a preview frame. Use the reload button in the camera picker to retry.

## Guest downloads (no cloud upload)

The kiosk runs on `127.0.0.1:8000` and saves photos under `sessions/`. With no configuration, the QR downloads locally and does not mark strips for public sharing. For mobile data, use a named Cloudflare Tunnel on `photos.stellarinium.me`. Once the domain is Active, publish only the route to `http://127.0.0.1:8001`, never the kiosk on port 8000. Do not enable a guest login or open router ports. Keep the tunnel credential outside the repository. Configure Cloudflare to bypass caching for this hostname; the download service also sends `Cache-Control: private, no-store`.

From `web-booth` in PowerShell, using the repo's `.venv`:

```powershell
# Guest download service (loopback only):
..\.venv\Scripts\python.exe -m uvicorn guest_download:app --host 127.0.0.1 --port 8001 --no-proxy-headers --no-access-log

# In another terminal, after confirming a dummy download through the tunnel:
$env:PHOTOBOOTH_SHARE_BASE_URL = 'https://photos.stellarinium.me'
..\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
```

`start_on_windows.bat` defaults to the public URL for the kiosk. It does not start the guest service or tunnel; both must already be running for guest downloads. For a local-only kiosk, run the kiosk command above without `PHOTOBOOTH_SHARE_BASE_URL` set.

Only new strips completed while the public base URL is configured are marked for sharing. The guest service serves only the finished JPEG to anyone with its unlisted link. To revoke a link without deleting the local photo, remove `.guest-share` from that session folder. The PC, service and tunnel must be online and the domain registered for old links to work; the **30-second** countdown only hides the QR.
