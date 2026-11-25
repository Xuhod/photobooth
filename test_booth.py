import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from PIL import Image

from booth import Drive, FOLDER_MIME_TYPE, Queue


class DriveFolderTest(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.files = self.service.files.return_value

    def test_creates_and_caches_upload_folder(self):
        self.files.list.return_value.execute.return_value = {"files": []}
        self.files.create.return_value.execute.return_value = {"id": "folder-1"}
        drive = Drive(self.service)
        self.assertEqual(drive.upload_folder(), "folder-1")
        self.assertEqual(drive.upload_folder(), "folder-1")
        self.files.list.assert_called_once()
        self.files.create.assert_called_once_with(
            body={"name": "Photo Booth Uploads", "mimeType": FOLDER_MIME_TYPE,
                  "appProperties": {"photobooth": "uploads"}},
            fields="id",
        )

    def test_upload_uses_existing_folder(self):
        self.files.list.return_value.execute.return_value = {"files": [{"id": "folder-1"}]}
        with TemporaryDirectory() as folder:
            image = Path(folder) / "sample.jpg"
            Image.new("RGB", (32, 32)).save(image)
            Drive(self.service).upload("photo-id", image, "job-id")
        self.files.create.assert_called_once()
        body = self.files.create.call_args.kwargs["body"]
        self.assertEqual(body["parents"], ["folder-1"])
        self.assertEqual(body["name"], "photobooth-job-id.jpg")


class QueueTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = Queue(self.root / "jobs")
        self.source = self.root / "sample.jpg"
        Image.new("RGB", (32, 32), "orange").save(self.source)
        self.drive = Mock()
        self.drive.reserve_id.return_value = "test-drive-id"
        self.drive.link.return_value = "https://example.com/test-drive-id"
        self.drive.upload.return_value = None

    def test_publish_and_resume_after_restart(self):
        job = self.queue.enqueue(self.source)
        self.source.unlink()
        self.drive.share.side_effect = [OSError("connection lost"), None]
        with self.assertRaises(OSError):
            self.queue.publish(job, self.drive)
        self.assertFalse((self.queue.root / job / "qr.png").exists())

        restarted_queue = Queue(self.queue.root)
        self.assertEqual(restarted_queue.pending(), [job])
        url, qr = restarted_queue.publish(job, self.drive)
        self.assertEqual(url, self.drive.link.return_value)
        with Image.open(qr) as image:
            self.assertEqual(image.format, "PNG")
            image.verify()
        self.assertEqual(restarted_queue.pending(), [])
        self.assertEqual(restarted_queue.publish(job, self.drive), (url, qr))
        self.drive.reserve_id.assert_called_once()
        self.drive.upload.assert_called_once()
        self.assertEqual(self.drive.share.call_count, 2)

    def test_retry_reuses_reserved_drive_id(self):
        job = self.queue.enqueue(self.source)
        self.drive.upload.side_effect = [OSError("upload response lost"), None]
        with self.assertRaises(OSError):
            self.queue.publish(job, self.drive)
        self.queue.publish(job, self.drive)
        self.drive.reserve_id.assert_called_once()
        first, second = self.drive.upload.call_args_list
        self.assertEqual(first, second)

    def test_fresh_upload_reuses_link_but_rechecks_sharing_on_retry(self):
        job = self.queue.enqueue(self.source)
        self.drive.upload.return_value = "https://example.com/fresh-upload"
        self.drive.share.side_effect = [OSError("lost permission response"), None]
        with self.assertRaises(OSError):
            self.queue.publish(job, self.drive)
        self.drive.share.assert_called_once_with("test-drive-id", fresh=True)
        self.assertFalse((self.queue.root / job / "qr.png").exists())
        link, qr = self.queue.publish(job, self.drive)
        self.drive.share.assert_called_with("test-drive-id", fresh=False)
        self.drive.upload.assert_called_once()
        self.drive.link.assert_not_called()
        self.assertEqual(link, self.drive.upload.return_value)
        self.assertTrue(qr.is_file())

    def test_rejects_non_jpeg(self):
        photo = self.root / "sample.png"
        Image.new("RGB", (32, 32)).save(photo)
        with self.assertRaisesRegex(ValueError, "JPEG"):
            self.queue.enqueue(photo)
        self.assertEqual(self.queue.pending(), [])

    def test_history_shows_only_completed_jobs_newest_first(self):
        oldest, latest, pending = [self.queue.enqueue(self.source) for _ in range(3)]
        for job_id, completed_at in ((oldest, 1_700_000_000), (latest, 1_700_000_001)):
            self.queue.publish(job_id, self.drive)
            manifest = self.queue.root / job_id / "job.json"
            os.utime(manifest, (completed_at, completed_at))
        self.assertEqual(self.queue.pending(), [pending])
        self.assertEqual([entry.job_id for entry in self.queue.history()], [latest, oldest])
        self.assertEqual([entry.job_id for entry in self.queue.history(limit=1)], [latest])


if __name__ == "__main__":
    unittest.main()
