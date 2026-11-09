"""Line-by-line styling for agent output, shared by the CLI stream and the interactive terminal.

Each call styles one complete line and returns a Rich ``Text``. State carries across lines so fenced code
blocks, thinking sections and error bodies keep their look until they end.
"""

import io
import re

from rich.console import Console
from rich.text import Text

from .ui import COLORS


def _hex(name):
    return "#%02x%02x%02x" % COLORS[name]


ORANGE, CORAL, TEAL, GRAY = (_hex(name) for name in ("orange", "coral", "teal", "gray"))
DIM = "#8a96a3"

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)")
_LABEL = re.compile(r"^\s*\[[^\]\n]*\]\s*$")
_ERROR_START = re.compile(r"^\s*(?:\[Error\b|Error:|ERROR\b|Traceback \(|FAILED\b)")
_HEADING = re.compile(r"^\s*(?:#{1,6}\s+.*|[A-Z][A-Z0-9 /·-]{5,}[A-Z0-9]\b.*|Run summary.*)$")
_KEY_VALUE = re.compile(
    r"^(\s*)(Project|Model|Planner|Worker|Reviewer|Review|Recovery|Repair|VERDICT|RECOVERY|Run logs|Logs and results|Logs|Verifying|Queue|Checkpoints|Checkpoint):(\s)"
)
_TICKET_START = re.compile(r"^(\s*)(\[(?:review )?\d+/(?:\d+|all)\])(\s+)(\S+)(.*)$")
_BULLET = re.compile(r"^(\s*)([-*•]|\d+[.)])(\s+)")

_GOOD = r"REQUEUED|[Rr]equeued|APPROVED|[Aa]pproved|Done|PASS(?:ED)?|[Pp]assed|OK|[Ss]uccess(?:ful(?:ly)?)?|[Cc]omplete(?:d)?|[Aa]llowed|✓"
_BAD = r"Blocked|BLOCKED|FAIL(?:ED|URE)?|[Ff]ailed|[Ff]ailure|Error|[Dd]enied|[Ss]topping|[Mm]issing|✗"
_WARN = (
    r"NEEDS HUMAN|CHANGES REQUESTED|[Cc]hanges requested|WARN(?:ING)?|[Ww]arning|[Ss]kipped|Human check|[Qq]ueued|TODO"
)
_PATH = r"(?:~|\.{1,2})?/?(?:[\w.-]+/)+[\w.-]+|\b[\w-]+(?:\.[\w-]+)*\.(?:md|py|cs|js|ts|tsx|jsx|json|ya?ml|toml|txt|sh|go|rs|java|kt|rb|php|c|h|cpp|hpp|unity|prefab|asset)\b"
_TOKENS = re.compile(
    rf"(?P<code>`[^`\n]+`)"
    rf"|(?P<bracket>\[[^\[\]\n]+\])"
    rf"|(?P<paren>\([^()\n]*\)|\{{[^{{}}\n]*\}}|[(){{}}])"
    rf"|(?P<path>{_PATH})"
    rf"|(?P<good>\b(?:{_GOOD})\b|✓)"
    rf"|(?P<bad>\b(?:{_BAD})\b|✗)"
    rf"|(?P<warn>\b(?:{_WARN})\b)"
    rf"|(?P<number>(?<![\w.])-?\d+(?:[.,:/]\d+)*(?:%|ms|s|m|h|x)?(?![\w]))"
)
_TOKEN_STYLES = {
    "code": ORANGE,
    "bracket": TEAL,
    "paren": TEAL,
    "path": f"underline {TEAL}",
    "good": f"bold {TEAL}",
    "bad": f"bold {CORAL}",
    "warn": f"bold {ORANGE}",
    "number": CORAL,
}

