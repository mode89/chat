"""A tiny vim-like terminal editor with functional core logic."""

# Testing: pytest editor.py
# Linting: pylint --max-line-length=80 editor.py

# pylint: disable=too-many-arguments
# pylint: disable=too-many-positional-arguments

import enum
import pathlib
import queue
import select
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, List, Sequence, Tuple, Union

from terminal import ( # pylint: disable=unused-import
    Key,
    clear_screen,
    cursor,
    move_to,
    terminal,
    tmux,
)


class Mode(enum.Enum):
    """Editor modes."""

    NORMAL = "NORMAL"
    INSERT = "INSERT"
    COMMAND = "COMMAND"


@dataclass(frozen=True)
class EditorState:  # pylint: disable=too-many-instance-attributes
    """Immutable editor state."""

    file_path: pathlib.Path
    lines: Tuple[str, ...]
    cursor: Tuple[int, int]     # (row, col) into the buffer
    top_line: int               # first visible buffer row
    mode: Mode
    cols: int
    rows: int
    should_quit: bool = False
    command: str = ""           # command-line mode input buffer


@dataclass(frozen=True)
class KeyEvent:
    """Input key event."""

    key: Union[Key, str]


@dataclass(frozen=True)
class ResizeEvent:
    """Terminal resize event."""

    cols: int
    rows: int


@dataclass(frozen=True)
class SaveFile:
    """Effect that writes full buffer to disk."""

    path: pathlib.Path
    content: str


@dataclass(frozen=True)
class LoadFile:
    """Effect that loads a file into the editor."""

    path: pathlib.Path


Event = Union[KeyEvent, ResizeEvent]
Effect = Union[SaveFile, LoadFile]


def main(argv: Sequence[str]) -> int:
    """CLI entry point."""
    if len(argv) != 2:
        print("Usage: python3 editor.py <path>")
        return 1
    return run_editor(pathlib.Path(argv[1]))


def ensure_lines(lines: Sequence[str]) -> Tuple[str, ...]:
    """Guarantees at least one editable line."""
    return tuple(lines) if lines else ("",)


def clamp_cursor(state: EditorState) -> EditorState:
    """Clamps cursor and top_line to valid bounds."""
    lines = ensure_lines(state.lines)
    row, col = state.cursor
    row = max(0, min(row, len(lines) - 1))
    col = max(0, min(col, len(lines[row])))
    text_rows = max(1, state.rows - 2)
    top = state.top_line
    top = min(top, row)                    # scroll up if cursor above viewport
    top = max(top, row - (text_rows - 1))  # scroll down if below viewport
    top = max(0, top)
    return replace(
        state,
        lines=lines,
        cursor=(row, col),
        top_line=top,
        cols=max(1, state.cols),
        rows=max(1, state.rows),
    )


def replace_lines(
    state: EditorState,
    lines: Sequence[str],
    row: int,
    col: int,
) -> EditorState:
    """Returns state with updated buffer and cursor location."""
    return clamp_cursor(
        replace(
            state,
            lines=ensure_lines(lines),
            cursor=(row, col),
        )
    )


def move_left(state: EditorState) -> EditorState:
    """Moves cursor one position left."""
    row, col = state.cursor
    return clamp_cursor(replace(state, cursor=(row, col - 1)))


def move_right(state: EditorState) -> EditorState:
    """Moves cursor one position right."""
    row, col = state.cursor
    return clamp_cursor(replace(state, cursor=(row, col + 1)))


def move_up(state: EditorState) -> EditorState:
    """Moves cursor one line up, keeping column when possible."""
    row, col = state.cursor
    return clamp_cursor(replace(state, cursor=(row - 1, col)))


def move_down(state: EditorState) -> EditorState:
    """Moves cursor one line down, keeping column when possible."""
    row, col = state.cursor
    return clamp_cursor(replace(state, cursor=(row + 1, col)))


def to_insert_mode(state: EditorState) -> EditorState:
    """Switches state to insert mode."""
    return clamp_cursor(replace(state, mode=Mode.INSERT))


