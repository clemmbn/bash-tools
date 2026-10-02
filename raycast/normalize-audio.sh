#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Normalize Audio
# @raycast.mode fullOutput
# @raycast.packageName Tools
# @raycast.icon 🔊

# Optional parameters:
# @raycast.description Even out the voice and normalize the selected Finder video (.mp4 / .mov) to -14 LUFS. Overwrites the file.
# @raycast.argument1 { "type": "dropdown", "placeholder": "Strength", "optional": true, "data": [{"title": "Normal (3:1)", "value": "normal"}, {"title": "Gentle (2:1)", "value": "gentle"}, {"title": "Strong (4:1)", "value": "strong"}] }

set -euo pipefail

# Grab the Finder selection as a POSIX path. Fails gracefully if nothing is selected.
FILE=$(osascript -e 'tell application "Finder" to get POSIX path of (selection as alias)' 2>/dev/null | tr -d '\n')

if [[ -z "$FILE" ]]; then
  echo "No file selected in Finder. Select an .mp4 or .mov file and try again."
  exit 1
fi

EXT_LOWER=$(echo "${FILE##*.}" | tr '[:upper:]' '[:lower:]')
if [[ "$EXT_LOWER" != "mp4" && "$EXT_LOWER" != "mov" ]]; then
  echo "Unsupported file type: .${FILE##*.}  —  expected .mp4 or .mov"
  exit 1
fi

# An optional dropdown left empty arrives as "", so fall back to the CLI default.
STRENGTH="${1:-normal}"

# Raycast runs with a minimal PATH; uv tool installs land in ~/.local/bin
# and Homebrew binaries (ffmpeg) live in /opt/homebrew/bin.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# NO_COLOR suppresses Rich ANSI codes so Raycast's output window stays readable.
NO_COLOR=1 tools media normalize-audio "$FILE" --strength "$STRENGTH"
