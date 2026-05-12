# AI  Clipper

AI-powered video automation system. Records a long-form video, cuts it into viral short clips, composites the brand template onto every frame (optionally), writes captions, and posts to multiple social platforms automatically.

---

## How it works

```
Record video
    ↓
Drop into Google Drive folder (or upload via dashboard)
    ↓
faster-whisper transcribes locally (free, no API cost)
    ↓
AI (Groq / Claude / Ollama) finds 3–6 viral clip moments
    ↓
FFmpeg cuts clips to 9:16 and composites brand template onto every frame
    ↓
AI writes natural captions and hashtags in MrBade's voice
    ↓
Dashboard: review, edit, approve or reject each clip
    ↓
Posts to YouTube Shorts, Instagram, Facebook, TikTok (via Make.com)
    ↓
Hook Learner feeds real performance stats back into AI → gets smarter over time
```

---

## Quick Start

### Requirements
- Python 3.10+
- FFmpeg: `apt install ffmpeg` (Ubuntu) or `brew install ffmpeg` (Mac)
- A Groq API key (free at console.groq.com) — or Anthropic/Ollama

### Install
```bash
git clone https://github.com/YOUR_ORG/ai-clipper
cd ai-clipper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure
```bash
cp .env.example .env
nano .env  # add GROQ_API_KEY at minimum
```

### Verify
```bash
python setup_check.py
```

### Run
```bash
python run.py
```

Open **http://localhost:8000** in your browser.

---

## Three ways to get videos into the system

| Method | How |
|---|---|
| Web dashboard | Drag and drop directly in the browser |
| Watch folder | Drop a file into `watch_inbox/` on the server |
| Google Drive | Drop a file into a shared Drive folder from any device |

---

## AI Provider

Set `AI_PROVIDER` in `.env` to switch between providers. Nothing else changes.

| Provider | Cost | Quality | Setup |
|---|---|---|---|
| `groq` | Free | Good (recommended) | Get key at console.groq.com |
| `claude` | Paid | Best | Get key at console.anthropic.com |
| `ollama` | Free | Good | Install locally, no key needed |

---

## Platforms

| Platform | Method | Status |
|---|---|---|
| YouTube Shorts | YouTube Data API v3 | Instant setup — no review needed |
| Instagram Reels | Meta Graph API | Requires Facebook Business account |
| Facebook Reels | Meta Graph API | Requires Facebook Page |
| TikTok | Make.com webhook | No API approval needed |
| TikTok (direct) | TikTok Content Posting API | Requires business verification (2-3 days) |

---

## Brand Template

The brand template (`overlays/template.png`) is composited onto every frame of every clip. The black region of the template becomes transparent so the video shows through, while all brand elements stay visible throughout.

To swap the template, go to **Settings → Brand Outro Template → Upload New Template**. The old one is archived automatically.

**Requirements for the design team:**
- Canvas: 1080 × 1920 px
- Video region: pure black (#000000)
- Format: PNG

---

## Google Drive Integration

Run once:
```bash
chmod +x scripts/gdrive_setup.sh
./scripts/gdrive_setup.sh
```

The script installs rclone, authenticates with Google Drive, and registers a systemd service that polls the configured Drive folder every 30 seconds.

```bash
# Check sync status
sudo systemctl status gdrive-sync
sudo journalctl -u gdrive-sync -f
```

---

## CLI Tool

```bash
source venv/bin/activate

python cli.py run /path/to/video.mp4     # Process a video
python cli.py clips                       # List all clips
python cli.py clips --status pending      # Filter by status
python cli.py approve abc1-clip1          # Approve a clip
python cli.py reject abc1-clip1           # Reject a clip
python cli.py post abc1-clip1             # Post a clip
python cli.py approve-all                 # Approve all pending
python cli.py post-approved               # Post all approved
python cli.py perf abc1-clip1 --views 50000 --watch-rate 0.68
python cli.py analytics                   # View analytics
python cli.py youtube-auth                # One-time YouTube auth
python cli.py watch                       # Start folder watcher
python cli.py check                       # Run setup check
```

---

## Systemd Services

```bash
# Main app
sudo systemctl status posting
sudo systemctl restart posting
sudo journalctl -u posting -f

