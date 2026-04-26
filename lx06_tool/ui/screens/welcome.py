"""
lx06_tool/ui/screens/welcome.py
--------------------------------
Welcome screen with prerequisite checks.

Shows system requirements and a "Start Setup" button to begin.
"""

from __future__ import annotations

import platform
import shutil
import sys
from typing import NamedTuple

from rich.panel import Panel
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Label, Static

from lx06_tool.ui.widgets import ActionButton


class PrereqCheck(NamedTuple):
    """Result of a single prerequisite check."""
    name: str
    passed: bool
    detail: str


def _check_prerequisites() -> list[PrereqCheck]:
    """Run all prerequisite checks and return results."""
    results: list[PrereqCheck] = []

    # --- Linux OS ---
    is_linux = platform.system() == "Linux"
    results.append(PrereqCheck(
        name="Linux Operating System",
        passed=is_linux,
        detail=platform.system() if not is_linux else "Detected",
    ))

    # --- Python 3.10+ ---
    py_ver = sys.version_info
    py_ok = py_ver >= (3, 10)
    results.append(PrereqCheck(
        name="Python 3.10+",
        passed=py_ok,
        detail=f"{'Detected' if py_ok else 'Found'} {py_ver.major}.{py_ver.minor}.{py_ver.micro}",
    ))

    # --- git ---
    git_found = shutil.which("git") is not None
    results.append(PrereqCheck(
        name="git",
        passed=git_found,
        detail="Found" if git_found else "Not found — install git",
    ))

    # --- USB port availability (lsusb presence as proxy) ---
    lsusb_found = shutil.which("lsusb") is not None
    results.append(PrereqCheck(
        name="USB tools (lsusb)",
        passed=lsusb_found,
        detail="Found" if lsusb_found else "Not found — install usbutils",
    ))

    return results


class WelcomeScreen(Screen):
    """Welcome screen with title, description, and prerequisite checks."""

    DEFAULT_CSS = """
    WelcomeScreen {
        align: center middle;
    }
    WelcomeScreen > VerticalScroll {
        width: 72;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 2 4;
    }
    WelcomeScreen .title {
        text-align: center;
        text-style: bold;
        color: $primary;
        padding: 1 0;
    }
    WelcomeScreen .subtitle {
        text-align: center;
        color: $text-muted;
        padding: 0 0 1 0;
    }
    WelcomeScreen .prereq-item {
        padding: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("🔧 LX06 Flash Tool", classes="title")
            yield Label(
                "Flash custom firmware to your Xiaomi LX06 (Mi AI) speaker.\n"
                "This tool will guide you through backing up, downloading,\n"
                "and flashing patched firmware to enable AirPlay, Spotify,\n"
                "and other custom features.",
                classes="subtitle",
            )
            yield Static(id="prereq_list")
            with Center():
                yield ActionButton(
                    label="Start Setup",
                    variant="success",
                    id="start_btn",
                )

    def on_mount(self) -> None:
        """Run prerequisite checks on mount."""
        self._refresh_prereqs()

    def _refresh_prereqs(self) -> None:
        """Render prerequisite check results."""
        checks = _check_prerequisites()
        lines: list[str] = []
        all_passed = True

        for check in checks:
            icon = "✅" if check.passed else "❌"
            lines.append(f"{icon}  {check.name} — {check.detail}")
            if not check.passed:
                all_passed = False

        header = "[bold]Prerequisite Checks[/bold]\n"
        if all_passed:
            header += "[green]All checks passed![/green]"
        else:
            header += "[red]Some checks failed. Fix them before continuing.[/red]"

        try:
            prereq_widget = self.query_one("#prereq_list", Static)
            prereq_widget.update(header + "\n" + "\n".join(lines))
        except Exception:
            pass

        try:
            start_btn = self.query_one("#start_btn", ActionButton)
            start_btn.set_enabled(all_passed)
        except Exception:
            pass

    def on_action_button_pressed(self, event: ActionButton.Pressed) -> None:
        """Handle Start Setup button press."""
        btn = event.action_button
        if btn.id == "start_btn":
            self.app.push_screen("environment")
