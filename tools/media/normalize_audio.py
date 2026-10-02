"""
tools/media/normalize_audio.py — `tools media normalize-audio`

Evens out the voice in a raw recording and normalizes it for YouTube (-14 LUFS,
-1 dBTP by default). Overwrites the input by default (or writes to -o); the video
stream is copied untouched and only the audio is re-encoded (AAC 320k).

Pipeline:
  1. Measure the raw audio (ebur128): integrated loudness, true peak, and the
     per-100 ms momentary loudness used to find where the voice is.
  2. Measure after the compressor alone → the gain that reaches the target.
  3. Measure the full chain (compressor → gain → limiter) → a small trim, because
     the limiter shaves off a little loudness.
  4. Render to a hidden temp file next to the destination: copy the video stream,
     process the first audio stream.
  5. Measure the temp file; only if that succeeds and hits the target, move it over
     the destination. Report before → after.

Non-obvious constraints:
  - Resolve's own normalization applies one gain to the whole clip, so it cannot fix
    passages that are louder or softer than others. The compressor does that here;
    its threshold follows each file's integrated loudness so quiet and loud
    recordings get the same treatment.
  - Raw LRA is dominated by pauses on unedited recordings, so the report uses a
    voice-only spread computed on timestamps where the raw file has speech.
  - iPhone data streams (Apple `mebx` metadata) are dropped: ffmpeg re-tags them
    incorrectly when copying and they carry no timecode.
  - Overwriting the original is the default (the user keeps one file per video), so
    the original is only replaced by an atomic rename after the result has been
    measured. Any failure before that leaves the original untouched and removes
    the temp file.
  - Measured design choices (why not dynaudnorm / loudnorm) are documented in
    docs/superpowers/specs/2026-10-01-normalize-audio-design.md.
"""

import math
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from tools.media import media_app
from tools.shared.ffmpeg import check_ffmpeg, has_audio_stream
from tools.shared.log import error, header, step, summary, warn

# strength → (compression ratio, threshold offset in dB below integrated loudness).
# A lower threshold and higher ratio compress more of the voice; attack/release
# (10 ms / 250 ms) are standard speech values shared by every preset.
COMPRESSOR_PRESETS: dict[str, tuple[int, float]] = {
    "gentle": (2, 4.0),
    "normal": (3, 6.0),
    "strong": (4, 8.0),
}

# acompressor rejects thresholds below 0.000976563 linear (≈ -60 dB).
MIN_THRESHOLD_DB = -60.0

# alimiter measures sample peaks, not true peaks. Running it at 192 kHz catches most
# inter-sample peaks; this margin absorbs the rest. 0.5 dB measured -1.46 dBTP for a
# -1 dBTP ceiling on both test recordings.
LIMITER_MARGIN_DB = 0.5

# Speech mask: momentary values within this many LU of the 75th percentile count as
# voice. 15 LU separated voice from room tone on both test recordings (voice around
# -21/-30 LUFS, pauses around -47/-70 LUFS).
SPEECH_GATE_LU = 15.0
DIGITAL_SILENCE_LUFS = -70.0

# Above this gain the room noise rises a lot along with the voice; worth a warning.
LARGE_GAIN_DB = 30.0

# Output audio: AAC 320k. The videos end up on Instagram/YouTube (lossy anyway), and
# the iPhone source is already ~95 kbps AAC, so lossless PCM only bloated files by
# ~17 MB/min without an audible benefit. 320k keeps the re-encode transparent.
AUDIO_CODEC_ARGS = ["-c:a", "aac", "-b:a", "320k"]

# The result must land this close to the target before it may replace the original.
# Normal runs land within 0.1 LU; anything further means something went wrong.
TARGET_TOLERANCE_LU = 1.0


class Strength(str, Enum):
    """CLI choices for --strength (values are COMPRESSOR_PRESETS keys)."""

    gentle = "gentle"
    normal = "normal"
    strong = "strong"


@dataclass
class Loudness:
    """One ebur128 measurement of a file or filter chain.

    Attributes:
        integrated: Integrated loudness, LUFS (-inf for silence).
        lra:        Loudness range, LU.
        true_peak:  Maximum true peak across channels, dBTP (-inf for silence).
        momentary:  Timestamp (s, 0.1 resolution) → momentary loudness, LUFS.
    """

    integrated: float
    lra: float
    true_peak: float
    momentary: dict[float, float]


