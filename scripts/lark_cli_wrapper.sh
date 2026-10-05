#!/bin/sh
set -eu

NODE_BIN=${PISELL_NODE_BIN:-/opt/homebrew/opt/node/bin/node}
LARK_SCRIPT=${PISELL_LARK_CLI_SCRIPT:-/opt/homebrew/lib/node_modules/@larksuite/cli/scripts/run.js}

if [ ! -x "$NODE_BIN" ]; then
  echo "Node.js executable is unavailable: $NODE_BIN" >&2
  exit 127
fi
if [ ! -f "$LARK_SCRIPT" ]; then
  echo "lark-cli script is unavailable: $LARK_SCRIPT" >&2
  exit 127
fi
exec "$NODE_BIN" "$LARK_SCRIPT" "$@"
