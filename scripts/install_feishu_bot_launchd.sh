#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${MERCHANT_AGENT_ENV_FILE:-"$PROJECT_DIR/.env"}
PYTHON_BIN=${MERCHANT_AGENT_PYTHON:-python3}
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$HOME/Library/Logs/Pisell"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_DIR/com.pisell.merchant-profile-feishu-bot.plist"
TEMPLATE="$PROJECT_DIR/deploy/com.pisell.merchant-profile-feishu-bot.plist.template"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE. Copy .env.example to .env and add real secrets." >&2
  exit 1
fi

chmod 600 "$ENV_FILE"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install -e "$PROJECT_DIR[feishu]"
mkdir -p "$LOG_DIR" "$LAUNCH_DIR"

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

sed \
  -e "s|__PYTHON__|$(escape_sed "$VENV_DIR/bin/python")|g" \
  -e "s|__ENV_FILE__|$(escape_sed "$ENV_FILE")|g" \
  -e "s|__WORKDIR__|$(escape_sed "$PROJECT_DIR")|g" \
  -e "s|__SOURCE_DIR__|$(escape_sed "$PROJECT_DIR/src")|g" \
  -e "s|__LOG_DIR__|$(escape_sed "$LOG_DIR")|g" \
  "$TEMPLATE" > "$PLIST_PATH"

plutil -lint "$PLIST_PATH"
launchctl bootout "gui/$(id -u)/com.pisell.merchant-profile-feishu-bot" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/com.pisell.merchant-profile-feishu-bot"

echo "Installed com.pisell.merchant-profile-feishu-bot"
echo "Logs: $LOG_DIR/merchant-profile-feishu-bot.log"
