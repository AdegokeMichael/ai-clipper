#!/usr/bin/env python3
"""
scripts/gdrive_watch.py

Google Drive folder watcher using a Service Account.
Replaces rclone + gdrive_sync.sh entirely.

WHY SERVICE ACCOUNT INSTEAD OF RCLONE
───────────────────────────────────────
rclone uses your personal Google OAuth token which expires every few hours/days
and requires you to open a browser and re-authenticate. A service account uses
a JSON key file that never expires unless you deliberately revoke it in Google
Cloud Console. Set it up once, works forever.

HOW IT WORKS
─────────────
1. Polls a shared Google Drive folder every GDRIVE_SYNC_INTERVAL seconds
2. Detects new video files (MP4, MOV, AVI, MKV, WEBM)
3. Downloads them to the local WATCH_FOLDER
4. Moves processed files to a 'Processed/' subfolder on Drive
5. Tracks already-seen files in logs/gdrive_seen.txt to avoid duplicates
6. Logs all activity to logs/gdrive_sync.log

SETUP (one time)
─────────────────
1. Google Cloud Console → IAM & Admin → Service Accounts → Create
   Name: brand-bade-drive-sync
   Download JSON key → save as gdrive_service_account.json in project root

2. Enable Google Drive API:
   Google Cloud Console → APIs & Services → Library → Google Drive API → Enable

3. Share your Drive folder with the service account email:
   Open Google Drive → right-click folder → Share →
   paste the client_email from the JSON file → Editor access

4. Get the folder ID from the Drive URL:
   drive.google.com/drive/folders/THIS_IS_THE_FOLDER_ID

5. Add to .env:
   GDRIVE_SERVICE_ACCOUNT_FILE=./gdrive_service_account.json
   GDRIVE_FOLDER_ID=your_folder_id_here
   GDRIVE_SYNC_INTERVAL=30
   WATCH_FOLDER=./gdrive_inbox

6. Run as a service:
   sudo cp scripts/gdrive-sync.service /etc/systemd/system/gdrive-sync.service
   (edit User and WorkingDirectory in the service file first)
   sudo systemctl daemon-reload
   sudo systemctl enable gdrive-sync
   sudo systemctl start gdrive-sync

DEPENDENCIES (already in requirements.txt)
────────────────────────────────────────────
google-api-python-client
google-auth
google-auth-httplib2
"""
import os
import io
import sys
import time
import logging
import json
from pathlib import Path
from datetime import datetime

# ── Load .env before anything else ────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── Config from environment ────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = os.getenv("GDRIVE_SERVICE_ACCOUNT_FILE", "./gdrive_service_account.json")
FOLDER_ID            = os.getenv("GDRIVE_FOLDER_ID", "")
WATCH_FOLDER         = Path(os.getenv("WATCH_FOLDER", "./gdrive_inbox"))
SYNC_INTERVAL        = int(os.getenv("GDRIVE_SYNC_INTERVAL", "30"))
LOG_DIR              = Path("logs")
LOG_FILE             = LOG_DIR / "gdrive_sync.log"
SEEN_FILE            = LOG_DIR / "gdrive_seen.txt"

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
PROCESSED_FOLDER_NAME = "Processed"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(exist_ok=True)
WATCH_FOLDER.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GDrive] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger(__name__)


# ── Google Drive client ────────────────────────────────────────────────────────

def build_drive_service():
    """
    Build authenticated Google Drive API client using service account.
    The service account key never expires.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_file = Path(SERVICE_ACCOUNT_FILE)
    if not sa_file.exists():
        log.error(
            f"Service account file not found: {sa_file.resolve()}\n"
            "Follow the setup instructions at the top of this file."
        )
        sys.exit(1)

    credentials = service_account.Credentials.from_service_account_file(
        str(sa_file),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    log.info(f"Authenticated via service account: {sa_file}")
    return service


# ── Seen file helpers ──────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(SEEN_FILE.read_text().splitlines())
    return set()


def mark_seen(file_id: str):
    with open(SEEN_FILE, "a") as f:
        f.write(file_id + "\n")


# ── Drive operations ───────────────────────────────────────────────────────────

def list_videos(service, folder_id: str) -> list[dict]:
    """List all video files in the Drive folder that haven't been processed."""
    ext_filters = " or ".join(
        f"name contains '{ext}'" for ext in SUPPORTED_EXTENSIONS
    )
    query = (
        f"'{folder_id}' in parents "
        f"and trashed = false "
        f"and mimeType contains 'video'"
    )
    result = service.files().list(
        q=query,
        fields="files(id, name, size, mimeType, modifiedTime)",
        orderBy="modifiedTime",
        pageSize=50,
    ).execute()
    return result.get("files", [])