# Google Drive sync
sudo systemctl status gdrive-sync
sudo systemctl restart gdrive-sync
sudo journalctl -u gdrive-sync -f
```

---

## File Structure

```
ai-clipper/
├── app/
│   ├── ai_brain.py          AI provider abstraction (Groq / Claude / Ollama)
│   ├── analyzer.py          Clip detection and caption writing
│   ├── editor.py            FFmpeg video cutting and thumbnails
│   ├── hook_learner.py      Performance tracking and AI feedback loop
│   ├── main.py              FastAPI routes and API endpoints
│   ├── models.py            Pydantic data models
│   ├── overlays.py          Brand template compositing
│   ├── pipeline.py          Full pipeline orchestrator
│   ├── platforms/
│   │   ├── __init__.py
│   │   ├── base.py          Abstract platform interface
│   │   ├── facebook.py      Facebook Reels (Meta Graph API)
│   │   ├── instagram.py     Instagram Reels (Meta Graph API)
│   │   ├── make.py          Make.com webhook (TikTok without API approval)
│   │   ├── router.py        Platform dispatcher
│   │   ├── tiktok.py        TikTok Content Posting API (direct)
│   │   └── youtube.py       YouTube Shorts (Data API v3)
│   ├── scheduler.py         APScheduler auto-posting engine
│   ├── storage.py           JSON-based local data store
│   ├── transcriber.py       faster-whisper wrapper
│   └── watcher.py           Folder watcher trigger
├── overlays/
│   └── template.png         Active brand template (eMigr8)
├── scripts/
│   ├── gdrive-sync.service  Systemd service file
│   ├── gdrive_setup.sh      One-time Google Drive setup
│   └── gdrive_sync.sh       Google Drive sync polling loop
├── static/
│   └── index.html           Web dashboard (single file)
├── nginx/
│   └── nginx.conf           Reverse proxy config
├── .github/
│   └── workflows/
│       └── deploy.yml       Auto-deploy to server on git push
├── Dockerfile
├── docker-compose.yml       Local development
├── docker-compose.prod.yml  EC2 / server production
├── cli.py                   Terminal CLI tool
├── run.py                   App entry point
├── setup_check.py           Pre-flight dependency checker
├── requirements.txt
├── .env                     Secrets and config (never commit)
├── .env.example             Config template
├── .gitignore
└── .dockerignore
```

Runtime directories (created automatically, not in Git):
```
uploads/          Source videos
output/clips/     Processed branded clips
output/thumbnails/
output/store/     clips.json, overlay_config.json, etc.
logs/             gdrive_sync.log, gdrive_seen.txt
gdrive_inbox/     Local landing folder for Drive sync
watch_inbox/      Manual drop folder
```

---

## Whisper Model Sizes

Set `WHISPER_MODEL` in `.env`:

| Model | Speed | Quality | RAM |
|---|---|---|---|
| tiny | Fastest | OK | ~1 GB |
| base | Fast | Good (default) | ~1.5 GB |
| small | Medium | Great | ~2.5 GB |
| medium | Slow | Excellent | ~5 GB |
| large-v3 | Slowest | Best | ~10 GB |

---

## Cost Breakdown

| Component | Cost | Notes |
|---|---|---|
| faster-whisper | Free | Runs locally |
| Groq API | Free | Generous daily limits |
| Claude API | ~$0.01–0.05/video | Only if AI_PROVIDER=claude |
| Make.com | Free tier | 1,000 operations/month |
| YouTube API | Free | No posting limits |
| FFmpeg | Free | Open source |

---

## Updating

```bash
cd ~/ai-autoposting-agent
git pull origin main
source venv/bin/activate
pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart posting
```

Automatic deployment is configured via GitHub Actions — push to `main` and it deploys to the server automatically (requires EC2_HOST, EC2_USER, EC2_SSH_KEY secrets in GitHub).
