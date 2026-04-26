"""lx06_tool/ui/widgets/debug_log.py
----------------------------------
Collapsible global debug/command-history panel.

Docks at the bottom of the main app.  Toggle visibility with **Ctrl+D**.

Features:
* Timestamped log of every command, result, and error
* Copy All button → system clipboard (xclip / wl-copy / temp-file fallback)
* Color-coded tags: CMD (cyan), OK (green), ERR (red), INFO (yellow)
* Persists across screen transitions (lives in the app, not per screen)
"""

from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, RichLog, Static

from lx06_tool.utils.debug_log import register_sink, unregister_sink

# ─── Styles ──────────────────────────────────────────────────────────────────

DEBUG_PANEL_CSS = """
DebugLogPanel {
    dock: bottom;
    height: auto;
    max-height: 60vh;
    min-height: 0;
    background: $surface-darken-1;
    border-top: tall $accent;
    padding: 0;
    margin: 0;
}

DebugLogPanel.hidden-panel {
    height: 0;
    min-height: 0;
    max-height: 0;
    border-top: none;
    overflow: hidden;
    display: none;
}

#debug-toolbar {
    height: 3;
    background: $primary-darken-2;
    padding: 0 1;
}

#debug-toolbar Static {
    color: $text-muted;
    padding: 0 1;
    width: auto;
}

#debug-toolbar Button {
    margin: 0 1;
    min-width: 0;
}

#debug-richlog {
    height: 20;
    max-height: 50vh;
    min-height: 5;
    scrollbar-size: 1 1;
    padding: 0 1;
    background: $surface-darken-2;
}
"""


class DebugLogPanel(Widget):
    """Global debug log panel — always present, toggle with Ctrl+D."""

    DEFAULT_CSS = DEBUG_PANEL_CSS

    # Exposed for the app to bind Ctrl+D
    BINDINGS = [
        Binding("ctrl+d", "toggle_panel", "Debug Log", show=False),
    ]

    # Reactive visibility so we can animate / style
    panel_visible: reactive[bool] = reactive(False)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(*children, name=name, id=id, classes=classes)
        self._line_count: int = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="debug-toolbar"):
            yield Static("🐛 Debug Log", id="debug-label")
            yield Button(
                "📋 Copy All", variant="primary", id="debug-copy-btn"
            )
            yield Button(
                "✕ Close", variant="default", id="debug-close-btn"
            )
        yield RichLog(
            id="debug-richlog",
            highlight=True,
            markup=True,
            auto_scroll=True,
            wrap=False,
        )

    def on_mount(self) -> None:
        """Register as the global debug sink."""
        register_sink(self)
        self._apply_visibility()

    def on_unmount(self) -> None:
        """Unregister the global debug sink."""
        unregister_sink(self)

    # ── DebugSink protocol ──────────────────────────────────────────────────

    def write_line(self, tag: str, message: str) -> None:
        """Receive a log line from the global debug_log module."""
        try:
            log = self.query_one("#debug-richlog", RichLog)
        except NoMatches:
            return

        color = _TAG_COLORS.get(tag, "white")
        # Write with markup for color-coded tags
        log.write(f"[{color}][{tag}][/] {message}")
        self._line_count += 1

        # Update label with count
        try:
            label = self.query_one("#debug-label", Static)
            label.update(f"🐛 Debug Log ({self._line_count} entries)")
        except NoMatches:
            pass

    def get_all_text(self) -> str:
        """Return the full log as plain text (for clipboard copy)."""
        try:
            log = self.query_one("#debug-richlog", RichLog)
            lines: list[str] = []
            for child in log._lines:
                try:
                    lines.append(child.plain if hasattr(child, "plain") else str(child))
                except Exception:
                    lines.append(str(child))
            return "\n".join(lines) if lines else "(empty debug log)"
        except NoMatches:
            return "(debug log not available)"

    # ── Toggle / Visibility ─────────────────────────────────────────────────

    def toggle_panel(self) -> None:
        """Toggle panel visibility."""
        self.panel_visible = not self.panel_visible
        self._apply_visibility()

    def show_panel(self) -> None:
        """Show the debug panel."""
        self.panel_visible = True
        self._apply_visibility()

    def hide_panel(self) -> None:
        """Hide the debug panel."""
        self.panel_visible = False
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        """Apply the visibility state to CSS classes."""
        if self.panel_visible:
            self.remove_class("hidden-panel")
        else:
            self.add_class("hidden-panel")

    # ── Reactive watcher ────────────────────────────────────────────────────

    def watch_panel_visible(self, visible: bool) -> None:
        """Called when panel_visible changes."""
        self._apply_visibility()

    # ── Button handlers ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "debug-copy-btn":
            self._copy_to_clipboard()
        elif btn_id == "debug-close-btn":
            self.hide_panel()

    # ── Clipboard ───────────────────────────────────────────────────────────

    def _copy_to_clipboard(self) -> None:
        """Copy the full debug log to the system clipboard."""
        text = self.get_all_text()
        try:
            # Try xclip (X11)
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                check=True,
                capture_output=True,
            )
            self.app.notify("Debug log copied to clipboard!", severity="information")
        except FileNotFoundError:
            try:
                # Try wl-copy (Wayland)
                subprocess.run(
                    ["wl-copy"],
                    input=text.encode(),
                    check=True,
                    capture_output=True,
                )
                self.app.notify("Debug log copied to clipboard!", severity="information")
            except FileNotFoundError:
                # Fallback: write to temp file
                import tempfile
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, prefix="lx06-debug-"
                )
                tmp.write(text)
                tmp.close()
                self.app.notify(
                    f"No clipboard tool. Log saved to: {tmp.name}",
                    severity="warning",
                )
        except Exception as exc:
            self.app.notify(f"Copy failed: {exc}", severity="error")


# ─── Tag colors ──────────────────────────────────────────────────────────────

_TAG_COLORS: dict[str, str] = {
    "CMD": "cyan",
    "OK": "green",
    "ERR": "red",
    "INFO": "yellow",
}
