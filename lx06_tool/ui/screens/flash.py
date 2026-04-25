"""Flash screen — firmware flashing to device with progress tracking."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Markdown, ProgressBar, RichLog, Static

from lx06_tool.app import LX06App, FlashResult
from lx06_tool.modules.flasher import (
    detect_active_partition,
    flash_all,
)

logger = logging.getLogger(__name__)

FLASH_INFO = """## Phase 4: Flash Firmware

This is the final step — writing the custom firmware to your device.

**How it works:**
1. Detect which A/B partition is currently **inactive**
2. Flash boot image to the inactive boot partition
3. Flash system image to the inactive system partition
4. Verify the flash

The active partition remains untouched, so if anything goes wrong,
your device will still boot from the original partition.

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
    """

    def compose(self) -> ComposeResult:
        yield Markdown(FLASH_INFO)
        yield Static("Ready to flash.", id="flash-status")
        yield ProgressBar(total=100, id="flash-progress")
        yield RichLog(id="flash-log", highlight=True, markup=True)
        with Vertical(id="flash-actions"):
            yield Button("Start Flashing", variant="error", id="start-btn")
            yield Button("Cancel", variant="default", id="cancel-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one(RichLog).write(
            "[bold]Ready to flash custom firmware.[/]\n"
            "Click 'Start Flashing' to begin.\n"
            "\n[dim]Warning: Do not disconnect the device during flashing.[/]"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.app.run_worker(self._run_flash())
        elif event.button.id == "cancel-btn":
            self.query_one(RichLog).write("[yellow]Cancel requested (current operation will complete)[/]")

    async def _run_flash(self) -> None:
        """Run the flashing process using new standalone functions."""
        log = self.query_one(RichLog)
        progress = self.query_one("#flash-progress", ProgressBar)
        status = self.query_one("#flash-status", Static)
        app = self.app

        if not isinstance(app, LX06App):
            return

        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#cancel-btn", Button).disabled = False

        start_time = time.monotonic()
        result = FlashResult()

        try:
            device = app.device
            if not device:
                log.write("[red]No device connected. Go back to USB Connection.[/]")
                self.query_one("#start-btn", Button).disabled = False
                return

            tool = app.get_aml_tool()

            # Step 1: Detect A/B partitions
            log.write("[bold blue]Step 1: Detecting A/B partitions...[/]")
            await detect_active_partition(tool, device)

            log.write(f"  Active boot:    {device.active_boot}")
            log.write(f"  Inactive boot:  {device.inactive_boot}")
            log.write(f"  Active system:  {device.active_system}")
            log.write(f"  Inactive system: {device.inactive_system}")
            status.update(f"Target: {device.inactive_system}")

            progress.update(progress=10)

            # Step 2: Find images
            build_dir = app.config.build_dir / "output"
            system_image = build_dir / "root.squashfs"
            boot_image = build_dir / "boot.img"

            if not system_image.exists():
                # Try alternative paths
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

            progress.update(progress=20)

            # Step 3: Flash with progress tracking
            log.write("\n[bold blue]Step 2: Flashing firmware...[/]")

            flash_progress = 20

            def on_step(step_msg: str) -> None:
                nonlocal flash_progress
                log.write(f"  [bold]{step_msg}[/]")
                status.update(step_msg)

            def on_progress(line: str) -> None:
                nonlocal flash_progress
                # Estimate progress from output
                flash_progress = min(flash_progress + 1, 95)
                progress.update(progress=flash_progress)
                app.update_progress(flash_progress)

            await flash_all(
                tool=tool,
                device=device,
                boot_image=boot_image,
                system_image=system_image,
                on_step=on_step,
                on_line=on_progress,
            )

            # Flash succeeded
            result.success = True
            result.boot_flashed = boot_image is not None
            result.system_flashed = True
            result.verified = True

            progress.update(progress=100)
            log.write("\n[bold green]Flash completed successfully![/]")
            status.update("Flash complete! Unplug USB and power cycle the device.")

            duration = time.monotonic() - start_time
            result.duration_sec = duration

            await app.on_flash_done(result)

        except Exception as exc:
            duration = time.monotonic() - start_time
            result.duration_sec = duration
            result.errors.append(str(exc))

            log.write(f"\n[bold red]Flash error:[/] {exc}")
            logger.error("Flash failed: %s", exc, exc_info=True)
            status.update(f"Flash error: {exc}")

            # Still report result to complete screen
            try:
                await app.on_flash_done(result)
            except Exception:
                self.query_one("#start-btn", Button).disabled = False

        self.query_one("#cancel-btn", Button).disabled = True