def get_or_create_processed_folder(service, parent_folder_id: str) -> str:
    """Get (or create) the 'Processed' subfolder inside the watched folder."""
    query = (
        f"'{parent_folder_id}' in parents "
        f"and name = '{PROCESSED_FOLDER_NAME}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    result = service.files().list(q=query, fields="files(id, name)").execute()
    files = result.get("files", [])

    if files:
        return files[0]["id"]

    # Create it
    folder_meta = {
        "name":     PROCESSED_FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder",
        "parents":  [parent_folder_id],
    }
    folder = service.files().create(body=folder_meta, fields="id").execute()
    log.info(f"Created 'Processed/' folder in Drive")
    return folder["id"]


def download_file(service, file_id: str, filename: str, dest_path: Path):
    """Download a file from Drive to a local path."""
    from googleapiclient.http import MediaIoBaseDownload

    request  = service.files().get_media(fileId=file_id)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                log.info(f"  Downloading {filename}: {pct}%")


def move_to_processed(service, file_id: str, filename: str, processed_folder_id: str):
    """Move a file to the Processed/ subfolder on Drive."""
    # Remove from current parent, add to processed folder
    file_meta = service.files().get(fileId=file_id, fields="parents").execute()
    current_parents = ",".join(file_meta["parents"])

    service.files().update(
        fileId=file_id,
        addParents=processed_folder_id,
        removeParents=current_parents,
        fields="id, parents",
    ).execute()
    log.info(f"  Moved to Processed/ on Drive: {filename}")


# ── Main sync loop ─────────────────────────────────────────────────────────────

def run():
    if not FOLDER_ID:
        log.error(
            "GDRIVE_FOLDER_ID is not set in .env\n"
            "Get it from your Drive folder URL: drive.google.com/drive/folders/FOLDER_ID_HERE"
        )
        sys.exit(1)

    log.info("━━━ Google Drive watcher started ━━━")
    log.info(f"Folder ID : {FOLDER_ID}")
    log.info(f"Local dir : {WATCH_FOLDER.resolve()}")
    log.info(f"Interval  : {SYNC_INTERVAL}s")
    log.info(f"Auth      : service account (never expires)")

    service = build_drive_service()

    # Pre-fetch or create the Processed/ folder
    processed_folder_id = get_or_create_processed_folder(service, FOLDER_ID)

    while True:
        try:
            seen = load_seen()
            videos = list_videos(service, FOLDER_ID)

            new_videos = [v for v in videos if v["id"] not in seen]

            if new_videos:
                log.info(f"Found {len(new_videos)} new video(s) in Drive")

            for video in new_videos:
                file_id   = video["id"]
                filename  = video["name"]
                dest_path = WATCH_FOLDER / filename

                log.info(f"New video detected: {filename}")

                try:
                    # Download to local watch folder
                    download_file(service, file_id, filename, dest_path)
                    log.info(f"Downloaded: {filename} → {dest_path}")

                    # Mark as seen immediately after download
                    mark_seen(file_id)

                    # Move original to Processed/ on Drive (keeps Drive folder clean)
                    move_to_processed(service, file_id, filename, processed_folder_id)

                    log.info(f"✓ {filename} ready — pipeline will trigger automatically")

                except Exception as e:
                    log.error(f"Failed to process {filename}: {e}")
                    # Don't mark as seen so it retries next cycle

        except Exception as e:
            log.error(f"Sync cycle error: {e}")

        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    run()