class FfmpegError(RuntimeError):
    """ffmpeg exited non-zero; the message holds the tail of its stderr."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_ebur128_summary(stderr: str) -> tuple[float, float, float]:
    """Extract integrated loudness, LRA and true peak from ebur128's summary.

    Args:
        stderr: ffmpeg stderr of a run with `ebur128=peak=true`.

    Returns:
        (integrated LUFS, LRA LU, true peak dBTP).

    Raises:
        ValueError: if the summary block is missing (e.g. ffmpeg failed early).
    """
    # Search after "Summary:" only: ebur128 logs no per-frame values at the default
    # log level, but anchoring avoids matching anything printed before it.
    start = stderr.rfind("Summary:")
    if start == -1:
        raise ValueError("ebur128 summary not found in ffmpeg output")
    block = stderr[start:]
    number = r"(-?inf|-?[\d.]+)"
    i = re.search(rf"I:\s+{number} LUFS", block)
    lra = re.search(rf"LRA:\s+{number} LU", block)
    tp = re.search(rf"Peak:\s+{number} dBFS", block)
    if not (i and lra and tp):
        raise ValueError("incomplete ebur128 summary in ffmpeg output")
    return float(i.group(1)), float(lra.group(1)), float(tp.group(1))


def parse_momentary(stdout: str) -> dict[float, float]:
    """Parse ametadata's per-frame output into timestamp → momentary loudness.

    Args:
        stdout: Output of `ametadata=print:key=lavfi.r128.M:file=-`, alternating
                "frame:… pts_time:T" and "lavfi.r128.M=V" lines.

    Returns:
        {T rounded to 0.1 s: V}; float() parses "-inf" for digital silence.
        Rounding lets raw and processed measurements line up even if resampling
        in the chain shifts pts by a few microseconds.
    """
    values: dict[float, float] = {}
    t = 0.0
    for line in stdout.splitlines():
        if m := re.search(r"pts_time:([\d.]+)", line):
            t = round(float(m.group(1)), 1)
        elif m := re.search(r"lavfi\.r128\.M=(-?inf|-?[\d.]+)", line):
            values[t] = float(m.group(1))
    return values


# ---------------------------------------------------------------------------
# Voice spread metric
# ---------------------------------------------------------------------------

def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation — plenty for a 0.1 LU report).

    Args:
        values: Non-empty list of numbers.
        pct:    Fraction in [0, 1).

    Returns:
        The value at rank int(len * pct) of the sorted list.
    """
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * pct), len(ordered) - 1)]


def speech_mask(momentary: dict[float, float]) -> set[float]:
    """Return the timestamps where the voice is present.

    Args:
        momentary: Timestamp → momentary loudness of the RAW file.

    Returns:
        Timestamps within SPEECH_GATE_LU of the 75th percentile of non-silent
        frames. Empty if the file is entirely digital silence.

    The 75th percentile sits inside the voice whenever someone talks for at least a
    quarter of the recording, so the gate adapts to each file's recording level.
    """
    audible = [v for v in momentary.values() if v > DIGITAL_SILENCE_LUFS]
    if not audible:
        return set()
    gate = _percentile(audible, 0.75) - SPEECH_GATE_LU
    return {t for t, v in momentary.items() if v > gate}


def voice_spread(momentary: dict[float, float], mask: set[float]) -> float:
    """Spread of the voice's loudness: 95th − 10th percentile over masked timestamps.

    Args:
        momentary: Timestamp → momentary loudness (raw or processed file).
        mask:      Speech timestamps from speech_mask() on the RAW file, so a chain
                   that raises pauses cannot pull them into the metric.

    Returns:
        Spread in LU, or 0.0 if no masked timestamp exists in momentary.
    """
    voiced = [momentary[t] for t in mask if t in momentary]
    if not voiced:
        return 0.0
    return _percentile(voiced, 0.95) - _percentile(voiced, 0.10)


# ---------------------------------------------------------------------------
# Filter builders
# ---------------------------------------------------------------------------

def compressor_filter(strength: str, integrated: float) -> str:
    """Build the acompressor filter for a preset and a file's loudness.

    Args:
        strength:   COMPRESSOR_PRESETS key.
        integrated: The raw file's integrated loudness (LUFS), used as the anchor
                    for the threshold so it adapts to the recording level.

    Returns:
        An `acompressor=…` filter string.

    Raises:
        KeyError: unknown strength.
    """
    ratio, offset = COMPRESSOR_PRESETS[strength]
    threshold = max(integrated - offset, MIN_THRESHOLD_DB)
    return f"acompressor=threshold={threshold:.1f}dB:ratio={ratio}:attack=10:release=250"


def limiter_filter(true_peak: float) -> str:
    """Build the oversampled true-peak limiter.

    Args:
        true_peak: Ceiling in dBTP.

    Returns:
        `aresample=192000,alimiter=…,aresample=48000`. level=0 disables alimiter's
        auto-gain (which would push everything to the limit); latency=1 compensates
        its look-ahead so audio stays in sync with the video.
    """
    limit = 10 ** ((true_peak - LIMITER_MARGIN_DB) / 20)
    return (
        f"aresample=192000,alimiter=limit={limit:.4f}:level=0:attack=5:release=100:latency=1,"
        "aresample=48000"
    )


