# Specification: Low-Level Terminal AI Assistant ("Raw" Edition)

## 1. Core Architecture

- **Language:** Python 3.x (Standard Library focus).
- **Terminal Control:** Low-level `sys`, `tty`, and `termios` for raw input/output.
- **Rendering:** Virtual Screen/Grid model using ANSI Escape Sequences for cursor manipulation.
- **Configuration:** API Credentials and Model parameters fetched via Environment Variables.

## 2. User Interface Design

- **Visual Layout:**
  - **Header/History Area:** Top section of the terminal; displays previous exchanges.
  - **Separator:** A static line of dashes (`----------`) clearly dividing history from input.
  - **Input Area:** Bottom section with 1-character padding.
- **Dynamic Input:** The input area supports multi-line text. As the user types past the terminal width, the input expands upward, dynamically pushing the separator and history window higher.
- **Response Style:** Non-streaming. The AI response is processed and then rendered as a complete block to ensure UI stability.

## 3. Interaction & Control Scheme

- **Navigation (Vim-style):**
  - `Ctrl-K`: Scroll History Up.
  - `Ctrl-J`: Scroll History Down.
- **Input Editing:**
  - `Ctrl-U`: Clear the entire current input buffer.
  - `Enter`: Submit the prompt to the AI.
- **Exit:** `Ctrl-C` (Standard Interrupt) to restore terminal settings and exit.

## 4. Technical Logic Requirements

- **Raw Mode Management:** - The script must save original terminal `termios` settings on startup.
  - It must enter `tty.setraw` mode to capture individual keystrokes (like `Ctrl-K`).
  - It MUST implement a `try...finally` block to restore terminal settings on exit/crash.
- **State Management:**
  - **Full Session Memory:** A persistent list/buffer of the entire conversation sent with every request.
  - **Virtual Scroll Offset:** An integer tracking the current "view" into the message history.
- **ANSI Engine:**
  - `\033[H`: Move cursor to home.
  - `\033[J`: Clear screen.
  - `\033[<L>;<C>H`: Move cursor to specific Line and Column for UI drawing.
- **Text Wrapping:** Manual calculation of string lengths vs. terminal columns (`os.get_terminal_size()`) to prevent text from "bleeding" into the separator line.

## 5. Feature Exclusions

- No Slash Commands (Pure Chat).
- No External TUI Libraries (Textual, Curses, etc.).
- No Streaming text (Block-based updates only).
