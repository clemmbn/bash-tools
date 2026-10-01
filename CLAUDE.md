# tools — Personal CLI Toolkit

## Project overview

A single `tools` CLI built with [Typer](https://typer.tiangolo.com/) and managed by [uv](https://docs.astral.sh/uv/). Commands are grouped by domain (e.g. `media`). Add or remove individual tools without touching other groups.

## Package layout

```
tools/                   Python package (maps to the `tools` CLI command)
├── main.py              Root Typer app — registers group sub-apps
├── shared/
│   ├── ffmpeg.py        check_ffmpeg(), extract_audio() — shared by media tools
│   ├── log.py           Shared Rich console + header/step/detail/warn/error/summary
│   └── whisper.py       transcribe(), format_transcript() — shared by audio-to-srt
└── media/
    ├── __init__.py      media_app = typer.Typer(); registers sub-commands via imports
    ├── video_to_edl.py  `tools media video-to-edl`
    └── audio_to_srt.py  `tools media audio-to-srt`
```

## Running the CLI

```bash
# During development (no install needed)
uv run tools --help
uv run tools media --help
uv run tools media video-to-edl input.mp4

# Install once for system-wide use
uv pip install -e .
tools --help
```

## Adding a new tool to an existing group

1. Create `tools/<group>/your_tool.py`.
2. Add a function decorated with `@<group>_app.command("your-tool")`.
3. Import your module at the bottom of `tools/<group>/__init__.py` so the command registers.

Example for the `media` group:

```python
# tools/media/your_tool.py
from tools.media import media_app

@media_app.command("your-tool")
def your_tool(input_file: Path) -> None:
    """One-line description shown in `tools media --help`."""
    ...
```

```python
# tools/media/__init__.py  — add at the bottom:
from tools.media import your_tool  # noqa: F401
```

## Adding a new group

1. Create `tools/<group>/` with an `__init__.py` that defines `<group>_app = typer.Typer(...)`.
2. Add tool modules under that package (same pattern as above).
3. Register the group in `tools/main.py`:

```python
from tools.<group> import <group>_app
app.add_typer(<group>_app, name="<group>")
```

## Dependencies

Add project dependencies with:
```bash
uv add <package>
```

## Shared utilities

- **`tools/shared/ffmpeg.py`** — `check_ffmpeg()` and `extract_audio()`. Import these instead of calling ffmpeg inline.
- **`tools/shared/whisper.py`** — `transcribe()` and `format_transcript()`. Import these in any tool that needs Whisper.
- **`tools/shared/log.py`** — all terminal output goes through it; never create a new `Console()`. A command calls `header()` once, wraps each phase in `with step("Do thing") as s:` (spinner + timer, then `✓ Do thing · s.result`; `s.detail()` adds dim lines under it), uses `warn()` / `error()` with an optional `hint=`, and ends with `summary({...})`. Escape user-supplied text (`rich.markup.escape`) before embedding it in a message. Output must stay readable with `NO_COLOR=1` (Raycast), so convey meaning with symbols/words, not color alone.
