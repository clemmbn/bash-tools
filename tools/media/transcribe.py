"""
tools/media/transcribe.py — `tools media transcribe` command.

Transcribes an audio/video file locally with Whisper and prints or exports the result.

Pipeline:
  1. Validate ffmpeg is on PATH.
  2. Convert input to a 16 kHz mono WAV (skipped if input is already .wav).
  3. Transcribe with local Whisper using word-level timestamps.
  4. Output: print formatted transcript to terminal, write .txt, or export raw JSON.

Output formats:
  (omit)  Print timestamped transcript to the terminal.
  txt     Write a plain-text transcript to <input>.txt.
  raw     Write the full Whisper result dict to <input>.json.
"""

import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from tools.media import media_app
from tools.shared.ffmpeg import check_ffmpeg, extract_audio
from tools.shared.whisper import format_transcript, transcribe

console = Console()

# Whisper model choices kept in sync with the Whisper API.
_MODEL_CHOICES = ["tiny", "base", "small", "medium", "large", "turbo"]

# Extensions supported by ffmpeg audio extraction.
_SUPPORTED_EXTENSIONS = {".mp3", ".mp4", ".mov", ".mkv", ".m4a", ".aac", ".wav"}


class OutputFormat(str, Enum):
    """Output format for the transcript."""

    raw = "raw"
    txt = "txt"


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
        Writes output_path to disk and prints a confirmation message.
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    console.print(f"[green]Raw transcript exported →[/green] {output_path}")


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
        Optional[OutputFormat],
        typer.Option(
            help="Export format: 'txt' (plain text file) or 'raw' (JSON). Omit to print to terminal.",
        ),
    ] = None,
) -> None:
    """Transcribe an audio/video file and print or export the result."""
    check_ffmpeg()

    input_path = input_file.resolve()
    if not input_path.exists():
        console.print(f"[bold red]Error:[/bold red] File not found: {input_path}")
        raise typer.Exit(1)

    if input_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        console.print(
            f"[bold red]Error:[/bold red] Unsupported file extension '{input_path.suffix}'.\n"
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )
        raise typer.Exit(1)

    tmp_wav: str | None = None
    try:
        if input_path.suffix.lower() == ".wav":
            wav_path = str(input_path)
        else:
            fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            extract_audio(str(input_path), tmp_wav)
            wav_path = tmp_wav

        result = transcribe(wav_path, model)
        if not result:
            console.print("[yellow]No speech detected — nothing to export.[/yellow]")
            return

        if output_format == OutputFormat.raw:
            _export_json(result, input_path.with_suffix(".json"))
        elif output_format == OutputFormat.txt:
            output_path = input_path.with_suffix(".txt")
            output_path.write_text(format_transcript(result, plain=True), encoding="utf-8")
            console.print(f"[green]Transcript exported →[/green] {output_path}")
        else:
            console.print()
            console.print(format_transcript(result))

    finally:
        if tmp_wav:
            Path(tmp_wav).unlink(missing_ok=True)