def to_normal_mode(state: EditorState) -> EditorState:
    """Switches state to normal mode."""
    return clamp_cursor(replace(state, mode=Mode.NORMAL))


def insert_char(state: EditorState, char: str) -> EditorState:
    """Inserts a printable char at the cursor position."""
    row, col = state.cursor
    line = state.lines[row]
    new_line = line[:col] + char + line[col:]
    lines = list(state.lines)
    lines[row] = new_line
    return replace_lines(state, lines, row, col + 1)


def split_line(state: EditorState) -> EditorState:
    """Splits the current line at the cursor."""
    row, col = state.cursor
    line = state.lines[row]
    lines = list(state.lines)
    lines[row] = line[:col]
    lines.insert(row + 1, line[col:])
    return replace_lines(state, lines, row + 1, 0)


def backspace(state: EditorState) -> EditorState:
    """Deletes one char left of cursor or merges with previous line."""
    row, col = state.cursor
    lines = list(state.lines)

    if col > 0:
        line = lines[row]
        lines[row] = line[: col - 1] + line[col:]
        return replace_lines(state, lines, row, col - 1)

    if row == 0:
        return state

    previous = lines[row - 1]
    current = lines[row]
    lines[row - 1] = previous + current
    del lines[row]
    return replace_lines(state, lines, row - 1, len(previous))


def resize_state(state: EditorState, cols: int, rows: int) -> EditorState:
    """Updates terminal dimensions."""
    return clamp_cursor(replace(state, cols=cols, rows=rows))


def should_insert_char(key: Union[Key, str]) -> bool:
    """Checks whether a key should be inserted as text."""
    return isinstance(key, str) and len(key) == 1 and key.isprintable()


def parse_command(cmd: str) -> Tuple[str, str]:
    """Parses a command string into (verb, arg)."""
    parts = cmd.strip().split(None, 1)
    verb = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    return verb, arg


def execute_command(
    state: EditorState,
) -> Tuple[EditorState, List[Effect]]:
    """Executes the command buffer and returns new state + effects."""
    verb, arg = parse_command(state.command)
    path = pathlib.Path(arg) if arg else state.file_path
    base = replace(state, mode=Mode.NORMAL, command="")

    if verb == "wq":
        return replace(base, should_quit=True, file_path=path), [
            SaveFile(path=path, content="\n".join(state.lines))
        ]
    if "write".startswith(verb) and verb:
        return replace(base, file_path=path), [
            SaveFile(path=path, content="\n".join(state.lines))
        ]
    if "edit".startswith(verb) and verb:
        return base, [LoadFile(path=path)]
    if "quit".startswith(verb) and verb:
        return replace(base, should_quit=True), []
    return base, []


def reduce_event(
    state: EditorState,
    event: Event,
) -> Tuple[EditorState, List[Effect]]:
    """Pure reducer from event to next state and effects."""
    # pylint: disable=too-many-return-statements
    # pylint: disable=too-many-branches
    if isinstance(event, ResizeEvent):
        return resize_state(state, event.cols, event.rows), []

    key = event.key
    next_state = state
    effects: List[Effect] = []

    if state.mode == Mode.NORMAL:
        motion = {
            "h": move_left,
            "j": move_down,
            "k": move_up,
            "l": move_right,
        }.get(key)
        if motion is not None:
            next_state = motion(state)
        elif key == "i":
            next_state = to_insert_mode(state)
        elif key == ":":
            next_state = replace(state, mode=Mode.COMMAND, command="")
        return next_state, effects

    if state.mode == Mode.COMMAND:
        if key == Key.ESCAPE:
            return replace(state, mode=Mode.NORMAL, command=""), []
        if key == Key.BACKSPACE:
            if state.command:
                return replace(state, command=state.command[:-1]), []
            return replace(state, mode=Mode.NORMAL, command=""), []
        if key == Key.ENTER:
            return execute_command(state)
        if should_insert_char(key):
            return replace(state, command=state.command + key), []
        return state, []

    # INSERT mode
    if key == Key.ESCAPE:
        next_state = to_normal_mode(state)
    elif key == Key.ENTER:
        next_state = split_line(state)
    elif key == Key.BACKSPACE:
        next_state = backspace(state)
    elif isinstance(key, str) and should_insert_char(key):
        next_state = insert_char(state, key)
    return next_state, effects


