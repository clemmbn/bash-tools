# `tools media normalize-audio` — Design

**Date:** 2026-10-01
**Status:** Approved — revised after measuring the test recordings (see "Why not dynaudnorm")

## Problem

YouTube-bound videos are normalized to -14 LUFS in DaVinci Resolve, but Resolve's
normalization applies a **single gain** computed from the clip's integrated loudness.
When the voice level varies across the recording (some sentences soft, others loud),
the average lands on -14 while individual passages stay too quiet or too loud, and
manual adjustment takes time.

The real problem is **loudness variation of the voice over time**, not the overall
level. It needs dynamics processing before the final gain.

## Goal

A command that takes a raw recording and produces a copy with:

- voice evened out (smaller spread between soft and loud passages),
- integrated loudness at the target (default -14 LUFS),
- true peak at or below the ceiling (default -1 dBTP),
- video stream untouched (no re-encode), audio in sync,

ready to import into Resolve in place of the raw file. Music and SFX are added later
in Resolve under the voice, so a final Resolve normalization only makes a small correction.

## Non-goals (YAGNI)

- Noise reduction, noise gate, EQ, de-essing.
- Batch processing of several files.
- Per-sentence levelling driven by speech detection.

## Interface

```bash
tools media normalize-audio video.mov                     # overwrites video.mov
tools media normalize-audio video.mov --strength strong
tools media normalize-audio video.mp4 --target -14 --true-peak -1 -o out.mp4
```

| Option | Default | Meaning |
|---|---|---|
| `input_file` (arg) | — | Any ffmpeg-readable video with an audio stream |
| `--target` | `-14.0` | Integrated loudness target, LUFS |
| `--true-peak` | `-1.0` | True-peak ceiling, dBTP |
| `--strength` | `normal` | `gentle` / `normal` / `strong` compressor preset |
| `-o / --output` | the input file | Write here instead of replacing the input |

**Revised 2026-10-02:** the original is overwritten by default (the videos are for
Instagram/YouTube and the user keeps one file per video). Safety: render to a hidden
temp file next to the destination, measure it, refuse if it is more than 1 LU off the
target, then `os.replace` (atomic). Any failure leaves the original untouched and
deletes the temp file.

A Raycast wrapper `raycast/normalize-audio.sh` runs it on the Finder selection, following
the pattern of the existing wrappers.

## Approach

Audio chain: **compressor → gain → true-peak limiter**.

1. `acompressor` evens out the voice. Its threshold is placed a few dB below the file's
   own integrated loudness, so it adapts to quiet and loud recordings alike. Pauses sit
   far below the threshold and are not touched by it.
2. `volume` applies one constant gain computed so the result lands on the target.
3. `alimiter` catches the peaks the gain pushed over the ceiling. It runs on audio
   upsampled to 192 kHz so it sees inter-sample (true) peaks, with a small safety margin
   under the ceiling, and `latency=1` so its look-ahead does not shift audio vs video.

### Strength presets

| Preset | Ratio | Threshold |
|---|---|---|
| `gentle` | 2:1 | integrated − 4 dB |
| `normal` | 3:1 | integrated − 6 dB |
| `strong` | 4:1 | integrated − 8 dB |

Attack 10 ms, release 250 ms (standard speech settings) for all presets.

### Why not dynaudnorm (measured 2026-10-01)

Measured on the two test recordings with a fixed speech mask (see Testing):

| Chain | IMG_4038 I / TP / voice spread | IMG_1050 I / TP / voice spread |
|---|---|---|
| raw | -19.9 / +2.2 / 20.1 | -28.5 / -7.7 / 11.9 |
| `dynaudnorm` (f=500 g=31 m=10) | -16.4 / +1.7 / 18.6 | -17.8 / -0.5 / 12.8 |
| `loudnorm` dynamic | -14.9 / -1.0 / 16.5 | -15.0 / -1.0 / 10.8 |
| compressor 3:1 + gain + limiter | -14.0 / -1.5 / 13.1 | -14.0 / -1.5 / 7.2 |

`dynaudnorm` is peak-driven, so it barely reduces voice spread (and increases it on
IMG_1050). It also leaves peaks near 0 dBFS, so the planned linear `loudnorm` would have
fallen back to dynamic mode on both files. `loudnorm` dynamic misses the target by ~1 LU.
Both files have a peak-to-loudness ratio of 20–22 dB while -14 LUFS / -1 dBTP allows 13,
so a limiter is required whatever the leveller.

