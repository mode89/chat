"""
Terminal management library for low-level terminal control and input handling.
"""

# Testing: pytest terminal.py
# Linting: pylint --max-line-length=80 terminal.py

# pylint: disable=import-error
# pylint: disable=too-many-lines

import enum
import os
import pathlib
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import termios
import textwrap
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Union, Tuple, List, Optional

class Key(enum.Enum):
    """Semantic terminal control keys."""
    CTRL_A = 1
    CTRL_B = 2
    CTRL_E = 5
    CTRL_F = 6
    CTRL_K = 11
    CTRL_J = 10
    CTRL_U = 21
    CTRL_W = 23
    TAB = 9
    ENTER = 13
    CTRL_C = 3
    ESCAPE = 27
    BACKSPACE = 127
    SHIFT_TAB = "\033[Z"
    DELETE = "\033[3~"
    PAGE_UP = "\033[5~"
    PAGE_DOWN = "\033[6~"
    UP = "\033[A"
    DOWN = "\033[B"
    LEFT = "\033[D"
    RIGHT = "\033[C"

@dataclass(frozen=True)
class Paste:
    """Text the terminal delivered as one bracketed-paste block."""
    text: str

@dataclass(frozen=True)
class Resize:
    """New terminal dimensions after the window changed size."""
    cols: int
    rows: int

Event = Union[Key, str, Paste, Resize, None]

@contextmanager
def terminal(
    fd: Optional[int] = None, alt_screen: bool = True
) -> Iterator["Terminal"]:
    """
    Saves termios attributes, applies raw mode, switches to the
    alternate screen buffer, enables bracketed paste, reports window
    resizes, and restores everything on exit.
    """
    fd = sys.stdin.fileno() if fd is None else fd
    original_attrs = termios.tcgetattr(fd)
    resize_read, resize_write = os.pipe()
    os.set_blocking(resize_write, False)

    def on_winch(_signum, _frame):
        try:
            os.write(resize_write, b"\x01")
        except BlockingIOError:
            pass

    previous_winch = signal.signal(signal.SIGWINCH, on_winch)
    try:
        tty.setraw(fd)
        if alt_screen:
            sys.stdout.write("\033[?1049h")
        sys.stdout.write("\033[?2004h")
        sys.stdout.flush()
        yield Terminal(fd, resize_read)
    finally:
        signal.signal(signal.SIGWINCH, previous_winch)
        os.close(resize_write)
        os.close(resize_read)
        termios.tcsetattr(fd, termios.TCSADRAIN, original_attrs)
        sys.stdout.write("\033[?2004l")
        if alt_screen:
            sys.stdout.write("\033[?1049l")
        sys.stdout.write(cursor(True))
        sys.stdout.flush()


class Terminal:
    """Terminal facade for output, size queries, and input events."""

    def __init__(self, fd: int, resize_fd: Optional[int] = None):
        self.fd = fd
        self.resize_fd = resize_fd

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

    def event(self, timeout: Optional[float] = None) -> Event:
        """Reads the next input event from this terminal instance.

        timeout: seconds to wait for the first byte; None waits forever,
        0 polls. Returns None when no event arrives in time. Once an
        event starts arriving it is always read to completion.
        """
        watched = [self.fd]
        if self.resize_fd is not None:
            watched.append(self.resize_fd)

        ready = select.select(watched, [], [], timeout)[0]
        if not ready:
            return None
        if self.resize_fd in ready:
            os.read(self.resize_fd, 64)  # coalesce a burst into one Resize
            return Resize(*self.size())

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

class Color(enum.Enum):
    """Foreground SGR color codes; the background code is this plus 10."""
    BLACK = 30
    RED = 31
    GREEN = 32
    YELLOW = 33
    BLUE = 34
    MAGENTA = 35
    CYAN = 36
    WHITE = 37
    DEFAULT = 39
    BRIGHT_BLACK = 90
    BRIGHT_RED = 91
    BRIGHT_GREEN = 92
    BRIGHT_YELLOW = 93
    BRIGHT_BLUE = 94
    BRIGHT_MAGENTA = 95
    BRIGHT_CYAN = 96
    BRIGHT_WHITE = 97

