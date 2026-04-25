"""LX06 Flash Tool — Main Textual Application.

Orchestrates the complete workflow through a series of screens:
  Welcome -> Environment -> USB Connect -> Backup -> Customize -> Build -> Flash -> Complete

Each screen drives one or more backend modules and reports progress
through shared callbacks. The app holds module instances and
passes them to screens as needed.

Usage:
    lx06-tool                # Launch the TUI
    lx06-tool --check        # Run environment check only
    lx06-tool --backup-only  # Only dump partitions (no flash)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Footer,
    Header,
    Label,
    ProgressBar,
    Static,
)

from lx06_tool.config import (
    AppConfig,
    CustomizationChoices,
    LX06Device,
)
from lx06_tool.state import StateMachine
from lx06_tool.utils.amlogic import AmlogicTool

logger = logging.getLogger(__name__)


# ── Flash Result (used by complete screen) ──────────────────────────────────

@dataclass
class FlashResult:
    """Result of the flash operation."""
    success: bool = False
    boot_flashed: bool = False
    system_flashed: bool = False
    verified: bool = False
    duration_sec: float = 0.0
    errors: list[str] = field(default_factory=list)


APP_CSS = """
Screen {
    background: $surface;
    padding: 1 2;
}

#main-container {
    width: 100%;
    height: 100%;
}

#phase-title {
    text-style: bold;
    color: $accent;
    text-align: center;
    padding: 1;
}

#phase-subtitle {
    color: $text-muted;
    text-align: center;
    padding: 0 0 1 0;
}

#content {
    height: 1fr;
    overflow-y: auto;
    padding: 1 2;
}

#status-bar {
    dock: bottom;
    height: 3;
    background: $primary-darken-1;
    padding: 0 2;
}

#status-text {
    color: $text;
    padding: 0 1;
}

