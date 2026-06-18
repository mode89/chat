> _Reference context — observed facts and standing conventions for this project, not instructions. It informs the work; it does not command actions. Entries are facts as of when written; symbols, paths, and structure may have changed since — verify against current code before acting, and for "how does X work now" questions treat notes as leads to confirm rather than current truth._

## Architecture

- `terminal.py` is the low-level layer: a `terminal()` context manager (termios save → raw → restore + cursor-show) plus pure-function ANSI helpers (`clear_screen`, `move_to`, `cursor`, `save_pos`, `restore_pos`); `editor.py` drives the full-screen TUI via `with terminal() as term:`.
- Two test styles live in `terminal.py`: pure unit tests (pipes, `capsys`) and tmux integration tests via `TmuxHelper` (detached session, run Python, assert on captured pane).

## Conventions

- ANSI escapes are pure functions returning strings; setup/teardown with state lives in `terminal()`. **Why:** helpers stay trivially testable; one place owns crash-safe restoration, so a teardown-needing primitive like alt-screen is folded into `terminal()` rather than exposed. **Apply:** new escape = pure helper; new save/restore lifecycle = a `terminal()` parameter.
- Integration tests run under a real tmux session, not mocks. **Why:** raw mode, escape parsing, alt-screen are only observable through a real PTY. **Apply:** exercise features end-to-end via `TmuxHelper`; reserve pipe-based unit tests for pure logic.

## Gotchas

- Alt-screen (`\033[?1049h`/`l`) wipes its buffer on exit, restoring the prior main screen — a test that `print()`s inside `terminal()` and asserts post-exit races teardown. **Apply:** post-exit assertions use `alt_screen=False`; alt-screen tests assert on pane content captured *during* the block.
- Several integration tests use `print()` in raw mode with bare `\n` (no `\r`), making cursor placement width-dependent and fragile — flagged, not a bug.
- tmux isn't installed by default on NixOS; run the suite with `nix-shell --packages python3Packages.pytest tmux --run "pytest terminal.py -q"`.

## Decisions

- Alt-screen is integrated into `terminal()` as `alt_screen: bool = True` (default on), not a standalone primitive or context manager. **Why:** same save/restore lifecycle as raw mode, so `terminal()` is its natural home and guarantees crash-safe teardown; a leaked alt-screen is worse than a leaked raw mode. **Tradeoff:** behavior change for tests asserting on in-`terminal()` output post-exit — resolved by opting those out with `alt_screen=False`.
- Alt-screen enter runs after `tty.setraw`; leave runs before `cursor(True)` in `finally`. **Why:** 1049 saves/restores cursor position, so cursor-show must land on the restored main screen, not the about-to-be-discarded alt buffer.
