"""
app/platforms/facebook.py — Facebook Reels via Meta Graph API.

Production hardening applied:
  - Video opened with a proper context manager in binary mode — file handle
    always closed even if upload raises; no encoding issue.
  - Large file support: files > 100 MB are uploaded in 100 MB chunks via
    Facebook's resumable upload protocol.
  - Explicit timeout on all httpx calls.
  - Error details logged before raising.

Required env vars:
  FACEBOOK_ACCESS_TOKEN    Long-lived Page Access Token
  FACEBOOK_PAGE_ID         Your Facebook Page ID
  FACEBOOK_PUBLIC_BASE_URL Your server's public URL (or falls back to
                           INSTAGRAM_PUBLIC_BASE_URL if set)
"""
import logging
import math
import os

import httpx

from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

GRAPH_API_BASE   = "https://graph.facebook.com/v19.0"
_CHUNK_SIZE      = 100 * 1024 * 1024   # 100 MB per chunk
_REQUEST_TIMEOUT = 300                  # seconds


class FacebookPlatform(BasePlatform):

    @property
    def name(self) -> str:
        return "Facebook Reels"

    @property
    def key(self) -> str:
        return "facebook"

    def is_configured(self) -> bool:
        token   = os.getenv("FACEBOOK_ACCESS_TOKEN", "")
        page_id = os.getenv("FACEBOOK_PAGE_ID", "")
        return bool(token and page_id and not token.startswith("your_"))

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        access_token = os.getenv("FACEBOOK_ACCESS_TOKEN")
        page_id      = os.getenv("FACEBOOK_PAGE_ID")
        public_base  = os.getenv(
            "FACEBOOK_PUBLIC_BASE_URL",
            os.getenv("INSTAGRAM_PUBLIC_BASE_URL", ""),
        ).rstrip("/")

        if not access_token or not page_id:
            raise ValueError("FACEBOOK_ACCESS_TOKEN and FACEBOOK_PAGE_ID must be set")

        if not public_base:
            raise ValueError("FACEBOOK_PUBLIC_BASE_URL must be set to your server's public URL")

        description = post_text[:63206]   # Facebook's description limit

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:

            # ── Step 1: Initialise resumable upload ───────────────────────────
            logger.info("[Facebook] Starting upload for clip %s", clip_id)
            init_resp = await client.post(
                f"{GRAPH_API_BASE}/{page_id}/video_reels",
                params={"upload_phase": "start", "access_token": access_token},
            )
            init_resp.raise_for_status()
            init_data = init_resp.json()

            if "error" in init_data:
                logger.error("[Facebook] Init error: %s", init_data["error"])
                raise RuntimeError(f"Facebook init error: {init_data['error']}")

            video_id   = init_data["video_id"]
            upload_url = init_data["upload_url"]
            logger.info("[Facebook] video_id: %s", video_id)

            # ── Step 2: Upload video in chunks ────────────────────────────────
            import os as _os
            file_size   = _os.path.getsize(clip_path)
            chunk_count = math.ceil(file_size / _CHUNK_SIZE)

            with open(clip_path, "rb") as f:
                for chunk_idx in range(chunk_count):
                    chunk_data  = f.read(_CHUNK_SIZE)
                    chunk_start = chunk_idx * _CHUNK_SIZE

                    upload_resp = await client.post(
                        upload_url,
                        headers={
                            "Authorization":  f"OAuth {access_token}",
                            "offset":         str(chunk_start),
                            "Content-Type":   "application/octet-stream",
                        },
                        content=chunk_data,
                    )
                    upload_resp.raise_for_status()
                    logger.info(
                        "[Facebook] Uploaded chunk %d/%d for clip %s",
                        chunk_idx + 1, chunk_count, clip_id,
                    )

            logger.info("[Facebook] Upload complete for clip %s", clip_id)

            # ── Step 3: Publish the Reel ──────────────────────────────────────
            publish_resp = await client.post(
                f"{GRAPH_API_BASE}/{page_id}/video_reels",
                params={
                    "upload_phase": "finish",
                    "video_id":     video_id,
                    "access_token": access_token,
                    "video_state":  "PUBLISHED",
                    "description":  description,
                },
            )
            publish_resp.raise_for_status()
            publish_data = publish_resp.json()

            if "error" in publish_data:
                logger.error("[Facebook] Publish error: %s", publish_data["error"])
                raise RuntimeError(f"Facebook publish error: {publish_data['error']}")

            logger.info("[Facebook] Posted clip %s — video_id: %s", clip_id, video_id)
            return {
                "publish_id": video_id,
                "status":     "published",
                "url":        f"https://www.facebook.com/reel/{video_id}",
            }
