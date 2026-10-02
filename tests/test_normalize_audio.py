"""
tests/test_normalize_audio.py — Tests for `tools media normalize-audio`.

Covers:
  - Pure helpers: ebur128 parsing, speech mask / voice spread, filter builders,
    codec choice, default output path.
  - End-to-end: a synthetic video whose tone jumps from -24 dBFS to -12 dBFS is
    compressed, normalized and limited, then re-measured with ffmpeg.

Non-obvious constraints:
  - End-to-end tests need ffmpeg on PATH and are skipped otherwise.
  - The synthetic clip is generated per test session in pytest's tmp dir, so the
    real recordings in assets/ are never touched by the suite.
"""

import math
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tools.main import app
from tools.media.normalize_audio import (
    COMPRESSOR_PRESETS,
    FfmpegError,
    build_chain,
    compressor_filter,
    limiter_filter,
    measure,
    parse_ebur128_summary,
    parse_momentary,
    speech_mask,
    temp_output_path,
    voice_spread,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# Trimmed real stderr tail of an `ebur128=peak=true` run.
SAMPLE_SUMMARY = """\
[out#0/null @ 0x7c0] video:0KiB audio:3750KiB
[Parsed_ebur128_0 @ 0x77c7021500] Summary:

  Integrated loudness:
    I:         -27.4 LUFS
    Threshold: -39.4 LUFS

  Loudness range:
    LRA:         5.6 LU
    Threshold: -49.5 LUFS
    LRA low:   -32.6 LUFS
    LRA high:  -27.0 LUFS

  True peak:
    Peak:       -9.9 dBFS
"""

# Trimmed real stdout of `ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=-`.
SAMPLE_MOMENTARY = """\
frame:0    pts:0       pts_time:0
lavfi.r128.M=-120.691
frame:1    pts:4800    pts_time:0.1
lavfi.r128.M=-63.562
frame:2    pts:9600    pts_time:0.2
lavfi.r128.M=-inf
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_ebur128_summary():
    assert parse_ebur128_summary(SAMPLE_SUMMARY) == (-27.4, 5.6, -9.9)


def test_parse_ebur128_summary_handles_silence():
    silent = SAMPLE_SUMMARY.replace("-27.4 LUFS", "-inf LUFS").replace("-9.9 dBFS", "-inf dBFS")
    i, _, tp = parse_ebur128_summary(silent)
    assert i == -math.inf and tp == -math.inf


def test_parse_ebur128_summary_raises_without_summary():
    with pytest.raises(ValueError):
        parse_ebur128_summary("ffmpeg: some error, no summary printed\n")


def test_parse_momentary():
    assert parse_momentary(SAMPLE_MOMENTARY) == {0.0: -120.691, 0.1: -63.562, 0.2: -math.inf}


# ---------------------------------------------------------------------------
# Speech mask / voice spread
# ---------------------------------------------------------------------------

def test_speech_mask_drops_pauses_and_digital_silence():
    # 0.0–0.9 s: voice around -20; 1.0–1.4 s: room tone at -50; 1.5 s: digital silence.
    momentary = {round(t / 10, 1): -20.0 - (t % 3) for t in range(10)}
    momentary |= {round(t / 10, 1): -50.0 for t in range(10, 15)}
    momentary[1.5] = -math.inf
    assert speech_mask(momentary) == {round(t / 10, 1) for t in range(10)}


def test_voice_spread_uses_only_masked_timestamps():
    momentary = {0.0: -30.0, 0.1: -20.0, 0.2: -60.0}
    # Only the first two count; their 95th–10th percentile spread is 10.
    assert voice_spread(momentary, {0.0, 0.1}) == pytest.approx(10.0)


def test_voice_spread_ignores_masked_timestamps_missing_from_output():
    assert voice_spread({0.0: -25.0, 0.1: -22.0}, {0.0, 0.1, 9.9}) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Filter builders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strength", list(COMPRESSOR_PRESETS))
def test_compressor_filter_threshold_follows_file_level(strength):
    ratio, offset = COMPRESSOR_PRESETS[strength]
    assert compressor_filter(strength, -20.0) == (
        f"acompressor=threshold={-20.0 - offset:.1f}dB:ratio={ratio}:attack=10:release=250"
    )


def test_compressor_filter_clamps_threshold_to_ffmpeg_minimum():
    # acompressor rejects thresholds below -60 dB (0.000976563 linear).
    assert "threshold=-60.0dB" in compressor_filter("strong", -58.0)


def test_compressor_filter_rejects_unknown_strength():
    with pytest.raises(KeyError):
        compressor_filter("extreme", -20.0)


def test_limiter_filter_oversamples_and_keeps_sync():
    f = limiter_filter(-1.0)
    assert f.startswith("aresample=192000,alimiter=")
    assert "level=0" in f and "latency=1" in f
    assert f.endswith(",aresample=48000")


def test_limiter_filter_sits_below_the_ceiling():
    limit = float(limiter_filter(-1.0).split("limit=")[1].split(":")[0])
    assert 20 * math.log10(limit) < -1.0


def test_build_chain_order():
    chain = build_chain("normal", -20.0, 6.5, -1.0)
    assert chain.index("acompressor") < chain.index("volume=6.50dB") < chain.index("alimiter")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_temp_output_path_is_hidden_sibling_with_same_extension():
    # Same folder → atomic rename; same extension → ffmpeg picks the same container.
    assert temp_output_path(Path("/videos/IMG_1050.MOV")) == Path("/videos/.IMG_1050.normalizing.MOV")


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

def probe(path: Path, stream: str, entry: str) -> str:
    """Read one ffprobe stream entry (e.g. codec_name, duration) from path.

    Args:
        path:   Media file.
        stream: ffprobe stream selector, e.g. "v:0".
        entry:  Stream entry name.

    Returns:
        The entry's value as printed by ffprobe.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
         f"stream={entry}", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="session")
