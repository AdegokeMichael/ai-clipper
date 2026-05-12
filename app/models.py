from pydantic import BaseModel
from typing import Optional
from enum import Enum


class ClipStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"
    FAILED = "failed"


class Platform(str, Enum):
    TIKTOK    = "tiktok"
    YOUTUBE   = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK  = "facebook"


class ClipSegment(BaseModel):
    """A single clip identified by Claude from the transcript"""
    start_time: float
    end_time: float
    hook_text: str
    hook_score: int
    hook_reason: str
    topic: str
    value_proposition: str


class GeneratedClip(BaseModel):
    """A fully processed clip ready for review"""
    id: str
    source_video: str
    clip_path: str
    thumbnail_path: Optional[str] = None
    start_time: float
    end_time: float
    duration: float
    hook_text: str
    hook_score: int
    caption: str
    hashtags: list[str]
    full_post_text: str
    topic: str
    status: ClipStatus = ClipStatus.PENDING
    # Multi-platform tracking
    post_ids: dict[str, str] = {}       # platform_key -> publish_id
    post_urls: dict[str, str] = {}      # platform_key -> public URL
    tiktok_post_id: Optional[str] = None  # legacy, kept for compatibility
    created_at: str


class PipelineJob(BaseModel):
    """Tracks a full pipeline run"""
    job_id: str
    source_video: str
    status: str
    clips_found: int = 0
    clips_generated: list[str] = []
    error: Optional[str] = None
    created_at: str


class ApprovalAction(BaseModel):
    caption: Optional[str] = None
    hashtags: Optional[list[str]] = None
    schedule_at: Optional[str] = None


class TranscriptSegment(BaseModel):
    """A single word/segment from faster-whisper"""
    start: float
    end: float
    text: str


class ScheduledPost(BaseModel):
    """A clip queued for scheduled posting"""
    id: str
    clip_id: str
    scheduled_at: str
    status: str = "queued"
    posted_at: Optional[str] = None
    error: Optional[str] = None


class HookPerformance(BaseModel):
    """Tracks how a hook performed after posting"""
    clip_id: str
    hook_text: str
    hook_score: int
    topic: str
    views: int = 0
    likes: int = 0
    shares: int = 0
    comments: int = 0
    watch_rate: float = 0.0
    recorded_at: Optional[str] = None


class ScheduleConfig(BaseModel):
    """Posting schedule settings"""
    enabled: bool = False
    daily_limit: int = 3
    posting_times: list[str] = ["08:00", "13:00", "19:00"]
    timezone: str = "Africa/Lagos"
    platforms: list[str] = ["youtube"]   # which platforms to auto-post to


class AnalyticsSummary(BaseModel):
    """Aggregated stats for the dashboard"""
    total_clips: int = 0
    pending: int = 0
    approved: int = 0
    posted: int = 0
    rejected: int = 0
    avg_hook_score: float = 0.0
    top_topics: list[str] = []
    scheduled_queue: int = 0
