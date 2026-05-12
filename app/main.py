"""
AI Clipper — FastAPI Backend

Production hardening applied:
  - /health endpoint for Docker/load-balancer health checks
  - CORS locked to configurable origins (no more wildcard)
  - Streaming file upload — reads in 1 MB chunks, never loads full video into RAM
  - asyncio.get_event_loop() → asyncio.get_running_loop() (Python 3.10+)
  - All open() calls have explicit encoding
  - /gdrive/status no longer calls systemctl (fails inside Docker)
  - Request size guard before accepting upload
  - Consistent error logging with exc_info
"""
import asyncio
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app.models import ApprovalAction, ClipStatus, ScheduleConfig
from app import storage
from app.pipeline import run_pipeline
from app.platforms import post_to_platform, get_platform_status, get_configured_platforms
from app.scheduler import start_scheduler, stop_scheduler, update_schedule, get_next_run_times
from app.hook_learner import record_performance, get_analytics_summary
from app.overlays import (
    get_overlay_config, save_overlay_config,
    save_template, delete_template,
    list_archived_templates, restore_template,
    get_active_template,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Clipper", version="0.1.0")

# ── CORS ──────────────────────────────────────────────────────────────────────
# Restrict to the actual origins that will use this API.
# Set CORS_ORIGINS in .env as a comma-separated list.
# Example: CORS_ORIGINS=http://localhost:8000,https://yourdomain.com
_raw_origins = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:8000", "http://127.0.0.1:8000"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# Limit pipeline concurrency to avoid OOM on small VMs.
# Each pipeline run loads a Whisper model and runs FFmpeg.
_MAX_PIPELINE_WORKERS = int(os.getenv("MAX_PIPELINE_WORKERS", "2"))
executor = ThreadPoolExecutor(max_workers=_MAX_PIPELINE_WORKERS)

# Upload size guard — reject before reading the body
_MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "2048"))
_MAX_UPLOAD_BYTES = _MAX_UPLOAD_MB * 1024 * 1024

# ── Watcher ───────────────────────────────────────────────────────────────────
_watcher_observer = None


def _maybe_start_watcher():
    global _watcher_observer
    watch_folder = os.getenv("WATCH_FOLDER")
    if watch_folder:
        from app.watcher import start_watcher
        _watcher_observer = start_watcher(run_pipeline)
        logger.info("[Watcher] Auto-watching folder: %s", watch_folder)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    start_scheduler()
    _maybe_start_watcher()
    logger.info("AI Clipper started ")


