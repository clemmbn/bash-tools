"""
tools/media/video_to_edl.py — `tools media video-to-edl` command.

Converts a raw MP4/MOV video to a CMX 3600 EDL file using ffmpeg silence detection.

Pipeline:
  1. Extract audio to a temp WAV (16 kHz mono) via ffmpeg.
  2. Pick a silence threshold: either the user's --silence-threshold, or one derived
     from this file's measured noise floor and voice level (so recordings made at
     different voice levels get comparable cuts).
  3. Detect silence intervals with ffmpeg silencedetect; derive speech intervals.
  4. Build CMX 3600 EDL from padded speech intervals.
  5. Write <input>.edl next to the input file; clean up the temp WAV.

Non-obvious constraints:
  - The audio itself is never boosted or normalized. An earlier attempt with
    ffmpeg dynaudnorm amplified room noise during long pauses above the threshold,
    so pauses stopped being cut. Moving the threshold instead avoids that.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from tools.media import media_app
from tools.shared.ffmpeg import check_ffmpeg, extract_audio

console = Console()


# ---------------------------------------------------------------------------
# Timecode utility
# ---------------------------------------------------------------------------

def seconds_to_timecode(seconds: float, fps: int) -> str:
    """Convert a duration in seconds to a CMX 3600 timecode string HH:MM:SS:FF.

    Args:
        seconds: Duration in seconds.
        fps:     Frame rate used to compute the frame component.

    Returns:
        Timecode string formatted as "HH:MM:SS:FF".
    """
    total_frames = round(seconds * fps)
    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = total_minutes // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


# ---------------------------------------------------------------------------
# Silence detection and speech interval derivation
# ---------------------------------------------------------------------------

# Window size for loudness measurement: 4000 samples = 0.25 s at the 16 kHz WAV rate
# produced by extract_audio(). Long enough to average out single clicks, short
# enough to resolve the gaps between sentences.
LEVEL_WINDOW_SAMPLES = 4000

# Percentiles of the per-window RMS levels used as reference points.
#   - 10th percentile ≈ room noise: every recording has pauses, so at least 10% of
#     windows are silence even in dense speech.
#   - 95th percentile ≈ voice level: robust as long as speech fills > 5% of the
#     file, while ignoring the few loudest outliers (laughs, bumps).
NOISE_PERCENTILE = 10
SPEECH_PERCENTILE = 95

# Where the threshold sits between noise floor (0.0) and voice level (1.0).
# 0.8 reproduces the historical fixed -25 dB default on a reference recording
# (noise -50.7 dB, voice -19.7 dB → -25.9 dB), so a "normal" take behaves as
# before, while a quieter voice pulls the threshold down by a proportional amount.
# A fraction (rather than "voice minus N dB") also keeps the threshold above the
# noise floor automatically when the voice is quiet relative to the room.
THRESHOLD_POSITION = 0.8

# Below this noise→voice gap the two can't be reliably separated; we still compute
# a threshold but warn the user to check the result or pass --silence-threshold.
MIN_LEVEL_GAP_DB = 15.0


def percentile(values: list[float], pct: float) -> float:
    """Return the pct-th percentile of values (nearest-rank, no interpolation).

    Args:
        values: Non-empty list of numbers (order doesn't matter).
        pct:    Percentile in [0, 100].

    Returns:
        The value at that rank in the sorted list.
    """
    ordered = sorted(values)
    return ordered[round(pct / 100 * (len(ordered) - 1))]


def measure_levels(wav_path: str) -> tuple[float, float]:
    """Measure the noise floor and voice level of a WAV file in dBFS.

    Splits the audio into LEVEL_WINDOW_SAMPLES windows, measures each window's RMS
    level with ffmpeg astats, and takes the NOISE_PERCENTILE / SPEECH_PERCENTILE.

    Args:
        wav_path: Path to the 16 kHz mono WAV produced by extract_audio().

    Returns:
        (noise_db, speech_db) tuple.

    Raises:
        subprocess.CalledProcessError: if ffmpeg exits non-zero.
        RuntimeError: if ffmpeg reports no usable level measurements.
    """
    console.print("[cyan]Measuring audio levels[/cyan] …")

    # ametadata=print logs each window's RMS to stderr as
    # "lavfi.astats.Overall.RMS_level=<dB>"; reset=1 makes astats per-window.
    result = subprocess.run(
        [
            "ffmpeg", "-i", wav_path,
            "-af", (
                f"asetnsamples={LEVEL_WINDOW_SAMPLES},"
                "astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level"
            ),
            "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )

    # "-inf" windows are digital silence (e.g. muted segments). Skipping them keeps
    # them from dragging the noise floor to -inf and the threshold with it.
    levels = [
        float(v)
        for v in re.findall(r"RMS_level=(-?[\d.]+)", result.stderr)
    ]
    if not levels:
        raise RuntimeError("Could not measure audio levels (no astats output from ffmpeg).")

    noise_db = percentile(levels, NOISE_PERCENTILE)
    speech_db = percentile(levels, SPEECH_PERCENTILE)
    console.print(
        f"[green]Levels measured[/green] over {len(levels)} window(s): "
        f"noise floor {noise_db:.1f} dB, voice {speech_db:.1f} dB"
    )
    return noise_db, speech_db


def auto_silence_threshold(noise_db: float, speech_db: float) -> float:
    """Derive a silence threshold from the measured noise floor and voice level.

    Args:
        noise_db:  Noise floor in dBFS (from measure_levels()).
        speech_db: Voice level in dBFS (from measure_levels()).

    Returns:
        Threshold in dBFS, THRESHOLD_POSITION of the way from noise to voice.

    Side effects:
        Prints a warning when the noise→voice gap is below MIN_LEVEL_GAP_DB.
    """
    gap = speech_db - noise_db
    if gap < MIN_LEVEL_GAP_DB:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] voice is only {gap:.1f} dB above the "
            "noise floor; cuts may be unreliable. Consider passing --silence-threshold."
        )
    return round(noise_db + THRESHOLD_POSITION * gap, 1)


def get_audio_duration(wav_path: str) -> float:
    """Return the duration of a WAV file in seconds using ffprobe.

    Args:
        wav_path: Path to the WAV file.

    Returns:
        Duration in seconds as a float.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            wav_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def detect_silences(
    wav_path: str,
    threshold_db: float,
    min_duration: float,
) -> list[tuple[float, float]]:
    """Run ffmpeg silencedetect and return (silence_start, silence_end) tuples.

    Args:
        wav_path:      Path to the WAV file to analyse.
        threshold_db:  Silence threshold in dB (e.g. -25).
        min_duration:  Minimum silence duration in seconds to count as silence.

    Returns:
        List of (start, end) pairs in seconds. An open-ended silence at the tail
        is represented as (start, inf).

    Raises:
        subprocess.CalledProcessError: if ffmpeg exits non-zero.
    """
    console.print(
        f"[cyan]Detecting silences[/cyan] "
        f"(threshold={threshold_db}dB, min_duration={min_duration}s) …"
    )

    result = subprocess.run(
        [
            "ffmpeg", "-i", wav_path,
            "-af", f"silencedetect=noise={threshold_db}dB:duration={min_duration}",
            "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        # Fail loudly: without this, an ffmpeg error yields no silences and the
        # whole video would silently be kept as one speech interval.
        check=True,
    )

    silences: list[tuple[float, float]] = []
    current_start: float | None = None

    for line in result.stderr.splitlines():
        if "silence_start" in line:
            m = re.search(r"silence_start:\s*([\d.eE+-]+)", line)
            if m:
                current_start = float(m.group(1))
        elif "silence_end" in line:
            m = re.search(r"silence_end:\s*([\d.eE+-]+)", line)
            if m and current_start is not None:
                silences.append((current_start, float(m.group(1))))
                current_start = None

    # A trailing silence that never gets a silence_end in the ffmpeg output.
    if current_start is not None:
        silences.append((current_start, float("inf")))

    console.print(f"[green]Silence detection complete.[/green] {len(silences)} silence interval(s) found.")
    return silences


def silences_to_speech_intervals(
    silences: list[tuple[float, float]],
    audio_duration: float,
) -> list[tuple[float, float]]:
    """Invert silence intervals to derive speech intervals.

    Args:
        silences:       List of (start, end) silence pairs from detect_silences().
        audio_duration: Total audio duration in seconds.

    Returns:
        List of (start, end) speech interval pairs in seconds.
    """
    speech: list[tuple[float, float]] = []
    cursor: float = 0.0

    for sil_start, sil_end in silences:
        if sil_start > cursor:
            speech.append((cursor, sil_start))
        cursor = sil_end

    if cursor < audio_duration:
        speech.append((cursor, audio_duration))

    return speech


# ---------------------------------------------------------------------------
# EDL generation
# ---------------------------------------------------------------------------

def compute_edl_intervals(
    speech_intervals: list[tuple[float, float]],
    padding: float,
) -> list[tuple[float, float, float, float]]:
    """Compute padded source and record timecodes for each speech interval.

    Padding is clamped so that adjacent intervals never overlap: the midpoint
    between a speech interval end and the next interval start acts as the hard cap.

    Args:
        speech_intervals: List of (start, end) speech pairs in seconds.
        padding:          Seconds to add before/after each interval.

    Returns:
        List of (pad_src_start, pad_src_end, rec_in, rec_out) tuples.
    """
    edl_intervals: list[tuple[float, float, float, float]] = []
    record_cursor: float = 0.0
    prev_padded_end: float = 0.0

    for i, (raw_start, raw_end) in enumerate(speech_intervals):
        pad_start = max(raw_start - padding, 0.0)
        pad_start = max(pad_start, prev_padded_end)
        pad_end   = raw_end + padding

        if i < len(speech_intervals) - 1:
            next_raw_start = speech_intervals[i + 1][0]
            midpoint       = (raw_end + next_raw_start) / 2.0
            pad_end        = min(pad_end, midpoint)

        pad_end = max(pad_end, raw_end)
        prev_padded_end = pad_end

        rec_in  = record_cursor
        rec_out = record_cursor + (pad_end - pad_start)
        record_cursor = rec_out

        edl_intervals.append((pad_start, pad_end, rec_in, rec_out))

    return edl_intervals


def generate_edl(
    edl_intervals: list[tuple[float, float, float, float]],
    title: str,
    fps: int,
) -> str:
    """Generate a CMX 3600 EDL string from pre-computed EDL intervals.

    Args:
        edl_intervals: Output of compute_edl_intervals().
        title:         EDL title (usually the input file stem).
        fps:           Frame rate for timecode calculation.

    Returns:
        CMX 3600 EDL as a string (newline-terminated).
    """
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]

    for event_num, (pad_start, pad_end, rec_in, rec_out) in enumerate(edl_intervals, start=1):
        src_in  = seconds_to_timecode(pad_start, fps)
        src_out = seconds_to_timecode(pad_end,   fps)
        ri      = seconds_to_timecode(rec_in,    fps)
        ro      = seconds_to_timecode(rec_out,   fps)
        lines.append(f"{event_num:03d}  AX       V     C        {src_in} {src_out} {ri} {ro}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------

@media_app.command("video-to-edl")
def video_to_edl(
    input_video: Annotated[Path, typer.Argument(help="Input video file (.mp4 or .mov).")],
    fps: Annotated[int, typer.Option(help="Frame rate for EDL timecodes (frames per second).")] = 30,
    padding: Annotated[float, typer.Option(help="Seconds of padding added around each speech interval.")] = 0.05,
    silence_threshold: Annotated[float | None, typer.Option(help="Silence detection threshold in dB (e.g. -25). Default: derived from the file's noise floor and voice level.")] = None,
    silence_duration: Annotated[float, typer.Option(help="Minimum silence duration in seconds to count as a cut point.")] = 0.2,
) -> None:
    """Convert a video file to a CMX 3600 EDL using ffmpeg silence detection."""
    input_path = input_video.resolve()
    if not input_path.exists():
        console.print(f"[bold red]Error:[/bold red] File not found: {input_path}")
        raise typer.Exit(1)
    if input_path.suffix.lower() not in (".mp4", ".mov"):
        console.print(
            f"[bold red]Error:[/bold red] Unsupported file type '{input_path.suffix}'. "
            "Only .mp4 and .mov are supported."
        )
        raise typer.Exit(1)

    check_ffmpeg()

    edl_path = input_path.with_suffix(".edl")
    wav_path = tempfile.mktemp(suffix=".wav", prefix="tools_video_to_edl_")

    console.rule(f"[bold]video-to-edl — {input_path.name}[/bold]")

    speech_intervals: list[tuple[float, float]] = []

    try:
        extract_audio(str(input_path), wav_path)

        audio_duration = get_audio_duration(wav_path)
        console.print(f"[dim]Audio duration: {audio_duration:.2f}s[/dim]")

        # An explicit --silence-threshold always wins; otherwise adapt to this file.
        if silence_threshold is None:
            noise_db, speech_db = measure_levels(wav_path)
            silence_threshold = auto_silence_threshold(noise_db, speech_db)
            console.print(f"[green]Auto silence threshold:[/green] {silence_threshold} dB")
        else:
            console.print(f"[dim]Using manual silence threshold: {silence_threshold} dB[/dim]")

        silences = detect_silences(wav_path, silence_threshold, silence_duration)
        speech_intervals = silences_to_speech_intervals(silences, audio_duration)

        if not speech_intervals:
            console.print(
                "[bold yellow]Warning:[/bold yellow] No speech intervals detected. "
                "Try lowering --silence-threshold. Exiting."
            )
            raise typer.Exit(0)

        # Drop intervals shorter than 5 frames — they produce unusable EDL entries.
        min_duration_secs = 5 / fps
        speech_intervals = [iv for iv in speech_intervals if (iv[1] - iv[0]) >= min_duration_secs]

        if not speech_intervals:
            console.print("[bold yellow]Warning:[/bold yellow] All speech intervals are too short. Exiting.")
            raise typer.Exit(0)

        edl_intervals = compute_edl_intervals(speech_intervals, padding)
        edl_str = generate_edl(edl_intervals, input_path.stem, fps)
        edl_path.write_text(edl_str, encoding="utf-8")

    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
            console.print(f"[dim]Cleaned up temp file: {wav_path}[/dim]")

    console.rule("[bold green]Done[/bold green]")
    console.print(f"  Intervals : [bold]{len(speech_intervals)}[/bold] speech interval(s)")
    console.print(f"  EDL       : [bold cyan]{edl_path}[/bold cyan]")
    console.print()
