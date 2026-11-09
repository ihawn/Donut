"""Small OS-specific locking and process-lifecycle operations."""

import os
import signal
import subprocess


def acquire_lock(path):
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as error:
        handle.close()
        raise BlockingIOError("another Donut batch is already running for this project") from error


def process_options():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def window_command(project):
    """The command a new terminal window runs: this Donut, in project."""
    import sys

    return [sys.executable, "-m", "donut", "-p", str(project)]


def open_window(project, env=None, which=None, system=None):
    """Open Donut for project in a new terminal window. Returns where it opened, or None when no way was found.

    Inside tmux it is a new tmux window; on macOS a Terminal window (iTerm when Donut runs in iTerm); on Windows a
    new console (Windows Terminal when installed); on Linux $TERMINAL or the first terminal emulator found.
    """
    import shlex
    import shutil
    import sys

    env = os.environ if env is None else env
    which = which or shutil.which
    system = system or sys.platform
    command = window_command(project)
    if env.get("TMUX") and which("tmux"):
        subprocess.Popen(["tmux", "new-window", "-c", str(project), *command])
        return "a new tmux window"
    if system == "darwin":
        line = "cd " + shlex.quote(str(project)) + " && " + shlex.join(command)
        quoted = '"' + line.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if env.get("TERM_PROGRAM") == "iTerm.app":
            script = 'tell application "iTerm" to create window with default profile command ' + quoted
            subprocess.Popen(["osascript", "-e", script])
            return "a new iTerm window"
        subprocess.Popen(
            [
                "osascript",
                "-e",
                'tell application "Terminal" to do script ' + quoted,
                "-e",
                'tell application "Terminal" to activate',
            ]
        )
        return "a new Terminal window"
    if system == "win32":
        if which("wt"):
            subprocess.Popen(["wt", "-d", str(project), *command])
            return "a new Windows Terminal window"
        subprocess.Popen(command, cwd=project, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return "a new console window"
    candidates = [env["TERMINAL"]] if env.get("TERMINAL") else []
    candidates += ["x-terminal-emulator", "gnome-terminal", "konsole", "kitty", "alacritty", "wezterm", "xterm"]
    for name in candidates:
        if not which(name):
            continue
        base = os.path.basename(name)
        if base == "gnome-terminal":
            args = [name, "--working-directory", str(project), "--", *command]
        elif base == "wezterm":
            args = [name, "start", "--cwd", str(project), "--", *command]
        elif base == "kitty":
            args = [name, "--directory", str(project), *command]
        else:
            args = [name, "-e", *command]
        subprocess.Popen(args, cwd=project)
        return f"a new {base} window"
    return None


def copy_native(text, system=None, which=None):
    """Put text on this computer's clipboard with its own tool. Returns whether one ran.

    Terminals copy for an app only when they support OSC 52 (macOS Terminal does not), so Donut also uses
    pbcopy, clip, wl-copy, xclip or xsel when present.
    """
    import shutil
    import sys

    system = system or sys.platform
    which = which or shutil.which
    if system == "darwin":
        commands = [["pbcopy"]]
    elif system == "win32":
        commands = [["clip"]]
    else:
        commands = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for command in commands:
        if not which(command[0]):
            continue
        data = text.encode("utf-16-le") if command[0] == "clip" else text.encode("utf-8")
        try:
            subprocess.run(command, input=data, timeout=3, check=True, capture_output=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
