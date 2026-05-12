#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  scripts/gdrive_sync.sh
#
#  Polls Google Drive every N seconds and copies new videos to the local
#  watch folder. When watcher.py picks up a file it moves it to uploads/,
#  so we track what has already been processed via a simple log file.
#
#  This script is managed by the gdrive-sync systemd service.
#  Start: sudo systemctl start gdrive-sync
#  Logs:  sudo journalctl -u gdrive-sync -f
# ─────────────────────────────────────────────────────────────────────────────

# Load environment
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${GDRIVE_REMOTE:-gdrive:BrandBadePoster}"
LOCAL="${WATCH_FOLDER:-$PROJECT_DIR/gdrive_inbox}"
INTERVAL="${GDRIVE_SYNC_INTERVAL:-30}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/gdrive_sync.log"
SEEN_FILE="$LOG_DIR/gdrive_seen.txt"

mkdir -p "$LOCAL" "$LOG_DIR"
touch "$SEEN_FILE"

log() {
    local msg="$(date '+%Y-%m-%d %H:%M:%S') [GDrive] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

log "━━━ Google Drive sync started ━━━"
log "Remote   : $REMOTE"
log "Local    : $LOCAL"
log "Interval : ${INTERVAL}s"

while true; do

    # ── List files on Google Drive ──────────────────────────────────────────
    # Get list of video files currently in the Drive folder
    DRIVE_FILES=$(rclone lsf "$REMOTE" \
        --include "*.mp4" --include "*.mov" \
        --include "*.avi" --include "*.mkv" --include "*.webm" \
        2>/dev/null)

    if [ -z "$DRIVE_FILES" ]; then
        # No videos in Drive folder yet — silent wait
        sleep "$INTERVAL"
        continue
    fi

    # ── Check for new files ─────────────────────────────────────────────────
    NEW_FILES=""
    while IFS= read -r filename; do
        [ -z "$filename" ] && continue
        if ! grep -qxF "$filename" "$SEEN_FILE" 2>/dev/null; then
            NEW_FILES="$NEW_FILES $filename"
        fi
    done <<< "$DRIVE_FILES"

    # ── Copy new files ──────────────────────────────────────────────────────
    if [ -n "$NEW_FILES" ]; then
        log "New video(s) detected on Google Drive:"
        for f in $NEW_FILES; do
            log "  → $f"
        done

        # Copy from Drive to local watch folder
        rclone copy "$REMOTE" "$LOCAL" \
            --include "*.mp4" --include "*.mov" \
            --include "*.avi" --include "*.mkv" --include "*.webm" \
            --no-update-modtime \
            2>> "$LOG_FILE"

        # Mark as seen so we don't process them again
        for f in $NEW_FILES; do
            echo "$f" >> "$SEEN_FILE"
            log "Copied to local: $f — pipeline will trigger automatically"
        done

        # Move processed files to a /Processed subfolder on Drive
        # so the Drive folder stays clean
        for f in $NEW_FILES; do
            rclone moveto \
                "$REMOTE/$f" \
                "$REMOTE/Processed/$f" \
                2>> "$LOG_FILE" && \
            log "Moved on Drive: $f → $REMOTE/Processed/$f"
        done

    fi

    sleep "$INTERVAL"

done
