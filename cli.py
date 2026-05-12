#!/usr/bin/env python3
"""
cli.py — AI Clipper command-line tool

Usage:
  python cli.py run <video_path>           Process a video through the full pipeline
  python cli.py clips                      List all clips
  python cli.py clips --status pending     Filter by status
  python cli.py approve <clip_id>          Approve a clip
  python cli.py reject <clip_id>           Reject a clip
  python cli.py post <clip_id>             Post an approved clip to TikTok
  python cli.py approve-all                Approve all pending clips
  python cli.py post-approved              Post all approved clips to TikTok
  python cli.py perf <clip_id> --views N  Record performance stats
  python cli.py analytics                  Show analytics summary
  python cli.py check                      Run setup check
  python cli.py watch                      Start watching the inbox folder
"""
import sys
import os
import argparse
import asyncio
from pathlib import Path
from datetime import datetime

# Load .env before importing app modules
from dotenv import load_dotenv
load_dotenv()

# Ensure app/ is importable
sys.path.insert(0, str(Path(__file__).parent))


def _print_header():
    print("\n\033[1m\033[96m AI Clipper CLI\033[0m")
    print(" " + "─" * 38)


def _status_colour(status: str) -> str:
    colours = {
        "pending":  "\033[93m",
        "approved": "\033[92m",
        "posted":   "\033[95m",
        "rejected": "\033[91m",
        "failed":   "\033[91m",
    }
    reset = "\033[0m"
    return colours.get(status, "") + status.upper() + reset


def cmd_run(args):
    """Process a video file through the full pipeline."""
    from app.pipeline import run_pipeline

    video = args.video
    if not Path(video).exists():
        print(f"\033[91m❌ File not found: {video}\033[0m")
        sys.exit(1)

    print(f"\n\033[1m🎬 Starting pipeline for:\033[0m {video}")
    print("   faster-whisper → Claude → FFmpeg → Captions\n")

    job = run_pipeline(video)

    if job.status == "complete":
        print(f"\n\033[92m✅ Done! {job.clips_found} clips generated.\033[0m")
        print(f"   Clip IDs: {', '.join(job.clips_generated)}")
        print(f"\n   Review them at \033[96mhttp://localhost:8000\033[0m  or run:\033[93m python cli.py clips --status pending\033[0m\n")
    else:
        print(f"\n\033[91m❌ Pipeline failed: {job.error}\033[0m\n")
        sys.exit(1)


def cmd_clips(args):
    """List all clips, optionally filtered."""
    from app import storage

    clips = storage.get_all_clips()
    if args.status:
        clips = [c for c in clips if c.status.value == args.status]
    clips.sort(key=lambda c: c.created_at, reverse=True)

    if not clips:
        print(f"\n  No clips found" + (f" with status: {args.status}" if args.status else "") + "\n")
        return

    print(f"\n  {'ID':<16} {'STATUS':<12} {'HOOK':>6}  {'DUR':>5}  TOPIC")
    print("  " + "─" * 72)
    for c in clips:
        dur = f"{int(c.duration//60)}m{int(c.duration%60)}s" if c.duration >= 60 else f"{int(c.duration)}s"
        topic_trunc = c.topic[:38] + "…" if len(c.topic) > 39 else c.topic
        print(f"  {c.id:<16} {_status_colour(c.status.value):<20} {c.hook_score:>4}/10  {dur:>5}  {topic_trunc}")
    print(f"\n  Total: {len(clips)} clip(s)\n")


def cmd_approve(args):
    from app import storage
    from app.models import ClipStatus

    clip = storage.get_clip(args.clip_id)
    if not clip:
        print(f"\033[91m❌ Clip not found: {args.clip_id}\033[0m")
        sys.exit(1)
    if clip.status != ClipStatus.PENDING:
        print(f"\033[93m⚠️  Clip is {clip.status.value}, not pending.\033[0m")
        return

    storage.update_clip_status(args.clip_id, ClipStatus.APPROVED)
    print(f"\033[92m✅ Approved: {args.clip_id}\033[0m  — {clip.topic}")


def cmd_reject(args):
    from app import storage
    from app.models import ClipStatus

    clip = storage.get_clip(args.clip_id)
    if not clip:
        print(f"\033[91m❌ Clip not found: {args.clip_id}\033[0m")
        sys.exit(1)
    storage.update_clip_status(args.clip_id, ClipStatus.REJECTED)
    print(f"\033[91m❌ Rejected: {args.clip_id}\033[0m")