def build_chain(strength: str, integrated: float, gain_db: float, true_peak: float) -> str:
    """Assemble compressor → gain → limiter.

    Args:
        strength:   COMPRESSOR_PRESETS key.
        integrated: Raw integrated loudness (LUFS), anchors the compressor threshold.
        gain_db:    Constant gain applied after compression.
        true_peak:  Limiter ceiling (dBTP).

    Returns:
        A comma-separated ffmpeg audio filter chain.
    """
    return ",".join([
        compressor_filter(strength, integrated),
        f"volume={gain_db:.2f}dB",
        limiter_filter(true_peak),
    ])


# ---------------------------------------------------------------------------
# Paths / codecs
# ---------------------------------------------------------------------------

def temp_output_path(destination: Path) -> Path:
    """Return a hidden temp path next to destination, e.g. `.clip.normalizing.mov`.

    Args:
        destination: Final output path.

    Returns:
        A path in the same folder (so the final os.replace is an atomic rename on the
        same filesystem) with the same extension (ffmpeg picks the container from it).
    """
    return destination.with_name(f".{destination.stem}.normalizing{destination.suffix}")


# ---------------------------------------------------------------------------
# ffmpeg runners
# ---------------------------------------------------------------------------

def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg with -hide_banner -nostats and capture its output.

    Args:
        args: Arguments after `ffmpeg`.

    Returns:
        The completed process (stdout / stderr as text).

    Raises:
        FfmpegError: on non-zero exit, carrying the last lines of stderr.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", *args],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise FfmpegError(tail)
    return result


def measure(path: Path, chain: str = "") -> Loudness:
    """Measure the first audio stream of path, optionally through a filter chain.

    Args:
        path:  Media file.
        chain: ffmpeg audio filters applied before measuring ("" = raw).

    Returns:
        A Loudness with summary values and the momentary series.

    Raises:
        FfmpegError: ffmpeg failed. ValueError: no ebur128 summary was printed.

    -vn skips decoding the video, which is most of the cost on long 4K clips.
    """
    measure_filters = "ebur128=metadata=1:peak=true,ametadata=print:key=lavfi.r128.M:file=-"
    af = f"{chain},{measure_filters}" if chain else measure_filters
    result = run_ffmpeg(["-i", str(path), "-vn", "-map", "0:a:0", "-af", af, "-f", "null", "-"])
    integrated, lra, true_peak = parse_ebur128_summary(result.stderr)
    return Loudness(integrated, lra, true_peak, parse_momentary(result.stdout))


def render(input_path: Path, output_path: Path, chain: str) -> None:
    """Write output_path: video stream copied, first audio stream processed.

    Args:
        input_path:  Source video.
        output_path: Destination (overwritten).
        chain:       Audio filter chain from build_chain().

    Raises:
        FfmpegError: ffmpeg failed.
    """
    run_ffmpeg([
        "-y", "-i", str(input_path),
        # Only the first video + audio streams: iPhone metadata streams are dropped
        # on purpose (see module docstring). -map_metadata keeps creation date etc.
        "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "0",
        "-c:v", "copy",
        "-af", chain, "-ar", "48000",
        *AUDIO_CODEC_ARGS,
        str(output_path),
    ])


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------

def _fmt(value: float, unit: str) -> str:
    """Format a measurement like "-14.0 LUFS", or "silent" for -inf."""
    return "silent" if math.isinf(value) else f"{value:.1f} {unit}"


