"""
Terminal management library for low-level terminal control and input handling.
"""

# Testing: pytest terminal.py
# Linting: pylint --max-line-length=80 terminal.py

# pylint: disable=import-error

import enum
import os
import pathlib
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import termios
import textwrap
import time
import tty
from contextlib import contextmanager
from typing import Iterator, Union, Tuple, List, Optional

class Key(enum.Enum):
    """Semantic terminal control keys."""
    CTRL_K = 11
    CTRL_J = 10
    CTRL_U = 21
    ENTER = 13
    CTRL_C = 3
    ESCAPE = 27
    BACKSPACE = 127
    DELETE = "\033[3~"
    UP = "\033[A"
    DOWN = "\033[B"
    LEFT = "\033[D"
    RIGHT = "\033[C"

@contextmanager
def terminal(fd: Optional[int] = None) -> Iterator["Terminal"]:
    """
    Saves termios attributes, applies raw mode, and ensures
    cursor visibility on exit.
    """
    fd = sys.stdin.fileno() if fd is None else fd
    original_attrs = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield Terminal(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_attrs)
        sys.stdout.write(cursor(True))
        sys.stdout.flush()


class Terminal:
    """Terminal facade for output, size queries, and key input."""

    def __init__(self, fd: int):
        self.fd = fd

    def write(self, *sequences: str):
        """Writes all sequences to stdout and flushes."""
        sys.stdout.write("".join(sequences))
        sys.stdout.flush()

    def size(self) -> Tuple[int, int]:
        """Returns terminal size as (columns, rows)."""
        try:
            with open("/dev/tty", "rb") as tty_device:
                size = os.get_terminal_size(tty_device.fileno())
        except OSError:
            size = shutil.get_terminal_size()
        return size.columns, size.lines

    def key(self) -> Union[Key, str]:
        """Reads a key from this terminal instance."""
        first = os.read(self.fd, 1)
        while not first:
            first = os.read(self.fd, 1)
        code = first[0]

        if code in (8, Key.BACKSPACE.value):
            return Key.BACKSPACE
        if code == Key.ESCAPE.value:
            return _parse_escape(self.fd)

        single_key = _key_from_value(code)
        if single_key is not None:
            return single_key
        return first.decode("utf-8", errors="ignore")

def clear_screen() -> str:
    """Returns ANSI sequence to clear screen."""
    return "\033[2J"

def move_to(row: int, col: int) -> str:
    """Returns ANSI sequence to move cursor to row and col."""
    return f"\033[{row};{col}H"

def cursor(visible: bool, shape: str = "block", blink: bool = True) -> str:
    """Returns ANSI sequence to set cursor visibility and shape.

    shape: 'block', 'underline', or 'bar' (ignored when visible=False)
    blink: whether the cursor blinks (ignored when visible=False)
    """
    if not visible:
        return "\033[?25l"
    codes = {
        ("block",     True):  1,
        ("block",     False): 2,
        ("underline", True):  3,
        ("underline", False): 4,
        ("bar",       True):  5,
        ("bar",       False): 6,
    }
    return f"\033[{codes[(shape, blink)]} q\033[?25h"

def save_pos() -> str:
    """Returns ANSI sequence to save cursor position."""
    return "\033[s"

def restore_pos() -> str:
    """Returns ANSI sequence to restore cursor position."""
    return "\033[u"

def _key_from_value(value: Union[int, str]) -> Optional[Key]:
    """Converts a key code or ANSI sequence to Key when supported."""
    try:
        return Key(value)
    except ValueError:
        return None

def _parse_escape(fd: int) -> Union[Key, str]:
    """Parses an escape-prefixed key sequence from fd."""
    if not select.select([fd], [], [], 0.1)[0]:
        return Key.ESCAPE

    second = os.read(fd, 1)
    if second != b"[":
        sequence = "\033" + (second + _read_available(fd)).decode(
            "utf-8", errors="ignore"
        )
        return _key_from_value(sequence) or sequence

    if not select.select([fd], [], [], 0.05)[0]:
        return "\033["

    third = os.read(fd, 1)
    if third == b"3":
        suffix = b""
        if select.select([fd], [], [], 0.05)[0]:
            suffix = os.read(fd, 1)
        sequence = (
            "\033[" + (third + suffix + _read_available(fd))
                .decode("utf-8", errors="ignore")
        )
        return _key_from_value(sequence) or sequence

    sequence = "\033[" + (third + _read_available(fd)).decode(
        "utf-8", errors="ignore"
    )
    return _key_from_value(sequence) or sequence

def _read_available(fd: int) -> bytes:
    """Reads all currently-available bytes from fd without blocking."""
    chunks = []
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 1)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)

