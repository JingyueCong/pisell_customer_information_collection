#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
BOOTSTRAP_SCRIPT="$PROJECT_DIR/scripts/bootstrap_launchagents_at_reboot.sh"
LOG_DIR="$HOME/Library/Logs/Pisell"
MARKER="pisell-merchant-profile-reboot"
TEMP_FILE=$(mktemp)
trap 'rm -f "$TEMP_FILE"' EXIT HUP INT TERM

chmod +x "$BOOTSTRAP_SCRIPT"
mkdir -p "$LOG_DIR"

(crontab -l 2>/dev/null || true) \
  | grep -v "$MARKER" > "$TEMP_FILE" || true
printf '%s\n' \
  "@reboot $BOOTSTRAP_SCRIPT >> $LOG_DIR/reboot-bootstrap.log 2>&1 # $MARKER" \
  >> "$TEMP_FILE"
crontab "$TEMP_FILE"

"$BOOTSTRAP_SCRIPT"
echo "Installed login-independent reboot bootstrap for merchant profile services."