def style( # pylint: disable=too-many-arguments,too-many-positional-arguments
    text: str,
    fg: Optional[Color] = None,
    bg: Optional[Color] = None,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    reverse: bool = False,
) -> str:
    """Returns text wrapped in SGR codes, always reset afterwards."""
    codes: List[int] = []
    if bold:
        codes.append(1)
    if italic:
        codes.append(3)
    if underline:
        codes.append(4)
    if reverse:
        codes.append(7)
    if fg is not None:
        codes.append(fg.value)
    if bg is not None:
        codes.append(bg.value + 10)
    if not codes:
        return text
    joined = ";".join(str(code) for code in codes)
    return f"\033[{joined}m{text}\033[0m"

def save_pos() -> str:
    """Returns ANSI sequence to save cursor position."""
    return "\033[s"

def restore_pos() -> str:
    """Returns ANSI sequence to restore cursor position."""
    return "\033[u"

_PASTE_START = "\033[200~"
_PASTE_END = "\033[201~"
_PASTE_TIMEOUT = 1.0

def _key_from_value(value: Union[int, str]) -> Optional[Key]:
    """Converts a key code or ANSI sequence to Key when supported."""
    try:
        return Key(value)
    except ValueError:
        return None

def _parse_escape(fd: int) -> Union[Key, str, Paste]:
    """Parses an escape-prefixed key sequence from fd."""
    if not select.select([fd], [], [], 0.1)[0]:
        return Key.ESCAPE

    second = os.read(fd, 1)
    if second != b"[":
        sequence = "\033" + (second + _read_available(fd)).decode(
            "utf-8", errors="ignore"
        )
        return _key_from_value(sequence) or sequence

    sequence = "\033[" + _read_csi(fd)
    if sequence == _PASTE_START:
        return Paste(_read_paste(fd))
    return _key_from_value(sequence) or sequence

def _read_csi(fd: int) -> str:
    """Reads CSI bytes up to and including the final byte (0x40-0x7E)."""
    chunks = []
    while select.select([fd], [], [], 0.05)[0]:
        byte = os.read(fd, 1)
        if not byte:
            break
        chunks.append(byte)
        if 0x40 <= byte[0] <= 0x7E:
            break
    return b"".join(chunks).decode("utf-8", errors="ignore")

def _read_paste(fd: int) -> str:
    """Reads pasted bytes up to the bracketed-paste end marker."""
    end_marker = _PASTE_END.encode()
    buffer = bytearray()
    # One byte per read: a chunked read would run past the end marker and
    # swallow keystrokes typed right after the paste.
    while not buffer.endswith(end_marker):
        if not select.select([fd], [], [], _PASTE_TIMEOUT)[0]:
            break
        byte = os.read(fd, 1)
        if not byte:
            break
        buffer += byte
    text = bytes(buffer).decode("utf-8", errors="replace")
    return _sanitize_paste(text.removesuffix(_PASTE_END))

