#!/bin/bash
# Removes what install.sh installed: the script copy and the "Merge GoPro Chapters" Quick Action.
set -euo pipefail

NAME="Merge GoPro Chapters"
APP_DIR="$HOME/Library/Application Support/gopro-merge"
WORKFLOW="$HOME/Library/Services/$NAME.workflow"

rm -rf "$APP_DIR" "$WORKFLOW"
/System/Library/CoreServices/pbs -flush
echo "Removed $NAME."
