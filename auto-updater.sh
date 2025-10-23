#!/bin/bash

# Git Auto-Updater - Polls for changes and rebuilds Discord bot container
# Checks every 60 seconds for new commits on the remote repository

set -e

REPO_DIR="/app"
CHECK_INTERVAL=60  # seconds
BRANCH="main"      # change to "master" if that's your default branch

echo "[Auto-Updater] Starting git auto-updater..."
echo "[Auto-Updater] Repository: $REPO_DIR"
echo "[Auto-Updater] Branch: $BRANCH"
echo "[Auto-Updater] Check interval: ${CHECK_INTERVAL}s"
echo "----------------------------------------"

cd "$REPO_DIR"

# Initial git fetch to ensure we're synced
git fetch origin "$BRANCH" 2>/dev/null || {
    echo "[Auto-Updater] Warning: Initial git fetch failed. Will retry..."
}

while true; do
    # Fetch latest from remote (without output spam)
    git fetch origin "$BRANCH" 2>/dev/null || {
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Warning: git fetch failed, skipping this check"
        sleep "$CHECK_INTERVAL"
        continue
    }

    # Get current local commit hash
    LOCAL_HASH=$(git rev-parse HEAD)

    # Get remote commit hash
    REMOTE_HASH=$(git rev-parse "origin/$BRANCH")

    # Check if remote is ahead
    if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
        echo "----------------------------------------"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔄 Changes detected!"
        echo "[Auto-Updater] Local:  $LOCAL_HASH"
        echo "[Auto-Updater] Remote: $REMOTE_HASH"

        # Pull latest changes
        echo "[Auto-Updater] Pulling latest changes..."
        git pull origin "$BRANCH" || {
            echo "[Auto-Updater] ERROR: git pull failed!"
            sleep "$CHECK_INTERVAL"
            continue
        }

        echo "[Auto-Updater] Successfully pulled changes"

        # Ensure required directories exist
        mkdir -p /app/logs /app/data

        # Rebuild and restart the Discord bot container
        echo "[Auto-Updater] Rebuilding discord-iv-bot container..."
        docker-compose --project-directory /app -f /app/docker-compose.yml up -d --build discord-iv-bot || {
            echo "[Auto-Updater] ERROR: docker-compose rebuild failed!"
            sleep "$CHECK_INTERVAL"
            continue
        }

        echo "[Auto-Updater] ✅ Container rebuilt and restarted successfully"
        echo "----------------------------------------"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes detected (${LOCAL_HASH:0:8})"
    fi

    # Wait before next check
    sleep "$CHECK_INTERVAL"
done