@media_app.command("normalize-audio")
def normalize_audio(
    input_file: Annotated[Path, typer.Argument(help="Input video with an audio stream (.mov, .mp4, …).")],
    target: Annotated[float, typer.Option(help="Integrated loudness target in LUFS.")] = -14.0,
    true_peak: Annotated[float, typer.Option(help="True-peak ceiling in dBTP.")] = -1.0,
    strength: Annotated[Strength, typer.Option(help="How much to even out the voice (compressor 2:1 / 3:1 / 4:1).")] = Strength.normal,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write here instead of overwriting the input.")] = None,
) -> None:
    """Even out the voice and normalize loudness (overwrites the input unless -o is given)."""
    input_path = input_file.resolve()
    output_path = (output or input_path).resolve()
    temp_path = temp_output_path(output_path)

    if not input_path.exists():
        error(f"File not found: {escape(str(input_path))}")
        raise typer.Exit(1)

    check_ffmpeg()
    if not has_audio_stream(str(input_path)):
        error(f"{escape(input_path.name)} has no audio stream.")
        raise typer.Exit(1)

    header("normalize-audio", input_path.name)

    try:
        with step("Measure original audio") as s:
            before = measure(input_path)
            # Both helpers return empty / 0.0 on silent audio, so no special case here.
            mask = speech_mask(before.momentary)
            spread_before = voice_spread(before.momentary, mask)
            s.result = f"{_fmt(before.integrated, 'LUFS')}, peak {_fmt(before.true_peak, 'dBTP')}"
            s.detail(f"voice spread {spread_before:.1f} LU · LRA {before.lra:.1f} LU · speech {len(mask) / max(len(before.momentary), 1):.0%} of the time")

        # Nothing to normalize: ebur128 reports silence as -70 LUFS (its absolute
        # gate), so the gain computed below would be absurd (+100 dB and more).
        if before.integrated <= DIGITAL_SILENCE_LUFS:
            error("The audio is silent — nothing to normalize.")
            raise typer.Exit(1)

        compressor = compressor_filter(strength.value, before.integrated)
        with step("Measure compression") as s:
            compressed = measure(input_path, compressor)
            gain = target - compressed.integrated
            s.result = f"{_fmt(compressed.integrated, 'LUFS')} → gain {gain:+.1f} dB"
            s.detail(f"filter: {compressor}")

        with step("Measure limiter") as s:
            chain = build_chain(strength.value, before.integrated, gain, true_peak)
            limited = measure(input_path, chain)
            # The limiter removes a little loudness; compensate with a final trim.
            # Re-measuring after the trim is skipped: the trim is a fraction of a dB,
            # so its effect on peaks stays inside LIMITER_MARGIN_DB.
            trim = target - limited.integrated
            gain += trim
            s.result = f"trim {trim:+.2f} dB → total gain {gain:+.1f} dB"
            s.detail(f"limiter ceiling {true_peak - LIMITER_MARGIN_DB:.1f} dB at 192 kHz")

        if gain > LARGE_GAIN_DB:
            warn(
                f"Very quiet recording: {gain:+.1f} dB of gain raises room noise just as much.",
                hint="Record closer to the mic or raise the input gain next time.",
            )

        chain = build_chain(strength.value, before.integrated, gain, true_peak)
        with step("Render") as s:
            render(input_path, temp_path, chain)
            s.result = "video copied, audio AAC 320k"
            s.detail(f"chain: {escape(chain)}")
            s.detail(f"temp file: {escape(temp_path.name)}")

        with step("Measure result") as s:
            after = measure(temp_path)
            spread_after = voice_spread(after.momentary, mask)
            s.result = f"{_fmt(after.integrated, 'LUFS')}, peak {_fmt(after.true_peak, 'dBTP')}"
            s.detail(f"voice spread {spread_after:.1f} LU · LRA {after.lra:.1f} LU")

        # Last gate before a destructive rename: a result far from the target means
        # the chain misbehaved, and the original is worth more than a bad copy.
        if abs(after.integrated - target) > TARGET_TOLERANCE_LU:
            error(
                f"Result is {_fmt(after.integrated, 'LUFS')}, too far from the {target} LUFS target.",
                hint="The original was left untouched.",
            )
            raise typer.Exit(1)

        with step("Replace original" if output_path == input_path else "Save output") as s:
            # Atomic on the same filesystem: readers see the old file or the new one,
            # never a half-written video.
            os.replace(temp_path, output_path)
            s.result = escape(output_path.name)

    except (FfmpegError, ValueError) as exc:
        error("ffmpeg failed — the original was left untouched.", hint=escape(str(exc)))
        raise typer.Exit(1) from exc
    finally:
        # Covers every exit path above (errors, the tolerance gate, Ctrl-C); after a
        # successful os.replace the temp path no longer exists.
        temp_path.unlink(missing_ok=True)

    # ebur128 reports peaks to 0.1 dB; anything above the ceiling means the limiter
    # could not hold it (should not happen with the oversampled limiter + margin).
    if after.true_peak > true_peak:
        warn(
            f"True peak {_fmt(after.true_peak, 'dBTP')} is above the {true_peak} dBTP ceiling.",
            hint="Try a lower --true-peak, or report the file so the limiter margin can be tuned.",
        )

    summary({
        "Loudness": f"{_fmt(before.integrated, 'LUFS')} → {_fmt(after.integrated, 'LUFS')}",
        "True peak": f"{_fmt(before.true_peak, 'dBTP')} → {_fmt(after.true_peak, 'dBTP')}",
        "Voice spread": f"{spread_before:.1f} LU → {spread_after:.1f} LU",
        "Strength": f"{strength.value} ({COMPRESSOR_PRESETS[strength.value][0]}:1 compression)",
        "Gain": f"{gain:+.1f} dB",
        "Output": output_path,
    })
