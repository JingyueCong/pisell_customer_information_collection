#!/bin/sh
set -eu

USER_ID=$(id -u)
LAUNCH_DOMAIN="user/$USER_ID"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

ensure_loaded() {
  label=$1
  plist=$2

  if launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    launchctl kickstart -k "$LAUNCH_DOMAIN/$label"
    return
  fi

  attempt=1
  while [ "$attempt" -le 12 ]; do
    if launchctl bootstrap "$LAUNCH_DOMAIN" "$plist"; then
      launchctl kickstart -k "$LAUNCH_DOMAIN/$label"
      return
    fi
    sleep 5
    attempt=$((attempt + 1))
  done

  echo "Could not bootstrap $label after 12 attempts" >&2
  return 1
}

ensure_loaded \
  "com.pisell.merchant-profile-agent" \
  "$LAUNCH_DIR/com.pisell.merchant-profile-agent.plist"
ensure_loaded \
  "com.pisell.merchant-profile-feishu-bot" \
  "$LAUNCH_DIR/com.pisell.merchant-profile-feishu-bot.plist"

