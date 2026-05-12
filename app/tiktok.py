"""
app/platforms/tiktok.py — TikTok Content Posting API.

Production hardening applied:
  - Video file opened in binary mode with explicit context manager —
    no encoding issue, file handle always closed on error.
  - Large file support: files > 64 MB are split into multiple chunks
    (TikTok's API requires chunk_size ≤ 64 MB per chunk).
  - Timeout raised to 300 s for large video uploads.
  - Status poll retries extended: 20 x 5 s = 100 s max wait.

Required env vars:
  TIKTOK_ACCESS_TOKEN
  TIKTOK_CLIENT_KEY      (kept for future OAuth refresh flow)
  TIKTOK_CLIENT_SECRET   (kept for future OAuth refresh flow)
"""
import logging
import math
import os

import httpx
from pathlib import Path

from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

TIKTOK_API_BASE  = "https://open.tiktokapis.com/v2"
_CHUNK_SIZE      = 64 * 1024 * 1024   # 64 MB — TikTok max per chunk
_UPLOAD_TIMEOUT  = 300                 # seconds


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
            "Content-Type":  "application/json; charset=UTF-8",
        }

        clip_size   = Path(clip_path).stat().st_size
        chunk_count = math.ceil(clip_size / _CHUNK_SIZE)
        chunk_size  = min(clip_size, _CHUNK_SIZE)

        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
            # ── Step 1: Initialise upload ─────────────────────────────────────
            logger.info("[TikTok] Initialising upload for clip %s (%d chunk(s))", clip_id, chunk_count)
            init_payload = {
                "post_info": {
                    "title":           post_text[:2200],
                    "privacy_level":   "PUBLIC_TO_EVERYONE",
                    "disable_duet":    False,
                    "disable_comment": False,
                    "disable_stitch":  False,
                },
                "source_info": {
                    "source":            "FILE_UPLOAD",
                    "video_size":        clip_size,
                    "chunk_size":        chunk_size,
                    "total_chunk_count": chunk_count,
                },
            }
            init_resp = await client.post(
                f"{TIKTOK_API_BASE}/post/publish/video/init/",
                headers=headers,
                json=init_payload,
            )
            init_resp.raise_for_status()
            init_data = init_resp.json()

            if init_data.get("error", {}).get("code") != "ok":
                raise RuntimeError(f"TikTok init failed: {init_data}")

            publish_id = init_data["data"]["publish_id"]
            upload_url = init_data["data"]["upload_url"]

            # ── Step 2: Upload chunks ─────────────────────────────────────────
            with open(clip_path, "rb") as f:
                for chunk_idx in range(chunk_count):
                    chunk_data   = f.read(_CHUNK_SIZE)
                    chunk_start  = chunk_idx * _CHUNK_SIZE
                    chunk_end    = chunk_start + len(chunk_data) - 1

                    upload_headers = {
                        "Content-Range":  f"bytes {chunk_start}-{chunk_end}/{clip_size}",
                        "Content-Length": str(len(chunk_data)),
                        "Content-Type":   "video/mp4",
                    }
                    upload_resp = await client.put(
                        upload_url,
                        content=chunk_data,
                        headers=upload_headers,
                    )
                    upload_resp.raise_for_status()
                    logger.info(
                        "[TikTok] Uploaded chunk %d/%d for clip %s",
                        chunk_idx + 1, chunk_count, clip_id,
                    )

            # ── Step 3: Poll publish status ───────────────────────────────────
            import asyncio
            status_code = "PROCESSING"
            for attempt in range(20):
                await asyncio.sleep(5)
                status_resp = await client.post(
                    f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
                    headers=headers,
                    json={"publish_id": publish_id},
                )
                status_data = status_resp.json()
                status_code = status_data.get("data", {}).get("status", "PROCESSING")
                logger.info("[TikTok] Publish status (attempt %d): %s", attempt + 1, status_code)
                if status_code not in ("PROCESSING", "PUBLISH_IN_PROGRESS"):
                    break

            logger.info("[TikTok] Posted clip %s — publish_id: %s", clip_id, publish_id)
            return {
                "publish_id": publish_id,
                "status":     status_code,
                "url":        f"https://www.tiktok.com/@me/video/{publish_id}",
            }