#global-progress {
    height: 1;
}
"""


class LX06App(App):
    """Xiaomi LX06 Smart Speaker Flash Tool.

    A Textual TUI that guides users through:
    1. Environment setup (dependencies, Docker, USB rules)
    2. USB connection & device handshake
    3. Bootloader unlock & partition backup
    4. Firmware customization (debloat, media, AI)
    5. Flashing the custom firmware to the device
    """

    TITLE = "LX06 Flash Tool"
    SUB_TITLE = "Xiaomi Xiaoai Speaker Pro — Custom Firmware Installer"

    CSS = APP_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("d", "toggle_dark", "Dark mode", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    # ── Reactive State ───────────────────────────────────────────────────────

    current_phase: reactive[str] = reactive("welcome")
    status_message: reactive[str] = reactive("Ready")
    log_output: reactive[str] = reactive("")

    # ── Constructor ───────────────────────────────────────────────────────────

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._config = AppConfig.load()
        self._state_machine = StateMachine()

        # Module instances (initialized lazily)
        self._aml_tool: AmlogicTool | None = None

        # Runtime state
        self._device: LX06Device | None = None
        self._choices = CustomizationChoices()
        self._flash_result: FlashResult | None = None

        # Environment state (populated by environment screen)
        self._os_info: Any | None = None  # OSInfo from detect_os()
        self._dep_statuses: list[Any] = []  # list[DependencyStatus]
        self._docker_ok: bool = False

        self._screens_loaded = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Create the app layout."""
        yield Header(show_clock=True)
        yield Container(
            Vertical(
                Static(self.TITLE, id="phase-title"),
                Static("", id="phase-subtitle"),
                Vertical(id="content"),
                Vertical(
                    Static(self.status_message, id="status-text"),
                    ProgressBar(total=100, id="global-progress"),
                    id="status-bar",
                ),
            ),
            id="main-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Initialize on first mount."""
        self._load_screens()
        self._push_welcome_screen()

    # ── Screen Management ─────────────────────────────────────────────────────

    def _load_screens(self) -> None:
        """Register all screens with the app."""
        if self._screens_loaded:
            return

        from lx06_tool.ui.screens.welcome import WelcomeScreen
        from lx06_tool.ui.screens.environment import EnvironmentScreen
        from lx06_tool.ui.screens.usb_connect import USBConnectScreen
        from lx06_tool.ui.screens.backup import BackupScreen
        from lx06_tool.ui.screens.customize import CustomizeScreen
        from lx06_tool.ui.screens.build import BuildScreen
        from lx06_tool.ui.screens.flash import FlashScreen
        from lx06_tool.ui.screens.complete import CompleteScreen

        screen_map = {
            "welcome": WelcomeScreen,
            "environment": EnvironmentScreen,
            "usb_connect": USBConnectScreen,
            "backup": BackupScreen,
            "customize": CustomizeScreen,
            "build": BuildScreen,
            "flash": FlashScreen,
            "complete": CompleteScreen,
        }

        for name, screen_cls in screen_map.items():
            self.install_screen(screen_cls(), name=name)

        self._screens_loaded = True

    def _push_welcome_screen(self) -> None:
        """Show the welcome screen."""
        self.push_screen("welcome")

    async def _go_to_screen(self, screen_name: str) -> None:
        """Transition to a named screen."""
        logger.info("Navigating to screen: %s", screen_name)
        self.current_phase = screen_name
        self._update_phase_display(screen_name)

        # Pop all screens and push the target
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen_name)

    def _update_phase_display(self, phase: str) -> None:
        """Update the header/subtitle for the current phase."""
        titles = {
            "welcome": ("LX06 Flash Tool", "Welcome"),
            "environment": ("Phase 1: Environment", "Checking & installing dependencies"),
            "usb_connect": ("Phase 1: USB Connection", "Connect your LX06 via USB"),
            "backup": ("Phase 2: Backup", "Dumping & verifying partitions"),
            "customize": ("Phase 3: Customize", "Select features for your custom firmware"),
            "build": ("Phase 3: Build", "Building custom firmware image"),
            "flash": ("Phase 4: Flash", "Writing firmware to device"),
            "complete": ("Complete!", "All done"),
        }
        title, subtitle = titles.get(phase, (self.TITLE, ""))
        try:
            self.query_one("#phase-title", Static).update(title)
            self.query_one("#phase-subtitle", Static).update(subtitle)
        except Exception:
            pass

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def device(self) -> LX06Device | None:
        return self._device

    @device.setter
    def device(self, value: LX06Device) -> None:
        self._device = value

    @property
    def choices(self) -> CustomizationChoices:
        return self._choices

    @choices.setter
    def choices(self, value: CustomizationChoices) -> None:
        self._choices = value

    @property
    def flash_result(self) -> FlashResult | None:
        return self._flash_result

    @flash_result.setter
    def flash_result(self, value: FlashResult) -> None:
        self._flash_result = value

    @property
    def os_info(self) -> Any | None:
        return self._os_info

    @os_info.setter
    def os_info(self, value: Any) -> None:
        self._os_info = value

    @property
    def dep_statuses(self) -> list[Any]:
        return self._dep_statuses

    @dep_statuses.setter
    def dep_statuses(self, value: list[Any]) -> None:
        self._dep_statuses = value

    @property
    def docker_ok(self) -> bool:
        return self._docker_ok

    @docker_ok.setter
    def docker_ok(self, value: bool) -> None:
        self._docker_ok = value

    # ── Module Accessors ──────────────────────────────────────────────────────

    def get_aml_tool(self) -> AmlogicTool:
        """Get or create the AmlogicTool instance."""
        if self._aml_tool is None:
            aml_path = self._config.update_exe_path
            if not aml_path or not Path(aml_path).exists():
                aml_path = Path("/usr/local/bin/aml-flash-tool/update")
            self._aml_tool = AmlogicTool(update_exe=aml_path)
        return self._aml_tool

    def get_firmware_pipeline(self) -> Any:
        """Get the firmware build pipeline.

        Note: FirmwareOrchestrator is not part of the reviewed module set.
        This accessor provides a compatibility shim.
        """
        from lx06_tool.modules.firmware import FirmwareOrchestrator, FirmwarePaths

        build_dir = self._config.build_dir
        backup_dir = self._config.backup_dir
        paths = FirmwarePaths(
            system_dump=backup_dir / "mtd4_system0.img",
            boot_dump=backup_dir / "mtd2_boot0.img",
            extract_dir=build_dir / "extracted",
            rootfs_dir=build_dir / "extracted" / "squashfs-root",
            output_dir=build_dir / "output",
            output_system=build_dir / "output" / "root.squashfs",
            output_boot=build_dir / "output" / "boot.img",
        )
        return FirmwareOrchestrator(
            paths=paths,
            choices=self._choices,
        )

    # ── Global Progress ───────────────────────────────────────────────────────

    def update_status(self, message: str) -> None:
        """Update the status bar message."""
        self.status_message = message
        try:
            self.query_one("#status-text", Static).update(message)
        except Exception:
            pass

    def update_progress(self, pct: float) -> None:
        """Update the global progress bar (0-100)."""
        try:
            bar = self.query_one("#global-progress", ProgressBar)
            bar.update(progress=int(pct))
        except Exception:
            pass

    def append_log(self, stream: str, text: str) -> None:
        """Append text to the log output."""
        self.log_output += text + "\n"
        lines = self.log_output.split("\n")
        if len(lines) > 200:
            self.log_output = "\n".join(lines[-200:])

    # ── Navigation Callbacks (used by screens) ────────────────────────────────

    async def on_environment_done(self, success: bool) -> None:
        if success:
            await self._go_to_screen("usb_connect")
        else:
            self.update_status("Environment setup failed. Check logs above.")

    async def on_usb_connected(self, device: LX06Device) -> None:
        self._device = device
        await self._go_to_screen("backup")

    async def on_backup_done(self, success: bool) -> None:
        if success:
            await self._go_to_screen("customize")
        else:
            self.update_status("Backup failed. Do NOT proceed without a backup!")

    async def on_customize_done(self, choices: CustomizationChoices) -> None:
        self._choices = choices
        await self._go_to_screen("build")

    async def on_build_done(self, success: bool) -> None:
        if success:
            await self._go_to_screen("flash")
        else:
            self.update_status("Firmware build failed. Check logs above.")

    async def on_flash_done(self, result: FlashResult) -> None:
        self._flash_result = result
        await self._go_to_screen("complete")

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_quit(self) -> None:
        logger.info("User requested quit")
        self.exit()

    def action_refresh(self) -> None:
        self.screen.refresh()


