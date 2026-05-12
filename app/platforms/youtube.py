"""
app/platforms/youtube.py — YouTube Shorts via YouTube Data API v3.

Production hardening applied:
  - asyncio.get_event_loop() → asyncio.get_running_loop() (Python 3.10+
    deprecates get_event_loop() when there is already a running loop).
  - OAuth token stored as JSON instead of pickle.
    Pickle is a binary serialisation format — it is not human-readable,
    cannot be inspected or rotated easily, and carries deserialization
    security risks. JSON is the format google-auth itself uses natively
    via Credentials.to_json() / Credentials.from_authorized_user_info().
  - Explicit encoding on all file I/O.
  - Token file written atomically (temp → rename) to avoid corruption on crash.

Setup (takes ~10 minutes):
1. console.cloud.google.com → Enable "YouTube Data API v3"
2. APIs & Services > Credentials → Create OAuth 2.0 Client ID (Desktop app)
3. Download the client_secret JSON → set YOUTUBE_CLIENT_SECRETS_FILE in .env
4. Run once: python cli.py youtube-auth  (opens browser, saves token)

Required env vars:
  YOUTUBE_CLIENT_SECRETS_FILE   path to downloaded client_secret JSON
  YOUTUBE_TOKEN_FILE            path where token is saved (default: youtube_token.json)
  YOUTUBE_CHANNEL_ID            optional, for logging
"""
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MAX_TITLE_LENGTH       = 100
MAX_DESCRIPTION_LENGTH = 5000


class YouTubePlatform(BasePlatform):

    @property
    def name(self) -> str:
        return "YouTube Shorts"

    @property
    def key(self) -> str:
        return "youtube"

    def is_configured(self) -> bool:
        secrets_file = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "")
        token_file   = os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json")
        return (
            bool(secrets_file)
            and Path(secrets_file).exists()
            and Path(token_file).exists()
        )

    def _get_credentials(self):
        """
        Load OAuth credentials from the JSON token file, refreshing if expired.
        Token is stored as JSON (google-auth native format) — not pickle.
        """
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        token_file = os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json")
        creds = None

        if Path(token_file).exists():
            info = json.loads(Path(token_file).read_text(encoding="utf-8"))
            creds = Credentials.from_authorized_user_info(info, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _save_token(creds, token_file)
            else:
                raise RuntimeError(
                    "YouTube token is missing or expired. "
                    "Run: python cli.py youtube-auth to re-authenticate."
                )
        return creds

    def _build_service(self):
        from googleapiclient.discovery import build
        creds = self._get_credentials()
        return build("youtube", "v3", credentials=creds)

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        """Upload a clip to YouTube as a Short (< 60 s + 9:16 = auto Short)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._blocking_upload(clip_path, post_text, clip_id, title),
        )

    def _blocking_upload(self, clip_path: str, post_text: str, clip_id: str, title: str) -> dict:
        from googleapiclient.http import MediaFileUpload

        youtube = self._build_service()

        lines       = post_text.strip().split("\n")
        video_title = (title or lines[0])[:MAX_TITLE_LENGTH]
        description = post_text[:MAX_DESCRIPTION_LENGTH]

        if "#Shorts" not in description and "#shorts" not in description:
            description += "\n\n#Shorts"

        body = {
            "snippet": {
                "title":       video_title,
                "description": description,
                "categoryId":  "22",   # People & Blogs
                "tags":        ["Shorts", "startup", "founder", "talentvisa"],
            },
            "status": {
                "privacyStatus":           "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            clip_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=4 * 1024 * 1024,  # 4 MB chunks
        )

        logger.info("[YouTube] Uploading clip %s...", clip_id)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("[YouTube] Upload progress: %d%%", int(status.progress() * 100))

        video_id  = response.get("id")
        video_url = f"https://www.youtube.com/shorts/{video_id}"
        logger.info("[YouTube] Posted clip %s — video_id: %s — %s", clip_id, video_id, video_url)

        return {"publish_id": video_id, "status": "published", "url": video_url}


# ── Token helpers ──────────────────────────────────────────────────────────────

def _save_token(creds, token_file: str) -> None:
    """Write credentials to JSON atomically (temp → rename)."""
    token_data = creds.to_json()
    dir_ = Path(token_file).parent
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token_data)
        os.replace(tmp, token_file)
    except Exception:
        os.unlink(tmp)
        raise


def run_youtube_auth():
    """
    One-time YouTube OAuth flow.
    Opens a browser, saves the token as JSON (not pickle).
    Called via: python cli.py youtube-auth
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets_file = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE")
    if not secrets_file or not Path(secrets_file).exists():
        print(
            "\n[YouTube Auth] ERROR: YOUTUBE_CLIENT_SECRETS_FILE not set or not found.\n"
            "  Download your client_secret JSON from Google Cloud Console\n"
            "  and set YOUTUBE_CLIENT_SECRETS_FILE=/path/to/client_secret.json in .env\n"
        )
        return

    token_file = os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json")

    flow  = InstalledAppFlow.from_client_secrets_file(secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)

    _save_token(creds, token_file)

    print(f"\n[YouTube Auth] Authentication successful!")
    print(f"  Token saved to: {token_file}  (JSON format)")
    print(f"  You can now post to YouTube via the dashboard.\n")