# --- Testing ---

try:
    import pytest
except ImportError:
    from unittest import mock
    pytest = mock.MagicMock()

class TmuxHelper:
    """Context manager for automated terminal-based testing using tmux."""

    def __init__(self, session_name: Optional[str] = None):
        self.session_name = session_name or f"test_{int(time.time() * 1000)}"
        self.temp_dir = None
        self._key_map = {
            Key.CTRL_K: "C-k",
            Key.CTRL_J: "C-j",
            Key.CTRL_U: "C-u",
            Key.ENTER: "Enter",
            Key.CTRL_C: "C-c",
            Key.ESCAPE: "Escape",
            Key.BACKSPACE: "BSpace",
            Key.DELETE: "Delete",
            Key.UP: "Up",
            Key.DOWN: "Down",
            Key.LEFT: "Left",
            Key.RIGHT: "Right",
        }

    def _run_tmux(self, *args):
        return subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            check=True
        )

    def __enter__(self):
        # Create a temporary directory for scripts
        self.temp_dir = tempfile.mkdtemp()
        # Start a new detached session with a specific size
        self._run_tmux(
            "new-session", "-d", "-s", self.session_name,
            "-x", "80", "-y", "24"
        )
        self._run_tmux("set-option", "-t", self.session_name, "status", "off")

        # Disable bash history and export PYTHONPATH
        self.send_keys("export HISTFILE=/dev/null", Key.ENTER)
        pkg_dir = pathlib.Path(__file__).resolve().parent
        self.send_keys(f"export PYTHONPATH={pkg_dir}:$PYTHONPATH", Key.ENTER)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._run_tmux("kill-session", "-t", self.session_name)
        except subprocess.CalledProcessError:
            pass
        finally:
            if self.temp_dir and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)

    def send_keys(self, *keys: Union[Key, str]):
        """Translates and sends keys to the tmux session."""
        for key in keys:
            if isinstance(key, Key):
                tmux_key = self._key_map[key]
            else:
                tmux_key = str(key)
            self._run_tmux("send-keys", "-t", self.session_name, tmux_key)

    def capture_pane(self) -> List[str]:
        """Returns the current text content of the tmux pane."""
        result = self._run_tmux("capture-pane", "-p", "-t", self.session_name)
        return result.stdout.splitlines()

    def resize(self, columns: int, rows: int):
        """Resizes the tmux window."""
        self._run_tmux(
            "resize-window", "-t", self.session_name,
            "-x", str(columns), "-y", str(rows)
        )

        # wait for the resize to take effect
        pane_tty = self._run_tmux(
            "display-message", "-p", "-t", self.session_name,
            "#{pane_tty}"
        ).stdout.strip()

        deadline = time.time() + 1.0
        while time.time() < deadline:
            with open(pane_tty, "rb") as tty_device:
                size = os.get_terminal_size(tty_device.fileno())
            current = (size.columns, size.lines)
            if current == (columns, rows):
                return
            time.sleep(0.01)

        raise AssertionError(
            "resize-window did not settle to "
            f"{columns}x{rows}; pane={current[0]}x{current[1]}"
        )

    def wait_for(
        self,
        regex: Union[str, re.Pattern],
        timeout: float = 2.0,
    ) -> Optional[re.Match]:
        """
        Wait for a regex match in the current pane output.
        Concatenates pane lines and allows multiline regex search.
        """
        assert isinstance(regex, str)
        regex = re.compile(regex, re.MULTILINE)

        start_time = time.time()
        while time.time() - start_time < timeout:
            content = "\n".join(self.capture_pane())
            match = regex.search(content)
            if match:
                return match
            time.sleep(0.01)
        return None

    def run_python(self, code: str):
        """Executes Python code within the tmux session."""
        assert self.temp_dir
        fd, tmp_path = tempfile.mkstemp(suffix=".py", dir=self.temp_dir)
        with os.fdopen(fd, "w") as script_file:
            script_file.write(textwrap.dedent(code))
        self.send_keys(f"python3 {tmp_path}", Key.ENTER)

@pytest.fixture
def tmux():
    """Pytest fixture that yields a TmuxHelper instance."""
    with TmuxHelper() as helper:
        yield helper

def test_tmux_helper(tmux): # pylint: disable=redefined-outer-name
    """Verify TmuxHelper can run a script and capture interaction."""
    tmux.resize(80, 24)
    tmux.run_python("""
        import sys
        name = input("prompt>")
        print(f"HELLO_{name}")
    """)
    assert tmux.wait_for("prompt>")
    tmux.send_keys("WORLD", Key.ENTER)
    assert tmux.wait_for("HELLO_WORLD")

