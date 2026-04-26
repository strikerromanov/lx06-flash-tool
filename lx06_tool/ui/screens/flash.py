"""Flash screen — firmware flashing to device with progress tracking."""

from __future__ import annotations

import logging
import time

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Markdown, ProgressBar, RichLog, Static

from lx06_tool.app import FlashResult, LX06App
from lx06_tool.modules.flasher import flash_all
from lx06_tool.utils.debug_log import RichLogSink, register_sink, unregister_sink

logger = logging.getLogger(__name__)

FLASH_INFO = """## Phase 4: Flash Firmware

This is the final step — writing the custom firmware to your device.

**Official procedure (from xiaoai-patch):**
1. Unlock bootloader: `setenv bootdelay 15` + `saveenv`
2. Flash boot0 AND boot1 with the same boot.img
3. Flash system0 AND system1 with the same root.squashfs

Both A/B slots are flashed to prevent soft-bricks.

\u26a0\ufe0f **Do NOT disconnect USB or power during flashing!**
"""


class FlashScreen(Screen):
    """Flash screen — writes firmware to the device."""

    DEFAULT_CSS = """
    FlashScreen { padding: 1 2; }
    #flash-log { height: 1fr; border: solid $primary; margin: 1 0; }
    #flash-progress { height: 1; margin: 0 0 1 0; }
    #flash-status { height: auto; padding: 1; background: $primary-darken-2; color: $text; text-align: center; }
    #flash-actions { height: auto; align: center middle; padding: 1 0; }
    #sudo-row {
        height: 3;
        padding: 0 1;
        align: center middle;
    }
    #sudo-row Static {
        width: auto;
        margin: 0 1 0 0;
    }
    #sudo-input {
        width: 30;
        margin: 1 0;
        border: solid $warning;
    }
    """

    def compose(self) -> ComposeResult:
        yield Markdown(FLASH_INFO)
        with Horizontal(id="sudo-row"):
            yield Static("\U0001f512 Sudo Password:")
            yield Input(
                placeholder="\U0001f510 Sudo password...",
                password=True,
                id="sudo-input",
            )
        yield Static("Ready to flash.", id="flash-status")
        yield ProgressBar(total=100, id="flash-progress")
        yield RichLog(id="flash-log", highlight=True, markup=True)
        with Vertical(id="flash-actions"):
            yield Button("Start Flashing", variant="error", id="start-btn")
            yield Button("Cancel", variant="default", id="cancel-btn", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write(
            "[bold]Ready to flash custom firmware.[/]\n"
            "Click 'Start Flashing' to begin.\n"
            "\n[dim]Warning: Do not disconnect the device during flashing.[/]"
        )
        self._debug_sink = RichLogSink(log)
        register_sink(self._debug_sink)

    def on_unmount(self) -> None:
        unregister_sink(self._debug_sink)

    def _get_sudo_password(self) -> str:
        """Get the sudo password from the input field and sync to app."""
        try:
            pw = self.query_one("#sudo-input", Input).value.strip()
        except Exception:
            pw = ""
        # Sync to app-level so other screens can use it
        app = self.app
        if isinstance(app, LX06App):
            app.sudo_password = pw
        return pw

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.app.run_worker(self._run_flash())
        elif event.button.id == "cancel-btn":
            self.query_one(RichLog).write("[yellow]Cancel requested (current operation will complete)[/]")

    async def _run_flash(self) -> None:
        """Run the flashing process using official xiaoai-patch procedure."""
        log = self.query_one(RichLog)
        progress = self.query_one("#flash-progress", ProgressBar)
        status = self.query_one("#flash-status", Static)
        app = self.app

        if not isinstance(app, LX06App):
            return

        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#cancel-btn", Button).disabled = False

        pw = self._get_sudo_password()

        start_time = time.monotonic()
        result = FlashResult()

        try:
            tool = app.get_aml_tool()

            # Step 1: Find images
            log.write("[bold blue]Step 1: Locating firmware images...[/]")

            build_dir = app.config.build_dir / "output"
            system_image = build_dir / "root.squashfs"
            boot_image = build_dir / "boot.img"

            if not system_image.exists():
                system_image = app.config.build_dir / "root.squashfs"
            if not boot_image.exists():
                boot_image = app.config.build_dir / "boot.img"

            if not system_image.exists():
                log.write(f"[red]System image not found: {system_image}[/]")
                self.query_one("#start-btn", Button).disabled = False
                return

            log.write(f"  System image: {system_image.name} ({system_image.stat().st_size:,} bytes)")
            if boot_image.exists():
                log.write(f"  Boot image: {boot_image.name} ({boot_image.stat().st_size:,} bytes)")
            else:
                log.write("  [yellow]No boot image found — skipping boot partition[/]")
                boot_image = None

            progress.update(progress=10)

            # Step 2: Flash using official procedure
            # This flashes BOTH A/B partitions (boot0+boot1, system0+system1)
            # and unlocks bootloader (setenv bootdelay + saveenv)
            log.write("\n[bold blue]Step 2: Flashing firmware (official procedure)...[/]")
            log.write("  Bootloader will be unlocked (bootdelay=15)")
            log.write("  Both A/B slots will be flashed for safety")

            flash_progress = 10

            def on_step(step_msg: str) -> None:
                nonlocal flash_progress
                log.write(f"  [bold]{step_msg}[/]")
                status.update(step_msg)

            def on_progress(line: str) -> None:
                nonlocal flash_progress
                flash_progress = min(flash_progress + 1, 95)
                progress.update(progress=flash_progress)
                app.update_progress(flash_progress)

            await flash_all(
                tool=tool,
                boot_image=boot_image,
                system_image=system_image,
                on_step=on_step,
                on_line=on_progress,
                sudo_password=pw,
            )

            progress.update(progress=100)
            app.update_progress(100)

            elapsed = time.monotonic() - start_time
            log.write(f"\n[bold green]Flash completed in {elapsed:.1f}s![/]")
            status.update("Flash complete! You can safely disconnect the device.")

            result.success = True
            result.elapsed_seconds = elapsed
            app.flash_result = result

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            log.write(f"\n[bold red]Flash failed after {elapsed:.1f}s: {exc}[/]")
            status.update(f"Flash failed: {exc}")
            result.success = False
            result.error_message = str(exc)
            result.elapsed_seconds = elapsed
            app.flash_result = result

        finally:
            self.query_one("#start-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True
