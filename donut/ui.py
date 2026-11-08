"""Terminal colors sampled approximately from the supplied palette."""

import os
import sys
import time
from importlib.metadata import PackageNotFoundError, version

COLORS = {
    "orange": (255, 168, 24),
    "navy": (18, 77, 99),
    "coral": (255, 94, 78),
    "teal": (23, 184, 190),
    "gray": (222, 224, 228),
}


LOGO = r"""
            _.------------._              .-----------------.
        .-''   *    +    o  ''-.          |  do the thing.  |
      .'   +    o    *    +     '.        '--.  .-----------'
     /   o   .-''''''''''-.   *   \         /  /
    |  *    /              \   +   |       / .'
    |   +  |                |  o   |      /.'
    |  o    \              /   *   |
     \   *   '-.________.-'   +   /                 .-''-.
      '.    +    o    *    o    .'                 ( '  . )
        '-._   *    +   o   _.-'                    '-..-'
            '-.__________.-'
"""[1:]

# Everything left of this column is the donut; to the right are the speech bubble and the donut hole.
_BUBBLE = 36
_HOLE_ROW = 7
_SPRINKLE_COLORS = ("coral", "teal", "gray", "coral", "teal")
_SPRINKLES = set("*+o")
_TAGLINE = "do the thing."


def logo_segments():
    """The logo as lines of (text, color) runs, shared by the ANSI printer and the Textual terminal.

    The outline avoids characters such as ~ whose shape varies between terminal fonts, and sprinkles use
    characters the outline never does, so the edge stays solid in any monospace font.
    """
    lines = []
    sprinkle = 0
    for row, line in enumerate(LOGO.splitlines()):
        runs = []
        tagline = line.find(_TAGLINE)
        for col, char in enumerate(line):
            if char == " ":
                color = None
            elif col < _BUBBLE:
                if char in _SPRINKLES:
                    color = _SPRINKLE_COLORS[sprinkle % len(_SPRINKLE_COLORS)]
                    sprinkle += 1
                else:
                    color = "orange"
            elif tagline != -1 and tagline <= col < tagline + len(_TAGLINE):
                color = "orange"
            elif row >= _HOLE_ROW:
                inside = "(" in line and line.index("(") < col < line.index(")")
                color = "coral" if inside else "orange"
            else:
                color = "gray"
            if runs and runs[-1][1] == color:
                runs[-1] = (runs[-1][0] + char, color)
            else:
                runs.append((char, color))
        lines.append(runs)
    return lines


def painted_logo():
    return "\n".join("".join(paint(text, color) if color else text for text, color in runs) for runs in logo_segments())


def paint(text, color="teal"):
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ:
        return text
    r, g, b = COLORS[color]
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def banner():
    print(
        paint(" ◉ ", "coral")
        + paint("DONUT", "orange")
        + paint(
            "  Your planning model writes the tickets. Your worker model takes them one at a time. Do the thing.",
            "gray",
        )
    )
    print("".join(paint("━" * 7, color) for color in COLORS))


def release_version():
    try:
        release = version("donut-cli")
    except PackageNotFoundError:
        release = "dev"
    return release


def welcome(installed=False):
    release = release_version()
    print(painted_logo())
    print(paint("  DONUT", "orange") + paint("  /  " + release, "coral"))
    print(paint("  Your planning model writes the tickets. Your worker model takes them one at a time.", "gray"))
    print(paint("  " + "-" * 38, "coral"))
    if installed:
        print(paint("  [OK] Installation complete.", "orange"))
        print("  Open a new terminal in your project, then:")
        print(paint("    donut setup", "coral"))
        print("  Explore commands: donut --help")
    else:
        print(paint("  SETUP  /  Local or hosted. Your choice.", "orange"))
    print()


class Progress:
    def __init__(self):
        self.started = time.monotonic()
        self.last = 0
        self.frame = 0
        self.visible = False

    def clear(self):
        if self.visible:
            self.visible = False
            print("\r\033[2K", end="", flush=True)

    def tick(self, label="Working"):
        elapsed = int(time.monotonic() - self.started)
        if sys.stdout.isatty():
            self.visible = True
            frames = "◐◓◑◒"
            print(
                "\r"
                + paint(frames[self.frame % 4], "orange")
                + f" {label} · {elapsed}s elapsed · waiting for agent output",
                end="",
                flush=True,
            )
            self.frame += 1
        elif elapsed - self.last >= 15:
            print(f"[{label} · {elapsed}s elapsed · waiting for agent output]", flush=True)
            self.last = elapsed


class CodeStream:
    """Highlight streamed agent output a line at a time, including lines split across stream deltas."""

    def __init__(self):
        from .highlight import AnsiRenderer, Highlighter

        self.pending = ""
        self.highlighter = Highlighter()
        self.renderer = AnsiRenderer()

    @property
    def fence(self):
        return self.highlighter.fence

    def feed(self, text, final=False):
        colored = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.pending += text
        output = []
        while "\n" in self.pending or (final and self.pending) or len(self.pending) >= 4096:
            end = self.pending.find("\n") + 1
            if not end:
                end = min(len(self.pending), 4096)
            line, self.pending = self.pending[:end], self.pending[end:]
            body, newline = (line[:-1], "\n") if line.endswith("\n") else (line, "")
            styled = self.highlighter.line(body)
            output.append((self.renderer.render(styled) if colored else body) + newline)
        return "".join(output)
