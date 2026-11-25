"""Windows photo booth: capture from the default camera and show a Drive QR."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from google.auth.exceptions import RefreshError
from PIL import Image
from PySide6.QtCore import QSignalBlocker, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from booth import Drive, Queue
from photo_template import CUTS, READY, render_strip


def queue_photos(queue: Queue, shots: tuple[QImage, ...], template: Path | None, fast=False) -> str:
    with TemporaryDirectory(prefix="photo-booth-") as folder:
        root = Path(folder)
        originals = []
        for index, shot in enumerate(shots, 1):
            path = root / f"shot-{index}.jpg"
            if not shot.save(str(path), "JPEG", 94):
                raise OSError(f"Could not save shot {index} as a JPEG.")
            originals.append(path)
        if template is None:
            return queue.enqueue(originals[0])
        photos = []
        for path in originals:
            with Image.open(path) as image:
                photos.append(image.copy())
        strip_path = root / "strip.jpg"
        with render_strip(template / "fast" if fast else template, photos) as strip:
            strip.save(strip_path, quality=88 if fast else 92)
        return queue.enqueue(strip_path, tuple(originals))


class UploadWorker(QThread):
    result = Signal(str, str)
    failed = Signal(str)

    def __init__(self, queue: Queue, shots=(), template=None, job_id=None, parent=None, fast=False):
        super().__init__(parent)
        self.queue = queue
        self.shots = shots
        self.template = template
        self.job_id = job_id
        self.fast = fast

    def run(self):
        try:
            if not self.job_id:
                self.job_id = queue_photos(self.queue, self.shots, self.template, self.fast)
            link, qr = self.queue.publish(self.job_id, Drive.connect())
        except RefreshError:
            self.failed.emit("Drive sign-in expired. Run `booth.py auth` and retry.")
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.result.emit(link, str(qr))


class CapturedPreview(QLabel):
    def __init__(self):
        super().__init__(alignment=Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background: black;")
        self.frame = QPixmap()

    def set_frame(self, image: QImage):
        self.frame = QPixmap.fromImage(image)
        self.fit_frame()

    def clear_frame(self):
        self.frame = QPixmap()
        self.clear()

    def fit_frame(self):
        if not self.frame.isNull():
            self.setPixmap(self.frame.scaled(
                self.contentsRect().size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_frame()


class HistoryDialog(QDialog):
    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recent QR codes")
        self.entries = entries
        self.selection = QComboBox()
        for entry in entries:
            self.selection.addItem(f"{entry.completed_at:%b %d, %H:%M:%S} · {entry.job_id[:8]}")
        self.qr = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.copy_link = QPushButton("Copy Drive Link")
        self.copy_link.clicked.connect(self.copy_selected_link)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.selection)
        layout.addWidget(self.qr, 1)
        layout.addWidget(self.copy_link)
        layout.addWidget(close)
        self.selection.currentIndexChanged.connect(self.show_entry)
        self.show_entry(self.selection.currentIndex())

    def show_entry(self, index):
        self.copy_link.setText("Copy Drive Link")
        self.copy_link.setEnabled(index >= 0)
        if index < 0:
            self.qr.setText("No uploaded photos yet.")
            return
        pixmap = QPixmap(str(self.entries[index].qr_path))
        if pixmap.isNull():
            self.qr.setText("QR image missing. You can still copy the link.")
        else:
            self.qr.setPixmap(pixmap)

    def copy_selected_link(self):
        index = self.selection.currentIndex()
        if index >= 0:
            QApplication.clipboard().setText(self.entries[index].link)
            self.copy_link.setText("Link Copied")


class BoothWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Photo Booth")
        self.resize(860, 700)
        self.setMinimumSize(430, 560)
        self.queue = Queue()
        self.worker = None
        self.capture_id = None
        self.job_id = None
        self.link = ""
        self.shots = []

        self.status = QLabel("Starting camera…")
        self.status.setWordWrap(True)
        self.preview = QVideoWidget()
        self.captured_preview = CapturedPreview()
        self.qr = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.display = QStackedWidget()
        self.display.addWidget(self.preview)
        self.display.addWidget(self.captured_preview)
        self.display.addWidget(self.qr)

        self.take = QPushButton("Take Photo")
        self.take.clicked.connect(self.take_photo)
        self.take.setEnabled(False)
        self.delay = QComboBox()
        for label, seconds in (("Off", 0), ("1s", 1), ("3s", 3), ("5s", 5)):
            self.delay.addItem(label, seconds)
        self.delay.setEnabled(False)
        self.delay_label = QLabel("Countdown:")
        self.delay_label.setBuddy(self.delay)
        self.again = QPushButton("Take Another Photo")
        self.again.clicked.connect(self.reset)
        self.again.hide()
        self.retry = QPushButton("Retry Upload")
        self.retry.clicked.connect(self.retry_upload)
        self.retry.hide()
        self.open_link = QPushButton("Open Drive Link")
        self.open_link.clicked.connect(self.open_in_browser)
        self.open_link.hide()
        self.history_button = QPushButton("History")
        self.history_button.setToolTip("Show recent photo QR codes")
        self.history_button.clicked.connect(self.show_history)
        self.camera_choice = QComboBox()
        self.camera_choice.setToolTip("Choose which camera the booth uses")
        self.camera_choice.currentIndexChanged.connect(self.select_camera)
        self.camera_label = QLabel("Camera:")
        self.camera_label.setBuddy(self.camera_choice)
        self.template_choice = QComboBox()
        for page in CUTS:
            self.template_choice.addItem(f"Canva Page {page} (3 photos)", page)
        self.template_choice.addItem("No template (1 photo)")
        self.template_choice.currentIndexChanged.connect(self.update_take)
        template_label = QLabel("Template:")
        template_label.setBuddy(self.template_choice)
        self.strip_size = QComboBox()
        self.strip_size.addItem("Fast sharing", True)
        self.strip_size.addItem("Full resolution", False)
        self.strip_size.setToolTip("Fast sharing: 2480 × 3508. Full resolution: 4961 × 7016.")
        self.start_over = QPushButton("Start Over")
        self.start_over.clicked.connect(self.reset)
        self.start_over.hide()

        layout = QVBoxLayout(self)
        camera_controls = QHBoxLayout()
        camera_controls.addWidget(self.camera_label)
        camera_controls.addWidget(self.camera_choice, 1)
        camera_controls.addWidget(template_label)
        camera_controls.addWidget(self.template_choice, 1)
        camera_controls.addWidget(self.strip_size)
        layout.addLayout(camera_controls)
        layout.addWidget(self.display, 1)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        controls.addWidget(self.delay_label)
        controls.addWidget(self.delay)
        controls.addWidget(self.take, 1)
        controls.addWidget(self.history_button)
        self.take.setMinimumHeight(40)
        layout.addLayout(controls)
        for button in (self.start_over, self.retry, self.open_link, self.again):
            button.setMinimumHeight(40)
            layout.addWidget(button)

        self.seconds_left = 0
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.countdown_timer.timeout.connect(self.countdown_tick)
        self.capture_timer = QTimer(self)
        self.capture_timer.setSingleShot(True)
        self.capture_timer.timeout.connect(self.capture_timed_out)
        self.session = QMediaCaptureSession(self)
        self.session.setVideoOutput(self.preview)
        self.camera = None
        self.capture = None
        self.camera_status = "Starting camera…"
        self.selected_camera_id = None
        self.selected_camera_name = ""
        self.available_cameras = {}
        self.default_camera = None
        self.devices = QMediaDevices(self)
        self.device_change_timer = QTimer(self)
        self.device_change_timer.setSingleShot(True)
        self.device_change_timer.setInterval(250)
        self.device_change_timer.timeout.connect(self.refresh_devices)
        self.devices.videoInputsChanged.connect(self.on_devices_changed)
        self.refresh_devices()

    def select_camera(self, index):
        if index < 0:
            return
        self.selected_camera_id = self.camera_choice.itemData(index)
        self.selected_camera_name = self.camera_choice.itemText(index)
        self.refresh_camera()

    def on_devices_changed(self):
        # Coalesce bursts of notifications from a single unplug/replug.
        self.device_change_timer.start()

    def refresh_devices(self):
        devices = QMediaDevices.videoInputs()
        self.available_cameras = {bytes(device.id()): device for device in devices}
        self.default_camera = QMediaDevices.defaultVideoInput()
        names = [device.description() for device in devices]
        with QSignalBlocker(self.camera_choice):
            self.camera_choice.clear()
            self.camera_choice.addItem("Windows default")
            for number, (device_id, device) in enumerate(self.available_cameras.items(), 1):
                name = device.description()
                label = f"{name} ({number})" if names.count(name) > 1 else name
                self.camera_choice.addItem(label, device_id)
            if self.selected_camera_id is None:
                index = 0
            else:
                index = self.camera_choice.findData(self.selected_camera_id)
                if index < 0:
                    self.camera_choice.addItem(f"{self.selected_camera_name} (disconnected)", self.selected_camera_id)
                    index = self.camera_choice.count() - 1
                else:
                    self.selected_camera_name = self.camera_choice.itemText(index)
            self.camera_choice.setCurrentIndex(index)
        self.refresh_camera()

    def set_camera_status(self, message):
        self.camera_status = message
        if self.display.currentWidget() == self.preview:
            self.status.setText(message)

    def refresh_camera(self):
        device = (self.default_camera if self.selected_camera_id is None
                  else self.available_cameras.get(self.selected_camera_id))
        if (device is not None and not device.isNull() and self.camera is not None
                and self.camera.cameraDevice().id() == device.id()
                and self.camera.error() == QCamera.Error.NoError
                and (self.camera.isActive() or self.camera_status == "Starting camera…")):
            return
        self.release_camera()
        if device is None or device.isNull():
            message = ("Selected camera disconnected. Reconnect it or choose another camera."
                       if self.selected_camera_id is not None else "No camera found. Connect or enable a camera.")
            self.set_camera_status(message)
            self.update_take()
            return
        self.set_camera_status("Starting camera…")
        camera = self.camera = QCamera(device, self)
        capture = self.capture = QImageCapture(self)
        self.session.setCamera(camera)
        self.session.setImageCapture(capture)
        # Ignore callbacks queued by a device we have since released or replaced.
        capture.readyForCaptureChanged.connect(
            lambda ready: self.camera_ready(ready) if self.capture is capture else None)
        capture.imageCaptured.connect(
            lambda request, image: self.photo_captured(request, image) if self.capture is capture else None,
            Qt.ConnectionType.QueuedConnection)
        capture.errorOccurred.connect(
            lambda request, error, message: self.capture_error(request, error, message)
            if self.capture is capture else None, Qt.ConnectionType.QueuedConnection)
        camera.errorOccurred.connect(
            lambda error, message: self.camera_error(error, message) if self.camera is camera else None)
        camera.start()
        self.camera_ready(capture.isReadyForCapture())

    def release_camera(self):
        self.stop_countdown()
        self.capture_timer.stop()
        self.capture_id = None
        camera, capture = self.camera, self.capture
        self.camera = self.capture = None
        if camera is not None:
            camera.stop()
        if capture is not None:
            self.session.setImageCapture(None)
            capture.deleteLater()
        if camera is not None:
            self.session.setCamera(None)
            camera.deleteLater()
        self.preview.videoSink().setVideoFrame(QVideoFrame())

    def update_take(self):
        ready = bool(
            self.capture and self.capture.isReadyForCapture()
            and (self.camera is None or self.camera.error() == QCamera.Error.NoError)
            and self.capture_id is None and self.worker is None
            and not self.countdown_timer.isActive()
            and self.display.currentWidget() == self.preview
        )
        self.take.setEnabled(ready)
        self.delay.setEnabled(ready)
        self.template_choice.setEnabled(
            not self.shots and self.capture_id is None and not self.countdown_timer.isActive()
            and self.worker is None and self.display.currentWidget() == self.preview
        )
        self.strip_size.setEnabled(self.template_choice.isEnabled() and self.shot_count() == 3)
        if not self.countdown_timer.isActive():
            self.take.setText(self.shutter_label())

    def shot_count(self):
        return 3 if self.template_choice.currentData() is not None else 1

    def shutter_label(self):
        return f"Take Photo {len(self.shots) + 1} of 3" if self.shot_count() == 3 else "Take Photo"

    def camera_ready(self, ready):
        self.update_take()
        if ready and self.camera_status == "Starting camera…":
            self.set_camera_status(f"Ready · {self.camera.cameraDevice().description()}")

    def take_photo(self):
        if not self.take.isEnabled():
            return
        self.seconds_left = self.delay.currentData()
        if self.seconds_left:
            self.show_countdown()
            self.countdown_timer.start()
            self.update_take()
        else:
            self.capture_photo()

    def show_countdown(self):
        self.take.setText(f"Taking photo in {self.seconds_left}…")
        self.status.setText("Get ready!")

    def countdown_tick(self):
        if not self.countdown_timer.isActive():
            return
        self.seconds_left -= 1
        if self.seconds_left > 0:
            self.show_countdown()
            return
        self.stop_countdown()
        self.capture_photo()

    def stop_countdown(self):
        self.countdown_timer.stop()
        self.seconds_left = 0
        self.take.setText(self.shutter_label())

    def capture_photo(self):
        if not self.capture.isReadyForCapture():
            self.status.setText("Camera isn't ready. Try again when the preview is ready.")
            self.update_take()
            return
        self.take.setEnabled(False)
        self.delay.setEnabled(False)
        self.template_choice.setEnabled(False)
        self.strip_size.setEnabled(False)
        self.status.setText("Taking photo…")
        request = self.capture.capture()
        if request < 0:
            self.status.setText(self.capture.errorString() or "Could not take a photo. Try again.")
            self.update_take()
            return
        self.capture_id = request
        self.capture_timer.start(8000)

    def photo_captured(self, request, image):
        if request != self.capture_id:
            return
        self.capture_id = None
        self.capture_timer.stop()
        still = image.copy()
        self.shots.append(still)
        if len(self.shots) < self.shot_count():
            self.status.setText(f"Photo {len(self.shots)} of 3 captured.")
            self.start_over.show()
            self.update_take()
        else:
            self.captured_preview.set_frame(still)
            self.display.setCurrentWidget(self.captured_preview)
            self.start_upload()

    def capture_error(self, request, _error, message):
        if request != self.capture_id:
            return
        self.capture_id = None
        self.capture_timer.stop()
        self.status.setText(message or "Could not take a photo. Try again.")
        self.update_take()

    def capture_timed_out(self):
        self.capture_id = None
        self.status.setText("The camera didn't return a photo. Try again or restart the app.")
        self.update_take()

    def camera_error(self, _error, message):
        self.stop_countdown()
        self.capture_timer.stop()
        self.capture_id = None
        self.set_camera_status(f"Camera unavailable: {message}. Check Windows camera settings or select another camera.")
        self.update_take()

    def start_upload(self):
        self.take.hide()
        self.delay.hide()
        self.delay_label.hide()
        self.retry.hide()
        self.again.hide()
        self.start_over.hide()
        page = self.template_choice.currentData()
        template = READY / f"page-{page}" if page is not None else None
        self.retry.setText("Retry Upload")
        self.status.setText("Uploading to Google Drive…" if self.job_id else
                            "Building and uploading photo strip…" if template else "Saving and uploading photo…")
        self.worker = UploadWorker(self.queue, tuple(self.shots), template, self.job_id, self,
                                   fast=bool(self.strip_size.currentData()))
        self.worker.result.connect(self.show_qr)
        self.worker.failed.connect(self.show_failure)
        self.worker.finished.connect(self.upload_finished)
        self.worker.start()

    def show_qr(self, link, qr_path):
        self.link = link
        pixmap = QPixmap(qr_path)
        if pixmap.isNull():
            self.status.setText("Photo uploaded. Could not display the QR; use Open Drive Link.")
        else:
            self.qr.setPixmap(pixmap)
            self.display.setCurrentWidget(self.qr)
            self.status.setText("Scan this QR with your phone to open the photo.")
        self.open_link.show()
        self.again.setEnabled(False)
        self.again.show()

    def show_failure(self, message):
        self.job_id = self.worker.job_id
        if self.job_id:
            self.status.setText(f"Photo saved locally. Upload failed: {message}")
            self.retry.setEnabled(False)
            self.retry.show()
        else:
            self.status.setText(f"Could not prepare the photo: {message}")
            self.retry.setText("Try Again")
            self.retry.setEnabled(False)
            self.retry.show()
        self.again.setEnabled(False)
        self.again.show()

    def upload_finished(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        self.retry.setEnabled(True)
        self.again.setEnabled(True)
        self.update_take()

    def retry_upload(self):
        if self.worker is None and (self.job_id or len(self.shots) == self.shot_count()):
            self.start_upload()

    def open_in_browser(self):
        if self.link:
            QDesktopServices.openUrl(QUrl(self.link))

    def show_history(self):
        HistoryDialog(self.queue.history(), self).exec()

    def reset(self):
        if self.worker is not None:
            return
        self.shots.clear()
        self.stop_countdown()
        self.capture_timer.stop()
        self.capture_id = None
        self.job_id = None
        self.link = ""
        self.qr.clear()
        self.captured_preview.clear_frame()
        self.display.setCurrentWidget(self.preview)
        self.open_link.hide()
        self.again.hide()
        self.retry.hide()
        self.start_over.hide()
        self.take.show()
        self.delay.show()
        self.delay_label.show()
        self.status.setText(self.camera_status)
        self.update_take()

    def closeEvent(self, event):
        if self.worker is not None:
            self.status.setText("An upload is in progress. Wait for it to finish before closing.")
            event.ignore()
            return
        self.device_change_timer.stop()
        self.release_camera()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = BoothWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
