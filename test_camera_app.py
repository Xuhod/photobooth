import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QCamera
from PySide6.QtWidgets import QApplication

from booth import Queue
from camera_app import BoothWindow, HistoryDialog


class CameraAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["camera-test", "-platform", "offscreen"])

    def make_window(self):
        with patch("camera_app.QMediaDevices") as media:
            media.defaultVideoInput.return_value.isNull.return_value = True
            media.videoInputs.return_value = []
            window = BoothWindow()
        self.addCleanup(window.close)
        return window

    def test_countdown_delays_capture(self):
        for seconds in (0, 1, 3, 5):
            with self.subTest(seconds=seconds):
                window = self.make_window()
                window.capture = Mock()
                window.capture.isReadyForCapture.return_value = True
                window.capture.capture.return_value = 42
                window.delay.setCurrentIndex(window.delay.findData(seconds))
                window.update_take()
                window.take.click()

                if seconds:
                    self.assertTrue(window.countdown_timer.isActive())
                    self.assertFalse(window.delay.isEnabled())
                    for remaining in range(seconds, 0, -1):
                        self.assertIn(str(remaining), window.take.text())
                        self.assertEqual(window.display.currentWidget(), window.preview)
                        window.capture.capture.assert_not_called()
                        window.countdown_timer.timeout.emit()

                window.capture.capture.assert_called_once_with()
                self.assertEqual(window.capture_id, 42)
                self.assertFalse(window.countdown_timer.isActive())

    def test_camera_selection_and_hotplug(self):
        with (patch("camera_app.QMediaDevices") as media,
              patch("camera_app.QCamera") as camera_type,
              patch("camera_app.QImageCapture") as capture_type,
              patch("camera_app.QMediaCaptureSession")):
            camera_type.Error = QCamera.Error
            def build_camera(device, _parent):
                camera = Mock()
                camera.error.return_value = QCamera.Error.NoError
                camera.isActive.return_value = True
                camera.cameraDevice.return_value = device
                return camera

            camera_type.side_effect = build_camera
            capture = capture_type.return_value
            capture.isReadyForCapture.return_value = True
            missing = Mock()
            missing.isNull.return_value = True
            missing.id.return_value = b""
            webcam = Mock()
            webcam.isNull.return_value = False
            webcam.id.return_value = b"webcam"
            webcam.description.return_value = "Laptop Webcam"
            usb = Mock()
            usb.isNull.return_value = False
            usb.id.return_value = b"usb"
            usb.description.return_value = "USB Webcam"
            media.defaultVideoInput.return_value = missing
            media.videoInputs.return_value = []
            window = BoothWindow()
            self.addCleanup(window.close)
            self.assertIsNone(window.camera)

            media.defaultVideoInput.return_value = webcam
            media.videoInputs.return_value = [webcam, usb]
            window.on_devices_changed()
            window.on_devices_changed()
            self.assertTrue(window.device_change_timer.isSingleShot())
            self.assertTrue(window.device_change_timer.isActive())
            media.defaultVideoInput.assert_called_once()
            window.device_change_timer.stop()
            window.device_change_timer.timeout.emit()
            first_camera = window.camera
            first_camera.start.assert_called_once()
            self.assertTrue(window.take.isEnabled())

            window.delay.setCurrentIndex(window.delay.findData(3))
            window.take.click()
            window.camera_choice.setCurrentIndex(window.camera_choice.findData(b"usb"))
            first_camera.stop.assert_called_once()
            self.assertFalse(window.countdown_timer.isActive())
            self.assertIs(window.camera.cameraDevice(), usb)
            media.videoInputs.return_value = [webcam]
            window.refresh_devices()
            self.assertIsNone(window.camera)
            self.assertFalse(window.take.isEnabled())
            self.assertIn("disconnected", window.camera_choice.currentText())
            media.videoInputs.return_value = [usb, webcam]
            window.refresh_devices()
            self.assertIs(window.camera.cameraDevice(), usb)
            self.assertEqual(window.camera_choice.currentText(), "USB Webcam")
            window.camera_choice.setCurrentIndex(0)
            self.assertIs(window.camera.cameraDevice(), webcam)

    def test_photo_to_qr_with_retry(self):
        with TemporaryDirectory() as folder:
            window = self.make_window()
            window.template_choice.setCurrentIndex(window.template_choice.count() - 1)
            window.queue = Queue(Path(folder) / "jobs")
            drive = Mock()
            drive.reserve_id.return_value = "drive-test-id"
            drive.share.side_effect = [OSError("offline"), None]
            drive.link.return_value = "https://drive.google.com/file/d/test/view"
            drive.upload.return_value = None
            image = QImage(32, 32, QImage.Format.Format_RGB32)
            image.fill(0xFF8040)

            def wait_for_upload():
                loop = QEventLoop()
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                window.worker.finished.connect(loop.quit)
                timer.start(5000)
                loop.exec()
                self.assertIsNone(window.worker)

            with patch("camera_app.Drive.connect", return_value=drive):
                window.capture_id = 1
                window.photo_captured(1, image)
                self.assertEqual(window.display.currentWidget(), window.captured_preview)
                self.assertEqual(window.captured_preview.frame.toImage().pixelColor(0, 0), image.pixelColor(0, 0))
                wait_for_upload()
                self.assertIn("Photo saved locally", window.status.text())
                self.assertEqual(window.display.currentWidget(), window.captured_preview)
                job = window.job_id
                self.assertIsNotNone(job)
                self.assertTrue((window.queue.root / job / "photo.jpg").is_file())

                window.retry_upload()
                wait_for_upload()
                self.assertEqual(window.display.currentWidget(), window.qr)
                self.assertFalse(window.qr.pixmap().isNull())
                self.assertEqual(window.link, drive.link.return_value)
                self.assertEqual(window.queue.pending(), [])
                drive.upload.assert_called_once()
                window.reset()
                self.assertEqual(window.display.currentWidget(), window.preview)
                self.assertTrue(window.captured_preview.frame.isNull())
                history = HistoryDialog(Queue(window.queue.root).history(), window)
                self.assertEqual(history.selection.count(), 1)
                self.assertFalse(history.qr.pixmap().isNull())
                self.assertEqual(history.entries[0].link, drive.link.return_value)

    def test_three_shot_strip_is_saved_before_upload(self):
        with TemporaryDirectory() as folder:
            window = self.make_window()
            window.queue = Queue(Path(folder) / "jobs")
            window.capture = Mock()
            window.capture.isReadyForCapture.return_value = True
            window.capture.capture.side_effect = [1, 2, 3]
            window.update_take()
            drive = Mock()
            drive.reserve_id.return_value = "strip-id"
            drive.link.return_value = "https://drive.google.com/file/d/strip/view"
            drive.upload.return_value = drive.link.return_value
            colors = [(229, 33, 41), (28, 205, 79), (26, 93, 250)]
            with patch("camera_app.Drive.connect", return_value=drive):
                for index, (r, g, b) in enumerate(colors, 1):
                    self.assertIn(f"{index} of 3", window.take.text())
                    window.take.click()
                    image = QImage(32, 24, QImage.Format.Format_RGB32)
                    image.fill(0xFF000000 | r << 16 | g << 8 | b)
                    window.photo_captured(index, image)
                    if index < 3:
                        self.assertIsNone(window.worker)
                        self.assertFalse(window.template_choice.isEnabled())
                        drive.upload.assert_not_called()
                        self.assertEqual(window.display.currentWidget(), window.preview)
                        self.assertTrue(window.take.isEnabled())
                        self.assertIn(f"{index + 1} of 3", window.take.text())
                        self.assertEqual(window.capture.capture.call_count, index)
                    else:
                        self.assertEqual(window.display.currentWidget(), window.captured_preview)

                loop = QEventLoop()
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                window.worker.finished.connect(loop.quit)
                timer.start(10000)
                loop.exec()

            self.assertIsNone(window.worker)
            self.assertEqual(window.display.currentWidget(), window.qr)
            drive.upload.assert_called_once()
            strip = drive.upload.call_args.args[1]
            for index in range(1, 4):
                self.assertTrue(strip.with_name(f"shot-{index}.jpg").is_file())
            with Image.open(strip) as image:
                self.assertEqual(image.size, (2480, 3508))
                for y, color in zip((1900, 3900, 5500), colors):
                    self.assertTrue(all(abs(a - b) <= 4 for a, b in zip(image.getpixel((1225, y // 2)), color)))


if __name__ == "__main__":
    unittest.main()
