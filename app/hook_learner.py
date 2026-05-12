"""
Hook Learner — closes the feedback loop between TikTok performance and Claude.

How it works:
1. After clips are posted, you (or a cron job) call record_performance() with real stats
2. hook_learner builds a "lessons learned" summary from top/bottom performers
3. This summary is injected into future Claude prompts so it gets smarter

Over time, Claude learns YOUR audience — what makes them stop, watch, and engage.
"""
import logging
from datetime import datetime, timezone

from app import storage
from app.models import HookPerformance

logger = logging.getLogger(__name__)


def record_performance(
    clip_id: str,
    views: int,
    likes: int = 0,
    shares: int = 0,
    comments: int = 0,
    watch_rate: float = 0.0,
):
    """
    Record real TikTok performance stats for a posted clip.
    Call this manually, or wire it to a TikTok analytics cron job later.

    watch_rate: % of viewers who watched past the 3-second mark (0.0–1.0)
    """
    clip = storage.get_clip(clip_id)
    if not clip:
        logger.warning(f"[HookLearner] Clip {clip_id} not found.")
        return

    perf = HookPerformance(
        clip_id=clip_id,
        hook_text=clip.hook_text,
        hook_score=clip.hook_score,
        topic=clip.topic,
        views=views,
        likes=likes,
        shares=shares,
        comments=comments,
        watch_rate=watch_rate,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    storage.save_hook_performance(perf)
    logger.info(f"[HookLearner] Recorded performance for {clip_id}: {views:,} views, {watch_rate:.0%} watch rate")


def build_hook_lessons() -> str:
    """
    Build a lessons-learned string from real performance data.
    This gets injected into Claude's prompt to make it smarter.

    Returns empty string if not enough data yet (< 3 clips with data).
    """
    perfs = storage.get_all_hook_performances()
    if len(perfs) < 3:
        return ""  # Not enough data yet

    # Sort by views
    perfs.sort(key=lambda p: p.views, reverse=True)
    total = len(perfs)
    top_n = max(1, total // 3)
    bottom_n = max(1, total // 3)

    top_performers = perfs[:top_n]
    bottom_performers = perfs[-bottom_n:]

    top_lines = "\n".join(
        f'  - [{p.views:,} views, {p.watch_rate:.0%} watch rate] "{p.hook_text}" (topic: {p.topic})'
        for p in top_performers
    )

    bottom_lines = "\n".join(
        f'  - [{p.views:,} views, {p.watch_rate:.0%} watch rate] "{p.hook_text}" (topic: {p.topic})'
        for p in bottom_performers
    )

    avg_views = sum(p.views for p in perfs) / total
    best_topics = _get_best_topics(perfs)

    lessons = f"""
PERFORMANCE DATA FROM MRBADE'S ACTUAL TIKTOK POSTS (use this to guide your choices):

✅ BEST PERFORMING HOOKS (these stopped the scroll):
{top_lines}

❌ WEAKEST PERFORMING HOOKS (these didn't hold attention):
{bottom_lines}

📊 SUMMARY:
- Average views per clip: {avg_views:,.0f}
- Best performing topics: {', '.join(best_topics[:3])}
- Total clips tracked: {total}

Use this data to PRIORITISE hooks and topics similar to the top performers, 
and AVOID patterns similar to the bottom performers.
"""
    return lessons.strip()


def _get_best_topics(perfs: list[HookPerformance]) -> list[str]:
    """Rank topics by average views."""
    topic_stats: dict[str, list[int]] = {}
    for p in perfs:
        topic_stats.setdefault(p.topic, []).append(p.views)
    avg_by_topic = {
        topic: sum(views) / len(views)
        for topic, views in topic_stats.items()
    }
    return sorted(avg_by_topic, key=avg_by_topic.get, reverse=True)


def get_analytics_summary() -> dict:
    """Build a summary dict for the dashboard analytics tab."""
    clips = storage.get_all_clips()
    perfs = storage.get_all_hook_performances()

    status_counts = {}
    for clip in clips:
        status_counts[clip.status.value] = status_counts.get(clip.status.value, 0) + 1

    hook_scores = [c.hook_score for c in clips]
    avg_hook = sum(hook_scores) / len(hook_scores) if hook_scores else 0

    total_views = sum(p.views for p in perfs)
    total_likes = sum(p.likes for p in perfs)
    top_clips = sorted(perfs, key=lambda p: p.views, reverse=True)[:5]

    return {
        "total_clips": len(clips),
        "by_status": status_counts,
        "avg_hook_score": round(avg_hook, 1),
        "total_views": total_views,
        "total_likes": total_likes,
        "top_clips": [
            {
                "clip_id": p.clip_id,
                "hook_text": p.hook_text,
                "topic": p.topic,
                "views": p.views,
                "watch_rate": p.watch_rate,
                "hook_score": p.hook_score,
            }
            for p in top_clips
        ],
        "scheduled_queue": len([c for c in clips if c.status.value == "approved"]),
        "lessons_available": len(perfs) >= 3,
    }
