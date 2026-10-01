"""
tools/media/transcribe.py — `tools media transcribe` command.

Transcribes an audio/video file locally with Whisper and prints or exports the result.

Pipeline:
  1. Validate ffmpeg is on PATH.
  2. Convert input to a 16 kHz mono WAV (skipped if input is already .wav).
  3. Transcribe with local Whisper using word-level timestamps.
  4. Output: print formatted transcript to terminal, write .txt/.md, or export raw JSON.

Output formats:
  (omit)  Print timestamped transcript to the terminal.
  txt     Write a plain-text transcript to <input>.txt.
  md      Write a Markdown transcript to <input>.md  ([MM:SS] / [HH:MM:SS] per sentence).
  raw     Write the full Whisper result dict to <input>.json.
"""

import json
import os
import re
import tempfile
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from tools.media import media_app
from tools.shared.ffmpeg import check_ffmpeg, extract_audio
from tools.shared.log import console, detail, error, header, step, summary, warn
from tools.shared.whisper import format_transcript, transcribe

# Whisper model choices kept in sync with the Whisper API.
_MODEL_CHOICES = ["tiny", "base", "small", "medium", "large", "turbo"]

# Extensions supported by ffmpeg audio extraction.
_SUPPORTED_EXTENSIONS = {".mp3", ".mp4", ".mov", ".mkv", ".m4a", ".aac", ".wav"}


class OutputFormat(str, Enum):
    """Output format for the transcript."""

    raw = "raw"
    txt = "txt"
    md = "md"


def _format_timestamp_md(seconds: float) -> str:
    """Format seconds as [MM:SS] or [HH:MM:SS] when the value exceeds one hour.

    Args:
        seconds: Duration in seconds.

    Returns:
        Bracketed timestamp string, e.g. "[01:05]" or "[01:02:03]".
    """
    total_s = int(seconds)
    h, remainder = divmod(total_s, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _format_transcript_md(result: dict) -> str:
    """Format a Whisper result dict into a Markdown transcript.

    Produces one sentence per line prefixed with a bracketed timestamp,
    matching the output format of `tools media srt-to-md`.

    Args:
        result: Whisper result dict as returned by transcribe().

    Returns:
        String with lines like "[MM:SS] sentence text", or "" if no words found.
    """
    if not result:
        return ""

    all_words = [
        word
        for seg in result.get("segments", [])
        for word in seg.get("words", [])
    ]
    if not all_words:
        return result.get("text", "").strip()

    SENT_END = set(".?!")
    lines: list[str] = []
    sentence_words: list[str] = []
    sentence_start: float | None = None

    for w in all_words:
        text = w["word"]
        if sentence_start is None:
            sentence_start = w["start"]
        sentence_words.append(text.strip())
        if text.rstrip() and text.rstrip()[-1] in SENT_END:
            ts = _format_timestamp_md(sentence_start)
            # Re-use the spacing-fix from shared/whisper.py inline to avoid coupling
            sentence_text = re.sub(r"(\w) (['''])(\w)", r"\1\2\3", " ".join(sentence_words))
            lines.append(f"{ts} {sentence_text}")
            sentence_words = []
            sentence_start = None

    # Flush any trailing words that didn't end with terminal punctuation
    if sentence_words:
        ts = _format_timestamp_md(sentence_start)  # type: ignore[arg-type]
        sentence_text = re.sub(r"(\w) (['''])(\w)", r"\1\2\3", " ".join(sentence_words))
        lines.append(f"{ts} {sentence_text}")

    return "\n".join(lines)


def _validate_model(value: str) -> str:
    """Validate --model against the supported Whisper model list."""
    if value not in _MODEL_CHOICES:
        raise typer.BadParameter(f"Choose from: {', '.join(_MODEL_CHOICES)}")
    return value


def _export_json(result: dict, output_path: Path) -> None:
    """Write the raw Whisper result dict to a JSON file.

    Args:
        result:      Whisper result dict as returned by transcribe().
        output_path: Destination path for the JSON file.

    Side effects:
        Writes output_path to disk.
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


@media_app.command("transcribe")
def transcribe_cmd(
    input_file: Annotated[Path, typer.Argument(help="Input audio or video file (MP3, MP4, MOV, WAV, M4A, AAC, MKV).")],
    model: Annotated[
        str,
        typer.Option(
            help=f"Whisper model size. Choices: {', '.join(_MODEL_CHOICES)}.",
            callback=_validate_model,
            is_eager=False,
        ),
    ] = "turbo",
    output_format: Annotated[
        OutputFormat | None,
        typer.Option(
            help="Export format: 'txt' (plain text), 'md' (Markdown with [MM:SS] timestamps), or 'raw' (JSON). Omit to print to terminal.",
        ),
    ] = None,
) -> None:
    """Transcribe an audio/video file and print or export the result."""
    check_ffmpeg()

    input_path = input_file.resolve()
    if not input_path.exists():
        error(f"File not found: {escape(str(input_path))}")
        raise typer.Exit(1)

    if input_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        error(
            f"Unsupported file extension '{escape(input_path.suffix)}'.",
            hint=f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}",
        )
        raise typer.Exit(1)

    header("transcribe", input_path.name)

    tmp_wav: str | None = None
    try:
        if input_path.suffix.lower() == ".wav":
            wav_path = str(input_path)
            detail("input is already a WAV — skipping audio extraction")
        else:
            fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            extract_audio(str(input_path), tmp_wav)
            wav_path = tmp_wav

        result = transcribe(wav_path, model)
        if not result:
            warn("No speech detected — nothing to export.")
            return

        if output_format is None:
            # Terminal mode: the transcript itself is the output, shown in its own
            # section. format_transcript() already escapes the transcript text.
            console.rule("[bold green]Transcript[/bold green]")
            console.print(format_transcript(result))
            console.print()
            return

        # Each export format: (output extension, summary label, writer function).
        exports = {
            OutputFormat.raw: (".json", "JSON", lambda path: _export_json(result, path)),
            OutputFormat.txt: (".txt", "Text", lambda path: path.write_text(format_transcript(result, plain=True), encoding="utf-8")),
            OutputFormat.md: (".md", "Markdown", lambda path: path.write_text(_format_transcript_md(result), encoding="utf-8")),
        }
        suffix, label, write = exports[output_format]
        output_path = input_path.with_suffix(suffix)
        with step(f"Write {label}"):
            write(output_path)

    finally:
        if tmp_wav:
            Path(tmp_wav).unlink(missing_ok=True)

    summary({label: output_path})
