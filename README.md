# tools

A personal CLI toolkit built with [Typer](https://typer.tiangolo.com/) and managed by [uv](https://docs.astral.sh/uv/). Commands are organised into groups (e.g. `media`). Each group can be extended independently without touching others.

---

## Requirements

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/) — for running and installing
- [ffmpeg](https://ffmpeg.org/) — required by all `media` commands (`brew install ffmpeg` on macOS)

---

## Installation

```bash
# Clone the repo
git clone https://github.com/<your-username>/bash-tools.git
cd bash-tools

# Run without installing (development)
uv run tools --help

# Install once for system-wide use
uv tool install .
tools --help
```

---

## Command reference

### `tools media`

Media processing tools — all require ffmpeg on your `PATH`.

---

#### `tools media video-to-edl`

Converts a video file to a **CMX 3600 EDL** (Edit Decision List) by detecting speech intervals via ffmpeg silence detection. The output `.edl` file is written next to the input file and can be imported into most non-linear editors (Premiere Pro, DaVinci Resolve, Final Cut Pro).

**Pipeline:**
1. Extract audio to a temporary 16 kHz mono WAV.
2. Detect silence intervals with `ffmpeg silencedetect`.
3. Invert silence → speech intervals; drop intervals shorter than 5 frames.
4. Pad each interval and build CMX 3600 EDL entries.
5. Write `<input>.edl`; clean up the temp WAV.

**Usage:**
```bash
tools media video-to-edl <input_video> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `INPUT_VIDEO` | Path to the input video file (`.mp4` or `.mov`). |

**Options:**

| Option | Default | Description |
|---|---|---|
| `--fps` | `30` | Frame rate used to compute timecodes in the EDL. |
| `--padding` | `0.05` | Seconds of padding added before and after each speech interval. |
| `--silence-threshold` | `-25` | Silence detection threshold in dB. Higher (e.g. `-20`) detects more silence; lower (e.g. `-35`) is more conservative. |
| `--silence-duration` | `0.2` | Minimum duration in seconds for a gap to be treated as silence. |

**Examples:**
```bash
# Basic usage — defaults suit most talking-head footage
tools media video-to-edl interview.mp4

# Quieter recording — lower threshold and tighter padding
tools media video-to-edl podcast.mov --silence-threshold -35 --padding 0.1

# 24 fps project
tools media video-to-edl footage.mp4 --fps 24
```

**Output:** `<input>.edl` in the same directory as the input file.

---

#### `tools media audio-to-srt`

Transcribes an audio or video file locally using [OpenAI Whisper](https://github.com/openai/whisper) and produces an **SRT subtitle file**. Transcription runs entirely on your machine — no API key or internet connection required.

**Pipeline:**
1. Convert input to a 16 kHz mono WAV (skipped if already `.wav`).
2. Transcribe with local Whisper using word-level timestamps.
3. Group words into caption blocks, breaking on silence gaps and sentence-ending punctuation.
4. Fill sub-threshold gaps between consecutive blocks by snapping both boundaries to their midpoint.
5. Write `<input>.srt`.

**Usage:**
```bash
tools media audio-to-srt <input_file> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `INPUT_FILE` | Path to any audio or video file (MP3, MP4, MOV, WAV, M4A, AAC). |

**Options:**

| Option | Default | Description |
|---|---|---|
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

**Examples:**
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

**Output:** `<input>.srt` in the same directory as the input file.

---

#### `tools media transcribe`

Transcribes an audio or video file locally using Whisper and prints a timestamped transcript to the terminal, or exports it as plain text, Markdown, or raw JSON.

**Pipeline:**
1. Validate ffmpeg is on `PATH`.
2. Convert input to a 16 kHz mono WAV (skipped if already `.wav`).
3. Transcribe with local Whisper using word-level timestamps.
4. Print to the terminal, or write `.txt` / `.md` / `.json` depending on `--output-format`.

**Usage:**
```bash
tools media transcribe <input_file> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|---|---|
| `INPUT_FILE` | Path to any audio or video file (MP3, MP4, MOV, WAV, M4A, AAC, MKV). |

**Options:**

| Option | Default | Description |
|---|---|---|
| `--model` | `turbo` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large`, `turbo`. |
| `--output-format` | _(none)_ | `txt` (plain text), `md` (Markdown with `[MM:SS]` timestamps), or `raw` (full Whisper result as JSON). Omit to print to the terminal. |

**Examples:**
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

**Output:** Printed to the terminal, or `<input>.txt` / `<input>.md` / `<input>.json` depending on `--output-format`.

---

#### `tools media srt-to-md`

Converts an SRT subtitle file into a timestamped Markdown transcript. Merges word-level or fragment-level SRT blocks into full sentences, stamping each with the start time of its first contributing block.

**Pipeline:**
1. Parse SRT blocks (strips HTML-like tags such as `<b>`).
2. Merge consecutive blocks into sentences, ending on `.`, `!`, or `?`.
3. Write one sentence per line, prefixed with its start timestamp.
4. Write `<input>.md`.

**Usage:**
```bash
tools media srt-to-md <input_file>
```

**Arguments:**

| Argument | Description |
|---|---|
| `INPUT_FILE` | Path to an existing `.srt` file. |

**Example:**
```bash
tools media srt-to-md interview.srt
```

**Output format** (one sentence per line):
```
[00:00] Hey, my name is Clement!
[00:05] I'm 22 and I love tennis.
```

**Output:** `<input>.md` in the same directory as the input file.

---

## Raycast integration

[`raycast/srt-to-md.sh`](raycast/srt-to-md.sh) wraps `tools media srt-to-md` as a Raycast script command. Select a `.srt` file in Finder, run the "SRT to MD" command from Raycast, and it converts the selected file in place.

To use it, add the `raycast/` directory as a script directory in Raycast's preferences (Extensions → Script Commands). The script extends `PATH` to include `~/.local/bin` (where `uv tool install` places binaries) and `/opt/homebrew/bin` (Homebrew's ffmpeg), since Raycast runs scripts with a minimal environment.

---

## Adding a new command

See [CLAUDE.md](CLAUDE.md) for the full walkthrough. The short version:

1. Create `tools/<group>/your_tool.py` with a function decorated `@<group>_app.command("your-tool")`.
2. Import your module at the bottom of `tools/<group>/__init__.py`.
3. Update this README with a new section under the relevant group.
