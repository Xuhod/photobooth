import tempfile
import unittest
from pathlib import Path

from PIL import Image

from sony_remote import capture_jpeg


class SonyRemoteTest(unittest.TestCase):
    def test_preserves_only_the_new_jpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "inbox"
            folder.mkdir()
            destination = Path(temp) / "shot.jpg"
            Image.new("RGB", (16, 16)).save(folder / "old.jpg")

            def trigger():
                Image.new("RGB", (6000, 4000)).save(folder / "new.JPG")
                (folder / "partial.jpg").write_bytes(b"unfinished")

            self.assertTrue(capture_jpeg(folder, destination, trigger=trigger, timeout=3))
            self.assertEqual(destination.read_bytes(), (folder / "new.JPG").read_bytes())
            with Image.open(destination) as image:
                self.assertEqual(image.size, (6000, 4000))

    def test_times_out_without_reusing_an_old_photo(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            Image.new("RGB", (16, 16)).save(folder / "old.jpg")
            with self.assertRaisesRegex(OSError, "No new JPEG"):
                capture_jpeg(folder, folder / "shot.jpg", trigger=lambda: None, timeout=0.2)
            self.assertFalse((folder / "shot.jpg").exists())
