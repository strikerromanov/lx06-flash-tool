"""
lx06_tool/ui/screens/complete.py
---------------------------------
Success / completion screen shown after flashing finishes.

Displays a summary of what was done and instructions for reassembly.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Label, Static

from lx06_tool.ui.widgets import ActionButton


class CompleteScreen(Screen):
    """Final success screen with summary and exit button."""

    DEFAULT_CSS = """
    CompleteScreen {
        align: center middle;
    }
    CompleteScreen > VerticalScroll {
        width: 72;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 2 4;
    }
    CompleteScreen .success-title {
        text-align: center;
        text-style: bold;
        color: $success;
        padding: 1 0;
    }
    CompleteScreen .section-label {
        text-style: bold;
        color: $primary;
        padding: 1 0 0 0;
    }
    CompleteScreen .info-text {
        color: $text;
        padding: 0 1;
    }
    CompleteScreen .instruction {
        color: $warning;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("🎉 Firmware Flash Complete!", classes="success-title")
            yield Static(id="summary")
            yield Label("Next Steps", classes="section-label")
            yield Label(
                "1. Disconnect the USB cable from the speaker.\n"
                "2. Carefully reassemble the speaker chassis.\n"
                "3. Reconnect the two internal cables.\n"
                "4. Replace the 6 screws under the bottom rubber cap.\n"
                "5. Power on the speaker and enjoy your custom firmware!",
                classes="info-text",
            )
            yield Label(
                "⚠ If the speaker does not boot, reconnect via USB and\n"
                "  re-flash. Your original backup is safe in the backup directory.",
                classes="instruction",
            )
            with Center():
                yield ActionButton(
                    label="Exit",
                    variant="primary",
                    id="exit_btn",
                )

    def on_mount(self) -> None:
        """Populate summary from app config on mount."""
        try:
            config = self.app.config  # type: ignore[attr-defined]
            backup_dir = str(config.backup_dir)

            lines: list[str] = [
                "[bold]Summary[/bold]\n",
                f"  📁 Backup location: {backup_dir}",
            ]

            # Show firmware version if available
            device = config.device
            if device.firmware_version and device.firmware_version != "unknown":
                lines.append(
                    f"  📋 Original firmware: {device.firmware_version}"
                )

            # Show backup status
            backup = config.backup
            if backup.timestamp:
                lines.append(f"  🕐 Backup timestamp: {backup.timestamp}")
            if backup.backup_dir:
                lines.append(f"  💾 Backup saved to: {backup.backup_dir}")

            lines.append("")
            lines.append("[green]All partitions backed up and new firmware flashed.[/green]")

            summary_widget = self.query_one("#summary", Static)
            summary_widget.update("\n".join(lines))
        except Exception as exc:
            try:
                self.query_one("#summary", Static).update(
                    f"Firmware flash completed successfully.\n(Could not load details: {exc})"
                )
            except Exception:
                pass

    def on_action_button_pressed(self, event: ActionButton.Pressed) -> None:
        """Handle Exit button press."""
        btn = event.action_button
        if btn.id == "exit_btn":
            self.app.exit(0)
