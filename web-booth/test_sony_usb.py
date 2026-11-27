import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from PIL import Image

from sony_usb import LIVE_OBJECT, STILL_OBJECT, SonyCamera, _objects_ready, _packet


class SonyUSBTest(unittest.TestCase):
    @patch("sony_usb.time.sleep")
    def test_handshake_retries_empty_descriptor_only(self, _sleep):
        camera = SonyCamera(None)
        camera._command = Mock(side_effect=[b"", b"", bytes(8), b"\xc8\x00" + bytes(8), b""])
        camera.connect()
        self.assertEqual(camera._command.call_args_list, [
            call(0x9201, 1, 0, 0), call(0x9201, 2, 0, 0),
            call(0x9202, 200), call(0x9202, 200), call(0x9201, 3, 0, 0),
        ])

    def test_wia_header_keeps_parameters_and_data_separate(self):
        packet = _packet(0x9207, (0xD2C1,), 4, b"\x02\x00")
        self.assertEqual(struct.unpack("<H9I", packet[:38]),
                         (0x9207, 0, 0, 0xD2C1, 0, 0, 0, 0, 1, 4))
        self.assertEqual(packet[38:], b"\x02\x00")

    def test_capture_status_uses_current_not_default(self):
        props = struct.pack("<IIHHBBHHB", 1, 0, 0xD215, 4, 1, 1, 0, 0x8001, 0)
        self.assertEqual(_objects_ready(props), 0x8001)
        with self.assertRaises(OSError):
            _objects_ready(props[:-3])

    def test_preview_honors_sony_jpeg_offset(self):
        camera = SonyCamera(None)
        jpeg = b"\xff\xd8preview\xff\xd9"
        camera._object = Mock(return_value=struct.pack("<I", 8) + b"\xff\xd8xx" + jpeg)
        self.assertEqual(camera.preview(), jpeg)
        camera._object.assert_called_once_with(LIVE_OBJECT)

    @patch("sony_usb.time.sleep")
    def test_shot_only_downloads_when_ready_and_preserves_original(self, _sleep):
        photo = io.BytesIO()
        Image.new("RGB", (6000, 4000)).save(photo, "JPEG")
        camera = SonyCamera(None)
        camera._ready = Mock(side_effect=(0, 1, 0x8001))
        camera._shutter = Mock()
        camera._object = Mock(return_value=photo.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "shot.jpg"
            camera.capture(destination)
            self.assertEqual(destination.read_bytes(), photo.getvalue())
        camera._object.assert_called_once_with(STILL_OBJECT)
        self.assertEqual(camera._ready.call_count, 3)
        self.assertEqual(camera._shutter.call_args_list,
                         [call(0xD2C1, 2), call(0xD2C2, 2), call(0xD2C2, 1), call(0xD2C1, 1)])

    def test_pending_object_prevents_shutter(self):
        camera = SonyCamera(None)
        camera._ready = Mock(return_value=1)
        camera._shutter = Mock()
        with self.assertRaises(OSError):
            camera.capture(None)
        camera._shutter.assert_not_called()