def cmd_post(args):
    """Post a single approved clip to TikTok."""
    from app import storage
    from app.models import ClipStatus
    from app.tiktok import upload_and_post

    clip = storage.get_clip(args.clip_id)
    if not clip:
        print(f"\033[91m❌ Clip not found: {args.clip_id}\033[0m")
        sys.exit(1)
    if clip.status != ClipStatus.APPROVED:
        print(f"\033[93m⚠️  Clip must be APPROVED before posting (currently: {clip.status.value})\033[0m")
        sys.exit(1)

    print(f"\033[96m🚀 Posting to TikTok: {args.clip_id}\033[0m")

    async def _post():
        result = await upload_and_post(clip.clip_path, clip.full_post_text, args.clip_id)
        storage.update_clip_status(args.clip_id, ClipStatus.POSTED, tiktok_post_id=result.get("publish_id"))
        print(f"\033[92m✅ Posted! TikTok publish_id: {result.get('publish_id')}\033[0m")

    asyncio.run(_post())


def cmd_approve_all(args):
    """Approve all pending clips at once."""
    from app import storage
    from app.models import ClipStatus

    pending = [c for c in storage.get_all_clips() if c.status == ClipStatus.PENDING]
    if not pending:
        print("\n  No pending clips to approve.\n")
        return

    print(f"\n  Approving {len(pending)} pending clip(s):\n")
    for c in pending:
        storage.update_clip_status(c.id, ClipStatus.APPROVED)
        print(f"  \033[92m✅\033[0m  {c.id}  {c.topic[:50]}")
    print(f"\n  Done. Run '\033[93mpython cli.py post-approved\033[0m' to post them all.\n")


def cmd_post_approved(args):
    """Post all approved clips to TikTok sequentially."""
    from app import storage
    from app.models import ClipStatus
    from app.tiktok import upload_and_post

    approved = [c for c in storage.get_all_clips() if c.status == ClipStatus.APPROVED]
    if not approved:
        print("\n  No approved clips to post.\n")
        return

    print(f"\n  Posting {len(approved)} approved clip(s) to TikTok...\n")

    async def _post_all():
        for c in approved:
            try:
                print(f"  \033[96m→\033[0m  {c.id}  {c.topic[:50]}...")
                result = await upload_and_post(c.clip_path, c.full_post_text, c.id)
                storage.update_clip_status(c.id, ClipStatus.POSTED, tiktok_post_id=result.get("publish_id"))
                print(f"       \033[92m✅ Posted\033[0m  publish_id: {result.get('publish_id')}")
            except Exception as e:
                storage.update_clip_status(c.id, ClipStatus.FAILED)
                print(f"       \033[91m❌ Failed: {e}\033[0m")
        print()

    asyncio.run(_post_all())


def cmd_perf(args):
    """Record real TikTok performance stats for a clip."""
    from app.hook_learner import record_performance

    record_performance(
        clip_id=args.clip_id,
        views=args.views,
        likes=args.likes,
        shares=args.shares,
        comments=args.comments,
        watch_rate=args.watch_rate,
    )
    print(f"\033[92m✅ Performance recorded for {args.clip_id}\033[0m")
    print(f"   Views: {args.views:,}  |  Watch rate: {args.watch_rate:.0%}  |  Likes: {args.likes}")
    print(f"\n   Claude will use this data to improve future hook scoring.\n")


