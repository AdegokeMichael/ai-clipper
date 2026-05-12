"""
Smart Scheduler — APScheduler-based auto-posting engine.

Production hardening applied:
  - Now posts to ALL platforms in config.platforms via the platform router,
    not just TikTok via the legacy app.tiktok module.
  - Removed direct dependency on app.tiktok — all posting flows through
    app.platforms.router.post_to_platform() for consistency.
  - Per-platform error handling — one platform failing does not prevent
    posting to the others.
  - Timestamps stored as UTC ISO-8601 strings for unambiguous timezone handling.
"""
import logging
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from app import storage
from app.models import ScheduledPost, ClipStatus

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_scheduler_started = False


# ── Core posting job ───────────────────────────────────────────────────────────

async def _run_scheduled_posting():
    """
    Called at each configured posting time.
    Picks the oldest approved clip and posts it to all configured platforms.
    """
    config = storage.get_schedule_config()
    if not config.enabled:
        return

    # Count posts made today (UTC date)
    today_str = datetime.now(timezone.utc).date().isoformat()
    todays_posts = [
        p for p in storage.get_all_scheduled_posts()
        if p.status == "posted" and p.posted_at and p.posted_at[:10] == today_str
    ]
    if len(todays_posts) >= config.daily_limit:
        logger.info("[Scheduler] Daily limit reached (%d). Skipping.", config.daily_limit)
        return

    # Get next approved clip (oldest first)
    approved = sorted(
        [c for c in storage.get_all_clips() if c.status == ClipStatus.APPROVED],
        key=lambda c: c.created_at,
    )
    if not approved:
        logger.info("[Scheduler] No approved clips waiting.")
        return

    clip = approved[0]
    logger.info("[Scheduler] Auto-posting clip %s — %s", clip.id, clip.topic)

    # Import here to avoid circular import at module load
    from app.platforms import post_to_platform

    # Post to every configured platform; collect results
    any_success = False
    any_failure = False

    for platform_key in config.platforms:
        try:
            result = await post_to_platform(
                platform_key=platform_key,
                clip_path=clip.clip_path,
                post_text=clip.full_post_text,
                clip_id=clip.id,
                title=clip.topic,
            )
            storage.update_clip_status(
                clip.id,
                ClipStatus.POSTED,
                platform_key=platform_key,
                publish_id=result.get("publish_id"),
                publish_url=result.get("url"),
            )
            logger.info(
                "[Scheduler] ✅ Posted %s to %s — publish_id: %s",
                clip.id, platform_key, result.get("publish_id"),
            )
            any_success = True

        except Exception as e:
            logger.error(
                "[Scheduler] ❌ Failed to post %s to %s: %s",
                clip.id, platform_key, e,
            )
            any_failure = True

    # Final clip status: posted if at least one platform succeeded
    final_status = ClipStatus.POSTED if any_success else ClipStatus.FAILED
    storage.update_clip_status(clip.id, final_status)

    # Record in schedule log
    post_record = ScheduledPost(
        id=str(uuid.uuid4())[:8],
        clip_id=clip.id,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        status="posted" if any_success else "failed",
        posted_at=datetime.now(timezone.utc).isoformat() if any_success else None,
        error="One or more platforms failed" if any_failure and any_success else (
            "All platforms failed" if any_failure else None
        ),
    )
    storage.save_scheduled_post(post_record)


# ── Schedule management ────────────────────────────────────────────────────────

def _rebuild_schedule():
    """Clear all posting jobs and rebuild from current config."""
    for job in scheduler.get_jobs():
        if job.id.startswith("post_time_"):
            job.remove()

    config = storage.get_schedule_config()
    if not config.enabled:
        logger.info("[Scheduler] Auto-posting is disabled.")
        return

    try:
        tz = pytz.timezone(config.timezone)
    except Exception:
        tz = pytz.timezone("Africa/Lagos")
        logger.warning(
            "[Scheduler] Invalid timezone '%s', defaulting to Africa/Lagos", config.timezone
        )

    for i, time_str in enumerate(config.posting_times):
        try:
            hour, minute = map(int, time_str.split(":"))
            trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)
            scheduler.add_job(
                _run_scheduled_posting,
                trigger=trigger,
                id=f"post_time_{i}",
                replace_existing=True,
                name=f"Auto-post at {time_str} ({config.timezone})",
            )
            logger.info("[Scheduler] Scheduled at %s %s", time_str, config.timezone)
        except ValueError:
            logger.warning("[Scheduler] Invalid time format: '%s' — skipping", time_str)

    logger.info(
        "[Scheduler] %d posting slots active. Daily limit: %d. Platforms: %s",
        len(config.posting_times), config.daily_limit, config.platforms,
    )


def start_scheduler():
    """Start APScheduler. Called once at app startup."""
    global _scheduler_started
    if _scheduler_started:
        return
    scheduler.start()
    _scheduler_started = True
    _rebuild_schedule()
    logger.info("[Scheduler] Engine started.")


def stop_scheduler():
    """Stop the scheduler cleanly at app shutdown."""
    global _scheduler_started
    if _scheduler_started and scheduler.running:
        scheduler.shutdown(wait=False)
        _scheduler_started = False


def update_schedule(config):
    """Update schedule config and rebuild jobs. Called when user saves settings."""
    storage.save_schedule_config(config)
    _rebuild_schedule()


def get_next_run_times() -> list[dict]:
    """Return upcoming scheduled posting times for the dashboard."""
    jobs = [j for j in scheduler.get_jobs() if j.id.startswith("post_time_")]
    result = []
    for job in jobs:
        if job.next_run_time:
            result.append({
                "job_id":   job.id,
                "name":     job.name,
                "next_run": job.next_run_time.isoformat(),
            })
    return sorted(result, key=lambda x: x["next_run"])