def _sanitize_paste(text: str) -> str:
    """Normalizes newlines and drops control characters from pasted text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        char for char in text
        if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    )

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
            Key.CTRL_A: "C-a",
            Key.CTRL_B: "C-b",
            Key.CTRL_E: "C-e",
            Key.CTRL_F: "C-f",
            Key.CTRL_K: "C-k",
            Key.CTRL_J: "C-j",
            Key.CTRL_U: "C-u",
            Key.CTRL_W: "C-w",
            Key.TAB: "Tab",
            Key.SHIFT_TAB: "BTab",
            Key.ENTER: "Enter",
            Key.CTRL_C: "C-c",
            Key.ESCAPE: "Escape",
            Key.BACKSPACE: "BSpace",
            Key.DELETE: "Delete",
            Key.PAGE_UP: "PageUp",
            Key.PAGE_DOWN: "PageDown",
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

    def capture_escapes(self) -> str:
        """Returns pane content with ANSI escape sequences preserved."""
        result = self._run_tmux(
            "capture-pane", "-p", "-e", "-t", self.session_name
        )
        return result.stdout

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

    def paste(self, text: str):
        """Pastes text into the session using bracketed paste."""
        self._run_tmux("set-buffer", "-t", self.session_name, text)
        self._run_tmux("paste-buffer", "-p", "-t", self.session_name)

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

def test_style():
    """Verify SGR styling wraps text and always resets."""
    assert style("hi") == "hi"
    assert style("hi", fg=Color.RED) == "\033[31mhi\033[0m"
    assert style("hi", bg=Color.BLUE) == "\033[44mhi\033[0m"
    assert style("hi", fg=Color.BRIGHT_WHITE) == "\033[97mhi\033[0m"
    assert style("hi", bg=Color.BRIGHT_BLACK) == "\033[100mhi\033[0m"
    assert style("hi", bold=True) == "\033[1mhi\033[0m"
    assert style("hi", italic=True) == "\033[3mhi\033[0m"
    assert style("hi", underline=True) == "\033[4mhi\033[0m"
    assert style("hi", reverse=True) == "\033[7mhi\033[0m"
    assert style(
        "hi", fg=Color.GREEN, bg=Color.BLACK, bold=True
    ) == "\033[1;32;40mhi\033[0m"
    assert style(
        "hi", fg=Color.RED, bold=True, italic=True, underline=True
    ) == "\033[1;3;4;31mhi\033[0m"

def test_style_no_leak(tmux): # pylint: disable=redefined-outer-name
    """Verify styled output resets before later unstyled text."""
    tmux.resize(80, 24)
    tmux.run_python("""
        import terminal as term
        from terminal import Color

        with term.terminal() as t:
            t.write(
                term.clear_screen(),
                term.move_to(1, 1),
                term.style("STYLED", fg=Color.RED, bold=True),
                term.move_to(2, 1),
                "PLAIN",
            )
            t.event()
    """)

    assert tmux.wait_for("PLAIN")
    styled_line, plain_line = tmux.capture_escapes().splitlines()[:2]
    assert styled_line == "\033[1m\033[31mSTYLED\033[0m"
    assert plain_line == "PLAIN"
    tmux.send_keys("a")

def test_terminal_write(capsys):
    """Verify Terminal.write joins sequences and flushes to stdout."""
    Terminal(0).write("A", "B", "C")
    captured = capsys.readouterr()
    assert captured.out == "ABC"

def test_terminal_event():
    """Verify Terminal.key handles printable and control sequences."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"x")
        assert term.event() == "x"

        os.write(write_fd, bytes([Key.CTRL_K.value]))
        assert term.event() == Key.CTRL_K

        for code, control in (
            (1, Key.CTRL_A), (2, Key.CTRL_B), (5, Key.CTRL_E),
            (6, Key.CTRL_F), (10, Key.CTRL_J), (21, Key.CTRL_U),
            (23, Key.CTRL_W),
        ):
            os.write(write_fd, bytes([code]))
            assert term.event() == control

        os.write(write_fd, bytes([8]))
        assert term.event() == Key.BACKSPACE

        os.write(write_fd, bytes([Key.CTRL_C.value]))
        assert term.event() == Key.CTRL_C

        os.write(write_fd, bytes([4]))
        assert term.event() == "\x04"

        os.write(write_fd, b"\033[A")
        assert term.event() == Key.UP

        os.write(write_fd, b"\033[B")
        assert term.event() == Key.DOWN

        os.write(write_fd, b"\033[D")
        assert term.event() == Key.LEFT

        os.write(write_fd, b"\033[C")
        assert term.event() == Key.RIGHT

        os.write(write_fd, b"\033[3~")
        assert term.event() == Key.DELETE

        os.write(write_fd, b"\033[5~")
        assert term.event() == Key.PAGE_UP

        os.write(write_fd, b"\033[6~")
        assert term.event() == Key.PAGE_DOWN

        os.write(write_fd, bytes([9]))
        assert term.event() == Key.TAB

        os.write(write_fd, b"\033[Z")
        assert term.event() == Key.SHIFT_TAB

        os.write(write_fd, bytes([Key.ESCAPE.value]))
        assert term.event() == Key.ESCAPE
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_event_escape_timing():
    """Verify ESC is disambiguated by trailing-byte timing."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"\033")
        fast_follow = threading.Timer(0.02, os.write, args=(write_fd, b"[A"))
        fast_follow.start()
        assert term.event() == Key.UP
        fast_follow.join()

        os.write(write_fd, b"\033")
        slow_follow = threading.Timer(0.2, os.write, args=(write_fd, b"[A"))
        slow_follow.start()
        assert term.event() == Key.ESCAPE
        slow_follow.join()

        assert term.event() == "["
        assert term.event() == "A"
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_event_resize():
    """Verify resize wakeups coalesce into one Resize event."""
    read_fd, write_fd = os.pipe()
    resize_read, resize_write = os.pipe()
    term = Terminal(read_fd, resize_read)
    try:
        os.write(resize_write, b"\x01\x01\x01")
        event = term.event()
        assert isinstance(event, Resize)
        assert (event.cols, event.rows) == term.size()
        assert term.event(timeout=0.05) is None

        os.write(write_fd, b"x")
        assert term.event() == "x"
    finally:
        for fd in (read_fd, write_fd, resize_read, resize_write):
            os.close(fd)

def test_terminal_event_timeout():
    """Verify key() returns None when no key arrives within timeout."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        started = time.monotonic()
        assert term.event(timeout=0.05) is None
        assert 0.05 <= time.monotonic() - started < 0.5

        assert term.event(timeout=0) is None

        os.write(write_fd, b"x")
        started = time.monotonic()
        assert term.event(timeout=1.0) == "x"
        assert time.monotonic() - started < 0.5

        os.write(write_fd, b"\033")
        follow = threading.Timer(0.02, os.write, args=(write_fd, b"[A"))
        follow.start()
        assert term.event(timeout=0.05) == Key.UP
        follow.join()
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_event_paste():
    """Verify bracketed paste arrives as one Paste with normalized text."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"\033[200~hello\r\nworld\033[201~")
        assert term.event() == Paste("hello\nworld")

        os.write(write_fd, "\033[200~caf\u00e9 \U0001f600\033[201~".encode())
        assert term.event() == Paste("caf\u00e9 \U0001f600")

        os.write(write_fd, b"\033[200~\033[201~")
        assert term.event() == Paste("")

        os.write(write_fd, b"\033[200~a\033[2Jb\033[201~")
        assert term.event() == Paste("a[2Jb")

        os.write(write_fd, b"\033[200~keep\033[201~x")
        assert term.event() == Paste("keep")
        assert term.event() == "x"
    finally:
        os.close(read_fd)
        os.close(write_fd)

def test_terminal_event_unknown_escape_sequence():
    """Verify unknown escape sequences are returned as raw strings."""
    read_fd, write_fd = os.pipe()
    term = Terminal(read_fd)
    try:
        os.write(write_fd, b"\033[1;5A")
        assert term.event() == "\033[1;5A"
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

        def next_key(t):
            while True:
                event = t.event()
                if not isinstance(event, term.Resize):
                    return event

        with term.terminal(alt_screen=False) as t:
            print("READY", flush=True)
            for _ in range(3):
                key = next_key(t)
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

def test_terminal_resize(tmux): # pylint: disable=redefined-outer-name
    """Integration test for SIGWINCH arriving as a Resize event."""
    tmux.resize(80, 24)
    tmux.run_python(r"""
        import terminal as term

        def emit(line):
            print(line, end="\r\n", flush=True)

        with term.terminal(alt_screen=False) as t:
            emit("READY")
            while True:
                event = t.event()
                if isinstance(event, term.Resize):
                    emit(f"RESIZE:{event.cols}:{event.rows}")
                elif event == "q":
                    break
        emit("DONE")
    """)

    assert tmux.wait_for("READY")

    tmux.resize(50, 20)
    assert tmux.wait_for("RESIZE:50:20")

    tmux.resize(100, 30)
    assert tmux.wait_for("RESIZE:100:30")

    tmux.send_keys("q")
    assert tmux.wait_for("DONE")

def test_terminal(tmux): # pylint: disable=redefined-outer-name
    """Verify terminal context modifies and restores attributes."""
    tmux.run_python("""
        import sys
        import termios
        from terminal import terminal

        fd = sys.stdin.fileno()
        orig = termios.tcgetattr(fd)

        with terminal(fd, alt_screen=False):
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
            with term.terminal(fd, alt_screen=False):
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

