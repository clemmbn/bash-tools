"""
tools/media/audio_to_srt.py — `tools media audio-to-srt` command.

Transcribes an audio/video file locally with Whisper and produces an SRT subtitle file.

Pipeline:
  1. Validate ffmpeg is on PATH.
  2. Convert input to a 16 kHz mono WAV (skipped if input is already .wav).
  3. Transcribe with local Whisper using word-level timestamps.
  4. Group words into caption blocks and write an .srt file.
"""

import os
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from tools.media import media_app
from tools.shared.ffmpeg import check_ffmpeg, extract_audio
from tools.shared.log import detail, error, header, step, summary, warn
from tools.shared.whisper import transcribe

# Whisper model choices kept in sync with the Whisper API.
_MODEL_CHOICES = ["tiny", "base", "small", "medium", "large", "turbo"]


def seconds_to_srt_time(seconds: float) -> str:
    """Convert a duration in seconds to an SRT timecode string HH:MM:SS,mmm.

    Args:
        seconds: Duration in seconds.

    Returns:
        Timecode string formatted as "HH:MM:SS,mmm".
    """
    ms = round(seconds * 1000)
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# Caption grouping
# ---------------------------------------------------------------------------

def merge_tokens(words: list[dict]) -> list[dict]:
    """Merge Whisper word tokens that should attach to the previous token without a space.

    Rules (applied to stripped token text):
      - Starts with ' (ASCII or unicode apostrophe) or - : concatenate without space.
      - Is a single ?, !, or :                           : append with a leading space.
      - Is a single ., ,, or ;                           : concatenate without space.

    Args:
        words: List of Whisper word dicts (each with "word", "start", "end").

    Returns:
        New list of merged word dicts. The merged token inherits the start of the
        first token and the end of the last.
    """
    SPACE_BEFORE = set("?!:")
    NO_SPACE_BEFORE = set(".,;")

    result: list[dict] = []
    for w in words:
        text = w["word"].strip()
        if not text:
            continue
        if result and (
            text[0] in ("'", "’", "-")
            or (len(text) == 1 and text in SPACE_BEFORE)
            or (len(text) == 1 and text in NO_SPACE_BEFORE)
        ):
            prev = result[-1]
            prev_text = prev["word"].strip()
            sep = " " if (len(text) == 1 and text in SPACE_BEFORE) else ""
            result[-1] = {"word": prev_text + sep + text, "start": prev["start"], "end": w["end"]}
        else:
            result.append(dict(w))
    return result


def _fill_short_gaps(
    blocks: list[tuple[str, float, float]],
    min_gap: float,
) -> list[tuple[str, float, float]]:
    """Eliminate sub-threshold gaps between consecutive subtitle blocks.

    When the silence between two blocks is shorter than min_gap, the gap is split
    at the midpoint: the first block's end is extended forward and the second
    block's start is pulled backward so they meet at that midpoint with no gap.

    Args:
        blocks:  List of (text, start, end) tuples in chronological order.
        min_gap: Minimum allowed gap in seconds between consecutive blocks.

    Returns:
        New list of (text, start, end) tuples with short gaps eliminated.
    """
    if len(blocks) < 2:
        return list(blocks)

    result = list(blocks)
    for i in range(len(result) - 1):
        text_a, start_a, end_a = result[i]
        text_b, start_b, end_b = result[i + 1]
        gap = start_b - end_a
        if 0 < gap < min_gap:
            mid = (end_a + start_b) / 2
            result[i] = (text_a, start_a, mid)
            result[i + 1] = (text_b, mid, end_b)
    return result


