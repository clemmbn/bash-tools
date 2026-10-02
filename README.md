# tools

<p align="center">
  <img width="748" height="161" alt="image" src="https://github.com/user-attachments/assets/02ed521d-001b-421a-a176-9330cccb5df3" />
</p>

A personal CLI toolkit built with [Typer](https://typer.tiangolo.com/) and managed by [uv](https://docs.astral.sh/uv/). Commands are organised into groups (e.g. `media`). Each group can be extended independently without touching others.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Commands at a glance](#commands-at-a-glance)
- [`tools media`](#tools-media)
  - [`normalize-audio`](#normalize-audio) — even out the voice, normalize to -14 LUFS
  - [`video-to-edl`](#video-to-edl) — cut silences into an EDL
  - [`audio-to-srt`](#audio-to-srt) — subtitles with Whisper
  - [`transcribe`](#transcribe) — transcript to terminal / txt / md / json
  - [`srt-to-md`](#srt-to-md) — SRT to a sentence-per-line transcript
- [Raycast integration](#raycast-integration)
- [Development](#development)

---

## Requirements

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/) — for running and installing
- [ffmpeg](https://ffmpeg.org/) — required by all `media` commands (`brew install ffmpeg` on macOS)

## Installation

```bash
git clone https://github.com/<your-username>/bash-tools.git
cd bash-tools

# Install once for system-wide use. --editable makes the installed `tools`
# follow the source code, so new commands appear without reinstalling.
uv tool install --editable .
tools --help

# Or run without installing (development)
uv run tools --help
```

> If a new command is missing from `tools --help`, the install is a non-editable snapshot: re-run `uv tool install --editable . --force`.

---

## Commands at a glance

| Command | What it does | Input → output |
|---|---|---|---|
| [`normalize-audio`](#normalize-audio) | Evens out the voice, normalizes to -14 LUFS / -1 dBTP | video → **same file, overwritten** |
| [`video-to-edl`](#video-to-edl) | Detects speech, writes cuts that remove silences | video → `.edl` |
| [`audio-to-srt`](#audio-to-srt) | Transcribes locally into subtitles | audio/video → `.srt` |
| [`transcribe`](#transcribe) | Transcribes locally into a timestamped transcript | audio/video → terminal / `.txt` / `.md` / `.json` |
| [`srt-to-md`](#srt-to-md) | Merges subtitle blocks into sentences | `.srt` → `.md` |

Typical order for a raw recording: `normalize-audio` → `video-to-edl` → edit in Resolve → `audio-to-srt`.

Every command prints a short report and works with `--help`, e.g. `tools media normalize-audio --help`.

---

## `tools media`

Media processing tools — all require ffmpeg on your `PATH`.

### `normalize-audio`

Evens out the voice and normalizes loudness to **-14 LUFS / -1 dBTP**, then **overwrites the original** (video stream untouched, audio re-encoded as AAC 320k).

```bash
tools media normalize-audio <input_file> [--strength gentle|normal|strong] [-o OUTPUT]
```

<details>
<summary><b>Options</b></summary>

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | — | Video with an audio stream (`.mov`, `.mp4`, …). |
| `--target` | `-14.0` | Integrated loudness target in LUFS. |
| `--true-peak` | `-1.0` | True-peak ceiling in dBTP. |
| `--strength` | `normal` | How much to even out the voice: `gentle` (2:1), `normal` (3:1), `strong` (4:1) compression. |
| `-o`, `--output` | _(overwrite input)_ | Write to this path instead of replacing the input. |

</details>

<details>
<summary><b>Examples</b></summary>

```bash
# Default: -14 LUFS, normal compression, original replaced
tools media normalize-audio IMG_4038.mov

# Very uneven recording
tools media normalize-audio IMG_4038.mov --strength strong

# Keep the original, custom target
tools media normalize-audio clip.mp4 --target -16 -o clip_web.mp4
```

</details>

<details>
<summary><b>How it works</b></summary>

Why not just Resolve's normalization: it applies **one gain** to the whole clip, so passages where you speak softer or louder stay that way — only the average lands on -14. This command compresses the voice first so passages sit closer together, then sets the gain and catches peaks with a true-peak limiter.

1. Measure the original audio with `ebur128` (loudness, true peak, voice spread).
2. Measure after the compressor (threshold set a few dB under the file's own loudness) → gain to reach the target.
3. Measure the full chain (compressor → gain → limiter at 192 kHz) → small trim to compensate the limiter.
4. Render to a hidden temp file next to the original: video copied (`-c:v copy`), audio processed, metadata kept; iPhone metadata streams dropped.
5. Measure the result. Only if it succeeded and landed within 1 LU of the target does it replace the original (atomic rename); on any failure the original is left untouched.

**Reading the report:**
- **Loudness / True peak** — should end at the target and at or under the ceiling.
- **Voice spread** — gap (LU) between soft and loud moments of your voice (95th − 10th percentile of momentary loudness, pauses excluded). Lower = more even. Raw LRA is not used because pauses dominate it on unedited footage.

⚠️ Don't run it twice on the same file: the voice would be compressed a second time.

Design notes and measurements: [docs/superpowers/specs/2026-10-01-normalize-audio-design.md](docs/superpowers/specs/2026-10-01-normalize-audio-design.md).

</details>

### `video-to-edl`

Converts a video to a **CMX 3600 EDL** that keeps only the speech, by detecting silences. Import the `.edl` into Resolve, Premiere Pro or Final Cut Pro.

```bash
tools media video-to-edl <input_video> [OPTIONS]
```

<details>
<summary><b>Options</b></summary>

| Option | Default | Description |
|---|---|---|
| `INPUT_VIDEO` | — | Path to the input video file (`.mp4` or `.mov`). |
| `--fps` | `30` | Frame rate used to compute timecodes in the EDL. |
| `--padding` | `0.05` | Seconds of padding added before and after each speech interval. |
| `--silence-threshold` | auto | Silence detection threshold in dB. By default it is derived from each file's measured noise floor and voice level, so videos recorded at different voice levels get the same cuts. Pass a value to override: higher (e.g. `-20`) detects more silence; lower (e.g. `-35`) is more conservative. |
| `--silence-duration` | `0.2` | Minimum duration in seconds for a gap to be treated as silence. |

</details>

<details>
<summary><b>Examples</b></summary>

```bash
# Basic usage — defaults suit most talking-head footage
tools media video-to-edl interview.mp4

# Quieter recording — lower threshold and tighter padding
tools media video-to-edl podcast.mov --silence-threshold -35 --padding 0.1

# 24 fps project
tools media video-to-edl footage.mp4 --fps 24
```

</details>

<details>
<summary><b>How it works</b></summary>

1. Extract audio to a temporary 16 kHz mono WAV.
2. Detect silence intervals with `ffmpeg silencedetect`.
3. Invert silence → speech intervals; drop intervals shorter than 5 frames.
4. Pad each interval and build CMX 3600 EDL entries.
5. Write `<input>.edl` next to the input; clean up the temp WAV.

</details>

### `audio-to-srt`

Transcribes an audio or video file locally with [OpenAI Whisper](https://github.com/openai/whisper) and writes an **SRT subtitle file** next to the input. Runs entirely on your machine — no API key or internet connection required.

```bash
tools media audio-to-srt <input_file> [OPTIONS]
```

<details>
<summary><b>Options</b></summary>

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | — | Path to any audio or video file (MP3, MP4, MOV, WAV, M4A, AAC). |
| `--model` | `turbo` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large`, `turbo`. Larger models are more accurate but slower and use more memory. |
| `--max-line-width` | `10` | Maximum characters per subtitle line. |
| `--silence-threshold` | `0.5` | Silence gap in seconds that forces a new caption block. |
| `--max-lines` | `1` | Maximum number of lines per caption block. |
| `--min-gap` | `0.1` | Minimum gap in seconds allowed between two consecutive subtitle blocks. Gaps shorter than this are eliminated by extending both blocks to meet at the midpoint. Set to `0.0` to disable. |

**Model comparison:**

| Model | Speed | Accuracy | VRAM |
|---|---|---|---|
| `tiny` | Fastest | Lowest | ~1 GB |
| `base` | Fast | Low | ~1 GB |
| `small` | Moderate | Good | ~2 GB |
| `medium` | Slow | Better | ~5 GB |
| `large` | Slowest | Best | ~10 GB |
| `turbo` | Fast | High | ~6 GB |

</details>

<details>
<summary><b>Examples</b></summary>

```bash
# Basic usage with default turbo model
tools media audio-to-srt interview.mp4

# Higher accuracy for complex speech
tools media audio-to-srt lecture.mp4 --model large

# Wider captions with more words per line
tools media audio-to-srt podcast.mp3 --max-line-width 40 --max-lines 2

# Faster transcription for a quick draft
tools media audio-to-srt clip.mov --model small

# Eliminate gaps shorter than 150 ms between subtitle blocks
tools media audio-to-srt interview.mp4 --min-gap 0.15
```

</details>

<details>
<summary><b>How it works</b></summary>

1. Convert input to a 16 kHz mono WAV (skipped if already `.wav`).
2. Transcribe with local Whisper using word-level timestamps.
3. Group words into caption blocks, breaking on silence gaps and sentence-ending punctuation.
4. Fill sub-threshold gaps between consecutive blocks by snapping both boundaries to their midpoint.
5. Write `<input>.srt` next to the input.

</details>

### `transcribe`

Transcribes an audio or video file locally with Whisper and prints a timestamped transcript to the terminal, or exports it as plain text, Markdown, or raw JSON.

```bash
tools media transcribe <input_file> [--output-format txt|md|raw]
```

<details>
<summary><b>Options</b></summary>

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | — | Path to any audio or video file (MP3, MP4, MOV, WAV, M4A, AAC, MKV). |
| `--model` | `turbo` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large`, `turbo`. |
| `--output-format` | _(none)_ | `txt` (plain text), `md` (Markdown with `[MM:SS]` timestamps), or `raw` (full Whisper result as JSON). Omit to print to the terminal. |

</details>

<details>
<summary><b>Examples</b></summary>

```bash
# Print a timestamped transcript to the terminal
tools media transcribe interview.mp4

# Export a plain-text transcript
tools media transcribe lecture.mp4 --output-format txt

# Export a Markdown transcript with timestamps
tools media transcribe podcast.mp3 --output-format md

# Export the raw Whisper result for downstream processing
tools media transcribe clip.mov --output-format raw
```

</details>

<details>
<summary><b>How it works</b></summary>

1. Validate ffmpeg is on `PATH`.
2. Convert input to a 16 kHz mono WAV (skipped if already `.wav`).
3. Transcribe with local Whisper using word-level timestamps.
4. Print to the terminal, or write `<input>.txt` / `.md` / `.json` depending on `--output-format`.

</details>

### `srt-to-md`

Converts an SRT subtitle file into a Markdown transcript with one timestamped sentence per line, written next to the input as `<input>.md`.

```bash
tools media srt-to-md <input_file.srt>
```

<details>
<summary><b>Output format</b></summary>

```
[00:00] Hey, my name is Clement!
[00:05] I'm 22 and I love tennis.
```

</details>

<details>
<summary><b>How it works</b></summary>

1. Parse SRT blocks (strips HTML-like tags such as `<b>`).
2. Merge consecutive blocks into sentences, ending on `.`, `!`, or `?`.
3. Write one sentence per line, prefixed with the start time of its first block.

</details>

---

## Raycast integration

Each command has a Raycast script command in [`raycast/`](raycast/). Select a file in Finder, run the command from Raycast, and the output window shows the report.

| Script | Raycast command | Accepts |
|---|---|---|
| [`normalize-audio.sh`](raycast/normalize-audio.sh) | 🔊 Normalize Audio (optional Strength dropdown) — **overwrites the file** | `.mp4`, `.mov` |
| [`video-to-edl.sh`](raycast/video-to-edl.sh) | 🎬 Video to EDL | `.mp4`, `.mov` |
| [`audio-to-srt.sh`](raycast/audio-to-srt.sh) | 🎤 Audio to SRT | `.mp3`, `.mp4`, `.mov`, `.wav`, `.m4a`, `.aac` |
| [`transcribe.sh`](raycast/transcribe.sh) | 🎙️ Transcribe to MD | `.mp3`, `.mp4`, `.mov`, `.mkv`, `.wav`, `.m4a`, `.aac` |
| [`srt-to-md.sh`](raycast/srt-to-md.sh) | 📝 SRT to MD | `.srt` |

<details>
<summary><b>Setup</b></summary>

Either add this repo's `raycast/` directory as a script directory in Raycast (Settings → Extensions → Script Commands), or copy the scripts into your existing script directory. With copies, re-copy a script after changing it, then run Raycast's **Reload Script Directories** if a new command doesn't show up.

The scripts extend `PATH` with `~/.local/bin` (where `uv tool install` puts `tools`) and `/opt/homebrew/bin` (Homebrew's ffmpeg), since Raycast runs scripts with a minimal environment, and set `NO_COLOR=1` so the output stays readable.

</details>

---

## Development

```bash
uv run pytest -q             # tests (end-to-end ones need ffmpeg)
uvx ruff check tools tests   # lint
```

<details>
<summary><b>Adding a new command</b></summary>

See [CLAUDE.md](CLAUDE.md) for the full walkthrough. The short version:

1. Create `tools/<group>/your_tool.py` with a function decorated `@<group>_app.command("your-tool")`.
2. Import your module at the bottom of `tools/<group>/__init__.py`.
3. Add a row to [Commands at a glance](#commands-at-a-glance), an entry in [Contents](#contents), and a section under the relevant group (same layout: one-line summary, usage, then Options / Examples / How it works toggles).
4. Optionally add a Raycast wrapper in `raycast/`.

</details>
