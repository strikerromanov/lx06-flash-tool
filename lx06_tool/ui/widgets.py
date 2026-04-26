"""
lx06_tool/ui/widgets.py
------------------------
Shared Textual widgets used across multiple screens.

- LogPanel: Scrollable log output for command feedback
- PasswordInput: Masked sudo password entry
- StepProgress: Multi-step progress indicator
- ActionButton: Styled button with loading state
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Label, ProgressBar, RichLog, Static


class LogPanel(Widget):
    """Scrollable log panel for real-time command output.

    Provides a RichLog with color-coded output and auto-scroll.
    Also registers as a debug_log sink so all command activity
    is visible.
    """

    DEFAULT_CSS = """
    LogPanel {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    LogPanel > RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, *args, title: str = "Log", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._title = title

    def compose(self) -> ComposeResult:
        yield RichLog(id="log_output", highlight=True, markup=True)

    def on_mount(self) -> None:
        from lx06_tool.utils.debug_log import RichLogSink, register_sink
        log_widget = self.query_one(RichLog)
        self._sink = RichLogSink(log_widget)
        register_sink(self._sink)

    def on_unmount(self) -> None:
        from lx06_tool.utils.debug_log import unregister_sink
        if hasattr(self, "_sink"):
            unregister_sink(self._sink)

    def write(self, text: str) -> None:
        """Append text to the log panel."""
        try:
            log = self.query_one(RichLog)
            log.write(text)
        except Exception:
            pass

    def clear(self) -> None:
        """Clear the log panel."""
        try:
            self.query_one(RichLog).clear()
        except Exception:
            pass


class PasswordInput(Widget):
    """Sudo password input with submit support.

    Messages:
        PasswordSubmitted: Emitted when user presses Enter with a non-empty password.
    """

    class PasswordSubmitted(Message):
        """Password was submitted."""
        def __init__(self, password: str) -> None:
            super().__init__()
            self.password = password

    DEFAULT_CSS = """
    PasswordInput {
        height: auto;
        padding: 1 0;
    }
    PasswordInput > Horizontal {
        height: auto;
    }
    PasswordInput > Horizontal > Label {
        width: auto;
        padding: 1 1 0 0;
    }
    PasswordInput > Horizontal > Input {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Label("🔑 Sudo Password:")
            yield Input(
                placeholder="Enter your sudo password...",
                password=True,
                id="password_field",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "password_field" and event.value.strip():
            self.post_message(self.PasswordSubmitted(event.value.strip()))


class StepProgress(Widget):
    """Multi-step progress indicator.

    Shows a horizontal list of steps with current step highlighted.
    """

    DEFAULT_CSS = """
    StepProgress {
        height: auto;
        padding: 1 0;
    }
    StepProgress > Horizontal {
        height: auto;
    }
    .step-label {
        padding: 0 1;
    }
    .step-active {
        color: $success;
        text-style: bold;
    }
    .step-done {
        color: $success;
    }
    .step-pending {
        color: $text-disabled;
    }
    """

    def __init__(
        self,
        *args,
        steps: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._steps = steps or []
        self._current = 0

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, step in enumerate(self._steps):
                yield Label(step, classes=f"step-label step-pending", id=f"step-{i}")

    def set_current(self, index: int) -> None:
        """Update which step is current."""
        self._current = index
        for i in range(len(self._steps)):
            try:
                label = self.query_one(f"#step-{i}", Label)
                if i < index:
                    label.set_class(True, "step-done")
                    label.set_class(False, "step-active")
                    label.set_class(False, "step-pending")
                    label.update(f"✓ {self._steps[i]}")
                elif i == index:
                    label.set_class(False, "step-done")
                    label.set_class(True, "step-active")
                    label.set_class(False, "step-pending")
                    label.update(f"► {self._steps[i]}")
                else:
                    label.set_class(False, "step-done")
                    label.set_class(False, "step-active")
                    label.set_class(True, "step-pending")
                    label.update(f"  {self._steps[i]}")
            except Exception:
                pass

    @property
    def total_steps(self) -> int:
        return len(self._steps)

    @property
    def current_step(self) -> int:
        return self._current


class ActionButton(Widget):
    """Styled button with loading state."""

    class Pressed(Message):
        """Button was pressed."""
        def __init__(self, action_button: "ActionButton") -> None:
            super().__init__()
            self.action_button = action_button

    DEFAULT_CSS = """
    ActionButton {
        height: auto;
        padding: 1 0;
    }
    ActionButton > Button {
        width: 100%;
    }
    """

    def __init__(
        self,
        *args,
        label: str = "Action",
        variant: str = "primary",
        disabled: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._label = label
        self._variant = variant
        self._disabled = disabled

    def compose(self) -> ComposeResult:
        yield Button(
            self._label,
            variant=self._variant,  # type: ignore
            id="action_btn",
            disabled=self._disabled,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action_btn":
            self.post_message(self.Pressed(self))

    def set_loading(self, loading: bool) -> None:
        """Set loading state — disables button and updates label."""
        try:
            btn = self.query_one("#action_btn", Button)
            btn.disabled = loading
            btn.label = "⏳ Please wait..." if loading else self._label
        except Exception:
            pass

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the button."""
        try:
            self.query_one("#action_btn", Button).disabled = not enabled
        except Exception:
            pass


class StatusLabel(Widget):
    """Simple status text that updates reactively."""

    DEFAULT_CSS = """
    StatusLabel {
        height: auto;
        padding: 0 1;
    }
    """

    status_text: reactive[str] = reactive("")
    status_class: reactive[str] = reactive("")

    def __init__(self, *args, text: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.status_text = text

    def compose(self) -> ComposeResult:
        yield Label(self.status_text, id="status_text")

    def watch_status_text(self, new_text: str) -> None:
        try:
            self.query_one("#status_text", Label).update(new_text)
        except Exception:
            pass

    def watch_status_class(self, new_class: str) -> None:
        try:
            label = self.query_one("#status_text", Label)
            label.set_class(bool(new_class), new_class)
        except Exception:
            pass
