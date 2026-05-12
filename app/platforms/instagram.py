"""
app/platforms/instagram.py — Instagram Reels via Meta Graph API.

Production hardening applied:
  - Polling loop extended to 30 x 10 s = 300 s max wait.
    Large videos (100 to 200 MB) routinely take 2 to3 minutes to process on Meta's
    servers; the original 15 x 5 s = 75 s cap caused spurious failures.
  - Explicit timeout on all httpx calls.
  - Error details logged before raising so failures are diagnosable.

Required env vars:
  INSTAGRAM_ACCESS_TOKEN      Long-lived page access token
  INSTAGRAM_ACCOUNT_ID        IG Business Account ID
  INSTAGRAM_PUBLIC_BASE_URL   Your server's public URL (e.g. https://yourdomain.com)
                              Meta requires the video to be at a public URL.
"""
import asyncio
import logging
import os

import httpx

from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

GRAPH_API_BASE  = "https://graph.facebook.com/v19.0"
_REQUEST_TIMEOUT = 120


class InstagramPlatform(BasePlatform):

    @property
    def name(self) -> str:
        return "Instagram Reels"

    @property
    def key(self) -> str:
        return "instagram"

    def is_configured(self) -> bool:
        token      = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
        account_id = os.getenv("INSTAGRAM_ACCOUNT_ID", "")
        return bool(token and account_id and not token.startswith("your_"))

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        """
        Post a Reel to Instagram via the Graph API.
        Meta requires the video at a public URL — set INSTAGRAM_PUBLIC_BASE_URL.
        """
        access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
        account_id   = os.getenv("INSTAGRAM_ACCOUNT_ID")
        public_base  = os.getenv("INSTAGRAM_PUBLIC_BASE_URL", "").rstrip("/")

        if not access_token or not account_id:
            raise ValueError("INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID must be set")

        if not public_base:
            raise ValueError(
                "INSTAGRAM_PUBLIC_BASE_URL must be set to your server's public URL "
                "(e.g. https://yourdomain.com). Meta needs to fetch the video from a public URL."
            )

        video_url = f"{public_base}/clips/{clip_id}/video"
        caption   = post_text[:2200]

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:

            # ── Step 1: Create media container ───────────────────────────────
            logger.info("[Instagram] Creating media container for clip %s", clip_id)
            container_resp = await client.post(
                f"{GRAPH_API_BASE}/{account_id}/media",
                params={
                    "media_type":   "REELS",
                    "video_url":    video_url,
                    "caption":      caption,
                    "share_to_feed": "true",
                    "access_token": access_token,
                },
            )
            container_resp.raise_for_status()
            container_data = container_resp.json()

            if "error" in container_data:
                logger.error("[Instagram] Container error: %s", container_data["error"])
                raise RuntimeError(f"Instagram container error: {container_data['error']}")

            container_id = container_data["id"]
            logger.info("[Instagram] Container created: %s", container_id)

            # ── Step 2: Poll until Meta finishes processing the video ─────────
            # Large clips can take 2–3 min on Meta's end; 30 × 10 s = 300 s max.
            for attempt in range(30):
                await asyncio.sleep(10)
                status_resp = await client.get(
                    f"{GRAPH_API_BASE}/{container_id}",
                    params={"fields": "status_code,status", "access_token": access_token},
                )
                status_data  = status_resp.json()
                status_code  = status_data.get("status_code", "")
                logger.info("[Instagram] Processing status (attempt %d): %s", attempt + 1, status_code)

                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    logger.error("[Instagram] Processing failed: %s", status_data)
                    raise RuntimeError(f"Instagram video processing failed: {status_data}")

                if attempt == 29:
                    raise RuntimeError(
                        f"Instagram processing timed out after 300 s for clip {clip_id}. "
                        f"Last status: {status_code}"
                    )

            # ── Step 3: Publish ───────────────────────────────────────────────
            logger.info("[Instagram] Publishing clip %s...", clip_id)
            publish_resp = await client.post(
                f"{GRAPH_API_BASE}/{account_id}/media_publish",
                params={"creation_id": container_id, "access_token": access_token},
            )
            publish_resp.raise_for_status()
            publish_data = publish_resp.json()

            if "error" in publish_data:
                logger.error("[Instagram] Publish error: %s", publish_data["error"])
                raise RuntimeError(f"Instagram publish error: {publish_data['error']}")

            media_id = publish_data["id"]
            logger.info("[Instagram] Posted clip %s — media_id: %s", clip_id, media_id)

            return {
                "publish_id": media_id,
                "status":     "published",
                "url":        f"https://www.instagram.com/reel/{media_id}/",
            }
