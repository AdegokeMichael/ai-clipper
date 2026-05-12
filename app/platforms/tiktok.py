"""
app/platforms/tiktok.py

TikTok Content Posting API.
Requires business account + API approval (2-3 days review).
Get credentials at: https://developers.tiktok.com

Required env vars:
  TIKTOK_ACCESS_TOKEN
  TIKTOK_CLIENT_KEY
  TIKTOK_CLIENT_SECRET
"""
import os
import logging
import httpx
from pathlib import Path
from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"


class TikTokPlatform(BasePlatform):

    @property
    def name(self) -> str:
        return "TikTok"

    @property
    def key(self) -> str:
        return "tiktok"

    def is_configured(self) -> bool:
        token = os.getenv("TIKTOK_ACCESS_TOKEN", "")
        return bool(token and not token.startswith("your_"))

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        access_token = os.getenv("TIKTOK_ACCESS_TOKEN")
        if not access_token:
            raise ValueError("TIKTOK_ACCESS_TOKEN not set in environment")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        clip_size = Path(clip_path).stat().st_size

        async with httpx.AsyncClient(timeout=120) as client:
            logger.info(f"[TikTok] Initialising upload for clip {clip_id}")
            init_payload = {
                "post_info": {
                    "title": post_text[:2200],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": clip_size,
                    "chunk_size": clip_size,
                    "total_chunk_count": 1,
                },
            }

            init_response = await client.post(
                f"{TIKTOK_API_BASE}/post/publish/video/init/",
                headers=headers,
                json=init_payload,
            )
            init_response.raise_for_status()
            init_data = init_response.json()

            if init_data.get("error", {}).get("code") != "ok":
                raise RuntimeError(f"TikTok init failed: {init_data}")

            publish_id = init_data["data"]["publish_id"]
            upload_url = init_data["data"]["upload_url"]

            with open(clip_path, "rb") as f:
                video_bytes = f.read()

            upload_headers = {
                "Content-Range": f"bytes 0-{clip_size - 1}/{clip_size}",
                "Content-Length": str(clip_size),
                "Content-Type": "video/mp4",
            }

            upload_response = await client.put(
                upload_url,
                content=video_bytes,
                headers=upload_headers,
            )
            upload_response.raise_for_status()

            status_response = await client.post(
                f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
                headers=headers,
                json={"publish_id": publish_id},
            )
            status_data = status_response.json()

            logger.info(f"[TikTok] Posted clip {clip_id} — publish_id: {publish_id}")
            return {
                "publish_id": publish_id,
                "status": status_data.get("data", {}).get("status", "PROCESSING"),
                "url": f"https://www.tiktok.com/@me/video/{publish_id}",
            }