def test_terminal_paste(tmux): # pylint: disable=redefined-outer-name
    """Verify a real tmux paste is delivered as a single Paste block."""
    tmux.run_python("""
        import terminal as term

        with term.terminal(alt_screen=False) as t:
            print("READY", flush=True)
            event = t.event()
            print(f"GOT:{type(event).__name__}:{event.text!r}", flush=True)
            print(f"NEXT:{t.event()!r}", flush=True)
    """)

    assert tmux.wait_for("READY")
    tmux.paste("one\ntwo")
    assert tmux.wait_for(re.escape("GOT:Paste:'one\\ntwo'"))
    tmux.send_keys("z")
    assert tmux.wait_for(re.escape("NEXT:'z'"))

def test_terminal_alt_screen(tmux): # pylint: disable=redefined-outer-name
    """Verify alt-screen preserves main screen content across terminal()."""
    tmux.run_python("""
        import terminal as term

        print("MARKER_BEFORE", flush=True)
        with term.terminal() as t:
            t.write(term.clear_screen(), term.move_to(1, 1), "INSIDE_ALT")
            print("LOOP_READY", flush=True)
            t.event()
        print("MARKER_AFTER", flush=True)
    """)

    assert tmux.wait_for("LOOP_READY")
    # While inside alt-screen, the main-screen marker must be hidden.
    content = "\n".join(tmux.capture_pane())
    assert "MARKER_BEFORE" not in content
    assert "INSIDE_ALT" in content

    tmux.send_keys("a")
    assert tmux.wait_for("MARKER_AFTER")
    # After exit, main screen is restored: marker back, alt-screen gone.
    content = "\n".join(tmux.capture_pane())
    assert "MARKER_BEFORE" in content
    assert "MARKER_AFTER" in content
    assert "INSIDE_ALT" not in content

