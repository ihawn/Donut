"""Terminal colors sampled approximately from the supplied palette."""
import os
import sys
import time

COLORS = {'orange': (255, 168, 24), 'navy': (18, 77, 99),
          'coral': (255, 94, 78), 'teal': (23, 184, 190), 'gray': (222, 224, 228)}


def paint(text, color='teal'):
    if not sys.stdout.isatty() or 'NO_COLOR' in os.environ:
        return text
    r, g, b = COLORS[color]
    return f'\033[38;2;{r};{g};{b}m{text}\033[0m'


def banner():
    print(paint(' ◉ ', 'orange') + paint('DONUT', 'teal') + paint('  small tickets · fresh contexts', 'gray'))
    print(''.join(paint('━' * 7, color) for color in COLORS))


class Progress:
    def __init__(self):
        self.started = time.monotonic()
        self.last = 0
        self.frame = 0
        self.visible = False

    def clear(self):
        if self.visible:
            self.visible = False
            print('\r\033[2K', end='', flush=True)

    def tick(self):
        elapsed = int(time.monotonic() - self.started)
        if sys.stdout.isatty():
            self.visible = True
            frames = '◐◓◑◒'
            print('\r' + paint(frames[self.frame % 4], 'orange') +
                  f' Working · {elapsed}s elapsed · waiting for agent output', end='', flush=True)
            self.frame += 1
        elif elapsed - self.last >= 15:
            print(f'[Working · {elapsed}s elapsed · waiting for agent output]', flush=True)
            self.last = elapsed
