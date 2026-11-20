"""Google Drive queue: clips waiting in one folder, moved to another once posted."""
import io
import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
VIDEO_EXT = (".mp4", ".mov")


class Drive:
    def __init__(self, service_account_json):
        info = json.loads(service_account_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.email = info.get("client_email")

    def list_files(self, folder_id):
        files, token = [], None
        while True:
            r = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id,name,size)",
                pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            files += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return files

    def queue(self, folder_id):
        """Videos sorted by file name (name them 001_, 002_ ... to control order).
        Returns list of (video, caption_file_or_None)."""
        files = self.list_files(folder_id)
        by_name = {f["name"]: f for f in files}
        videos = sorted((f for f in files if f["name"].lower().endswith(VIDEO_EXT)),
                        key=lambda f: f["name"])
        return [(v, by_name.get(os.path.splitext(v["name"])[0] + ".txt")) for v in videos]

    def download(self, file_id, dest_path):
        req = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest_path, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=32 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()

    def read_text(self, file_id):
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.svc.files().get_media(fileId=file_id, supportsAllDrives=True))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue().decode("utf-8-sig").strip()

    def move(self, file_id, from_folder, to_folder):
        self.svc.files().update(fileId=file_id, addParents=to_folder, removeParents=from_folder,
                                supportsAllDrives=True, fields="id").execute()

    def trash(self, file_id):
        """Moves a file to Drive's Trash (Google empties it automatically after 30 days)."""
        self.svc.files().update(fileId=file_id, body={"trashed": True},
                                supportsAllDrives=True, fields="id").execute()

    def folder_name(self, folder_id):
        return self.svc.files().get(fileId=folder_id, fields="name", supportsAllDrives=True).execute()["name"]
