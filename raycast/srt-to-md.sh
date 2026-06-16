#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title SRT to MD
# @raycast.mode fullOutput
# @raycast.packageName Tools
# @raycast.icon 📝

# Optional parameters:
# @raycast.description Convert the selected Finder SRT subtitle file to a Markdown file.

set -euo pipefail

# Raycast runs with a minimal PATH; uv tool installs land in ~/.local/bin
# and Homebrew binaries (ffmpeg) live in /opt/homebrew/bin.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# Grab the Finder selection as a POSIX path. Fails gracefully if nothing is selected.
FILE=$(osascript -e 'tell application "Finder" to get POSIX path of (selection as alias)' 2>/dev/null | tr -d '\n')

if [[ -z "$FILE" ]]; then
  echo "No file selected in Finder. Select an SRT file and try again."
  exit 1
fi

EXT_LOWER=$(echo "${FILE##*.}" | tr '[:upper:]' '[:lower:]')
VALID_EXTS=("srt")

VALID=0
for e in "${VALID_EXTS[@]}"; do
  [[ "$EXT_LOWER" == "$e" ]] && VALID=1 && break
done

if [[ $VALID -eq 0 ]]; then
  echo "Unsupported file type: .${FILE##*.}  —  expected .srt"
  exit 1
fi

# NO_COLOR suppresses Rich ANSI codes so Raycast's output window stays readable.
NO_COLOR=1 tools media srt-to-md "$FILE"
