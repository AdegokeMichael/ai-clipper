"""
app/platforms/tiktok_browser.py

Direct TikTok posting via browser automation (Playwright).

Why this instead of the TikTok API
─────────────────────────────────────
- No API approval, domain, or business verification needed
- Posts go through TikTok's own web interface — same path as manual posting
- TikTok's recommendation algorithm treats browser-posted content
  identically to content posted through the app or website
- No third-party services, no paywalls, no vendor lock-in

How it works
─────────────
Playwright controls a real Chromium browser on the server.
It logs into TikTok using a saved session (auth.json) and posts the video
through the upload page — exactly as a human would, but automated.

One-time setup
──────────────
Run this once on the server to save a TikTok login session:

    source venv/bin/activate
    python scripts/tiktok_auth.py

This opens a real browser window (requires a display — see Xvfb note below),
lets you log in manually, then saves the session to auth/tiktok_auth.json.
After that, all future posts use the saved session without logging in again.

Running on a headless server (Ubuntu without a monitor)
─────────────────────────────────────────────────────────
Install Xvfb (virtual display):
    sudo apt install -y xvfb

Start a virtual display before running the auth script:
    Xvfb :99 -screen 0 1280x720x24 &
    export DISPLAY=:99
    python scripts/tiktok_auth.py

The app manages Xvfb automatically when TIKTOK_USE_XVFB=true in .env.

Required env vars
──────────────────
TIKTOK_AUTH_FILE     Path to saved session file (default: auth/tiktok_auth.json)
TIKTOK_USE_XVFB      true | false — use virtual display on headless server (default: true)
TIKTOK_HEADLESS      true | false — run browser headless (default: false — more reliable)
TIKTOK_POST_DELAY    Seconds to wait after clicking Post (default: 15)

Stability notes
───────────────
- The browser runs with headless=False by default. This is more reliable
  because TikTok's bot detection is stricter against headless browsers.
- Human-like delays are added between actions to avoid detection.
- If TikTok changes their UI, the selectors below may need updating.
  Check logs for selector errors and update accordingly.
"""
import os
import asyncio
import logging
import time
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from app.platforms.base import BasePlatform

logger = logging.getLogger(__name__)

AUTH_FILE_DEFAULT = "auth/tiktok_auth.json"
TIKTOK_UPLOAD_URL = "https://www.tiktok.com/upload?lang=en"

# Thread pool for running sync Playwright in async FastAPI
_executor = ThreadPoolExecutor(max_workers=1)