def visible_top_row(state: EditorState) -> int:
    """Returns top buffer row shown in the viewport."""
    text_rows = max(0, state.rows - 2)
    if text_rows <= 0:
        return 0
    max_top = max(0, len(state.lines) - text_rows)
    return min(state.top_line, max_top)


def status_line(state: EditorState) -> str:
    """Renders a fixed-width status line."""
    text = (
        f"-- {state.mode.value} -- "
        f"{state.file_path} {state.cols}x{state.rows}"
    )
    clipped = text[: state.cols]
    return clipped.ljust(state.cols)


def command_line(state: EditorState) -> str:
    """Renders the command input line below the status bar."""
    if state.mode == Mode.COMMAND:
        text = ":" + state.command
    else:
        text = ""
    return text[: state.cols].ljust(state.cols)


def render(state: EditorState) -> str:
    """Pure full-screen render of editor frame."""
    top = visible_top_row(state)
    text_rows = max(0, state.rows - 2)
    parts = [clear_screen()]

    for screen_row in range(text_rows):
        index = top + screen_row
        line = state.lines[index] if index < len(state.lines) else ""
        parts.append(move_to(screen_row + 1, 1))
        parts.append(line[: state.cols])

    status_row = max(1, state.rows - 1)
    cmd_row = max(1, state.rows)
    parts.append(move_to(status_row, 1))
    parts.append(status_line(state))
    parts.append(move_to(cmd_row, 1))
    parts.append(command_line(state))

    if state.mode == Mode.COMMAND:
        cmd_col = min(len(":" + state.command) + 1, state.cols)
        parts.append(move_to(cmd_row, cmd_col))
        parts.append(cursor(True, shape="bar", blink=False))
    elif text_rows > 0:
        row, col = state.cursor
        cursor_screen_row = row - top + 1
        cursor_screen_row = max(1, min(cursor_screen_row, text_rows))
        cursor_screen_col = max(1, min(col + 1, state.cols))
        parts.append(move_to(cursor_screen_row, cursor_screen_col))
        shape = "bar" if state.mode == Mode.INSERT else "block"
        parts.append(cursor(True, shape=shape, blink=False))
    else:
        parts.append(move_to(1, 1))
        parts.append(cursor(True, shape="block", blink=False))

    return "".join(parts)


def read_file_lines(path: pathlib.Path) -> Tuple[str, ...]:
    """Reads a file as editor lines."""
    if not path.exists():
        return ("",)
    content = path.read_text(encoding="utf-8")
    if content == "":
        return ("",)
    return ensure_lines(content.split("\n"))


def run_effects(
    state: EditorState,
    effects: Sequence[Effect],
) -> EditorState:
    """Executes side effects and returns updated state."""
    for effect in effects:
        if isinstance(effect, SaveFile):
            effect.path.write_text(effect.content, encoding="utf-8")
        elif isinstance(effect, LoadFile):
            lines = read_file_lines(effect.path)
            state = clamp_cursor(replace(
                state,
                file_path=effect.path,
                lines=lines,
                cursor=(0, 0),
                top_line=0,
                mode=Mode.NORMAL,
                command="",
            ))
    return state


def resize_watcher_loop(
    size_provider: Callable[[], Tuple[int, int]],
    emit: Callable[[Tuple[int, int]], None],
    stop_event: threading.Event,
    interval: float = 0.1,
):
    """Polls terminal size and emits changes."""
    previous = size_provider()
    while not stop_event.wait(interval):
        current = size_provider()
        if current != previous:
            previous = current
            emit(current)


