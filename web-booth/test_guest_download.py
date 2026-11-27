import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image

import guest_download as guest


class GuestDownloadTests(unittest.IsolatedAsyncioTestCase):
    session_id = "20261127_120000_" + "a" * 32
    public_base = "https://photos.stellarinium.me"

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(guest, "SESSIONS_DIR", self.root))
        self.session = self.root / self.session_id
        self.session.mkdir()
        self.strip = self.session / "composite.jpg"
        Image.new("RGB", (8, 8), "navy").save(self.strip)
        (self.session / "raw_1.jpg").write_bytes(self.strip.read_bytes())
        self.path = f"/s/{self.session_id}/strip.jpg"

    async def request(self, target):
        url = urlsplit(target)
        events = []
        request_received = False

        async def receive():
            nonlocal request_received
            if request_received:
                await asyncio.Event().wait()  # No disconnect while the response is in flight.
            request_received = True
            return {"type": "http.request"}

        async def send(event):
            events.append(event)

        await guest.app({"type": "http", "method": "GET", "scheme": "https",
                         "path": url.path, "query_string": url.query.encode(),
                         "headers": [], "http_version": "1.1"}, receive, send)
        return (events[0]["status"], dict(events[0]["headers"]),
                b"".join(event.get("body", b"") for event in events[1:]))

    async def test_local_url_does_not_publish_existing_strip(self):
        url = guest.prepare_download_url(self.session_id, None)
        self.assertEqual(url, f"http://127.0.0.1:8000{self.path}?download=true")
        self.assertFalse((self.session / guest.SHARE_MARKER).exists())
        self.assertEqual((await self.request(self.path))[0], 404)

    async def test_shared_strip_is_direct_download_and_can_be_revoked(self):
        url = guest.prepare_download_url(self.session_id, self.public_base)
        self.assertEqual(url, f"{self.public_base}{self.path}?download=true")
        status, headers, body = await self.request(url)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"image/jpeg")
        self.assertEqual(headers[b"content-disposition"],
                         b'attachment; filename="photobooth-strip-20261127-120000-aaaaaaaa.jpg"')
        self.assertEqual(headers[b"cache-control"], b"private, no-store")
        self.assertEqual(body, self.strip.read_bytes())
        self.assertEqual((await self.request(url))[0], 200)
        (self.session / guest.SHARE_MARKER).unlink()
        self.assertEqual((await self.request(url))[0], 404)
        self.assertTrue(self.strip.is_file())

    async def test_sessions_in_same_second_have_different_download_names(self):
        second_id = "20261127_120000_" + "b" * 32
        second_session = self.root / second_id
        second_session.mkdir()
        (second_session / "composite.jpg").write_bytes(self.strip.read_bytes())
        first_url = guest.prepare_download_url(self.session_id, self.public_base)
        second_url = guest.prepare_download_url(second_id, self.public_base)
        _, first_headers, _ = await self.request(first_url)
        _, second_headers, _ = await self.request(second_url)
        self.assertNotEqual(first_headers[b"content-disposition"], second_headers[b"content-disposition"])

    async def test_public_service_has_no_other_routes(self):
        guest.prepare_download_url(self.session_id, self.public_base)
        for path in ("/", "/docs", "/openapi.json", "/ws", "/video_feed",
                     f"/s/{self.session_id}", f"/s/{self.session_id}/raw_1.jpg",
                     f"/files/{self.session_id}/raw_1.jpg", "/s/invalid/strip.jpg"):
            with self.subTest(path=path):
                self.assertEqual((await self.request(path))[0], 404)
