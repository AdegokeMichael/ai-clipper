"""
app/platforms/buffer.py

Buffer API integration — post to TikTok, Instagram, and Facebook
through Buffer's approved third-party access.

Why Buffer instead of direct TikTok API
────────────────────────────────────────
Buffer is an approved TikTok partner. Posting through Buffer means
you never need your own TikTok developer app approval, a domain name,
or any business verification. You just connect your TikTok account to
Buffer and use their API.

Setup (takes ~10 minutes, completely free)
──────────────────────────────────────────
1. Create a Buffer account at https://buffer.com (free plan supports 3 channels)
2. Connect your TikTok account (@bade.clips or similar) to Buffer
3. Optionally also connect Instagram and/or Facebook Page
4. Go to https://buffer.com/developers → Create an app (or use personal access token)
5. Get your Access Token
6. Find your Channel IDs (profile IDs) — see BUFFER_CHANNEL_IDS below

Required env vars
─────────────────
BUFFER_ACCESS_TOKEN         Your Buffer personal access token
BUFFER_TIKTOK_CHANNEL_ID    Buffer profile ID for your TikTok channel
BUFFER_INSTAGRAM_CHANNEL_ID Buffer profile ID for your Instagram (optional)
BUFFER_FACEBOOK_CHANNEL_ID  Buffer profile ID for your Facebook Page (optional)

Finding your Channel IDs
─────────────────────────
After adding your accounts to Buffer, run this in your terminal:

  curl https://api.bufferapp.com/1/profiles.json \\
    -d "access_token=YOUR_ACCESS_TOKEN"

Each object in the response has an "id" field — that is the channel ID.
Match them by "service" field: "tiktok", "instagram", "facebook".

Or add BUFFER_ACCESS_TOKEN to .env and open:
  http://YOUR_SERVER:8000/buffer/channels

This endpoint lists all your connected Buffer channels with their IDs.

Scheduling vs immediate posting
────────────────────────────────
By default this posts immediately (scheduled for "now").
Set BUFFER_SCHEDULE=true to add clips to your Buffer queue instead,
which respects Buffer's built-in posting schedule.
"""
import os
import logging
import base64
import httpx
from pathlib import Path
from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

BUFFER_API = "https://api.bufferapp.com/1"

# Map our platform keys to Buffer service names
SERVICE_MAP = {
    "tiktok_buffer":    ("BUFFER_TIKTOK_CHANNEL_ID",    "TikTok via Buffer"),
    "instagram_buffer": ("BUFFER_INSTAGRAM_CHANNEL_ID", "Instagram via Buffer"),
    "facebook_buffer":  ("BUFFER_FACEBOOK_CHANNEL_ID",  "Facebook via Buffer"),
}


