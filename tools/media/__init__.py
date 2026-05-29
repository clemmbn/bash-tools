"""
tools/media — Typer sub-app grouping all media-processing commands.

Commands:
  video-to-edl  Convert a video to a CMX 3600 EDL using silence detection.
  audio-to-srt  Transcribe audio/video and produce an SRT subtitle file.
  srt-to-md     Convert an SRT subtitle file to a timestamped Markdown transcript.
  transcribe    Transcribe audio/video and print or export a plain-text transcript.

To add a new media tool:
  1. Create tools/media/your_tool.py with a function decorated @media_app.command("your-tool").
  2. Import it at the bottom of this file so the command registers on startup.
"""

import typer

# Define media_app first — submodules imported below reference it.
media_app = typer.Typer(help="Media processing tools.")

# Side-effect imports: each module registers its command on media_app via @media_app.command().
from tools.media import audio_to_srt, srt_to_md, transcribe, video_to_edl  # noqa: F401, E402
