"""
Video editing with FFmpeg.
Cuts clips from source video at precise timestamps.
Also generates thumbnails for the approval UI.

Production hardening applied:
  - subprocess.run() now always has a timeout — prevents pipeline workers
    from hanging forever on a corrupt or malformed input video.
  - Directories created lazily (in functions) rather than at import time
    to avoid side-effects during testing.
  - Explicit json import moved to top level.
"""
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR    = Path("output/clips")
THUMBNAIL_DIR = Path("output/thumbnails")

# Timeout constants (seconds)
_FFMPEG_CUT_TIMEOUT     = 600   # 10 min — generous for a 90s clip re-encode
_FFMPEG_THUMB_TIMEOUT   = 30
_FFPROBE_TIMEOUT        = 30


def _ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)


def cut_clip(
    source_path: str,
    clip_id: str,
    start_time: float,
    end_time: float,
) -> str:
    """
    Cut a clip from source video using FFmpeg.
    Re-encodes for clean cuts at exact timestamps.
    Returns the path to the output clip.
    """
    _ensure_dirs()
    output_path = str(OUTPUT_DIR / f"{clip_id}.mp4")
    duration = end_time - start_time

    cmd = [
        "ffmpeg",
        "-y",                          # Overwrite if exists
        "-ss", str(start_time),        # Seek before input (fast seek)
        "-i", source_path,
        "-t", str(duration),
        "-c:v", "libx264",
        "-c:a", "aac",
        "-preset", "fast",
        "-crf", "23",
        "-movflags", "+faststart",     # Optimise for web playback
        "-vf", (
            "scale=1080:1920:"
            "force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
        ),
        output_path,
    ]

    logger.info("Cutting clip %s: %.1fs → %.1fs", clip_id, start_time, end_time)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_FFMPEG_CUT_TIMEOUT,
    )

    if result.returncode != 0:
        logger.error("FFmpeg error for %s:\n%s", clip_id, result.stderr[-1000:])
        raise RuntimeError(f"FFmpeg failed for clip {clip_id}: {result.stderr[-500:]}")

    logger.info("Clip saved: %s", output_path)
    return output_path


def generate_thumbnail(clip_path: str, clip_id: str, timestamp: float = 1.0) -> str:
    """
    Extract a thumbnail from a clip at the given timestamp.
    Returns path to thumbnail image, or empty string on failure.
    """
    _ensure_dirs()
    thumb_path = str(THUMBNAIL_DIR / f"{clip_id}.jpg")

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(timestamp),
        "-i", clip_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_FFMPEG_THUMB_TIMEOUT,
    )
    if result.returncode != 0:
        logger.warning("Thumbnail generation failed for %s: %s", clip_id, result.stderr[-300:])
        return ""

    return thumb_path


def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        video_path,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_FFPROBE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")

    data = json.loads(result.stdout)
    return float(data["format"]["duration"])