class BufferPlatform(BasePlatform):
    """
    Generic Buffer platform wrapper.
    Instantiated once per connected service (TikTok, Instagram, Facebook).
    """

    def __init__(self, service_key: str):
        """
        service_key: one of tiktok_buffer | instagram_buffer | facebook_buffer
        """
        self._service_key = service_key
        self._channel_env, self._display_name = SERVICE_MAP[service_key]

    @property
    def name(self) -> str:
        return self._display_name

    @property
    def key(self) -> str:
        return self._service_key

    def is_configured(self) -> bool:
        token   = os.getenv("BUFFER_ACCESS_TOKEN", "")
        channel = os.getenv(self._channel_env, "")
        return bool(
            token and not token.startswith("your_") and
            channel and not channel.startswith("your_")
        )

    def _token(self) -> str:
        t = os.getenv("BUFFER_ACCESS_TOKEN")
        if not t:
            raise ValueError("BUFFER_ACCESS_TOKEN is not set in .env")
        return t

    def _channel_id(self) -> str:
        c = os.getenv(self._channel_env)
        if not c:
            raise ValueError(
                f"{self._channel_env} is not set in .env. "
                f"Find your channel IDs at http://YOUR_SERVER:8000/buffer/channels"
            )
        return c

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        """
        Upload the video clip to Buffer's media endpoint, then create
        an update (post) on the connected channel.

        Returns dict with publish_id, status, and url.
        """
        token      = self._token()
        channel_id = self._channel_id()
        schedule   = os.getenv("BUFFER_SCHEDULE", "false").lower() == "true"

        async with httpx.AsyncClient(timeout=180) as client:

            # ── Step 1: Upload video to Buffer's media upload endpoint ────────
            logger.info(f"[Buffer] Uploading video for clip {clip_id} to {self._display_name}")

            clip_size = Path(clip_path).stat().st_size
            with open(clip_path, "rb") as f:
                video_bytes = f.read()

            # Buffer accepts multipart form upload
            upload_response = await client.post(
                f"{BUFFER_API}/media/upload.json",
                data={"access_token": token},
                files={"file": (Path(clip_path).name, video_bytes, "video/mp4")},
            )

            if upload_response.status_code not in (200, 201):
                raise RuntimeError(
                    f"[Buffer] Video upload failed ({upload_response.status_code}): "
                    f"{upload_response.text[:400]}"
                )

            upload_data = upload_response.json()
            media_id = upload_data.get("id") or upload_data.get("media_id")

            if not media_id:
                raise RuntimeError(
                    f"[Buffer] Upload response missing media ID: {upload_data}"
                )

            logger.info(f"[Buffer] Video uploaded. media_id: {media_id}")

            # ── Step 2: Create the post update ────────────────────────────────
            # Trim caption to 2200 chars (TikTok limit)
            caption = post_text[:2200]

            payload = {
                "access_token":  token,
                "profile_ids[]": channel_id,
                "text":          caption,
                "media[video]":  media_id,
                "now":           "true" if not schedule else "false",
            }

            # Title is used for YouTube — for TikTok/IG/FB we skip it
            if title and self._service_key not in ("tiktok_buffer",):
                payload["media[title]"] = title[:100]

            update_response = await client.post(
                f"{BUFFER_API}/updates/create.json",
                data=payload,
            )

            if update_response.status_code not in (200, 201):
                raise RuntimeError(
                    f"[Buffer] Post creation failed ({update_response.status_code}): "
                    f"{update_response.text[:400]}"
                )

            update_data = update_response.json()
            updates     = update_data.get("updates", [{}])
            update_id   = updates[0].get("id", "") if updates else ""
            status      = updates[0].get("status", "pending") if updates else "pending"

            logger.info(
                f"[Buffer] Post created for clip {clip_id} on {self._display_name}. "
                f"update_id: {update_id} status: {status}"
            )

            return {
                "publish_id": update_id,
                "status":     "queued" if schedule else "published",
                "url":        f"https://buffer.com/publish",
            }


# ── Helper: list all connected Buffer channels ─────────────────────────────────

async def list_buffer_channels(access_token: str) -> list[dict]:
    """
    Fetch all Buffer profiles connected to this account.
    Returns a simplified list — use to find Channel IDs.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BUFFER_API}/profiles.json",
            params={"access_token": access_token},
        )
        r.raise_for_status()
        profiles = r.json()

    result = []
    for p in profiles:
        result.append({
            "id":       p.get("id"),
            "service":  p.get("service"),          # tiktok / instagram / facebook
            "name":     p.get("service_username") or p.get("formatted_username", ""),
            "env_var":  _service_to_env(p.get("service", "")),
        })
    return result


def _service_to_env(service: str) -> str:
    mapping = {
        "tiktok":    "BUFFER_TIKTOK_CHANNEL_ID",
        "instagram": "BUFFER_INSTAGRAM_CHANNEL_ID",
        "facebook":  "BUFFER_FACEBOOK_CHANNEL_ID",
    }
    return mapping.get(service.lower(), f"BUFFER_{service.upper()}_CHANNEL_ID")
