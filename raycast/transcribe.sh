#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Transcribe to MD
# @raycast.mode fullOutput
# @raycast.packageName Tools
# @raycast.icon 🎙️

# Optional parameters:
# @raycast.description Transcribe the selected Finder audio/video file to a Markdown transcript using Whisper.

set -euo pipefail

# Raycast runs with a minimal PATH; uv tool installs land in ~/.local/bin
# and Homebrew binaries (ffmpeg) live in /opt/homebrew/bin.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# Grab the Finder selection as a POSIX path. Fails gracefully if nothing is selected.
FILE=$(osascript -e 'tell application "Finder" to get POSIX path of (selection as alias)' 2>/dev/null | tr -d '\n')

if [[ -z "$FILE" ]]; then
  echo "No file selected in Finder. Select an audio or video file and try again."
  exit 1
fi

EXT_LOWER=$(echo "${FILE##*.}" | tr '[:upper:]' '[:lower:]')
VALID_EXTS=("mp3" "mp4" "mov" "mkv" "wav" "m4a" "aac")

VALID=0
for e in "${VALID_EXTS[@]}"; do
  [[ "$EXT_LOWER" == "$e" ]] && VALID=1 && break
done

if [[ $VALID -eq 0 ]]; then
  echo "Unsupported file type: .${FILE##*.}  —  expected one of: ${VALID_EXTS[*]}"
  exit 1
fi

# NO_COLOR suppresses Rich ANSI codes so Raycast's output window stays readable.
NO_COLOR=1 tools media transcribe --output-format md "$FILE"