def write_srt(
    result: dict,
    output_path: str,
    max_line_width: int,
    silence_threshold: float,
    max_lines: int,
    min_gap: float,
) -> int:
    """Group Whisper word timestamps into caption blocks and write an SRT file.

    A *line* is a list of word-dicts appearing on one subtitle row.
    A *block* is a list of lines forming one SRT entry (one timestamp range).

    Blocks break on:
      - A silence gap between words longer than silence_threshold seconds.
      - Sentence-ending punctuation (. ! ?).

    Lines break when adding the next word would exceed max_line_width characters,
    preferring to split at the most recent punctuation within the current line.

    After all blocks are collected, consecutive pairs with a gap shorter than
    min_gap are merged at their midpoint so no short silence appears.

    Args:
        result:            Whisper result dict from transcribe().
        output_path:       Destination path for the .srt file.
        max_line_width:    Maximum characters per subtitle row.
        silence_threshold: Gap in seconds that triggers a new caption block.
        max_lines:         Maximum number of rows per caption block.
        min_gap:           Minimum gap in seconds allowed between two blocks;
                           shorter gaps are filled by splitting at the midpoint.

    Returns:
        Number of caption blocks written.

    Side effects:
        Writes the .srt file to output_path.
    """
    SENTENCE_PUNCT = {'.', '!', '?'}
    BREAK_PUNCT = {',', ';', '.', '!', '?'}

    all_words = [
        word
        for seg in result.get("segments", [])
        for word in seg.get("words", [])
    ]
    all_words = merge_tokens(all_words)

    blocks: list[tuple[str, float, float]] = []

    def emit_block(lines: list[list[dict]], start: float, end: float) -> None:
        row_texts = [" ".join(wd["word"].strip() for wd in line) for line in lines]
        row_texts = [r for r in row_texts if r]
        if not row_texts:
            return
        blocks.append(("\n".join(row_texts), start, end))

    current_lines: list[list[dict]] = [[]]
    block_start: float | None = None
    last_end: float = 0.0

    for w in all_words:
        text = w["word"].strip()
        if not text:
            continue

        gap = w["start"] - last_end

        # Long silence — flush the current block and start fresh.
        if gap > silence_threshold and current_lines != [[]]:
            emit_block(current_lines, block_start, last_end)
            current_lines = [[]]
            block_start = None

        if block_start is None:
            block_start = w["start"]

        current_line = current_lines[-1]
        current_line_text = " ".join(wd["word"].strip() for wd in current_line)
        candidate = (current_line_text + " " + text).strip()

        if len(candidate) <= max_line_width:
            current_line.append(w)
        else:
            if len(current_lines) >= max_lines:
                # Block is full — flush and start a new one with this word.
                emit_block(current_lines, block_start, last_end)
                current_lines = [[w]]
                block_start = w["start"]
            else:
                # Try splitting at the last break-punct for a more natural line break.
                punct_idx = -1
                for i, wd in enumerate(current_line):
                    if wd["word"].strip() and wd["word"].strip()[-1] in BREAK_PUNCT:
                        punct_idx = i

                if punct_idx >= 0 and punct_idx < len(current_line) - 1:
                    tail = current_line[punct_idx + 1:]
                    current_lines[-1] = current_line[:punct_idx + 1]
                    current_lines.append(tail + [w])
                else:
                    current_lines.append([w])

        last_end = w["end"]

        # Sentence-ending punctuation — flush the block immediately.
        if text[-1] in SENTENCE_PUNCT:
            emit_block(current_lines, block_start, last_end)
            current_lines = [[]]
            block_start = None

    if any(line for line in current_lines if line):
        emit_block(current_lines, block_start, last_end)

    blocks = _fill_short_gaps(blocks, min_gap)

    srt_lines: list[str] = []
    for idx, (text, start, end) in enumerate(blocks, start=1):
        srt_lines.append(str(idx))
        srt_lines.append(f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}")
        srt_lines.append(text)
        srt_lines.append("")

    Path(output_path).write_text("\n".join(srt_lines), encoding="utf-8")
    return len(blocks)


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------

def _validate_model(value: str) -> str:
    """Validate --model against the supported Whisper model list."""
    if value not in _MODEL_CHOICES:
        raise typer.BadParameter(f"Choose from: {', '.join(_MODEL_CHOICES)}")
    return value


@media_app.command("audio-to-srt")
def audio_to_srt(
    input_file: Annotated[Path, typer.Argument(help="Input audio or video file (MP3, MP4, MOV, WAV, M4A, AAC).")],
    model: Annotated[str, typer.Option(help=f"Whisper model size. Choices: {', '.join(_MODEL_CHOICES)}.", callback=_validate_model, is_eager=False)] = "turbo",
    max_line_width: Annotated[int, typer.Option(help="Maximum characters per subtitle line.")] = 10,
    silence_threshold: Annotated[float, typer.Option(help="Silence gap in seconds that forces a new caption block.")] = 0.5,
    max_lines: Annotated[int, typer.Option(help="Maximum lines per caption block.")] = 1,
    min_gap: Annotated[float, typer.Option(help="Minimum gap in seconds between subtitle blocks; shorter gaps are filled by splitting at the midpoint.")] = 0.2,
) -> None:
    """Transcribe an audio/video file and generate an SRT subtitle file."""
    check_ffmpeg()

    input_path = input_file.resolve()
    if not input_path.exists():
        error(f"File not found: {escape(str(input_path))}")
        raise typer.Exit(1)

    header("audio-to-srt", input_path.name)

    output_path = input_path.with_suffix(".srt")
    suffix = input_path.suffix.lower()
    tmp_wav: str | None = None

    try:
        if suffix == ".wav":
            wav_path = str(input_path)
            detail("input is already a WAV — skipping audio extraction")
        else:
            fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            extract_audio(str(input_path), tmp_wav)
            wav_path = tmp_wav

        result = transcribe(wav_path, model)
        if not result:
            warn("No speech detected — no SRT file written.")
            return

        with step("Write SRT") as s:
            block_count = write_srt(result, str(output_path), max_line_width, silence_threshold, max_lines, min_gap)
            s.result = f"{block_count} caption block(s)"
            s.detail(
                f"max {max_line_width} chars × {max_lines} line(s), "
                f"new block after {silence_threshold}s silence, min gap {min_gap}s"
            )

    finally:
        if tmp_wav:
            Path(tmp_wav).unlink(missing_ok=True)

    summary({"Captions": block_count, "SRT": output_path})
