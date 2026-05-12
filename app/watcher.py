"""
Folder Watcher — auto-triggers the pipeline when a video is dropped into the watch folder.

Production hardening applied:
  - Duplicate filename collision fix — appends a short UUID suffix before
    moving to uploads/ so two files with the same name never silently
    overwrite each other.
  - Watchdog stability poll moved to a named helper for clarity.
  - Daemon thread named for easier identification in thread dumps.
"""
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

logger = logging.getLogger(__name__)

WATCH_DIR     = Path(os.getenv("WATCH_FOLDER", "watch_inbox"))
PROCESSED_DIR = Path("watch_processed")
SUPPORTED_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


class VideoDropHandler(FileSystemEventHandler):
    """Handles new video files dropped into the watch folder."""

    def __init__(self, pipeline_fn):
        self.pipeline_fn = pipeline_fn
        self._processing: set[str] = set()

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return

        path = Path(event.src_path)
        if path.suffix.lower() not in SUPPORTED_EXTS:
            logger.debug("[Watcher] Ignoring non-video file: %s", path.name)
            return

        if str(path) in self._processing:
            return

        logger.info("[Watcher] Detected: %s — waiting for write to complete...", path.name)
        self._processing.add(str(path))

        thread = threading.Thread(
            target=self._process,
            args=(path,),
            daemon=True,
            name=f"watcher-{path.stem[:20]}",
        )
        thread.start()

    def _process(self, path: Path):
        """Wait for file to finish writing, then hand it off to the pipeline."""
        try:
            _wait_for_stable(path)
        except FileNotFoundError:
            logger.warning("[Watcher] File disappeared before processing: %s", path.name)
            self._processing.discard(str(path))
            return

        logger.info("[Watcher] File stable — starting pipeline for: %s", path.name)

        # Build a collision-safe destination path.
        # Two uploads named "interview.mp4" would silently overwrite without this.
        short_id = str(uuid.uuid4())[:6]
        stem = path.stem[:40]          # truncate very long names
        dest = Path("uploads") / f"{stem}_{short_id}{path.suffix.lower()}"
        dest.parent.mkdir(exist_ok=True)

        try:
            shutil.move(str(path), str(dest))
        except (OSError, shutil.Error) as e:
            logger.error("[Watcher] Failed to move %s → %s: %s", path.name, dest, e)
            self._processing.discard(str(path))
            return

        # Archive a record of what was processed
        PROCESSED_DIR.mkdir(exist_ok=True)
        (PROCESSED_DIR / path.name).with_suffix(".processed").touch()

        try:
            self.pipeline_fn(str(dest))
            logger.info("[Watcher] ✅ Pipeline complete for: %s", path.name)
        except Exception as e:
            logger.error("[Watcher] ❌ Pipeline failed for %s: %s", path.name, e, exc_info=True)
        finally:
            self._processing.discard(str(path))


def _wait_for_stable(path: Path, stable_ticks: int = 2, poll_interval: float = 1.0, max_wait: int = 60):
    """
    Poll until the file size stops changing.
    Raises FileNotFoundError if the file disappears while waiting.
    """
    prev_size = -1
    consecutive_stable = 0

    for _ in range(max_wait):
        time.sleep(poll_interval)
        current_size = path.stat().st_size   # raises FileNotFoundError if gone
        if current_size == prev_size:
            consecutive_stable += 1
            if consecutive_stable >= stable_ticks:
                return
        else:
            consecutive_stable = 0
        prev_size = current_size


def start_watcher(pipeline_fn) -> Observer:
    """
    Start watching the inbox folder.
    pipeline_fn: callable that takes a video_path string and runs the pipeline.
    Returns the Observer so the caller can stop it later.
    """
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("[Watcher] 👁  Watching folder: %s", WATCH_DIR.resolve())
    logger.info("[Watcher] Drop any video here and the pipeline will start automatically.")

    handler  = VideoDropHandler(pipeline_fn)
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=False)
    observer.start()
    return observer


def stop_watcher(observer: Observer):
    observer.stop()
    observer.join()
    logger.info("[Watcher] Stopped.")


# ── Standalone mode ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from app.pipeline import run_pipeline

    obs = start_watcher(run_pipeline)
    print(f"\n👁  Watching: {WATCH_DIR.resolve()}")
    print("Drop a video file in that folder to start the pipeline automatically.")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_watcher(obs)
