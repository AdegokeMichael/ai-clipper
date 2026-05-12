"""
app/platforms/uploadpost.py — Upload-Post unified posting API.

Production hardening applied:
  - Video file opened with a proper binary context manager — handle always
    closed; no encoding issue on Windows or Linux.
  - File streamed directly to httpx rather than read fully into memory —
    safe for large clips.
  - Explicit timeout.
  - Validation errors surface a clear message instead of a bare KeyError.

Required env vars:
  UPLOADPOST_API_KEY    Your Upload-Post API key
  UPLOADPOST_PROFILE    Your profile name in the Upload-Post dashboard
"""
import logging
import os

import httpx
from pathlib import Path

from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

UPLOADPOST_API = "https://api.upload-post.com/api"
_REQUEST_TIMEOUT = 300   # seconds — generous for large video uploads

_PLATFORM_MAP = {
    "tiktok_uploadpost":    "tiktok",
    "instagram_uploadpost": "instagram",
    "facebook_uploadpost":  "facebook",
    "youtube_uploadpost":   "youtube",
}

_DISPLAY_NAMES = {
    "tiktok_uploadpost":    "TikTok (Upload-Post)",
    "instagram_uploadpost": "Instagram (Upload-Post)",
    "facebook_uploadpost":  "Facebook (Upload-Post)",
    "youtube_uploadpost":   "YouTube (Upload-Post)",
}


class UploadPostPlatform(BasePlatform):

    def __init__(self, service_key: str):
        self._service_key  = service_key
        self._up_platform  = _PLATFORM_MAP[service_key]

    @property
    def name(self) -> str:
        return _DISPLAY_NAMES[self._service_key]

    @property
    def key(self) -> str:
        return self._service_key

    def is_configured(self) -> bool:
        api_key = os.getenv("UPLOADPOST_API_KEY", "")
        profile = os.getenv("UPLOADPOST_PROFILE", "")
        return bool(
            api_key and not api_key.startswith("your_")
            and profile and not profile.startswith("your_")
        )

    def _api_key(self) -> str:
        key = os.getenv("UPLOADPOST_API_KEY", "")
        if not key:
            raise ValueError(
                "UPLOADPOST_API_KEY is not set. "
                "Get your key at https://upload-post.com → Dashboard → API Keys."
            )
        return key

    def _profile(self) -> str:
        p = os.getenv("UPLOADPOST_PROFILE", "")
        if not p:
            raise ValueError(
                "UPLOADPOST_PROFILE is not set. "
                "Use the profile name you assigned when connecting accounts in Upload-Post."
            )
        return p

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        """
        Upload a clip to Upload-Post which posts it to the target platform.
        The video is streamed from disk — not loaded fully into memory.
        """
        api_key    = self._api_key()
        profile    = self._profile()
        caption    = post_text[:2200]
        post_title = (title or caption.split("\n")[0])[:100]

        logger.info(
            "[Upload-Post] Posting clip %s to %s (profile: %s)",
            clip_id, self.name, profile,
        )

        form_data = {
            "user":       profile,
            "platform[]": self._up_platform,
            "title":      caption,
        }

        if self._up_platform == "youtube":
            form_data["title"]         = post_title
            form_data["youtube_title"] = post_title
        elif self._up_platform == "tiktok":
            form_data["tiktok_title"]  = caption[:2200]

        # Stream the file from disk rather than reading it all into memory
        with open(clip_path, "rb") as video_file:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"{UPLOADPOST_API}/upload",
                    headers={"Authorization": f"Apikey {api_key}"},
                    data=form_data,
                    files={"video": (Path(clip_path).name, video_file, "video/mp4")},
                )

        if response.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"[Upload-Post] API error ({response.status_code}): {response.text[:400]}"
            )

        data = response.json()
        logger.info("[Upload-Post] Response for clip %s: %s", clip_id, data)

        request_id = data.get("request_id") or data.get("id", clip_id)
        status     = data.get("status", "processing")

        post_url = ""
        results  = data.get("results", {})
        if isinstance(results, dict):
            platform_result = results.get(self._up_platform, {})
            post_url = platform_result.get("post_url", "") or platform_result.get("url", "")

        logger.info(
            "[Upload-Post] Clip %s submitted — request_id: %s | status: %s",
            clip_id, request_id, status,
        )

        return {
            "publish_id": request_id,
            "status":     status,
            "url":        post_url or "https://upload-post.com",
        }