def test_terminal_flow(tmux): # pylint: disable=redefined-outer-name
    """Integration test for terminal helpers in raw mode."""
    tmux.resize(120, 30)
    tmux.run_python(r"""
        import terminal as term
        from terminal import Key

        def key_to_string(key):
            if isinstance(key, str):
                return key
            return key.name

        with term.terminal(alt_screen=False) as t:
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
            print(f"SIZE:{cols},{rows}", end="\r\n", flush=True)
            print("LOOP_READY", end="\r\n", flush=True)

            while True:
                key = t.event()
                print(f"K:{key_to_string(key)}", end="\r\n", flush=True)
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

    tmux.send_keys(Key.PAGE_UP)
    assert tmux.wait_for("K:PAGE_UP")

    tmux.send_keys(Key.PAGE_DOWN)
    assert tmux.wait_for("K:PAGE_DOWN")

    tmux.send_keys(Key.TAB)
    assert tmux.wait_for("K:TAB")

    tmux.send_keys(Key.SHIFT_TAB)
    assert tmux.wait_for("K:SHIFT_TAB")

    tmux.send_keys(Key.ESCAPE)
    assert tmux.wait_for("K:ESCAPE")

    for control in (
        Key.CTRL_A, Key.CTRL_B, Key.CTRL_E, Key.CTRL_F, Key.CTRL_W,
    ):
        tmux.send_keys(control)
        assert tmux.wait_for(f"K:{control.name}")

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