def test_ansi_sequences():
    """Verify ANSI sequence generators return correct strings."""
    assert clear_screen() == "\033[2J"
    assert move_to(10, 5) == "\033[10;5H"
    assert cursor(False) == "\033[?25l"
    assert cursor(True) == "\033[1 q\033[?25h"
    assert cursor(True, shape="block", blink=False) == "\033[2 q\033[?25h"
    assert cursor(True, shape="underline") == "\033[3 q\033[?25h"
    assert cursor(True, shape="underline", blink=False) == "\033[4 q\033[?25h"
    assert cursor(True, shape="bar") == "\033[5 q\033[?25h"
    assert cursor(True, shape="bar", blink=False) == "\033[6 q\033[?25h"
    assert save_pos() == "\033[s"
    assert restore_pos() == "\033[u"

def test_terminal_write(capsys):
    """Verify Terminal.write joins sequences and flushes to stdout."""
    Terminal(0).write("A", "B", "C")
    captured = capsys.readouterr()
    assert captured.out == "ABC"

def test_terminal_key():
    """Verify Terminal.key handles printable and control sequences."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"x")
        assert term.key() == "x"

        os.write(write_fd, bytes([Key.CTRL_K.value]))
        assert term.key() == Key.CTRL_K

        os.write(write_fd, bytes([8]))
        assert term.key() == Key.BACKSPACE

        os.write(write_fd, bytes([Key.CTRL_C.value]))
        assert term.key() == Key.CTRL_C

        os.write(write_fd, bytes([4]))
        assert term.key() == "\x04"

        os.write(write_fd, b"\033[A")
        assert term.key() == Key.UP

        os.write(write_fd, b"\033[B")
        assert term.key() == Key.DOWN

        os.write(write_fd, b"\033[D")
        assert term.key() == Key.LEFT

        os.write(write_fd, b"\033[C")
        assert term.key() == Key.RIGHT

        os.write(write_fd, b"\033[3~")
        assert term.key() == Key.DELETE

        os.write(write_fd, bytes([Key.ESCAPE.value]))
        assert term.key() == Key.ESCAPE
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_key_escape_timing():
    """Verify ESC is disambiguated by trailing-byte timing."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"\033")
        fast_follow = threading.Timer(0.02, os.write, args=(write_fd, b"[A"))
        fast_follow.start()
        assert term.key() == Key.UP
        fast_follow.join()

        os.write(write_fd, b"\033")
        slow_follow = threading.Timer(0.2, os.write, args=(write_fd, b"[A"))
        slow_follow.start()
        assert term.key() == Key.ESCAPE
        slow_follow.join()

        assert term.key() == "["
        assert term.key() == "A"
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_key_unknown_escape_sequence():
    """Verify unknown escape sequences are returned as raw strings."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"\033[1;5A")
        assert term.key() == "\033[1;5A"
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_size(tmux): # pylint: disable=redefined-outer-name
    """Verify Terminal.size returns correct terminal dimensions."""
    tmux.resize(50, 20)
    tmux.run_python("""
        import terminal as term
        with term.terminal() as t:
            cols, rows = t.size()
        print(f"SIZE:{cols},{rows}")
    """)
    assert tmux.wait_for("SIZE:50,20")
    tmux.resize(80, 24)
    tmux.run_python("""
        import terminal as term
        with term.terminal() as t:
            cols, rows = t.size()
        print(f"SIZE:{cols},{rows}")
    """)
    assert tmux.wait_for("SIZE:80,24")

def test_terminal_size_runtime(tmux): # pylint: disable=redefined-outer-name
    """Integration test for size updates during an active terminal loop."""
    tmux.resize(80, 24)
    tmux.run_python("""
        import terminal as term

        def key_to_string(key):
            if isinstance(key, str):
                return key
            return key.name

        with term.terminal() as t:
            print("READY", flush=True)
            for _ in range(3):
                key = t.key()
                cols, rows = t.size()
                print(f"{key_to_string(key)}:{cols}:{rows}", flush=True)

        print("DONE", flush=True)
    """)

    assert tmux.wait_for("READY")

    tmux.send_keys("a")
    assert tmux.wait_for("a:80:24")

    tmux.resize(50, 20)
    tmux.send_keys("b")
    assert tmux.wait_for("b:50:20")

    tmux.resize(100, 30)
    tmux.send_keys("c")
    assert tmux.wait_for("c:100:30")
    assert tmux.wait_for("DONE")

def test_terminal(tmux): # pylint: disable=redefined-outer-name
    """Verify terminal context modifies and restores attributes."""
    tmux.run_python("""
        import sys
        import termios
        from terminal import terminal

        fd = sys.stdin.fileno()
        orig = termios.tcgetattr(fd)

        with terminal(fd):
            raw = termios.tcgetattr(fd)
            if raw != orig:
                print("ATTRS_CHANGED")
            if not (raw[3] & termios.ICANON):
                print("ICANON_OFF")

        final = termios.tcgetattr(fd)
        if final == orig:
            print("ATTRS_RESTORED")
    """)
    assert tmux.wait_for("ATTRS_CHANGED")
    assert tmux.wait_for("ICANON_OFF")
    assert tmux.wait_for("ATTRS_RESTORED")

def test_terminal_exception(tmux): # pylint: disable=redefined-outer-name
    """Integration test for terminal restoration on exception."""
    tmux.run_python("""
        import sys
        import termios
        import terminal as term

        fd = sys.stdin.fileno()
        orig = termios.tcgetattr(fd)

        try:
            with term.terminal(fd):
                raw = termios.tcgetattr(fd)
                if raw != orig:
                    print("IN_RAW", flush=True)
                raise RuntimeError("boom")
        except RuntimeError:
            print("EXC_CAUGHT", flush=True)

        final = termios.tcgetattr(fd)
        if final == orig:
            print("RESTORED", flush=True)
        if final[3] & termios.ICANON:
            print("ICANON_ON", flush=True)
    """)

    assert tmux.wait_for("IN_RAW")
    assert tmux.wait_for("EXC_CAUGHT")
    assert tmux.wait_for("RESTORED")
    assert tmux.wait_for("ICANON_ON")

def test_terminal_flow(tmux): # pylint: disable=redefined-outer-name
    """Integration test for terminal helpers in raw mode."""
    tmux.resize(120, 30)
    tmux.run_python("""
        import terminal as term
        from terminal import Key

        def key_to_string(key):
            if isinstance(key, str):
                return key
            return key.name

        with term.terminal() as t:
            cols, rows = t.size()
            t.write(
                term.clear_screen(),
                term.move_to(1, 1),
                term.save_pos(),
                term.cursor(False),
                "READY",
                term.restore_pos(),
                term.cursor(True),
            )
            print(f"SIZE:{cols},{rows}", flush=True)
            print("LOOP_READY", flush=True)

            while True:
                key = t.key()
                print(f"K:{key_to_string(key)}", flush=True)
                if key == Key.CTRL_C:
                    break

        print("DONE", flush=True)
    """)

    assert tmux.wait_for("SIZE:120,30")
    assert tmux.wait_for("LOOP_READY")

    tmux.send_keys("x")
    assert tmux.wait_for("K:x")

    tmux.send_keys(Key.UP)
    assert tmux.wait_for("K:UP")

    tmux.send_keys(Key.DOWN)
    assert tmux.wait_for("K:DOWN")

    tmux.send_keys(Key.LEFT)
    assert tmux.wait_for("K:LEFT")

    tmux.send_keys(Key.RIGHT)
    assert tmux.wait_for("K:RIGHT")

    tmux.send_keys(Key.DELETE)
    assert tmux.wait_for("K:DELETE")

    tmux.send_keys(Key.ESCAPE)
    assert tmux.wait_for("K:ESCAPE")

    tmux.send_keys(Key.CTRL_C)
    assert tmux.wait_for("K:CTRL_C")
    assert tmux.wait_for("DONE")

def test_terminal_clear_and_move(tmux): # pylint: disable=redefined-outer-name
    """Verify tiny-screen updates match full-screen regex snapshots."""
    tmux.resize(5, 5)
    tmux.run_python("""
        import time
        import terminal as term

        updates = [
            (1, 1, "A"),
            (2, 2, "B"),
            (3, 3, "C"),
            (4, 4, "D"),
            (5, 5, "E"),
        ]

        time.sleep(0.3)
        with term.terminal() as t:
            for row, col, token in updates:
                t.write(
                    term.clear_screen(),
                    term.move_to(row, col),
                    token,
                )
                time.sleep(0.1)
    """)

    def screen(row: int, col: int, token: str) -> str:
        lines = []
        for line_index in range(1, 6):
            if line_index == row:
                lines.append(re.escape((" " * (col - 1)) + token))
            else:
                lines.append("")
        return r"\A" + "\n".join(lines) + r"\Z"

    assert tmux.wait_for(screen(1, 1, "A"))
    assert tmux.wait_for(screen(2, 2, "B"))
    assert tmux.wait_for(screen(3, 3, "C"))
    assert tmux.wait_for(screen(4, 4, "D"))
    assert tmux.wait_for(screen(5, 5, "E"))