def run_editor(file_path: pathlib.Path) -> int:
    """Runs the terminal editor loop."""
    def dispatch(state: EditorState, event: Event) -> EditorState:
        next_state, effects = reduce_event(state, event)
        return run_effects(next_state, effects)

    lines = read_file_lines(file_path)
    resize_queue: queue.Queue[ResizeEvent] = queue.Queue()
    stop_event = threading.Event()

    with terminal() as term:
        cols, rows = term.size()
        state = clamp_cursor(EditorState(
            file_path=file_path,
            lines=lines,
            cursor=(0, 0),
            top_line=0,
            mode=Mode.NORMAL,
            cols=cols,
            rows=rows,
        ))
        watcher = threading.Thread(
            target=resize_watcher_loop,
            args=(
                term.size,
                lambda s: resize_queue.put(ResizeEvent(*s)),
                stop_event,
            ),
            daemon=True,
        )
        watcher.start()
        term.write(render(state))

        try:
            while not state.should_quit:
                updated = False
                while True:
                    try:
                        state = dispatch(state, resize_queue.get_nowait())
                        updated = True
                    except queue.Empty:
                        break
                if select.select([term.fd], [], [], 0.05)[0]:
                    state = dispatch(state, KeyEvent(term.key()))
                    updated = True
                if not state.should_quit and updated:
                    term.write(render(state))
        finally:
            stop_event.set()
            watcher.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


# --- Tests ---

try:
    import pytest # pylint: disable=unused-import
except ImportError:
    from unittest import mock
    pytest = mock.MagicMock()


def make_state(
    lines: Sequence[str],
    row: int = 0,
    col: int = 0,
    mode: Mode = Mode.NORMAL,
    size: Tuple[int, int] = (80, 24),
    top_line: int = 0,
) -> EditorState:
    """Helper for concise state construction in tests."""
    cols, rows = size
    return clamp_cursor(
        EditorState(
            file_path=pathlib.Path("sample.txt"),
            lines=ensure_lines(lines),
            cursor=(row, col),
            top_line=top_line,
            mode=mode,
            cols=cols,
            rows=rows,
        )
    )


def test_navigation_hjkl():
    """Navigation uses vim-like normal mode keys."""
    state = make_state(["ab", "xyz"], row=0, col=1)

    state, _ = reduce_event(state, KeyEvent("h"))
    assert state.cursor == (0, 0)

    state, _ = reduce_event(state, KeyEvent("j"))
    assert state.cursor == (1, 0)

    state, _ = reduce_event(state, KeyEvent("l"))
    assert state.cursor == (1, 1)

    state, _ = reduce_event(state, KeyEvent("k"))
    assert state.cursor == (0, 1)


def test_insert_edit_and_escape():
    """Insert mode edits text and ESC returns to normal mode."""
    state = make_state(["abc"])

    state, _ = reduce_event(state, KeyEvent("i"))
    assert state.mode == Mode.INSERT

    state, _ = reduce_event(state, KeyEvent("X"))
    assert state.lines == ("Xabc",)
    assert state.cursor[1] == 1

    state, _ = reduce_event(state, KeyEvent(Key.ESCAPE))
    assert state.mode == Mode.NORMAL


def test_enter_split_backspace_merge():
    """Enter splits and backspace at col 0 merges lines."""
    state = make_state(["ab"], row=0, col=1, mode=Mode.INSERT)

    state, _ = reduce_event(state, KeyEvent(Key.ENTER))
    assert state.lines == ("a", "b")
    assert state.cursor == (1, 0)

    state, _ = reduce_event(state, KeyEvent(Key.BACKSPACE))
    assert state.lines == ("ab",)
    assert state.cursor == (0, 1)


def test_render_status_mode_size():
    """Rendered frame includes status details."""
    state = make_state(["hello"], mode=Mode.INSERT, size=(30, 6))
    frame = render(state)

    assert "-- INSERT --" in frame
    assert "30x6" in frame


def test_render_normal_mode_cursor():
    """Normal mode renders a steady block cursor."""
    state = make_state(["hello"], mode=Mode.NORMAL, size=(30, 6))
    assert cursor(True, shape="block", blink=False) in render(state)


