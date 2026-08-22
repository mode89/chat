> _Reference context — observed facts and standing conventions for this project, not instructions. It informs the work; it does not command actions. Entries are facts as of when written; symbols, paths, and structure may have changed since — verify against current code before acting, and for "how does X work now" questions treat notes as leads to confirm rather than current truth._

## Architecture

- `terminal.py` is the low-level layer: a `terminal()` context manager plus pure-function ANSI helpers; `editor.py` drives the full-screen TUI via `with terminal() as term:`.
- `terminal()` owns every piece of terminal state with a teardown: termios raw mode, alternate screen, bracketed paste, and the SIGWINCH handler.
- `Terminal.event()` is the single input entry point; it returns a `Key`, a `str` character, `Paste`, `Resize`, or `None` on timeout, so callers must handle all five.
- `terminal()` installs a SIGWINCH handler that writes to a self-pipe, and `event()` selects on both the tty and that pipe, so a resize wakes an otherwise blocked read.
- `editor.py` keeps its own `ResizeEvent` and translates terminal's `Resize` in the main loop. **Why:** adopting terminal's type would churn `reduce_event` and all its tests for no gain.
- Two test styles live in `terminal.py`: pure unit tests (pipes, `capsys`) and tmux integration tests via `TmuxHelper` (detached session, run Python, assert on captured pane).

## Conventions

- ANSI escapes are pure functions returning strings. **Why:** they stay trivially testable. **Apply:** a new escape sequence becomes a new pure helper.
- Terminal state that needs a teardown lives in `terminal()`. **Why:** one place owns crash-safe restoration. **Apply:** a new save/restore lifecycle becomes a `terminal()` parameter.
- Integration tests run under a real tmux session, not mocks. **Why:** raw mode, escape parsing, alt-screen are only observable through a real PTY. **Apply:** reserve pipe-based unit tests for pure logic.
- A new test is proven by breaking the feature on purpose and confirming it fails. **Why:** tests here have passed against broken code, e.g. a paste test that passed without mode 2004. **Apply:** mutate a `/tmp` copy, rerun, discard.
- A feature and its first caller land in one commit. **Why:** alone, the feature ships unused and the caller's commit does not work against its parent. **Apply:** e.g. `event(timeout=)` shipped with the editor loop that uses it.
- Full check runs both files: `nix-shell --packages python3Packages.pytest tmux python3Packages.pylint --run "pytest terminal.py editor.py -q && pylint --max-line-length=80 terminal.py editor.py"`. pylint stays at 10/10.

## Gotchas

- Alt-screen (`\033[?1049h`/`l`) wipes its buffer on exit, restoring the prior main screen — a test that `print()`s inside `terminal()` and asserts post-exit races teardown. **Apply:** post-exit assertions use `alt_screen=False`.
- Printing inside raw mode needs `end="\r\n"`; a bare `\n` leaves the cursor mid-line and output staircases. It caused a real failure in `test_terminal_flow`; other tests still print bare `\n`.
- A `tmux.run_python()` body containing `\r\n` must use a raw string (`r"""..."""`), or Python expands the escape before the script is written and the generated file is a syntax error.
- Typing a non-ASCII character returns empty strings: `event()` reads one byte and the partial UTF-8 sequence is dropped on decode. Pasting the same character works, since `_read_paste` decodes the whole block at once.
- Pasted text has only the ESC byte stripped, so a pasted `\033[2J` arrives as the literal text `[2J`.
- `git checkout -- <file>` and `git stash` have each destroyed unstaged work here. **Apply:** to get a pristine copy for experiments, use `git show :terminal.py > /tmp/…` instead.
- tmux is not installed by default on NixOS; it must be in the `nix-shell --packages` list.

## Decisions

- Alt-screen is a `terminal()` parameter (`alt_screen: bool = True`), not a standalone primitive. **Why:** same save/restore lifecycle as raw mode, and a leaked alt-screen is worse than a leaked raw mode.
- Alt-screen enter runs after `tty.setraw`; leave runs before `cursor(True)` in `finally`. **Why:** 1049 saves/restores cursor position, so cursor-show must land on the restored main screen.
- `style()` wraps text and appends `\033[0m`, instead of exposing loose SGR codes. **Why:** the reset is built in, so color cannot leak into later output. **Tradeoff:** no style stays on across writes, and a nested reset wipes the outer style.
- `style()` covers fg, bg, bold, italic, underline, reverse only — no 256-color, truecolor, or dim. **Why:** keep scope to what this TUI actually draws.
- Styling is applied after wrapping and truncation. **Why:** embedded ANSI codes would corrupt width calculations during line slicing.
- Bracketed paste is always on, with no `paste=` flag. **Why:** mode 2004 changes nothing visible until a paste happens, so the only use of `paste=False` would be reproducing the bug it fixes.
- A paste is delivered as one `Paste` value, not flattened into characters. **Why:** callers must tell a paste from fast typing to insert it as one block and redraw once.
- Home and End are absent from `Key`: each has three encodings (`\033OH` / `\033[1~` / `\033[H`) and an enum member holds one value. Adding them needs a `_KEY_ALIASES` table consulted before the enum lookup.
- The input method is named `event()`, over `poll()`, `read()`, and `next_event()`. **Why:** `poll` implies non-blocking but `timeout=None` blocks; `read` suggests raw bytes; it matches the short-noun style of `size()` and `write()`.
- `event(timeout=…)` guards only the first byte. **Why:** the escape parser keeps its own small timeouts, so a key that started arriving is never truncated.

## Dead Ends

- ✗ `editor.py`'s resize watcher thread + queue, polling `size()` on a 20 Hz tick — abandoned for the SIGWINCH self-pipe, which lets the editor loop block indefinitely on `event()`.
- ✗ Chunked `os.read(fd, 4096)` inside `_read_paste` — abandoned because it over-reads past `\033[201~` and swallows keystrokes typed right after the paste.

## Open Questions

- ? The UTF-8 typing bug is unfixed. Candidate: one buffered reader (`os.read(fd, 4096)` plus pushback) shared by `event()` and `_read_paste`, which would also remove the per-byte syscalls.