@app.on_event("shutdown")
async def on_shutdown():
    stop_scheduler()
    global _watcher_observer
    if _watcher_observer:
        from app.watcher import stop_watcher
        stop_watcher(_watcher_observer)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Lightweight liveness probe used by Docker HEALTHCHECK, nginx, and CI.
    Returns 200 as long as the app process is running.
    """
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


# ── Upload & Pipeline ─────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_video(request: Request, file: UploadFile = File(...)):
    """
    Upload a video file and kick off the AI pipeline.

    Streams the upload in 1 MB chunks — never loads the entire video into RAM.
    The pipeline runs in a background thread (non-blocking for the HTTP layer).
    """
    # Guard: reject oversized uploads before writing anything
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File too large. Maximum upload size is {_MAX_UPLOAD_MB} MB.",
        )

    if not file.filename.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
        raise HTTPException(400, "Unsupported file type. Use mp4, mov, avi, mkv, or webm.")

    upload_id = str(uuid.uuid4())[:8]
    ext = Path(file.filename).suffix.lower()
    save_path = UPLOAD_DIR / f"{upload_id}{ext}"

    # Stream to disk in 1 MB chunks — safe for 2 GB videos
    chunk_size = 1024 * 1024  # 1 MB
    total_bytes = 0
    async with aiofiles.open(save_path, "wb") as out:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > _MAX_UPLOAD_BYTES:
                await out.close()
                save_path.unlink(missing_ok=True)
                raise HTTPException(
                    413,
                    f"File exceeded {_MAX_UPLOAD_MB} MB limit while uploading.",
                )
            await out.write(chunk)

    file_size_mb = total_bytes / (1024 * 1024)
    logger.info(
        "Video uploaded: %s (%.1f MB) → %s", file.filename, file_size_mb, save_path
    )

    # Submit pipeline to thread pool — returns immediately to caller
    loop = asyncio.get_running_loop()
    loop.run_in_executor(executor, run_pipeline, str(save_path))

    return {
        "message": "Video uploaded. Pipeline is running.",
        "filename": file.filename,
        "size_mb": round(file_size_mb, 1),
        "note": "Poll GET /clips to see clips as they become ready.",
    }


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ── Clips ─────────────────────────────────────────────────────────────────────

@app.get("/clips")
async def list_clips(status: str = None):
    clips = storage.get_all_clips()
    if status:
        clips = [c for c in clips if c.status.value == status]
    clips.sort(key=lambda c: c.created_at, reverse=True)
    return clips


@app.get("/clips/{clip_id}")
async def get_clip(clip_id: str):
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


@app.post("/clips/{clip_id}/approve")
async def approve_clip(clip_id: str, action: ApprovalAction = None):
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.status != ClipStatus.PENDING:
        raise HTTPException(400, f"Clip is not pending (current status: {clip.status})")

    if action and (action.caption or action.hashtags):
        caption = action.caption or clip.caption
        hashtags = action.hashtags or clip.hashtags
        storage.update_clip_content(clip_id, caption, hashtags)

    storage.update_clip_status(clip_id, ClipStatus.APPROVED)
    logger.info("Clip %s approved ", clip_id)
    return {"status": "approved", "clip_id": clip_id}


@app.post("/clips/{clip_id}/reject")
async def reject_clip(clip_id: str):
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    storage.update_clip_status(clip_id, ClipStatus.REJECTED)
    logger.info("Clip %s rejected ", clip_id)
    return {"status": "rejected", "clip_id": clip_id}


@app.post("/clips/{clip_id}/post")
async def post_clip(clip_id: str, platform: str = "youtube"):
    """
    Post an approved clip to a platform.
    platform: tiktok | youtube | instagram | facebook (default: youtube)
    """
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.status != ClipStatus.APPROVED:
        raise HTTPException(400, "Clip must be approved before posting")

    try:
        result = await post_to_platform(
            platform_key=platform,
            clip_path=clip.clip_path,
            post_text=clip.full_post_text,
            clip_id=clip_id,
            title=clip.topic,
        )
        storage.update_clip_status(
            clip_id,
            ClipStatus.POSTED,
            platform_key=platform,
            publish_id=result.get("publish_id"),
            publish_url=result.get("url"),
        )
        return {
            "status": "posted",
            "platform": result.get("platform_name"),
            "publish_id": result.get("publish_id"),
            "url": result.get("url"),
        }
    except Exception as e:
        logger.error("Posting clip %s to %s failed: %s", clip_id, platform, e, exc_info=True)
        storage.update_clip_status(clip_id, ClipStatus.FAILED)
        raise HTTPException(500, f"Posting to {platform} failed: {e}")


@app.get("/platforms")
async def list_platforms():
    """Return all platforms and their configuration status."""
    return get_platform_status()


@app.get("/uploadpost/verify")
async def verify_uploadpost():
    """Verify the Upload-Post API key and profile are configured correctly."""
    import httpx

    api_key = os.getenv("UPLOADPOST_API_KEY", "")
    if not api_key or api_key.startswith("your_"):
        raise HTTPException(400, "UPLOADPOST_API_KEY is not set in .env")

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.upload-post.com/api/uploadposts/me",
            headers={"Authorization": f"Apikey {api_key}"},
        )

    if r.status_code == 200:
        return {"status": "ok", "message": "Upload-Post API key is valid.", "account": r.json()}
    raise HTTPException(r.status_code, f"Upload-Post rejected key ({r.status_code}): {r.text[:200]}")


@app.post("/clips/{clip_id}/performance")
async def record_clip_performance(
    clip_id: str,
    views: int = 0,
    likes: int = 0,
    shares: int = 0,
    comments: int = 0,
    watch_rate: float = 0.0,
):
    """
    Record real platform performance stats for a clip.
    Feeds back into Claude's hook scoring to improve future clips.
    watch_rate = decimal 0.0–1.0 (e.g. 0.65 = 65% watch past 3 s)
    """
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    record_performance(
        clip_id, views=views, likes=likes,
        shares=shares, comments=comments, watch_rate=watch_rate,
    )
    return {"status": "recorded", "clip_id": clip_id, "views": views}


# ── Schedule ──────────────────────────────────────────────────────────────────

@app.get("/schedule/config")
async def get_schedule_config():
    return storage.get_schedule_config()


@app.patch("/schedule/config")
async def patch_schedule_config(config: ScheduleConfig):
    update_schedule(config)
    return {"status": "updated", "config": config}


@app.get("/schedule/queue")
async def get_schedule_queue():
    return storage.get_all_scheduled_posts()


@app.get("/schedule/next-runs")
async def get_next_runs():
    return get_next_run_times()


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.get("/analytics")
async def get_analytics():
    return get_analytics_summary()


@app.get("/ai/provider")
async def get_ai_provider():
    """Return the currently active AI provider and model."""
    from app.ai_brain import get_provider_info
    return get_provider_info()


@app.get("/gdrive/status")
async def get_gdrive_status():
    """
    Return Google Drive sync status.
    NOTE: systemctl is not available inside Docker — service status is inferred
    from log recency instead of querying systemd directly.
    """
    from datetime import timedelta

    log_file  = Path("logs/gdrive_sync.log")
    seen_file = Path("logs/gdrive_seen.txt")

    # Infer "active" from whether a log entry appeared in the last 2× sync interval
    sync_interval = int(os.getenv("GDRIVE_SYNC_INTERVAL", "30"))
    service_active = False
    if log_file.exists():
        age_seconds = (
            datetime.now(timezone.utc).timestamp() - log_file.stat().st_mtime
        )
        service_active = age_seconds < sync_interval * 2

    last_lines: list[str] = []
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        last_lines = lines[-5:] if lines else []

    files_synced = 0
    if seen_file.exists():
        files_synced = sum(
            1 for line in seen_file.read_text(encoding="utf-8").splitlines() if line.strip()
        )

    sa_file   = os.getenv("GDRIVE_SERVICE_ACCOUNT_FILE", "")
    folder_id = os.getenv("GDRIVE_FOLDER_ID", "")
    configured = bool(sa_file and folder_id and Path(sa_file).exists())

    return {
        "service_active":        service_active,
        "auth_method":           "service_account",
        "folder_id":             folder_id,
        "local_folder":          os.getenv("WATCH_FOLDER", ""),
        "sync_interval_seconds": sync_interval,
        "total_files_synced":    files_synced,
        "recent_log":            last_lines,
        "configured":            configured,
        "token_expires":         False,
    }


# ── Overlay / Brand Template ───────────────────────────────────────────────────

@app.get("/overlay/config")
async def get_overlay():
    config = get_overlay_config()
    template = get_active_template()
    return {
        **config,
        "template_exists": template is not None,
        "template_path": template,
        "archived_templates": list_archived_templates(),
    }


@app.patch("/overlay/config")
async def update_overlay_config(enabled: bool = None, similarity: float = None):
    updates = {}
    if enabled is not None:
        updates["enabled"] = enabled
    if similarity is not None:
        updates["similarity"] = max(0.01, min(0.5, similarity))
    save_overlay_config(updates)
    return {"status": "updated", "config": get_overlay_config()}


@app.post("/overlay/template")
async def upload_template(file: UploadFile = File(...)):
    """Upload a new brand template image. Recommended: 1080×1920 PNG (9:16)."""
    if not file.filename.lower().endswith((".png", ".jpg", ".jpeg")):
        raise HTTPException(400, "Template must be a PNG or JPG image.")

    contents = await file.read()
    size_kb = len(contents) / 1024

    if size_kb > 10240:
        raise HTTPException(400, "Template file is too large. Maximum 10 MB.")

    template_path = save_template(contents, file.filename)
    logger.info("New brand template uploaded: %s (%.1f KB)", file.filename, size_kb)
    return {
        "status": "uploaded",
        "filename": file.filename,
        "size_kb": round(size_kb, 1),
        "path": template_path,
    }


@app.delete("/overlay/template")
async def remove_template():
    delete_template()
    return {"status": "deleted"}


@app.get("/overlay/template/preview")
async def preview_template():
    template = get_active_template()
    if not template or not Path(template).exists():
        raise HTTPException(404, "No template found")
    media_type = "image/png" if template.endswith(".png") else "image/jpeg"
    return FileResponse(template, media_type=media_type)


@app.post("/overlay/template/restore/{filename}")
async def restore_archived_template(filename: str):
    try:
        path = restore_template(filename)
        return {"status": "restored", "path": path}
    except FileNotFoundError:
        raise HTTPException(404, f"Archive not found: {filename}")


# ── Media serving ─────────────────────────────────────────────────────────────

@app.get("/clips/{clip_id}/video")
async def serve_video(clip_id: str):
    clip = storage.get_clip(clip_id)
    if not clip or not Path(clip.clip_path).exists():
        raise HTTPException(404, "Video file not found")
    return FileResponse(clip.clip_path, media_type="video/mp4")


@app.get("/clips/{clip_id}/thumb")
async def serve_thumbnail(clip_id: str):
    clip = storage.get_clip(clip_id)
    if not clip or not clip.thumbnail_path or not Path(clip.thumbnail_path).exists():
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(clip.thumbnail_path, media_type="image/jpeg")


@app.get("/clips/{clip_id}/download")
async def download_clip(clip_id: str):
    """Download the clip video with a clean filename (triggers Save As in browser)."""
    clip = storage.get_clip(clip_id)
    if not clip or not Path(clip.clip_path).exists():
        raise HTTPException(404, "Video file not found")

    safe_topic = "".join(c if c.isalnum() or c in " -_" else "" for c in clip.topic)
    safe_topic = safe_topic.strip().replace(" ", "_")[:50]
    filename = f"{clip_id}_{safe_topic}.mp4"

    return FileResponse(
        clip.clip_path,
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/clips/{clip_id}/download-package")
async def download_package(clip_id: str):
    """
    Download a ZIP with: video, caption.txt, hashtags.txt, post.txt, metadata.json.
    Built in memory — clips are typically < 200 MB so this is acceptable.
    """
    import io
    import json
    import zipfile
    from fastapi.responses import StreamingResponse

    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if not Path(clip.clip_path).exists():
        raise HTTPException(404, "Video file not found")

    safe_topic = "".join(c if c.isalnum() or c in " -_" else "" for c in clip.topic)
    safe_topic = safe_topic.strip().replace(" ", "_")[:50]
    zip_filename = f"{clip_id}_{safe_topic}.zip"
    video_name   = f"{clip_id}_{safe_topic}.mp4"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(clip.clip_path, video_name)
        zf.writestr("caption.txt", clip.caption)
        hashtag_line = " ".join(f"#{h.lstrip('#')}" for h in clip.hashtags)
        zf.writestr("hashtags.txt", hashtag_line)
        zf.writestr("post.txt", clip.full_post_text)
        meta = {
            "clip_id":          clip.id,
            "topic":            clip.topic,
            "hook_text":        clip.hook_text,
            "hook_score":       clip.hook_score,
            "duration_seconds": clip.duration,
            "start_time":       clip.start_time,
            "end_time":         clip.end_time,
            "status":           clip.status,
            "created_at":       clip.created_at,
            "post_urls":        clip.post_urls,
        }
        zf.writestr("metadata.json", json.dumps(meta, indent=2))

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@app.delete("/clips/{clip_id}")
async def delete_clip(clip_id: str):
    """Permanently delete a clip and its files from disk. Cannot be undone."""
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")

    deleted = storage.delete_clip(clip_id)
    if not deleted:
        raise HTTPException(500, "Failed to delete clip")

    logger.info("Clip %s deleted by user.", clip_id)
    return {"status": "deleted", "clip_id": clip_id}