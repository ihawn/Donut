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
         _.-~~~~~~~~~~~~-._               .-------------------.
     .-~'     '   .   ,    '~-.           |   do the thing.   |
   .'   .   ,   *   .   '   o  '.         '---.  .------------'
  /   ,   .-~~~~~~~~~~~~-.    *  \            | /
 ;  *    /                \       ;           |/
 |    .  |                |   '   |
 ;  '    \                /       ;                   .-~~-.
  \   o   '-.__________.-'    *  /                   ( .  ' )
   '.   *   '   .   ,   *   .  .'                     '-..-'
     '-._     '   .   ,    _.-'
         '~-.__________.-~'
"""[1:]

# Columns where the speech bubble starts; everything to its left is the donut.
_BUBBLE = 38
_SPRINKLE_COLORS = ("coral", "teal", "coral", "gray", "coral")
_SPRINKLES = set("*.,'o")


def logo_segments():
    """The logo as lines of (text, color) runs, shared by the ANSI printer and the Textual terminal."""
    lines = []
    for row, line in enumerate(LOGO.splitlines()):
        runs, sprinkle = [], row
        tagline = line.find("do the thing.")
        for col, char in enumerate(line):
            if char == " ":
                color = None
            elif col >= _BUBBLE:
                is_text = tagline != -1 and tagline <= col < tagline + len("do the thing.")
                inside_hole = line.rstrip().endswith(")") and col > line.index("(") and col < line.rindex(")")
                if is_text:
                    color = "orange"
                elif inside_hole and char in _SPRINKLES:
                    color = "coral"
                elif col >= _BUBBLE + 14 and row >= 6:
                    color = "orange"
                else:
                    color = "gray"
            else:
                neighbors = line[max(col - 1, 0) : col + 2]
                if char in _SPRINKLES and neighbors.strip() == char:
                    color = _SPRINKLE_COLORS[sprinkle % len(_SPRINKLE_COLORS)]
                    sprinkle += 1
                else:
                    color = "orange"
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
    """Highlight Markdown code a line at a time, including split stream deltas."""

    def __init__(self):
        self.pending = ""
        self.fence = None
        self.error = False

    def feed(self, text, final=False):
        import re

        self.pending += text
        output = []
        while "\n" in self.pending or (final and self.pending) or len(self.pending) >= 4096:
            end = self.pending.find("\n") + 1
            if not end:
                end = min(len(self.pending), 4096)
            line, self.pending = self.pending[:end], self.pending[end:]
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if not line.strip() or re.match(r"^\[.*\]\s*$", line):
                self.error = False
            if re.match(r"^\s*(?:\[Error\b|Error:|ERROR\b|Traceback \()", line):
                self.error = True
            if self.error:
                output.append(paint(line, "coral"))
            elif marker:
                delimiter = marker[1]
                if self.fence is None:
                    self.fence = delimiter
                elif delimiter[0] == self.fence[0] and len(delimiter) >= len(self.fence):
                    self.fence = None
                output.append(paint(line, "teal"))
            elif self.fence:
                # Lightweight language-neutral tokens; no lexer dependency.
                tokens = re.split(
                    r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b\d+(?:\.\d+)?\b|\b(?:class|def|return|if|else|for|while|import|from|const|let|var|function|public|private|void|new|true|false|null|None|await|async)\b|//[^\n]*|#[^\n]*)',
                    line,
                )
                for index, token in enumerate(tokens):
                    color = "gray"
                    if index % 2:
                        color = (
                            "teal"
                            if token.startswith(('"', "'"))
                            else "orange"
                            if token[:1].isdigit()
                            else "teal"
                            if token.startswith(("//", "#"))
                            else "coral"
                        )
                    output.append(paint(token, color))
            else:
                parts = re.split(r"(`[^`\n]+`|\([^()\n]*\)|\{[^{}\n]*\}|[(){}]|(?<!\w)-?\d+(?:\.\d+)?\b)", line)
                for index, part in enumerate(parts):
                    color = "teal" if part.startswith("[") else "gray"
                    if index % 2:
                        color = "orange" if part.startswith("`") else "teal" if part[:1] in "(){}" else "coral"
                    output.append(paint(part, color))
        return "".join(output)
