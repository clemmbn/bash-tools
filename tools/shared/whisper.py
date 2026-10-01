"""
tools/shared/whisper.py — Shared OpenAI Whisper transcription utilities.

Responsibilities:
  - Load a Whisper model and transcribe a WAV file with word-level timestamps.
  - Format the raw Whisper result into a human-readable transcript string.

Used by: tools.media.audio_to_srt, tools.media.transcribe
Absorbed from: ../transcribe/transcribe.py
"""

import re

from rich.markup import escape

from tools.shared.log import step


def transcribe(wav_path: str, model_name: str) -> dict:
    """Load a Whisper model and transcribe wav_path with word-level timestamps.

    Args:
        wav_path:   Path to a 16 kHz mono WAV file.
        model_name: Whisper model size — one of tiny/base/small/medium/large/turbo.

    Returns:
        Whisper result dict with "segments" (each containing "words") and "text".
        Returns {} if no speech was detected.

    Side effects:
        Prints "Load Whisper model" and "Transcribe" step lines, each with a live
        elapsed-time spinner while running.
    """
    # Deferred import — avoids paying the PyTorch/Whisper startup cost on every
    # CLI invocation. Only loaded when transcription is actually requested.
    import whisper

    with step(f"Load Whisper model ({model_name})"):
        model = whisper.load_model(model_name)

    # step() shows a live elapsed timer while this runs, which matters here:
    # transcription is the slowest part of every Whisper command (minutes).
    with step(f"Transcribe with Whisper ({model_name})") as s:
        result = model.transcribe(
            wav_path,
            word_timestamps=True,
            temperature=0.1,
            condition_on_previous_text=False,
            fp16=False,
        )

        all_words = [
            word
            for seg in result.get("segments", [])
            for word in seg.get("words", [])
        ]

        if not all_words:
            s.result = "no speech detected"
            return {}

        # Count sentences: words ending in punctuation, plus a trailing unpunctuated tail.
        PUNCT = set(".,;?!")
        count = sum(1 for w in all_words if w["word"].rstrip() and w["word"].rstrip()[-1] in PUNCT)
        if not all_words[-1]["word"].rstrip() or all_words[-1]["word"].rstrip()[-1] not in PUNCT:
            count += 1

        s.result = f"{count} segment(s), {len(all_words)} word(s)"
        if result.get("language"):
            s.detail(f"detected language: {result['language']}")

    return result


def format_transcript(result: dict, plain: bool = False) -> str:
    """Format a Whisper result dict into one sentence per line with MM:SS timestamps.

    Args:
        result: Whisper result dict as returned by transcribe().
        plain:  If True, emit plain text timestamps; if False, use Rich markup.

    Returns:
        A string with one sentence per line, prefixed with a MM:SS timestamp.
    """
    if not result:
        return ""
    all_words = [
        word
        for seg in result.get("segments", [])
        for word in seg.get("words", [])
    ]
    if not all_words:
        return result.get("text", "").strip()

    SENT_END = set(".?!")
    lines = []
    sentence_words: list[str] = []
    sentence_start: float | None = None

    for w in all_words:
        text = w["word"]
        if sentence_start is None:
            sentence_start = w["start"]
        sentence_words.append(text.strip())
        if text.rstrip() and text.rstrip()[-1] in SENT_END:
            m, s = divmod(int(sentence_start), 60)
            ts = f"{m:02d}:{s:02d}"
            prefix = ts if plain else f"[dim]{ts}[/dim]"
            lines.append(f"{prefix}  {_render(_fix_spacing(' '.join(sentence_words)), plain)}")
            sentence_words = []
            sentence_start = None

    if sentence_words:
        m, s = divmod(int(sentence_start), 60)  # type: ignore[arg-type]
        ts = f"{m:02d}:{s:02d}"
        prefix = ts if plain else f"[dim]{ts}[/dim]"
        lines.append(f"{prefix}  {_render(_fix_spacing(' '.join(sentence_words)), plain)}")

    return "\n".join(lines)


def _render(text: str, plain: bool) -> str:
    """Escape transcript text for Rich output; return it untouched for plain text.

    Args:
        text:  Sentence text from Whisper.
        plain: True when the output is written to a file (no markup involved).

    Returns:
        Text safe to embed in the requested output mode. Without escaping, text
        like "[music]" would be parsed as a Rich tag and silently disappear.
    """
    return text if plain else escape(text)


def _fix_spacing(text: str) -> str:
    """Remove spurious spaces before apostrophes in contractions (e.g. 'c 'est' → 'c'est')."""
    return re.sub(r"(\w) (['''])(\w)", r"\1\2\3", text)
