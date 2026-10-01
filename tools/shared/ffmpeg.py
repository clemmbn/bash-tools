"""
tools/shared/ffmpeg.py — Shared ffmpeg utilities.

Responsibilities:
  - Verify ffmpeg is on PATH before any media operation.
  - Extract audio from any media file to a 16 kHz mono WAV for downstream processing.

Used by: tools.media.video_to_edl, tools.media.audio_to_srt, tools.media.transcribe
"""

import shutil
import subprocess
import sys

from rich.markup import escape

from tools.shared.log import error, step


def check_ffmpeg() -> None:
    """Exit with a clear message if ffmpeg is not found on PATH."""
    if shutil.which("ffmpeg") is None:
        error("ffmpeg was not found on your PATH.", hint="Install it with: brew install ffmpeg")
        sys.exit(1)


def extract_audio(input_path: str, wav_path: str) -> None:
    """Extract/convert input_path to a 16 kHz mono WAV file at wav_path.

    Args:
        input_path: Path to any ffmpeg-supported media file.
        wav_path:   Destination path for the WAV output.

    Side effects:
        Writes a WAV file to wav_path. Prints an "Extract audio" step line.
        Raises SystemExit if the file has no audio stream.
        Raises subprocess.CalledProcessError if ffmpeg exits non-zero for other reasons.
    """
    # Probe for audio streams before attempting extraction to give a clear error
    # instead of a cryptic CalledProcessError when the file is video-only.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        # A failing ffprobe leaves stdout empty, which the check below already
        # reports as "no audio stream" — so a non-zero exit is handled, not ignored.
        check=False,
    )
    if not probe.stdout.strip():
        error(f"{escape(input_path)} has no audio stream.")
        sys.exit(1)

    with step("Extract audio") as s:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        s.result = "16 kHz mono WAV"
        s.detail(f"temp file: {escape(wav_path)}")
