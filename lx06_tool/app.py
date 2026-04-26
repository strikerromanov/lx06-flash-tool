"""
lx06_tool/app.py
----------------
Main Textual TUI application for the LX06 Flash Tool.

Wires together all screens and provides shared state:
  - AppConfig (loaded from XDG config dir)
  - SudoContext (sudo password management)
  - AmlogicTool (lazy-initialized USB tool wrapper)

Entry point: ``lx06-tool`` console script (defined in pyproject.toml).
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding

from lx06_tool.config import AppConfig
from lx06_tool.constants import (
    AML_FLASH_TOOL_DIR,
    UPDATE_EXE_RELPATH,
)
from lx06_tool.ui.screens.backup_flash import BackupFlashScreen
from lx06_tool.ui.screens.complete import CompleteScreen
from lx06_tool.ui.screens.environment import EnvironmentScreen
from lx06_tool.ui.screens.usb_connect import USBConnectScreen
from lx06_tool.ui.screens.welcome import WelcomeScreen
from lx06_tool.utils.amlogic import AmlogicTool
from lx06_tool.utils.sudo import SudoContext


class LX06App(App):
    """LX06 Flash Tool — Textual TUI application.

    Screens navigate by calling ``self.app.push_screen("next_screen")``.
    Each screen accesses ``self.app.config``, ``self.app.sudo_ctx``,
    ``self.app.get_aml_tool()``.
    """

    TITLE = "LX06 Flash Tool"
    SUB_TITLE = "Flash custom firmware to Xiaomi LX06 speaker"

    CSS = """
    Screen {
        align: center middle;
    }
    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    SCREENS = {
        "welcome": WelcomeScreen,
        "environment": EnvironmentScreen,
        "usb_connect": USBConnectScreen,
        "backup_flash": BackupFlashScreen,
        "complete": CompleteScreen,
    }

    def __init__(self) -> None:
        super().__init__()
        self.config = AppConfig.load()
        self.config.ensure_dirs()
        self.sudo_ctx: SudoContext = SudoContext()
        self._aml_tool: AmlogicTool | None = None

    def on_mount(self) -> None:
        """Show the welcome screen on startup."""
        self.push_screen("welcome")

    def get_aml_tool(self) -> AmlogicTool:
        """Get or create the AmlogicTool instance.

        Lazily resolves the update binary path on first call.

        Returns:
            Configured AmlogicTool instance.

        Raises:
            FileNotFoundError: If the update binary cannot be found.
        """
        if not self._aml_tool:
            update_path = self._resolve_update_path()
            self._aml_tool = AmlogicTool(update_path, self.sudo_ctx)
        return self._aml_tool

    def _resolve_update_path(self) -> str:
        """Resolve the path to the Amlogic update binary.

        Search order:
          1. config.update_exe_path (saved from previous run)
          2. tools_dir/aml-flash-tool/tools/linux-x86/update

        Returns:
            Absolute path to the update binary.

        Raises:
            FileNotFoundError: If no valid binary is found.
        """
        candidates: list[Path] = []

        # 1. Saved path from config
        if self.config.update_exe_path:
            candidates.append(self.config.update_exe_path)

        # 2. XDG tools directory
        xdg_update = self.config.tools_dir / AML_FLASH_TOOL_DIR / UPDATE_EXE_RELPATH
        candidates.append(xdg_update)

        # 3. Also check for update.exe symlink
        xdg_update_exe = xdg_update.parent / "update.exe"
        candidates.append(xdg_update_exe)

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                resolved = str(candidate.resolve())
                # Cache for next time
                self.config.update_exe_path = candidate
                return resolved

        # Build helpful error message
        tried = "\n  - ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"Amlogic update binary not found. Tried:\n  - {tried}\n\n"
            f"Run the Environment Setup step first to download aml-flash-tool."
        )


def main() -> None:
    """Entry point for the lx06-tool console script."""
    app = LX06App()
    app.run()


if __name__ == "__main__":
    main()