def test_render_insert_mode_cursor():
    """Insert mode renders a steady bar cursor."""
    state = make_state(["hello"], mode=Mode.INSERT, size=(30, 6))
    assert cursor(True, shape="bar", blink=False) in render(state)


def test_scroll_cursor_moves_before_viewport_scrolls():
    """Cursor moves down on screen until bottom, then viewport scrolls."""
    lines = [str(i) for i in range(30)]
    # rows=6 → 4 text rows visible (two rows reserved for status + command bar).
    # With top=0, visible buffer rows are 0-3 (screen rows 1-4).
    state = make_state(lines, row=0, col=0, size=(80, 6))

    # Press j 3 times: cursor reaches bottom of screen, no scroll yet
    for _ in range(3):
        state, _ = reduce_event(state, KeyEvent("j"))
    assert state.cursor == (3, 0)
    assert state.top_line == 0

    # 4th j: cursor at row 4 is off screen → viewport must scroll
    state, _ = reduce_event(state, KeyEvent("j"))
    assert state.cursor == (4, 0)
    assert state.top_line == 1

    # 5th j: scrolls again
    state, _ = reduce_event(state, KeyEvent("j"))
    assert state.cursor == (5, 0)
    assert state.top_line == 2


def test_resize_watcher():
    """Resize watcher emits new dimensions when size changes."""
    sizes = iter(
        [
            (80, 24),
            (80, 24),
            (90, 24),
            (90, 24),
            (90, 25),
        ]
    )

    def size_provider() -> Tuple[int, int]:
        try:
            return next(sizes)
        except StopIteration:
            return (90, 25)

    seen: List[Tuple[int, int]] = []
    stop_event = threading.Event()
    thread = threading.Thread(
        target=resize_watcher_loop,
        args=(size_provider, seen.append, stop_event, 0.01),
        daemon=True,
    )
    thread.start()
    time.sleep(0.07)
    stop_event.set()
    thread.join(timeout=1.0)

    assert seen == [(90, 24), (90, 25)]


def test_command_mode_entry():
    """Colon in normal mode enters command mode."""
    state = make_state(["hello"])
    state, effects = reduce_event(state, KeyEvent(":"))
    assert state.mode == Mode.COMMAND
    assert state.command == ""
    assert not effects


def test_command_mode_typing():
    """Characters typed in command mode accumulate in the command buffer."""
    state = make_state(["hello"])
    state, _ = reduce_event(state, KeyEvent(":"))
    state, _ = reduce_event(state, KeyEvent("w"))
    state, _ = reduce_event(state, KeyEvent("r"))
    assert state.command == "wr"


def test_command_mode_backspace():
    """Backspace in command mode removes the last character."""
    state = make_state(["hello"])
    for key in [":", "w", "r"]:
        state, _ = reduce_event(state, KeyEvent(key))
    state, _ = reduce_event(state, KeyEvent(Key.BACKSPACE))
    assert state.command == "w"
    assert state.mode == Mode.COMMAND


def test_command_mode_backspace_empty_exits():
    """Backspace on an empty command buffer exits command mode."""
    state = make_state(["hello"])
    state, _ = reduce_event(state, KeyEvent(":"))
    state, _ = reduce_event(state, KeyEvent(Key.BACKSPACE))
    assert state.mode == Mode.NORMAL
    assert state.command == ""


def test_command_escape_cancels():
    """ESC in command mode returns to normal without executing."""
    state = make_state(["hello"])
    state, _ = reduce_event(state, KeyEvent(":"))
    state, _ = reduce_event(state, KeyEvent("q"))
    state, _ = reduce_event(state, KeyEvent(Key.ESCAPE))
    assert state.mode == Mode.NORMAL
    assert state.should_quit is False


def test_command_write():
    """Write command emits SaveFile to the current path."""
    state = make_state(["hello"])
    state = replace(state, mode=Mode.COMMAND, command="w")
    next_state, effects = reduce_event(state, KeyEvent(Key.ENTER))
    assert next_state.mode == Mode.NORMAL
    assert len(effects) == 1
    assert isinstance(effects[0], SaveFile)
    assert effects[0].content == "hello"
    assert effects[0].path == pathlib.Path("sample.txt")


