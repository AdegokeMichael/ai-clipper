#!/usr/bin/env python3
"""
setup_check.py — Run this before your first launch.
Verifies: Python version, FFmpeg, faster-whisper, Anthropic API key,
          TikTok token (optional), folder permissions.
"""
import sys
import os
import subprocess
import importlib
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

ok    = f"{GREEN}✅{RESET}"
fail  = f"{RED}❌{RESET}"
warn  = f"{YELLOW}⚠️ {RESET}"
info  = f"{CYAN}ℹ️ {RESET}"

errors = 0
warnings = 0


def check(label: str, passed: bool, detail: str = "", is_warning: bool = False):
    global errors, warnings
    if passed:
        print(f"  {ok} {label}" + (f"  {CYAN}({detail}){RESET}" if detail else ""))
    elif is_warning:
        warnings += 1
        print(f"  {warn} {label}" + (f"  {YELLOW}{detail}{RESET}" if detail else ""))
    else:
        errors += 1
        print(f"  {fail} {label}" + (f"  {RED}{detail}{RESET}" if detail else ""))


print(f"\n{BOLD}MrBade AutoPoster — Setup Check{RESET}")
print("=" * 45)

# ── Python version ────────────────────────────────────────────────────────────
print(f"\n{BOLD}Python{RESET}")
major, minor = sys.version_info[:2]
check(
    f"Python {major}.{minor}",
    major == 3 and minor >= 10,
    detail=sys.executable if major == 3 and minor >= 10 else f"Need Python 3.10+, got {major}.{minor}",
    is_warning=False,
)

# ── FFmpeg ────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}FFmpeg{RESET}")
for binary in ["ffmpeg", "ffprobe"]:
    result = subprocess.run(["which", binary], capture_output=True, text=True)
    found = result.returncode == 0
    path = result.stdout.strip() if found else ""
    check(binary, found, detail=path, is_warning=False)
    if not found:
        print(f"       → Install: brew install ffmpeg  (Mac) | apt install ffmpeg  (Linux)")

# ── Python packages ───────────────────────────────────────────────────────────
print(f"\n{BOLD}Python packages{RESET}")
required_packages = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("faster_whisper", "faster-whisper"),
    ("anthropic", "anthropic"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
    ("apscheduler", "apscheduler"),
    ("watchdog", "watchdog"),
    ("dotenv", "python-dotenv"),
    ("aiofiles", "aiofiles"),
    ("rich", "rich"),
]

for import_name, pkg_name in required_packages:
    try:
        mod = importlib.import_module(import_name)
        version = getattr(mod, "__version__", "?")
        check(pkg_name, True, detail=f"v{version}")
    except ImportError:
        check(pkg_name, False, detail=f"Run: pip install {pkg_name}")

# ── Environment / API keys ────────────────────────────────────────────────────
print(f"\n{BOLD}Environment (.env){RESET}")

env_file = Path(".env")
check(".env file exists", env_file.exists(),
      detail="Copy .env.example to .env and fill in your keys")

anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
check(
    "ANTHROPIC_API_KEY",
    bool(anthropic_key and not anthropic_key.startswith("your_")),
    detail="Not set or still placeholder" if not anthropic_key else f"sk-...{anthropic_key[-4:]}",
)

tiktok_token = os.getenv("TIKTOK_ACCESS_TOKEN", "")
check(
    "TIKTOK_ACCESS_TOKEN",
    bool(tiktok_token and not tiktok_token.startswith("your_")),
    detail="Not set — TikTok posting will be disabled until you add this",
    is_warning=True,
)

whisper_model = os.getenv("WHISPER_MODEL", "base")
check(f"WHISPER_MODEL={whisper_model}", True, detail="(can be: tiny/base/small/medium/large-v3)")

# ── Anthropic API connectivity ────────────────────────────────────────────────
if anthropic_key and not anthropic_key.startswith("your_"):
    print(f"\n{BOLD}Anthropic API{RESET}")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        check("API connection", True, detail=f"Model: {msg.model}")
    except Exception as e:
        check("API connection", False, detail=str(e)[:80])

# ── Folder permissions ────────────────────────────────────────────────────────
print(f"\n{BOLD}Folders & permissions{RESET}")
folders = ["uploads", "output/clips", "output/thumbnails", "output/store", "static"]
for folder in folders:
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    writable = os.access(str(path), os.W_OK)
    check(f"{folder}/", writable, detail="Not writable!" if not writable else "")

# ── faster-whisper model download ─────────────────────────────────────────────
print(f"\n{BOLD}Whisper model{RESET}")
try:
    from faster_whisper import WhisperModel
    model_size = os.getenv("WHISPER_MODEL", "base")
    print(f"  {info} Downloading '{model_size}' model on first use (this happens once)...")
    # Don't actually load here — just verify the import works
    check(f"faster-whisper '{model_size}' importable", True, detail="Will download on first run")
except Exception as e:
    check("faster-whisper model", False, detail=str(e)[:80])

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 45)
if errors == 0 and warnings == 0:
    print(f"{GREEN}{BOLD}✅ All checks passed! You're ready to run.{RESET}")
    print(f"\n   {CYAN}python run.py{RESET}\n")
elif errors == 0:
    print(f"{YELLOW}{BOLD}⚠️  Setup complete with {warnings} warning(s).{RESET}")
    print(f"   TikTok posting won't work until you add your access token.")
    print(f"\n   {CYAN}python run.py{RESET}\n")
else:
    print(f"{RED}{BOLD}❌ {errors} error(s) found. Fix them before running.{RESET}")
    if warnings:
        print(f"{YELLOW}   {warnings} warning(s) also found.{RESET}")
    print()
    sys.exit(1)
