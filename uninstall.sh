#!/bin/bash

set -euo pipefail

LABEL="com.wrinkledeth.yt-bar"
GUI_DOMAIN="gui/$(id -u)"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "$GUI_DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl disable "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Removed $LABEL"
echo "Deleted $PLIST_PATH"