def uneven_clip(tmp_path_factory) -> Path:
    """Generate a 20 s video whose 440 Hz tone is at -24 dBFS, then -12 dBFS.

    The 12 dB jump mimics a speaker who is much quieter in one section, which is
    exactly the variation the compressor must reduce. It stays under the 15 LU
    speech gate so both halves count as voice (a larger jump would make the quiet
    half look like a pause).
    """
    path = tmp_path_factory.mktemp("media") / "uneven.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=20",
         # 0.0631 ≈ -24 dBFS and 0.251 ≈ -12 dBFS amplitude.
         "-f", "lavfi", "-i", "aevalsrc='if(lt(t,10),0.0631,0.251)*sin(2*PI*440*t)':d=20:s=48000",
         "-c:v", "mpeg4", "-c:a", "pcm_s16le", "-shortest", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
def test_normalize_audio_end_to_end(uneven_clip, tmp_path):
    output = tmp_path / "out.mov"
    result = CliRunner().invoke(app, ["media", "normalize-audio", str(uneven_clip), "-o", str(output)])
    assert result.exit_code == 0, result.output

    before, after = measure(uneven_clip), measure(output)
    assert abs(after.integrated + 14) <= 1.0
    assert after.true_peak <= -0.5  # -1 dBTP ceiling, small measurement tolerance

    # The compressor must shrink the gap between the quiet and loud halves.
    mask = speech_mask(before.momentary)
    assert voice_spread(after.momentary, mask) < voice_spread(before.momentary, mask)

    # Video copied, not re-encoded; audio AAC; audio not shifted or truncated.
    assert probe(output, "v:0", "codec_name") == "mpeg4"
    assert probe(output, "a:0", "codec_name") == "aac"
    assert abs(float(probe(output, "a:0", "duration")) - float(probe(uneven_clip, "a:0", "duration"))) <= 0.05


def copy_clip(source: Path, folder: Path) -> Path:
    """Copy the session clip into a test's own folder so it can be overwritten.

    Args:
        source: The session-scoped synthetic clip.
        folder: The test's tmp_path.

    Returns:
        Path of the copy.
    """
    clip = folder / "clip.mov"
    shutil.copy(source, clip)
    return clip


@needs_ffmpeg
def test_normalize_audio_overwrites_input_by_default(uneven_clip, tmp_path):
    clip = copy_clip(uneven_clip, tmp_path)
    result = CliRunner().invoke(app, ["media", "normalize-audio", str(clip)])
    assert result.exit_code == 0, result.output

    assert abs(measure(clip).integrated + 14) <= 1.0
    assert probe(clip, "a:0", "codec_name") == "aac"
    # Only the replaced file remains: no "_normalized" copy, no leftover temp file.
    assert [p.name for p in tmp_path.iterdir()] == ["clip.mov"]


@needs_ffmpeg
def test_normalize_audio_keeps_original_when_render_fails(uneven_clip, tmp_path, monkeypatch):
    clip = copy_clip(uneven_clip, tmp_path)
    original = clip.read_bytes()

    def broken_render(input_path, output_path, chain):
        # Simulate ffmpeg dying halfway: a partial temp file, then an error.
        output_path.write_bytes(b"partial")
        raise FfmpegError("simulated failure")

    monkeypatch.setattr("tools.media.normalize_audio.render", broken_render)
    result = CliRunner().invoke(app, ["media", "normalize-audio", str(clip)])

    assert result.exit_code == 1
    assert clip.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["clip.mov"]


@needs_ffmpeg
def test_normalize_audio_rejects_file_without_audio(tmp_path):
    silent = tmp_path / "noaudio.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=1",
         "-c:v", "mpeg4", str(silent)],
        check=True,
    )
    result = CliRunner().invoke(app, ["media", "normalize-audio", str(silent)])
    assert result.exit_code == 1


@needs_ffmpeg
def test_normalize_audio_rejects_silent_audio(tmp_path):
    # ebur128 reports digital silence as -70 LUFS (its absolute gate), not -inf.
    silent = tmp_path / "silent.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=3",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3",
         "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(silent)],
        check=True,
    )
    original = silent.read_bytes()
    result = CliRunner().invoke(app, ["media", "normalize-audio", str(silent)])
    assert result.exit_code == 1
    assert silent.read_bytes() == original
