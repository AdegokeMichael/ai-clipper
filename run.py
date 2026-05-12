#!/usr/bin/env python3
"""
AI Clipper — Entry Point
Run: python run.py

First time? Run: python setup_check.py
"""
import os
import sys
import uvicorn
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Pre-flight hints ──────────────────────────────────────────────────────────
def _check_critical():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key or key.startswith("your_"):
        print("\n\033[91m ANTHROPIC_API_KEY is not set in your .env file.\033[0m")
        print("   Run \033[93mpython setup_check.py\033[0m for full diagnostics.\n")
        sys.exit(1)

    import shutil
    if not shutil.which("ffmpeg"):
        print("\n\033[91m FFmpeg is not installed or not in PATH.\033[0m")
        print("   Mac:   brew install ffmpeg")
        print("   Linux: apt install ffmpeg\n")
        sys.exit(1)


def _banner():
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    watch = os.getenv("WATCH_FOLDER", "")
    tiktok = bool(os.getenv("TIKTOK_ACCESS_TOKEN", "").strip())

    print("\n\033[1m\033[96m ╔══════════════════════════════╗")
    print(" ║   AI Clipper  v0.1.0          ║")
    print(" ╚══════════════════════════════╝\033[0m")
    print(f"\n  \033[92m●\033[0m  Dashboard  →  \033[96mhttp://localhost:{port}\033[0m")
    print(f"  {'✅' if tiktok else '⚠️ '} TikTok API  →  {'Connected' if tiktok else 'Not configured (set TIKTOK_ACCESS_TOKEN)'}")
    print(f"  {'✅' if watch else '○ '} Watch Folder →  {Path(watch).resolve() if watch else 'Disabled (set WATCH_FOLDER in .env)'}")
    print(f"\n  \033[90mPress Ctrl+C to stop\033[0m\n")


if __name__ == "__main__":
    _check_critical()
    _banner()

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,          # Set True for development
        log_level="info",
        access_log=False,      # Reduce noise; pipeline logs are enough
    )
