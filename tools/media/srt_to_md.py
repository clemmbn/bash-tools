"""
tools/media/srt_to_md.py — Convert an SRT subtitle file to a Markdown transcript.

Responsibilities:
  - Parse SRT blocks (with or without HTML tags like <b>)
  - Merge word-level blocks into complete sentences
  - Stamp each sentence with the start-time of its first contributing block
  - Write a .md file beside the source SRT

Output format (one sentence per line):
  [00:00] Hey, my name is Clement!
  [00:05] I'm 22 and I love tennis.
"""

import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from tools.media import media_app

console = Console()


def parse_srt_time(ts: str) -> float:
    """
    Convert an SRT timestamp string to total seconds.

    Args:
        ts: Timestamp in "HH:MM:SS,mmm" format (e.g. "00:01:05,233").

    Returns:
        Total seconds as a float (e.g. 65.233).
    """
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def format_timestamp(seconds: float) -> str:
    """
    Format a duration in seconds as [M:SS] for Markdown output.

    Args:
        seconds: Duration in seconds.

    Returns:
        String like "[00:05]" or "[01:23]".
    """
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"[{minutes:02d}:{secs:02d}]"


def strip_tags(text: str) -> str:
    """
    Remove HTML-like tags (e.g. <b>, </b>) from a string.

    Args:
        text: Raw subtitle text, possibly containing tags.

    Returns:
        Text with all tags removed and surrounding whitespace stripped.
    """
    return re.sub(r"<[^>]+>", "", text).strip()


def parse_srt(content: str) -> list[tuple[float, str]]:
    """
    Parse raw SRT file content into a list of (start_seconds, text) tuples.

    Blocks are separated by blank lines. Each block has:
      - Line 1: sequence number (ignored)
      - Line 2: "HH:MM:SS,mmm --> HH:MM:SS,mmm" timecodes
      - Line 3+: subtitle text (may contain HTML tags)

    Blocks whose text is empty after tag-stripping are skipped.

    Args:
        content: Full text of an SRT file.

    Returns:
        List of (start_seconds, cleaned_text) in file order.
    """
    blocks = re.split(r"\n\s*\n", content.strip())
    result: list[tuple[float, str]] = []

    for block in blocks:
        lines = block.strip().splitlines()
        # Need at least: index line, timecode line, one text line
        if len(lines) < 3:
            continue

        # Extract start time from "HH:MM:SS,mmm --> HH:MM:SS,mmm"
        timecode_line = lines[1]
        start_str = timecode_line.split(" --> ")[0].strip()
        start_seconds = parse_srt_time(start_str)

        # Join all text lines and strip tags
        raw_text = " ".join(lines[2:])
        text = strip_tags(raw_text)

        if text:
            result.append((start_seconds, text))

    return result


def build_sentences(blocks: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """
    Merge word-level SRT blocks into complete sentences.

    A sentence starts at the timestamp of its first contributing block and
    ends when the accumulated text ends with a sentence-terminal punctuation
    mark (., !, or ?).  Any trailing tokens with no terminal punctuation are
    flushed as a final sentence.

    Args:
        blocks: Ordered list of (start_seconds, text) from parse_srt().

    Returns:
        List of (sentence_start_seconds, sentence_text).
    """
    sentences: list[tuple[float, str]] = []
    current_tokens: list[str] = []
    sentence_start: float | None = None

    for start_time, text in blocks:
        # Record the timestamp of the first block in a new sentence
        if sentence_start is None:
            sentence_start = start_time

        current_tokens.append(text)
        combined = " ".join(current_tokens)

        # Flush when the accumulated text ends with sentence-terminal punctuation
        if re.search(r"[.!?]\s*$", combined):
            sentences.append((sentence_start, combined.strip()))
            current_tokens = []
            sentence_start = None

    # Flush any remaining tokens that never hit terminal punctuation
    if current_tokens:
        sentences.append((sentence_start, " ".join(current_tokens).strip()))  # type: ignore[arg-type]

    return sentences


@media_app.command("srt-to-md")
def srt_to_md(
    input_file: Annotated[Path, typer.Argument(help="Input .srt file to convert.")],
) -> None:
    """
    Convert an SRT subtitle file to a timestamped Markdown transcript.

    Each sentence is placed on its own line, prefixed by the start-time of
    its first subtitle block:

      [00:00] Hey, my name is Clement!
      [00:05] I'm 22 and I love tennis.

    The output file is written alongside the source with a .md extension.

    Args:
        input_file: Path to an existing .srt file.
    """
    if not input_file.exists():
        console.print(f"[red]Error:[/red] file not found: {input_file}")
        raise typer.Exit(1)

    if input_file.suffix.lower() != ".srt":
        console.print(f"[red]Error:[/red] expected a .srt file, got: {input_file.suffix}")
        raise typer.Exit(1)

    content = input_file.read_text(encoding="utf-8")
    blocks = parse_srt(content)

    if not blocks:
        console.print("[yellow]Warning:[/yellow] no subtitle blocks found in the file.")
        raise typer.Exit(1)

    sentences = build_sentences(blocks)
    lines = [f"{format_timestamp(ts)} {text}" for ts, text in sentences]
    output = "\n".join(lines)

    output_path = input_file.with_suffix(".md")
    output_path.write_text(output, encoding="utf-8")

    console.print(f"[green]Markdown written →[/green] {output_path}")
