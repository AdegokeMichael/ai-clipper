"""
Simple JSON-based storage for clips and jobs.

Production hardening applied:
  - Per-file threading.Lock → safe for concurrent pipeline runs
  - Atomic writes (write temp → fsync → rename) → no corrupt JSON on crash
  - Explicit encoding on all file I/O
  - Source video cleanup helper
"""
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from app.models import (
    GeneratedClip,
    HookPerformance,
    PipelineJob,
    ClipStatus,
    ScheduledPost,
    ScheduleConfig,
)

logger = logging.getLogger(__name__)

STORE_DIR = Path("output/store")
STORE_DIR.mkdir(parents=True, exist_ok=True)

CLIPS_FILE    = STORE_DIR / "clips.json"
JOBS_FILE     = STORE_DIR / "jobs.json"
SCHEDULE_FILE = STORE_DIR / "schedule.json"
HOOK_PERF_FILE = STORE_DIR / "hook_performance.json"
CONFIG_FILE   = STORE_DIR / "config.json"

# One lock per storage file — prevents concurrent read-modify-write races
# when two pipeline workers finish at the same time.
_LOCKS: dict[Path, threading.Lock] = {
    CLIPS_FILE:     threading.Lock(),
    JOBS_FILE:      threading.Lock(),
    SCHEDULE_FILE:  threading.Lock(),
    HOOK_PERF_FILE: threading.Lock(),
    CONFIG_FILE:    threading.Lock(),
}


# ── I/O helpers ────────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    """Read JSON from path, returning {} if absent or corrupt."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("Corrupt JSON in %s (%s) — returning empty store", path, exc)
    return {}


def _save(path: Path, data: dict) -> None:
    """
    Atomic write: serialise to a temp file in the same directory,
    fsync, then rename over the target. Guarantees the reader always
    sees either the old complete file or the new complete file, never
    a half-written one.
    """
    dir_ = path.parent
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.unlink(tmp_path)
            raise
        os.replace(tmp_path, path)  # atomic on POSIX
    except Exception as exc:
        logger.error("Failed to save %s: %s", path, exc)
        raise


def _locked_update(path: Path, mutate_fn):
    """
    Thread-safe read-modify-write for a JSON file.
    mutate_fn receives the current dict and returns the modified dict.
    """
    lock = _LOCKS[path]
    with lock:
        data = _load(path)
        updated = mutate_fn(data)
        _save(path, updated)


# ── Clips ──────────────────────────────────────────────────────────────────────

def save_clip(clip: GeneratedClip) -> None:
    def _mutate(data: dict) -> dict:
        data[clip.id] = clip.model_dump()
        return data
    _locked_update(CLIPS_FILE, _mutate)


def get_clip(clip_id: str) -> Optional[GeneratedClip]:
    with _LOCKS[CLIPS_FILE]:
        data = _load(CLIPS_FILE)
    if clip_id in data:
        return GeneratedClip(**data[clip_id])
    return None


def get_all_clips() -> list[GeneratedClip]:
    with _LOCKS[CLIPS_FILE]:
        data = _load(CLIPS_FILE)
    return [GeneratedClip(**v) for v in data.values()]


def get_pending_clips() -> list[GeneratedClip]:
    return [c for c in get_all_clips() if c.status == ClipStatus.PENDING]


def delete_clip(clip_id: str) -> bool:
    """
    Remove a clip from storage and delete its files from disk.
    Returns True if deleted, False if not found.
    """
    deleted = False

    def _mutate(data: dict) -> dict:
        nonlocal deleted
        if clip_id not in data:
            return data

        clip_data = data[clip_id]

        # Delete video file
        clip_path = clip_data.get("clip_path")
        if clip_path:
            p = Path(clip_path)
            if p.exists():
                p.unlink(missing_ok=True)

        # Delete thumbnail
        thumb_path = clip_data.get("thumbnail_path")
        if thumb_path:
            p = Path(thumb_path)
            if p.exists():
                p.unlink(missing_ok=True)

        del data[clip_id]
        deleted = True
        logger.info("Deleted clip %s and its files.", clip_id)
        return data

    _locked_update(CLIPS_FILE, _mutate)
    return deleted


def update_clip_status(
    clip_id: str,
    status: ClipStatus,
    tiktok_post_id: str = None,
    platform_key: str = None,
    publish_id: str = None,
    publish_url: str = None,
) -> None:
    def _mutate(data: dict) -> dict:
        if clip_id not in data:
            return data
        data[clip_id]["status"] = status.value
        if tiktok_post_id:
            data[clip_id]["tiktok_post_id"] = tiktok_post_id
        if platform_key and publish_id:
            data[clip_id].setdefault("post_ids", {})[platform_key] = publish_id
            if publish_url:
                data[clip_id].setdefault("post_urls", {})[platform_key] = publish_url
        return data

    _locked_update(CLIPS_FILE, _mutate)


