"""
app/platforms/router.py

Single entry point for all platform posting.
The rest of the app calls post_to_platform() — never platform code directly.
"""
import logging
from app.platforms.base import BasePlatform
from app.platforms.tiktok import TikTokPlatform
from app.platforms.youtube import YouTubePlatform
from app.platforms.instagram import InstagramPlatform
from app.platforms.facebook import FacebookPlatform
from app.platforms.uploadpost import UploadPostPlatform
from app.platforms.tiktok_browser import TikTokBrowserPlatform

logger = logging.getLogger(__name__)

# Registry of all platforms
_PLATFORMS: dict[str, BasePlatform] = {
    "tiktok":               TikTokPlatform(),
    "youtube":              YouTubePlatform(),
    "instagram":            InstagramPlatform(),
    "facebook":             FacebookPlatform(),
    # Direct browser posting — no API approval needed
    "tiktok_browser":       TikTokBrowserPlatform(),
    # Upload-Post — paid tier required for TikTok
    "tiktok_uploadpost":    UploadPostPlatform("tiktok_uploadpost"),
    "instagram_uploadpost": UploadPostPlatform("instagram_uploadpost"),
    "facebook_uploadpost":  UploadPostPlatform("facebook_uploadpost"),
    "youtube_uploadpost":   UploadPostPlatform("youtube_uploadpost"),
}


def get_platform(key: str) -> BasePlatform:
    """Get a platform by its key. Raises ValueError if unknown."""
    if key not in _PLATFORMS:
        raise ValueError(f"Unknown platform: '{key}'. Valid options: {list(_PLATFORMS.keys())}")
    return _PLATFORMS[key]


def get_all_platforms() -> list[BasePlatform]:
    return list(_PLATFORMS.values())


def get_configured_platforms() -> list[BasePlatform]:
    """Return only platforms that have all required env vars set."""
    return [p for p in _PLATFORMS.values() if p.is_configured()]


def get_platform_status() -> list[dict]:
    """
    Returns a summary of all platforms and their configuration status.
    Used by the dashboard Settings tab.
    """
    return [
        {
            "key": p.key,
            "name": p.name,
            "configured": p.is_configured(),
        }
        for p in _PLATFORMS.values()
    ]


async def post_to_platform(
    platform_key: str,
    clip_path: str,
    post_text: str,
    clip_id: str,
    title: str = "",
) -> dict:
    """
    Post a clip to a specific platform.
    Returns the platform's response dict with publish_id, status, and url.
    """
    platform = get_platform(platform_key)

    if not platform.is_configured():
        raise ValueError(
            f"{platform.name} is not configured. "
            f"Check your .env file for the required credentials."
        )

    logger.info(f"[Router] Posting clip {clip_id} to {platform.name}")
    result = await platform.upload_and_post(
        clip_path=clip_path,
        post_text=post_text,
        clip_id=clip_id,
        title=title,
    )
    result["platform"] = platform_key
    result["platform_name"] = platform.name
    return result