def test_command_write_prefix_forms():
    """All prefix forms of 'write' emit a SaveFile effect."""
    for verb in ("w", "wr", "wri", "writ", "write"):
        state = make_state(["hi"])
        state = replace(state, mode=Mode.COMMAND, command=verb)
        _, effects = reduce_event(state, KeyEvent(Key.ENTER))
        assert len(effects) == 1
        assert isinstance(effects[0], SaveFile), f"Failed for verb: {verb!r}"


def test_command_write_path():
    """Write with a path argument saves to that path."""
    state = make_state(["hello"])
    state = replace(state, mode=Mode.COMMAND, command="w /tmp/out.txt")
    next_state, effects = reduce_event(state, KeyEvent(Key.ENTER))
    assert effects[0].path == pathlib.Path("/tmp/out.txt")
    assert next_state.file_path == pathlib.Path("/tmp/out.txt")


def test_command_quit():
    """Quit command sets should_quit without saving."""
    state = make_state(["hello"])
    state = replace(state, mode=Mode.COMMAND, command="q")
    next_state, effects = reduce_event(state, KeyEvent(Key.ENTER))
    assert next_state.should_quit is True
    assert not effects


def test_command_quit_prefix_forms():
    """All prefix forms of 'quit' set should_quit."""
    for verb in ("q", "qu", "qui", "quit"):
        state = make_state(["hi"])
        state = replace(state, mode=Mode.COMMAND, command=verb)
        next_state, _ = reduce_event(state, KeyEvent(Key.ENTER))
        assert next_state.should_quit is True, f"Failed for verb: {verb!r}"


def test_command_edit_prefix_forms():
    """All prefix forms of 'edit' emit a LoadFile effect."""
    for verb in ("e", "ed", "edi", "edit"):
        state = make_state(["hi"])
        state = replace(state, mode=Mode.COMMAND, command=f"{verb} other.txt")
        _, effects = reduce_event(state, KeyEvent(Key.ENTER))
        assert len(effects) == 1
        assert isinstance(effects[0], LoadFile), f"Failed for verb: {verb!r}"
        assert effects[0].path == pathlib.Path("other.txt")


def test_command_wq():
    """wq command saves and quits in one step."""
    state = make_state(["hello"])
    state = replace(state, mode=Mode.COMMAND, command="wq")
    next_state, effects = reduce_event(state, KeyEvent(Key.ENTER))
    assert next_state.should_quit is True
    assert len(effects) == 1
    assert isinstance(effects[0], SaveFile)
    assert effects[0].content == "hello"


def test_render_command_line():
    """Command line is rendered below the status bar."""
    state = make_state(["hello"], size=(30, 6))
    state = replace(state, mode=Mode.COMMAND, command="wq")
    assert ":wq" in render(state)


def test_render_command_mode_cursor():
    """Command mode renders a bar cursor in the command line."""
    state = make_state(["hello"], size=(30, 6))
    state = replace(state, mode=Mode.COMMAND, command="")
    assert cursor(True, shape="bar", blink=False) in render(state)


def test_editor_save_quit(tmp_path, tmux): # pylint: disable=redefined-outer-name
    """End-to-end test for insert + save + quit via command-line mode."""
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello", encoding="utf-8")

    editor_path = pathlib.Path(__file__).resolve()

    tmux.resize(80, 24)
    tmux.send_keys(f"python3 {editor_path} {file_path}", Key.ENTER)

    assert tmux.wait_for("-- NORMAL --")

    tmux.send_keys("i", "X", Key.ESCAPE)
    assert tmux.wait_for("-- NORMAL --")

    tmux.send_keys(":")
    assert tmux.wait_for(":")
    tmux.send_keys("w", "q", Key.ENTER)
    time.sleep(0.2)

    assert file_path.read_text(encoding="utf-8") == "Xhello"
