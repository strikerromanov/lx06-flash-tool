"""Build screen — firmware extraction, customization, and repacking."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Markdown, ProgressBar, RichLog

from lx06_tool.app import LX06App

logger = logging.getLogger(__name__)

BUILD_INFO = """## Phase 3: Firmware Build

This phase will:

1. **Extract** the stock firmware (squashfs) from your backup
2. **Apply** your selected customizations (debloat, media, AI)
3. **Repack** the modified rootfs into a new squashfs image
4. **Validate** the output image

This may take several minutes depending on your selections.
"""


class BuildScreen(Screen):
    """Build screen — runs the firmware customization pipeline."""

    DEFAULT_CSS = """
    BuildScreen { padding: 1 2; }
    #build-log { height: 1fr; border: solid $primary; margin: 1 0; }
    #build-progress { height: 1; margin: 0 0 1 0; }
    #build-actions { height: auto; align: center middle; padding: 1 0; }
    """

    def compose(self) -> ComposeResult:
        yield Markdown(BUILD_INFO)
        yield ProgressBar(total=100, id="build-progress")
        yield RichLog(id="build-log", highlight=True, markup=True)
        with Vertical(id="build-actions"):
            yield Button("Start Build", variant="primary", id="start-btn")
            yield Button("Continue", variant="success", id="continue-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one(RichLog).write("Ready to build custom firmware. Click 'Start Build' to begin.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.app.run_worker(self._run_build())
        elif event.button.id == "continue-btn":
            self.app.run_worker(self._go_next())

    async def _run_build(self) -> None:
        """Run the firmware build pipeline."""
        log = self.query_one(RichLog)
        progress = self.query_one("#build-progress", ProgressBar)
        app = self.app

        if not isinstance(app, LX06App):
            return

        self.query_one("#start-btn", Button).disabled = True

        def on_step(step_name: str, pct: int) -> None:
            log.write(f"[bold blue]{step_name}[/]")
            progress.update(progress=pct)
            app.update_status(step_name)

        try:
            pipeline = app.get_firmware_pipeline()
            choices = app.choices

            # Step 1: Find backup system image
            on_step("Extracting firmware from backup...", 5)
            backup_dir = app.config.backup_dir

            # Find system image from backup
            system_image = None
            for mtd_name in ["mtd4", "mtd5"]:
                candidate = backup_dir / f"{mtd_name}.img"
                if candidate.exists():
                    system_image = candidate
                    break

            if not system_image or not system_image.exists():
                log.write("[red]No system backup found. Ensure backup phase completed.[/]")
                self.query_one("#start-btn", Button).disabled = False
                return

            log.write(f"  Using: {system_image.name}")

            # Step 2: Run the full pipeline
            on_step("Running customization pipeline...", 10)

            output_image = await pipeline.run(
                choices=choices,
                on_output=lambda s, l: log.write(f"  [{s}] {l}"),
            )

            on_step("Firmware build complete!", 100)
            log.write(f"\n[bold green]Custom firmware built successfully![/]")
            log.write(f"  Output: {output_image}")
            log.write(f"  Size: {output_image.stat().st_size:,} bytes")

            self.query_one("#continue-btn", Button).disabled = False
            app.update_status("Build complete")

        except Exception as exc:
            log.write(f"\n[bold red]Build error:[/] {exc}")
            logger.error("Build failed: %s", exc, exc_info=True)
            self.query_one("#start-btn", Button).disabled = False
            app.update_status(f"Build failed: {exc}")

    async def _go_next(self) -> None:
        app = self.app
        if isinstance(app, LX06App):
            await app.on_build_done(True)