def cmd_analytics(args):
    """Print analytics summary."""
    from app.hook_learner import get_analytics_summary

    data = get_analytics_summary()
    status = data.get("by_status", {})

    print(f"\n  \033[1mClip Summary\033[0m")
    print(f"  {'Total':<14} {data['total_clips']}")
    print(f"  {'Pending':<14} {status.get('pending', 0)}")
    print(f"  {'Approved':<14} {status.get('approved', 0)}")
    print(f"  {'Posted':<14} {status.get('posted', 0)}")
    print(f"  {'Rejected':<14} {status.get('rejected', 0)}")
    print(f"  {'Avg Hook Score':<14} {data['avg_hook_score']}/10")
    print(f"  {'Total Views':<14} {data['total_views']:,}")
    print(f"  {'Total Likes':<14} {data['total_likes']:,}")

    if data.get("top_clips"):
        print(f"\n  \033[1mTop Clips\033[0m")
        print(f"  {'CLIP ID':<16} {'VIEWS':>8}  {'WATCH%':>6}  {'HOOK':>5}  TOPIC")
        print("  " + "─" * 65)
        for c in data["top_clips"]:
            watch = f"{c['watch_rate']*100:.0f}%" if c['watch_rate'] else "—"
            topic = c['topic'][:30] + "…" if len(c['topic']) > 31 else c['topic']
            print(f"  {c['clip_id']:<16} {c['views']:>8,}  {watch:>6}  {c['hook_score']:>4}/10  {topic}")

    hook_status = "\033[92mActive ✅\033[0m" if data.get("lessons_available") else "\033[93mNeed 3+ clips with data\033[0m"
    print(f"\n  Hook Learner: {hook_status}\n")


def cmd_check(args):
    """Run setup_check.py."""
    os.execv(sys.executable, [sys.executable, "setup_check.py"])


def cmd_watch(args):
    """Start the folder watcher in standalone mode."""
    import time
    from app.pipeline import run_pipeline
    from app.watcher import start_watcher, stop_watcher

    watch_dir = os.getenv("WATCH_FOLDER", "./watch_inbox")
    print(f"\n\033[1m👁  Folder Watcher\033[0m")
    print(f"   Watching: \033[96m{Path(watch_dir).resolve()}\033[0m")
    print(f"   Drop any video there to trigger the pipeline automatically.")
    print(f"   Press \033[93mCtrl+C\033[0m to stop.\n")

    observer = start_watcher(run_pipeline)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_watcher(observer)
        print("\n\033[93m Watcher stopped.\033[0m\n")


# ── Arg parser ─────────────────────────────────────────────────────────────────

def main():
    _print_header()

    parser = argparse.ArgumentParser(prog="cli.py", add_help=True)
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Process a video through the full pipeline")
    p_run.add_argument("video", help="Path to video file")

    # clips
    p_clips = sub.add_parser("clips", help="List clips")
    p_clips.add_argument("--status", choices=["pending","approved","posted","rejected","failed"], help="Filter by status")

    # approve / reject
    p_approve = sub.add_parser("approve", help="Approve a clip")
    p_approve.add_argument("clip_id")

    p_reject = sub.add_parser("reject", help="Reject a clip")
    p_reject.add_argument("clip_id")

    # post
    p_post = sub.add_parser("post", help="Post a clip to TikTok")
    p_post.add_argument("clip_id")

    # bulk
    sub.add_parser("approve-all", help="Approve all pending clips")
    sub.add_parser("post-approved", help="Post all approved clips to TikTok")

    # perf
    p_perf = sub.add_parser("perf", help="Record performance stats for a clip")
    p_perf.add_argument("clip_id")
    p_perf.add_argument("--views",      type=int,   default=0)
    p_perf.add_argument("--likes",      type=int,   default=0)
    p_perf.add_argument("--shares",     type=int,   default=0)
    p_perf.add_argument("--comments",   type=int,   default=0)
    p_perf.add_argument("--watch-rate", type=float, default=0.0, dest="watch_rate",
                        help="0.0-1.0, e.g. 0.65 = 65%% watched past 3s")

    # analytics
    sub.add_parser("analytics", help="Show analytics summary")

    # check + watch
    sub.add_parser("check", help="Run setup check")
    sub.add_parser("watch", help="Start watching the inbox folder")

    # youtube-auth
    sub.add_parser("youtube-auth", help="Authenticate with YouTube (run once)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        print()
        return

    dispatch = {
        "run":           cmd_run,
        "clips":         cmd_clips,
        "approve":       cmd_approve,
        "reject":        cmd_reject,
        "post":          cmd_post,
        "approve-all":   cmd_approve_all,
        "post-approved": cmd_post_approved,
        "perf":          cmd_perf,
        "analytics":     cmd_analytics,
        "check":         cmd_check,
        "watch":         cmd_watch,
        "youtube-auth":  cmd_youtube_auth,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()


def cmd_youtube_auth(args):
    """Run YouTube OAuth flow to authenticate the channel."""
    from app.platforms.youtube import run_youtube_auth
    run_youtube_auth()


# Register the new command in main()