# ── Entry Point ──────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="lx06-tool",
        description="LX06 Flash Tool — Xiaomi Xiaoai Speaker Pro Custom Firmware Installer",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Run environment check only (no TUI)",
    )
    parser.add_argument(
        "--backup-only", action="store_true",
        help="Only dump partitions (no flash)",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config file",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.check:
        # Non-interactive environment check using new standalone functions
        import asyncio as _aio
        from lx06_tool.modules.environment import (
            detect_os,
            check_dependencies,
            verify_docker,
        )

        async def _check() -> bool:
            try:
                os_info = detect_os()
                print(f"OS: {os_info.name} ({os_info.id})")
                print(f"Family: {os_info.family}")
                print(f"Package Manager: {os_info.pkg_manager}")

                deps = check_dependencies(os_info)
                missing = [d for d in deps if not d.installed]

                print(f"\nDependencies checked: {len(deps)}")
                if missing:
                    print(f"Missing packages:")
                    for d in missing:
                        print(f"  - {d.package_name} ({d.logical_name})")
                else:
                    print("All dependencies satisfied.")

                try:
                    await verify_docker(os_info)
                    print("Docker: OK")
                except Exception as e:
                    print(f"Docker: {e}")

                return len(missing) == 0

            except Exception as exc:
                print(f"Error: {exc}")
                return False

        success = _aio.run(_check())
        raise SystemExit(0 if success else 1)

    app = LX06App()
    app.run()


if __name__ == "__main__":
    main()
