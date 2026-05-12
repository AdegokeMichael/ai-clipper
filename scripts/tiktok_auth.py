#!/usr/bin/env python3
"""
scripts/tiktok_auth.py

One-time TikTok login — saves a browser session so future posts
don't need to log in again.

Run once on the server:
    source venv/bin/activate
    python scripts/tiktok_auth.py

On a headless server (no monitor), start Xvfb first:
    sudo apt install -y xvfb
    Xvfb :99 -screen 0 1280x800x24 &
    export DISPLAY=:99
    python scripts/tiktok_auth.py

What it does:
- Opens a real Chromium browser window
- Takes you to the TikTok login page
- You log in manually (QR code, phone, email — whatever you prefer)
- Once you are logged in, press ENTER in the terminal
- The session is saved to auth/tiktok_auth.json
- All future posts use this saved session automatically

The session lasts until TikTok invalidates it (typically weeks to months).
Re-run this script whenever posting fails with a session expired error.
"""
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

AUTH_DIR  = Path("auth")
AUTH_FILE = AUTH_DIR / "tiktok_auth.json"


def main():
    print()
    print("━" * 55)
    print("  TikTok Browser — One-Time Login")
    print("━" * 55)
    print()

    # Check Playwright is installed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌  Playwright is not installed.")
        print("    Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    # Check DISPLAY is set (needed for headful browser on server)
    if not os.environ.get("DISPLAY") and sys.platform != "darwin":
        print("⚠️   No DISPLAY environment variable set.")
        print("    On a headless server, run Xvfb first:")
        print()
        print("    sudo apt install -y xvfb")
        print("    Xvfb :99 -screen 0 1280x800x24 &")
        print("    export DISPLAY=:99")
        print("    python scripts/tiktok_auth.py")
        print()
        print("    Or set TIKTOK_HEADLESS=true in .env to use headless mode")
        print("    (less reliable but works without a display).")
        print()
        headless = input("Continue in headless mode anyway? [y/N]: ").strip().lower()
        if headless != "y":
            sys.exit(0)
        headless_mode = True
    else:
        headless_mode = os.getenv("TIKTOK_HEADLESS", "false").lower() == "true"

    AUTH_DIR.mkdir(exist_ok=True)

    print("Opening browser — please log in to TikTok...")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless_mode,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto("https://www.tiktok.com/login", wait_until="networkidle")

        print("━" * 55)
        print()
        print("  Log in to TikTok in the browser window.")
        print("  Use any method: QR code, phone, email, etc.")
        print()
        print("  ⚠️  Make sure you are fully logged in and can")
        print("      see your TikTok feed before pressing ENTER.")
        print()
        print("━" * 55)
        input("  Press ENTER once you are logged in... ")
        print()

        # Verify we are actually logged in
        current_url = page.url
        if "login" in current_url.lower():
            print("⚠️   Still on the login page. Are you sure you logged in?")
            input("  Press ENTER to save anyway, or Ctrl+C to cancel... ")

        # Save the session
        context.storage_state(path=str(AUTH_FILE))
        browser.close()

    print(f"✅  Session saved to {AUTH_FILE}")
    print()
    print("━" * 55)
    print("  Setup complete!")
    print()
    print("  TikTok (Browser) is now available as a posting")
    print("  option in the dashboard on all clip cards.")
    print()
    print("  If posting fails with a session expired error,")
    print("  simply run this script again to refresh.")
    print("━" * 55)
    print()


if __name__ == "__main__":
    main()