# Fallback for unlabeled code blocks: a few language-neutral tokens.
_CODE_TOKENS = re.compile(
    r"(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?P<comment>//.*|#.*)"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<keyword>\b(?:class|def|return|if|elif|else|for|while|import|from|const|let|var|function|public|"
    r"private|protected|static|void|new|true|false|null|None|True|False|await|async|using|namespace|struct)\b)"
)
_CODE_STYLES = {"string": TEAL, "comment": f"italic {DIM}", "number": ORANGE, "keyword": CORAL}


def _token_style(token_type):
    """Map a Pygments token type to the Donut palette."""
    from pygments import token as t

    for kind, style in (
        (t.Comment, f"italic {DIM}"),
        (t.Keyword, CORAL),
        (t.Name.Builtin, CORAL),
        (t.Literal.String, TEAL),
        (t.Literal.Number, ORANGE),
        (t.Name.Function, f"bold {ORANGE}"),
        (t.Name.Class, f"bold {ORANGE}"),
        (t.Name.Decorator, ORANGE),
        (t.Name.Tag, CORAL),
        (t.Name.Attribute, ORANGE),
        (t.Generic.Inserted, TEAL),
        (t.Generic.Deleted, CORAL),
        (t.Operator, GRAY),
    ):
        if token_type in kind:
            return style
    return GRAY


def _lexer(language):
    if not language:
        return None
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        return get_lexer_by_name(language.lower(), stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


class Highlighter:
    def __init__(self):
        self.fence = None
        self.lexer = None
        self.error = False
        self.thinking = False

    def line(self, line):
        """Style one line (without its newline) and return a Rich Text."""
        fence = _FENCE.match(line)
        if fence and (self.fence is None or (fence[1][0] == self.fence[0] and len(fence[1]) >= len(self.fence))):
            if self.fence is None:
                self.fence, self.lexer = fence[1], _lexer(fence[2])
            else:
                self.fence, self.lexer = None, None
            return Text(line, style=f"{DIM}")
        if self.fence is not None:
            return self.code(line)
        if not line.strip() or _LABEL.match(line):
            self.error = False
        if _ERROR_START.match(line):
            self.error = True
        if self.error:
            return Text(line, style=CORAL)
        if _LABEL.match(line):
            return self.label(line)
        if self.thinking:
            return Text(line, style=f"italic {DIM}")
        return self.prose(line)

    def label(self, line):
        """Section labels the runner prints, such as [model], [Tool: read_file] or [model · Thinking]."""
        inner = line.strip()[1:-1]
        self.thinking = "Thinking" in inner
        if inner.startswith("Error") or "Error ·" in inner:
            return Text(line, style=f"bold {CORAL}")
        if inner.startswith("Tool result"):
            return Text(line, style=f"bold {DIM}")
        if inner.startswith("Tool"):
            text = Text(line, style=f"bold {TEAL}")
            text.stylize(f"bold {ORANGE}", line.index(":") + 1 if ":" in line else 0, len(line))
            return text
        if self.thinking:
            return Text(line, style=f"italic {DIM}")
        return Text(line, style=f"bold {ORANGE}")

    def code(self, line):
        text = Text()
        if self.lexer is not None:
            # Lexers match line comments up to a newline, so lex with one and drop it afterwards.
            tokens = list(self.lexer.get_tokens(line + "\n"))
            if "".join(value for _, value in tokens) == line + "\n":
                for token_type, value in tokens:
                    text.append(value, style=_token_style(token_type))
                text.right_crop(1)
                return text
            # A lexer that changed the text (for example by normalizing whitespace) falls back below.
        position = 0
        for match in _CODE_TOKENS.finditer(line):
            text.append(line[position : match.start()], style=GRAY)
            text.append(match.group(), style=_CODE_STYLES[match.lastgroup])
            position = match.end()
        text.append(line[position:], style=GRAY)
        return text

    def prose(self, line):
        ticket = _TICKET_START.match(line)
        if ticket:
            text = Text(ticket[1])
            text.append(ticket[2], style=f"bold {ORANGE}")
            text.append(ticket[3])
            text.append(ticket[4], style=f"bold {TEAL}")
            text.append_text(self.tokens(ticket[5], DIM))
            return text
        if _HEADING.match(line) and not line.lstrip().startswith("|"):
            return Text(line, style=f"bold {ORANGE}")
        if line.lstrip().startswith("|"):
            return self.table_row(line)
        key = _KEY_VALUE.match(line)
        if key:
            text = Text(key[1])
            text.append(key[2] + ":", style=DIM)
            text.append(key[3])
            text.append_text(self.tokens(line[key.end() :]))
            return text
        bullet = _BULLET.match(line)
        if bullet:
            text = Text(bullet[1])
            text.append(bullet[2], style=f"bold {ORANGE}")
            text.append(bullet[3])
            text.append_text(self.tokens(line[bullet.end() :]))
            return text
        return self.tokens(line)

    def table_row(self, line):
        """Markdown table rows: dim pipes and separators, bold header cells, highlighted values."""
        if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line):
            return Text(line, style=DIM)
        text = Text()
        for index, cell in enumerate(re.split(r"(\|)", line)):
            if cell == "|":
                text.append(cell, style=DIM)
            elif cell.strip() in ("Task", "Result", "Status", "Note", "What changed", "Tests", "Human check"):
                text.append(cell, style=f"bold {GRAY}")
            else:
                text.append_text(self.tokens(cell))
        return text

    def tokens(self, line, base=GRAY):
        text = Text()
        position = 0
        for match in _TOKENS.finditer(line):
            text.append(line[position : match.start()], style=base)
            text.append(match.group(), style=_TOKEN_STYLES[match.lastgroup])
            position = match.end()
        text.append(line[position:], style=base)
        return text


class AnsiRenderer:
    """Turn styled Text into ANSI escape codes for a plain terminal stream."""

    def __init__(self):
        self.buffer = io.StringIO()
        self.console = Console(
            file=self.buffer,
            force_terminal=True,
            color_system="truecolor",
            width=1_000_000,
            highlight=False,
            emoji=False,
            markup=False,
            legacy_windows=False,
            no_color=False,
        )

    def render(self, text):
        self.buffer.seek(0)
        self.buffer.truncate()
        self.console.print(text, end="", soft_wrap=True)
        return self.buffer.getvalue()
