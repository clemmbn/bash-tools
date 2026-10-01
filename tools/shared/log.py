"""
tools/shared/log.py — Shared Rich logging helpers for every `tools` command.

Responsibilities:
  - Own the single Console all commands print to, so styling is consistent.
  - Provide a small vocabulary: header(), step(), detail(), warn(), error(), summary().

Typical output:
  ───────────── video-to-edl · IMG_4038.mov ─────────────
  ✓ Extract audio (1.2s)
  ✓ Measure audio levels · noise -50.7 dB, voice -19.6 dB (0.8s)
  ✓ Detect silences · 132 found (0.9s)
    filter: silencedetect=noise=-25.8dB:duration=0.2
  ──────────────────────── Done ────────────────────────
    Clips  79
    EDL    /path/to/IMG_4038.edl

Non-obvious constraints:
  - Raycast runs commands with NO_COLOR=1 and no TTY. Every message therefore
    carries meaning through symbols and words (✓ ✗ ⚠), never color alone. Rich
    suppresses the live spinner on non-terminals, so only the final ✓ lines show.
  - User-supplied text (file names, transcript text) can contain "[...]", which
    Rich would parse as markup. Helpers escape the plain-data arguments they
    receive; callers must escape() any such text they embed in a message.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

# highlight=False: Rich's auto-highlighter colors random numbers and paths, which
# competes with the explicit styling below and makes the output noisier.
console = Console(highlight=False)


def format_duration(seconds: float) -> str:
    """Format an elapsed time compactly: "0.8s", "12.3s", "2m05s".

    Args:
        seconds: Elapsed time in seconds.

    Returns:
        Human-readable duration string.
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _line(message: str) -> None:
    """Print one log line without Rich's hard wrapping.

    Args:
        message: Rich-markup text.

    Trade-off: soft_wrap leaves wrapping to the terminal instead of inserting
    newlines, so long paths stay intact when copy-pasted; the cost is that wrapped
    continuation lines aren't indented.
    """
    console.print(message, soft_wrap=True)


def header(command: str, subject: str) -> None:
    """Print the opening rule of a command run, e.g. "video-to-edl · clip.mov".

    Args:
        command: CLI command name.
        subject: What it runs on, usually the input file name (escaped here).
    """
    console.print()
    console.rule(f"[bold]{command}[/bold] [dim]·[/dim] {escape(subject)}")


@dataclass
class Step:
    """Handle yielded by step() so the body can report what it found.

    Attributes:
        result:  Short outcome shown on the ✓ line after a "·" (e.g. "132 found").
        details: Extra lines printed, dimmed and indented, under the ✓ line.
    """

    result: str = ""
    details: list[str] = field(default_factory=list)

    def detail(self, message: str) -> None:
        """Queue a dimmed detail line to print under this step's ✓ line.

        Args:
            message: Rich-markup text (escape user data first).
        """
        self.details.append(message)


@contextmanager
def step(title: str) -> Iterator[Step]:
    """Run a block as a named step: live spinner + timer, then a ✓ / ✗ line.

    Args:
        title: Imperative step name, e.g. "Extract audio".

    Yields:
        A Step whose .result / .detail() feed the final ✓ line.

    Side effects:
        Shows a transient spinner while the block runs (terminals only), then
        prints "✓ title · result (elapsed)" plus any details. If the block raises
        an Exception, prints "✗ title (elapsed)" and re-raises it.

    Trade-off: details are buffered and printed after the ✓ line rather than as
    they happen, so they read top-to-bottom under the step they belong to instead
    of appearing above it while the spinner is still live.
    """
    handle = Step()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}[/cyan] …"),
        TimeElapsedColumn(),
        console=console,
        transient=True,  # The spinner line disappears and is replaced by ✓ / ✗.
    )
    start = time.monotonic()

    try:
        # The spinner only makes sense on a real terminal. Elsewhere (Raycast, pipes)
        # a transient Progress still emits an empty line on exit, so skip it.
        if console.is_terminal:
            with progress:
                progress.add_task(title, total=None)
                yield handle
        else:
            yield handle
    except Exception:
        _line(f"[bold red]✗[/bold red] {title} [dim]({format_duration(time.monotonic() - start)})[/dim]")
        raise

    outcome = f" [dim]·[/dim] {handle.result}" if handle.result else ""
    _line(f"[green]✓[/green] {title}{outcome} [dim]({format_duration(time.monotonic() - start)})[/dim]")
    for line in handle.details:
        _line(f"  [dim]{line}[/dim]")


def detail(message: str) -> None:
    """Print a standalone dimmed, indented detail line (outside of a step).

    Args:
        message: Rich-markup text (escape user data first).
    """
    _line(f"  [dim]{message}[/dim]")


def warn(message: str, hint: str = "") -> None:
    """Print a warning, optionally followed by a dimmed hint on how to fix it.

    Args:
        message: What went wrong (Rich markup; escape user data first).
        hint:    Optional suggestion, e.g. which option to try.
    """
    _line(f"[bold yellow]⚠ Warning:[/bold yellow] {message}")
    if hint:
        _line(f"  [dim]{hint}[/dim]")


def error(message: str, hint: str = "") -> None:
    """Print an error, optionally followed by a dimmed hint on how to fix it.

    Args:
        message: What went wrong (Rich markup; escape user data first).
        hint:    Optional suggestion, e.g. a command to install a dependency.
    """
    _line(f"[bold red]✗ Error:[/bold red] {message}")
    if hint:
        _line(f"  [dim]{hint}[/dim]")


def summary(rows: dict[str, object], title: str = "Done") -> None:
    """Print the closing rule and an aligned key/value table of results.

    Args:
        rows:  Label → value pairs, e.g. {"Clips": 79, "EDL": path}. Values are
               converted with str() and escaped, so paths print verbatim.
        title: Text in the closing rule.
    """
    console.rule(f"[bold green]{title}[/bold green]")
    # Plain aligned lines rather than a rich Table: table cells hard-wrap long
    # paths mid-string, which breaks copy-pasting them; _line() never does.
    width = max(len(label) for label in rows)
    for label, value in rows.items():
        _line(f"  [dim]{label.ljust(width)}[/dim]  [bold]{escape(str(value))}[/bold]")
    console.print()
