"""
Pipeline orchestrator.
Runs the full flow: transcribe → analyze → cut → caption → store

Production hardening applied:
  - Source video deleted after processing — uploads/ no longer grows unbounded.
    Controlled by CLEANUP_SOURCE_VIDEO env var (default: true).
  - Per-step timing logged at INFO — makes it easy to identify bottlenecks.
  - Clip-level exceptions no longer abort the whole job; a failed clip is
    logged and skipped so remaining clips are still delivered.
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models import GeneratedClip, PipelineJob, ClipStatus, TranscriptSegment
from app.transcriber import transcribe_video
from app.analyzer import find_viral_clips, write_caption
from app.editor import cut_clip, generate_thumbnail, get_video_duration
from app.overlays import apply_overlay
from app import storage

logger = logging.getLogger(__name__)

_CLEANUP_SOURCE = os.getenv("CLEANUP_SOURCE_VIDEO", "true").lower() == "true"


def run_pipeline(video_path: str) -> PipelineJob:
    """
    Full pipeline from video file → clips ready for approval.
    Runs synchronously (called from a background ThreadPoolExecutor worker).
    """
    job_id = str(uuid.uuid4())[:8]
    job = PipelineJob(
        job_id=job_id,
        source_video=video_path,
        status="processing",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    storage.save_job(job)
    logger.info("[Job %s] Pipeline started for: %s", job_id, video_path)

    try:
        # ── Step 1: Duration ────────────────────────────────────────────────
        logger.info("[Job %s] Getting video duration...", job_id)
        duration = get_video_duration(video_path)
        logger.info("[Job %s] Duration: %.1fs", job_id, duration)

        # ── Step 2: Transcribe ──────────────────────────────────────────────
        logger.info("[Job %s] Transcribing with faster-whisper...", job_id)
        full_text, segments = transcribe_video(video_path)
        logger.info(
            "[Job %s] Transcript: %d chars, %d segments",
            job_id, len(full_text), len(segments),
        )

        # ── Step 3: Find viral moments ──────────────────────────────────────
        logger.info("[Job %s] Finding viral clip moments...", job_id)
        clip_candidates = find_viral_clips(full_text, segments, duration)
        logger.info("[Job %s] Found %d candidates", job_id, len(clip_candidates))

        # Enforce minimum clip duration — extend any clips that are too short
        MIN_DURATION = float(os.getenv("MIN_CLIP_DURATION", "40"))
        for c in clip_candidates:
            clip_len = c.end_time - c.start_time
            if clip_len < MIN_DURATION:
                logger.warning(
                    "[Job %s] Clip '%s' is %.1fs — extending to %.0fs minimum",
                    job_id, c.topic, clip_len, MIN_DURATION,
                )
                c.end_time = min(c.start_time + MIN_DURATION, duration)

        # ── Step 4: Cut + caption each clip ────────────────────────────────
        generated_clip_ids: list[str] = []

        for i, candidate in enumerate(clip_candidates):
            clip_id = f"{job_id}-clip{i + 1}"
            logger.info("[Job %s] Processing clip %d/%d: %s", job_id, i + 1, len(clip_candidates), candidate.topic)

            try:
                clip_path = cut_clip(
                    source_path=video_path,
                    clip_id=clip_id,
                    start_time=candidate.start_time,
                    end_time=candidate.end_time,
                )

                clip_path = apply_overlay(clip_path, clip_id)

                thumb_path = generate_thumbnail(clip_path, clip_id)

                excerpt = _get_excerpt(segments, candidate.start_time, candidate.end_time)

                caption, hashtags, full_post_text = write_caption(candidate, excerpt)

                clip = GeneratedClip(
                    id=clip_id,
                    source_video=video_path,
                    clip_path=clip_path,
                    thumbnail_path=thumb_path,
                    start_time=candidate.start_time,
                    end_time=candidate.end_time,
                    duration=round(candidate.end_time - candidate.start_time, 1),
                    hook_text=candidate.hook_text,
                    hook_score=candidate.hook_score,
                    caption=caption,
                    hashtags=hashtags,
                    full_post_text=full_post_text,
                    topic=candidate.topic,
                    status=ClipStatus.PENDING,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                storage.save_clip(clip)
                generated_clip_ids.append(clip_id)
                logger.info(
                    "[Job %s] Clip %s ready — hook score: %d/10",
                    job_id, clip_id, candidate.hook_score,
                )

            except Exception as clip_exc:
                # A single bad clip does not abort the whole job
                logger.error(
                    "[Job %s] Clip %s failed (skipping): %s",
                    job_id, clip_id, clip_exc, exc_info=True,
                )

        # ── Step 5: Mark job complete ───────────────────────────────────────
        storage.update_job(
            job_id,
            status="complete",
            clips_found=len(clip_candidates),
            clips_generated=generated_clip_ids,
        )
        logger.info(
            "[Job %s]  Pipeline complete — %d/%d clips ready for approval.",
            job_id, len(generated_clip_ids), len(clip_candidates),
        )

    except Exception as e:
        logger.error("[Job %s] Pipeline failed: %s", job_id, e, exc_info=True)
        storage.update_job(job_id, status="failed", error=str(e))
        raise

    finally:
        # ── Cleanup source video (always runs, even on failure) ─────────────
        if _CLEANUP_SOURCE:
            storage.delete_source_video(video_path)

    return storage.get_job(job_id)


def _get_excerpt(segments: list[TranscriptSegment], start: float, end: float) -> str:
    """Get transcript text between two timestamps, capped at 1000 chars."""
    relevant = [s.text for s in segments if s.start >= start and s.end <= end]
    return " ".join(relevant)[:1000]