#!/bin/bash
# Run the Expo -> Dev.to cross-posting script
# Scheduled: Daily at 12:00 PM PT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/crosspost.log"

# Activate venv and run
source "$SCRIPT_DIR/.venv/bin/activate"

echo "--- Run started: $(date) ---" >> "$LOG_FILE"
python3 "$SCRIPT_DIR/crosspost.py" >> "$LOG_FILE" 2>&1
echo "--- Run finished: $(date) ---" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
