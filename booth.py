"""Local photo queue and Google Drive uploader for one booth worker."""

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import google_auth_httplib2
import httplib2
import qrcode
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from oauthlib.oauth2 import OAuth2Error
from PIL import Image


ROOT = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
UPLOAD_FOLDER_NAME = "Photo Booth Uploads"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def write_text(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def authorize() -> None:
    config = ROOT / "credentials.json"
    if not config.is_file():
        raise ValueError("Put the Google Desktop OAuth client JSON next to booth.py as credentials.json.")
    flow = InstalledAppFlow.from_client_secrets_file(str(config), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=180, prompt="consent")
    write_text(ROOT / "token.json", creds.to_json())


class Drive:
    def __init__(self, service):
        self.service = service
        self._folder_id = None

    @classmethod
    def connect(cls):
        token = ROOT / "token.json"
        if not token.is_file():
            raise ValueError("Run 'python booth.py auth' to sign in first.")
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        return cls(build("drive", "v3", http=http))

    def reserve_id(self) -> str:
        return self.service.files().generateIds(count=1).execute(num_retries=3)["ids"][0]

    def upload_folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        files = self.service.files()
        page_token = None
        while True:
            result = files.list(
                q=f"trashed = false and mimeType = '{FOLDER_MIME_TYPE}' "
                  "and appProperties has { key='photobooth' and value='uploads' }",
                spaces="drive", orderBy="createdTime", fields="nextPageToken,files(id)",
                pageToken=page_token,
            ).execute(num_retries=3)
            if result.get("files"):
                self._folder_id = result["files"][0]["id"]
                return self._folder_id
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        folder = files.create(
            body={"name": UPLOAD_FOLDER_NAME, "mimeType": FOLDER_MIME_TYPE,
                  "appProperties": {"photobooth": "uploads"}},
            fields="id",
        ).execute()
        self._folder_id = folder["id"]
        return self._folder_id

    def upload(self, file_id: str, photo: Path, job_id: str) -> str | None:
        folder_id = self.upload_folder()
        # A create retried with the same pre-generated ID returns 409 if the first one succeeded.
        with photo.open("rb") as stream:
            try:
                result = self.service.files().create(
                    body={"id": file_id, "name": f"photobooth-{job_id}.jpg", "mimeType": "image/jpeg",
                          "parents": [folder_id]},
                    media_body=MediaIoBaseUpload(stream, mimetype="image/jpeg"),
                    fields="id,webViewLink",
                ).execute(num_retries=3)
                return result.get("webViewLink")
            except HttpError as exc:
                if exc.resp.status != 409:
                    raise
                existing = self.service.files().get(
                    fileId=file_id, fields="name,trashed"
                ).execute(num_retries=3)
                if existing.get("trashed") or existing.get("name") != f"photobooth-{job_id}.jpg":
                    raise ValueError("The existing Drive file is trashed or does not match this job.") from exc
                return None

    def share(self, file_id: str, *, fresh=False) -> None:
        permissions = self.service.permissions()
        if not fresh:
            page_token = None
            while True:
                result = permissions.list(
                    fileId=file_id, fields="nextPageToken,permissions(id,type,role,allowFileDiscovery)",
                    pageToken=page_token,
                ).execute(num_retries=3)
                for p in result.get("permissions", []):
                    if p["type"] == "anyone":
                        if p["role"] != "reader" or p.get("allowFileDiscovery", False):
                            raise ValueError("Existing public sharing is not restricted to link-only viewing.")
                        return
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        permissions.create(
            fileId=file_id, body={"type": "anyone", "role": "reader", "allowFileDiscovery": False},
        ).execute()

    def link(self, file_id: str) -> str:
        data = self.service.files().get(fileId=file_id, fields="webViewLink,trashed").execute(num_retries=3)
        if data.get("trashed") or not data.get("webViewLink"):
            raise ValueError("Drive did not return a preview link for this file.")
        return data["webViewLink"]


@dataclass(frozen=True)
class HistoryEntry:
    job_id: str
    completed_at: datetime
    link: str
    qr_path: Path


class Queue:
    def __init__(self, root: Path = ROOT / "jobs"):
        self.root = root

    def enqueue(self, source: Path, originals: tuple[Path, ...] = ()) -> str:
        job_id = uuid4().hex
        folder = self.root / job_id
        folder.mkdir(parents=True)
        try:
            photo = folder / "photo.jpg"
            shutil.copyfile(source, photo)
            with Image.open(photo) as image:
                if image.format != "JPEG":
                    raise ValueError("Expected a finished JPEG image.")
                image.load()
            for index, original in enumerate(originals, 1):
                shutil.copyfile(original, folder / f"shot-{index}.jpg")
            write_text(folder / "job.json", "{}")
        except Exception:
            shutil.rmtree(folder)
            raise
        return job_id

    def pending(self) -> list[str]:
        return sorted(
            p.parent.name for p in self.root.glob("*/job.json")
            if not json.loads(p.read_text(encoding="utf-8")).get("done")
        )

    def history(self, limit: int = 20) -> list[HistoryEntry]:
        entries = []
        for manifest in self.root.glob("*/job.json"):
            try:
                state = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("done") is not True:
                    continue
                link = state.get("link")
                if not isinstance(link, str) or not link:
                    continue
                completed = datetime.fromtimestamp(manifest.stat().st_mtime)
            except (OSError, ValueError):
                continue
            entries.append(HistoryEntry(manifest.parent.name, completed, link,
                                        manifest.with_name("qr.png")))
        return sorted(entries, key=lambda entry: entry.completed_at, reverse=True)[:limit]

    def publish(self, job_id: str, drive: Drive) -> tuple[str, Path]:
        folder = self.root / UUID(job_id).hex
        manifest = folder / "job.json"
        state = json.loads(manifest.read_text(encoding="utf-8"))
        qr_path = folder / "qr.png"

        def save():
            write_text(manifest, json.dumps(state))

        if not state.get("file_id"):
            state["file_id"] = drive.reserve_id()
            save()
        file_id = state["file_id"]
        fresh = False
        if not state.get("uploaded"):
            link = drive.upload(file_id, folder / "photo.jpg", folder.name)
            if link:
                state["link"] = link
                fresh = True
            state["uploaded"] = True
            save()
        if not state.get("shared"):
            drive.share(file_id, fresh=fresh)
            state["shared"] = True
            save()
        if not state.get("link"):
            state["link"] = drive.link(file_id)
            save()
        if not state.get("done") or not qr_path.is_file():
            temp = folder / "qr.tmp.png"
            qrcode.make(state["link"]).save(temp)
            temp.replace(qr_path)
            state["done"] = True
            save()
        return state["link"], qr_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="sign in to Google Drive as the booth operator")
    send = commands.add_parser("send", help="queue and publish a finished JPEG")
    send.add_argument("photo", type=Path)
    resume = commands.add_parser("resume", help="retry one job or all pending jobs")
    resume.add_argument("job_id", nargs="?")
    args = parser.parse_args()
    queue = Queue()

    try:
        if args.command == "auth":
            authorize()
            print("Drive sign-in saved locally.")
            return 0
        if args.command == "send":
            job_id = queue.enqueue(args.photo)
            print(f"Saved locally. Job ID: {job_id}", flush=True)
            job_ids = [job_id]
        else:
            job_ids = [args.job_id] if args.job_id else queue.pending()
        if not job_ids:
            print("No pending jobs.")
            return 0
        drive = Drive.connect()
        failed = False
        for job_id in job_ids:
            try:
                link, qr = queue.publish(job_id, drive)
                print(f"Job {job_id}\nDrive link: {link}\nQR: {qr}")
            except (OSError, ValueError, HttpError, httplib2.HttpLib2Error) as exc:
                print(f"Job {job_id}: {exc}", file=sys.stderr)
                failed = True
        return int(failed)
    except RefreshError:
        print("Google sign-in failed. Run 'python booth.py auth', then 'python booth.py resume'.", file=sys.stderr)
    except (OSError, ValueError, HttpError, httplib2.HttpLib2Error, OAuth2Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