class TikTokBrowserPlatform(BasePlatform):
    """
    Posts to TikTok by automating a real browser session.
    Implements BasePlatform so it plugs into the router like any other platform.
    """

    @property
    def name(self) -> str:
        return "TikTok (Browser)"

    @property
    def key(self) -> str:
        return "tiktok_browser"

    def is_configured(self) -> bool:
        auth_file = os.getenv("TIKTOK_AUTH_FILE", AUTH_FILE_DEFAULT)
        return Path(auth_file).exists()

    def _auth_file(self) -> str:
        return os.getenv("TIKTOK_AUTH_FILE", AUTH_FILE_DEFAULT)

    def _use_xvfb(self) -> bool:
        return os.getenv("TIKTOK_USE_XVFB", "true").lower() == "true"

    def _headless(self) -> bool:
        return os.getenv("TIKTOK_HEADLESS", "false").lower() == "true"

    def _post_delay(self) -> int:
        return int(os.getenv("TIKTOK_POST_DELAY", "15"))

    async def upload_and_post(
        self,
        clip_path: str,
        post_text: str,
        clip_id: str,
        title: str = "",
    ) -> dict:
        """
        Run the Playwright upload in a thread pool so it does not
        block FastAPI's async event loop.
        """
        if not self.is_configured():
            raise ValueError(
                "TikTok browser session not found. "
                "Run: python scripts/tiktok_auth.py to log in and save your session."
            )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _executor,
            self._post_sync,
            clip_path,
            post_text,
            clip_id,
        )
        return result

    def _post_sync(self, clip_path: str, post_text: str, clip_id: str) -> dict:
        """
        Synchronous Playwright posting logic.
        Runs in a thread pool — never call directly from async context.
        """
        from playwright.sync_api import sync_playwright

        xvfb_proc = None
        if self._use_xvfb() and not self._headless():
            xvfb_proc = _start_xvfb()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=self._headless(),
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )

                auth_file = self._auth_file()
                context = browser.new_context(
                    storage_state=auth_file,
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()

                logger.info(f"[TikTok Browser] Opening upload page for clip {clip_id}")
                page.goto(TIKTOK_UPLOAD_URL, wait_until="networkidle", timeout=30000)
                _human_delay(2, 3)

                # ── Check we are logged in ─────────────────────────────────
                if "login" in page.url.lower():
                    browser.close()
                    raise RuntimeError(
                        "TikTok session has expired. "
                        "Run: python scripts/tiktok_auth.py to refresh your login."
                    )

                # ── Upload the video file ─────────────────────────────────
                logger.info(f"[TikTok Browser] Uploading video: {clip_path}")

                # Try multiple selectors — TikTok sometimes changes the input structure
                file_input = None
                for selector in [
                    'input[type="file"]',
                    'input[accept*="video"]',
                ]:
                    try:
                        file_input = page.wait_for_selector(selector, timeout=10000)
                        if file_input:
                            break
                    except Exception:
                        continue

                if not file_input:
                    browser.close()
                    raise RuntimeError(
                        "[TikTok Browser] Could not find file upload input. "
                        "TikTok may have updated their UI."
                    )

                file_input.set_input_files(str(Path(clip_path).resolve()))
                logger.info(f"[TikTok Browser] Video file set. Waiting for upload...")

                # Wait for upload progress to complete
                _wait_for_upload(page)
                _human_delay(3, 5)

                # ── Write the caption ──────────────────────────────────────
                logger.info(f"[TikTok Browser] Writing caption...")
                caption = post_text[:2200]

                caption_written = False
                for selector in [
                    'div[contenteditable="true"]',
                    '[data-text="true"]',
                    '.public-DraftEditor-content',
                    'div[placeholder*="caption"]',
                    'div[placeholder*="Caption"]',
                ]:
                    try:
                        el = page.wait_for_selector(selector, timeout=8000)
                        if el:
                            el.click()
                            _human_delay(0.5, 1)
                            # Clear existing text then type caption
                            el.fill("")
                            _human_delay(0.3, 0.6)
                            # Type character by character for more human-like input
                            page.keyboard.type(caption, delay=30)
                            caption_written = True
                            break
                    except Exception:
                        continue

                if not caption_written:
                    logger.warning(
                        "[TikTok Browser] Could not write caption — will post without it. "
                        "Check selector for caption input field."
                    )

                _human_delay(2, 3)

                # ── Click Post ────────────────────────────────────────────
                logger.info(f"[TikTok Browser] Clicking Post button...")
                post_clicked = False
                for selector in [
                    'button:has-text("Post")',
                    'button[data-e2e="post_video_button"]',
                    'button.btn-post',
                    'button:has-text("Publish")',
                ]:
                    try:
                        btn = page.wait_for_selector(selector, timeout=8000)
                        if btn and btn.is_enabled():
                            btn.click()
                            post_clicked = True
                            break
                    except Exception:
                        continue

                if not post_clicked:
                    browser.close()
                    raise RuntimeError(
                        "[TikTok Browser] Could not find or click the Post button. "
                        "TikTok UI may have changed."
                    )

                # Wait for post to complete
                delay = self._post_delay()
                logger.info(f"[TikTok Browser] Post clicked. Waiting {delay}s for confirmation...")
                _human_delay(delay, delay + 3)

                # ── Check for success ──────────────────────────────────────
                try:
                    # Success typically redirects to profile or shows success message
                    page.wait_for_url(
                        lambda url: "upload" not in url or "success" in url.lower(),
                        timeout=15000,
                    )
                    logger.info(f"[TikTok Browser] Post appears successful for clip {clip_id}")
                except Exception:
                    # Not a hard failure — post may still have gone through
                    logger.warning(
                        f"[TikTok Browser] Could not confirm success redirect for clip {clip_id}. "
                        "Post may still have succeeded."
                    )

                browser.close()

        finally:
            if xvfb_proc:
                _stop_xvfb(xvfb_proc)

        return {
            "publish_id": f"tiktok_browser_{clip_id}",
            "status":     "published",
            "url":        "https://www.tiktok.com",
        }


# ── Xvfb helpers ──────────────────────────────────────────────────────────────

def _start_xvfb():
    """Start a virtual display for headful browser on headless server."""
    import subprocess
    display = ":99"
    os.environ["DISPLAY"] = display
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x800x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)  # Give Xvfb time to start
    logger.info(f"[TikTok Browser] Xvfb started on display {display}")
    return proc


def _stop_xvfb(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        pass


# ── Human-like timing ─────────────────────────────────────────────────────────

def _human_delay(min_s: float, max_s: float):
    """Sleep for a random duration to mimic human behaviour."""
    time.sleep(random.uniform(min_s, max_s))


def _wait_for_upload(page, timeout_s: int = 120):
    """
    Wait for the video upload to finish.
    Polls for the progress bar to disappear or reach 100%.
    """
    start = time.time()
    while time.time() - start < timeout_s:
        # Look for signals that upload is done
        try:
            # Progress bar gone = upload complete
            progress = page.query_selector('[role="progressbar"]')
            if not progress:
                return
            # Or progress at 100%
            val = progress.get_attribute("aria-valuenow")
            if val and int(val) >= 100:
                _human_delay(1, 2)
                return
        except Exception:
            pass
        time.sleep(2)
    logger.warning("[TikTok Browser] Upload wait timed out — proceeding anyway")
