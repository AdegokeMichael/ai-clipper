"""
app/platforms/base.py

Abstract base class for all posting platforms.
Every platform (TikTok, YouTube, Instagram, Facebook) implements this interface.
The rest of the app only talks to this interface — never to platform code directly.
"""
from abc import ABC, abstractmethod


class BasePlatform(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable platform name e.g. 'YouTube Shorts'"""

    @property
    @abstractmethod
    def key(self) -> str:
        """Short identifier used in storage and API e.g. 'youtube'"""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if all required env vars are set for this platform."""

    @abstractmethod
    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        """
        Upload and publish a clip to this platform.

        Returns a dict with at minimum:
          - publish_id: str   (platform's post/video ID)
          - status: str       (e.g. 'published', 'processing')
          - url: str          (public URL to the post, if available immediately)
        """