def update_clip_content(clip_id: str, caption: str, hashtags: list[str]) -> None:
    def _mutate(data: dict) -> dict:
        if clip_id not in data:
            return data
        data[clip_id]["caption"] = caption
        data[clip_id]["hashtags"] = hashtags
        data[clip_id]["full_post_text"] = (
            caption + "\n\n" + " ".join(f"#{h.lstrip('#')}" for h in hashtags)
        )
        return data

    _locked_update(CLIPS_FILE, _mutate)


# ── Source video cleanup ───────────────────────────────────────────────────────

def delete_source_video(video_path: str) -> None:
    """
    Remove the original uploaded video after the pipeline completes.
    Keeps uploads/ from growing without bound.
    Silently skips if the file is already gone.
    """
    try:
        p = Path(video_path)
        if p.exists():
            p.unlink()
            logger.info("Cleaned up source video: %s", video_path)
    except OSError as exc:
        logger.warning("Could not delete source video %s: %s", video_path, exc)


# ── Jobs ───────────────────────────────────────────────────────────────────────

def save_job(job: PipelineJob) -> None:
    def _mutate(data: dict) -> dict:
        data[job.job_id] = job.model_dump()
        return data
    _locked_update(JOBS_FILE, _mutate)


def get_job(job_id: str) -> Optional[PipelineJob]:
    with _LOCKS[JOBS_FILE]:
        data = _load(JOBS_FILE)
    if job_id in data:
        return PipelineJob(**data[job_id])
    return None


def update_job(job_id: str, **kwargs) -> None:
    def _mutate(data: dict) -> dict:
        if job_id in data:
            data[job_id].update(kwargs)
        return data
    _locked_update(JOBS_FILE, _mutate)


# ── Scheduled Posts ────────────────────────────────────────────────────────────

def save_scheduled_post(post: ScheduledPost) -> None:
    def _mutate(data: dict) -> dict:
        data[post.id] = post.model_dump()
        return data
    _locked_update(SCHEDULE_FILE, _mutate)


def get_scheduled_post(post_id: str) -> Optional[ScheduledPost]:
    with _LOCKS[SCHEDULE_FILE]:
        data = _load(SCHEDULE_FILE)
    if post_id in data:
        return ScheduledPost(**data[post_id])
    return None


def get_all_scheduled_posts() -> list[ScheduledPost]:
    with _LOCKS[SCHEDULE_FILE]:
        data = _load(SCHEDULE_FILE)
    return [ScheduledPost(**v) for v in data.values()]


def get_queued_posts() -> list[ScheduledPost]:
    return [p for p in get_all_scheduled_posts() if p.status == "queued"]


def update_scheduled_post(post_id: str, **kwargs) -> None:
    def _mutate(data: dict) -> dict:
        if post_id in data:
            data[post_id].update(kwargs)
        return data
    _locked_update(SCHEDULE_FILE, _mutate)


def cancel_scheduled_post(post_id: str) -> None:
    update_scheduled_post(post_id, status="cancelled")


# ── Hook Performance ───────────────────────────────────────────────────────────

def save_hook_performance(perf: HookPerformance) -> None:
    def _mutate(data: dict) -> dict:
        data[perf.clip_id] = perf.model_dump()
        return data
    _locked_update(HOOK_PERF_FILE, _mutate)


def get_all_hook_performances() -> list[HookPerformance]:
    with _LOCKS[HOOK_PERF_FILE]:
        data = _load(HOOK_PERF_FILE)
    return [HookPerformance(**v) for v in data.values()]


def get_top_hooks(limit: int = 10) -> list[HookPerformance]:
    perfs = get_all_hook_performances()
    return sorted(perfs, key=lambda p: p.views, reverse=True)[:limit]


# ── Schedule Config ────────────────────────────────────────────────────────────

def get_schedule_config() -> ScheduleConfig:
    with _LOCKS[CONFIG_FILE]:
        data = _load(CONFIG_FILE)
    if "schedule" in data:
        return ScheduleConfig(**data["schedule"])
    return ScheduleConfig()


def save_schedule_config(config: ScheduleConfig) -> None:
    def _mutate(data: dict) -> dict:
        data["schedule"] = config.model_dump()
        return data
    _locked_update(CONFIG_FILE, _mutate)