## Pipeline

Module: `tools/media/normalize_audio.py`. All measurements use one ffmpeg `ebur128` pass
(`-vn`, first audio stream): the summary on stderr gives I, LRA and true peak; the
per-100 ms momentary loudness on stdout feeds the voice-spread metric.

1. **Checks** — input exists, output ≠ input, `check_ffmpeg()`, `has_audio_stream()`
   (extracted from `extract_audio()` into `tools/shared/ffmpeg.py`).
2. **Measure raw** — I, LRA, TP, momentary series; derive the speech mask.
3. **Measure compressed** — I after `acompressor` only → gain = target − I.
4. **Measure full chain** — I after compressor + gain + limiter → trim = target − I
   (the limiter removes a little loudness); final gain = gain + trim.
5. **Render** — one ffmpeg command:
   `-map 0:v:0 -map 0:a:0 -map_metadata 0 -c:v copy -af <chain> -ar 48000 <codec>`;
   audio codec `aac -b:a 320k` for every container (revised 2026-10-02: PCM added
   ~17 MB/min with no benefit for Instagram/YouTube; the source is ~95 kbps AAC).
6. **Measure output** — I, LRA, TP, voice spread on the rendered file.
7. **Summary** — before → after for Loudness, True peak, Voice spread, plus the gain
   applied and the output path.

Data streams are **not** copied: on iPhone files they are Apple `mebx` metadata, which
ffmpeg re-tags incorrectly (`stts`) when copying. These files carry no timecode track.

### Voice spread metric

Raw LRA is dominated by pauses on unedited recordings (IMG_4038 reads 24 LU while the
voice itself is fairly steady), so the report uses a voice-only metric:

- speech mask = timestamps where the **raw** momentary loudness is within 15 LU of its
  75th percentile (ignoring digital silence below -70 LUFS);
- voice spread = 95th − 10th percentile of momentary loudness over the masked timestamps.

The mask is computed once on the raw file and reused on the output, so a chain that
raises pauses cannot sneak them into the metric.

### Helpers (small, testable functions)

- `parse_ebur128_summary(stderr) -> (i, lra, tp)`
- `parse_momentary(stdout) -> dict[float, float]` — timestamp → momentary LUFS.
- `speech_mask(momentary) -> set[float]`, `voice_spread(momentary, mask) -> float`.
- `compressor_filter(strength, integrated)`, `limiter_filter(true_peak)`,
  `build_chain(strength, integrated, gain_db, true_peak)`.
- `temp_output_path(destination) -> Path` — hidden sibling `.stem.normalizing.ext`.

## Errors and edge cases

- Missing input, output == input, no audio stream → `error()` + exit 1.
- ffmpeg non-zero exit → `error()` with the tail of stderr + exit 1.
- Silent audio (ebur128 reports -70 LUFS, its absolute gate) → `error()` + exit 1.
- Output true peak above the ceiling (limiter could not hold it) → `warn()`.
- Gain above +30 dB → `warn()` that the recording is very quiet and room noise will rise.
- All user-supplied paths escaped with `rich.markup.escape`; output readable with `NO_COLOR=1`.

## Testing

`pytest` (dev dependency), `tests/test_normalize_audio.py`:

- Unit: summary and momentary parsing, speech mask / spread, filter builders (each
  preset), codec choice, default output path.
- End-to-end (skipped without ffmpeg): synthetic video whose tone jumps from -30 dBFS to
  -10 dBFS → output at -14 ±1 LUFS, true peak ≤ -0.5 dBTP, video stream codec unchanged,
  audio duration equal to input ±50 ms. Plus refusal cases (output == input, no audio).

Manual validation on the two real recordings in `assets/` (untracked, not committed):

| File | Before I | Before TP | Before voice spread | Notes |
|---|---|---|---|---|
| `IMG_1050.MOV` | -28.45 LUFS | -7.73 dBTP | 11.9 LU | HEVC, quiet overall |
| `IMG_4038.mov` | -19.86 LUFS | +2.44 dBTP | 20.1 LU | H.264, long pauses, clipping peaks |

Success: both outputs at -14 ±0.5 LUFS, TP ≤ -1 dBTP, voice spread clearly reduced, video
stream copied, durations equal, files open in Resolve. Listen for pumping and adjust presets.
