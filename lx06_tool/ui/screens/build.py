"""Build screen — firmware extraction, customization, and repacking."""

from __future__ import annotations

import logging
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Markdown, ProgressBar, RichLog

from lx06_tool.app import LX06App
from lx06_tool.utils.debug_log import RichLogSink, register_sink, unregister_sink

logger = logging.getLogger(__name__)

BUILD_INFO = """## Phase 3: Firmware Build

This phase will:

1. **Extract** the stock firmware (squashfs) from backup or directly from device
2. **Apply** your selected customizations (debloat, media, AI)
3. **Repack** the modified rootfs into a new squashfs image
4. **Validate** the output image

If you skipped backup, the firmware will be extracted directly from
your connected device instead.

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
        log = self.query_one(RichLog)
        log.write("Ready to build custom firmware. Click 'Start Build' to begin.")
        self._debug_sink = RichLogSink(log)
        register_sink(self._debug_sink)

    def on_unmount(self) -> None:
        unregister_sink(self._debug_sink)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.app.run_worker(self._run_build())
        elif event.button.id == "continue-btn":
            self.app.run_worker(self._go_next())

    def _find_backup_image(
        self,
        backup_dir: Path,
        prefixes: list[str],
        suffixes: list[str],
    ) -> Path | None:
        """Search for an existing backup partition image.

        Tries all combinations of prefixes and suffixes to locate a dump.
        e.g. prefixes=["mtd4", "mtd5"], suffixes=["_system0", "_system1", ""]
        """
        for prefix in prefixes:
            for suffix in suffixes:
                candidate = backup_dir / f"{prefix}{suffix}.img"
                if candidate.exists():
                    return candidate
        return None

    async def _extract_from_device(
        self,
        app: LX06App,
        log: RichLog,
        build_dir: Path,
    ) -> tuple[Path | None, Path | None]:
        """Extract system and boot partitions directly from connected device.

        Returns:
            Tuple of (system_image_path, boot_image_path or None).
        """
        from lx06_tool.modules.firmware import (
            extract_partition_from_device,
            extract_active_system_from_device,
        )

        tool = app.get_aml_tool()
        extract_dir = build_dir / "device_extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        log.write("[bold cyan]No backup found — extracting firmware directly from device[/]")

        # Extract system partition (auto-detects active slot)
        def on_extract_progress(line: str) -> None:
            # Show key progress lines from the mread tool
            stripped = line.strip()
            if stripped and not stripped.startswith("I/"):
                log.write(f"  [dim]{stripped}[/]")

        system_image, active_slot = await extract_active_system_from_device(
            tool,
            extract_dir,
            on_progress=on_extract_progress,
        )
        log.write(f"  [green]✓[/] Extracted system partition ({active_slot}): {system_image.name}")
        log.write(f"    Size: {system_image.stat().st_size:,} bytes")

        # Extract corresponding boot partition
        boot_image: Path | None = None
        slot_boot_map = {"system0": "boot0", "system1": "boot1"}
        boot_label = slot_boot_map.get(active_slot, "boot0")
        boot_path = extract_dir / f"boot_{boot_label}.img"

        try:
            log.write(f"  Extracting {boot_label} partition from device...")
            boot_image = await extract_partition_from_device(
                tool, boot_label, boot_path, on_progress=on_extract_progress,
            )
            log.write(f"  [green]✓[/] Extracted boot partition: {boot_image.name}")
        except Exception as exc:
            log.write(f"  [yellow]⚠ Boot extraction skipped: {exc}[/]")
            log.write("  [dim]Boot partition is optional for most customizations.[/]")
            boot_image = None

        return system_image, boot_image

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
            choices = app.choices
            backup_dir = app.config.backup_dir
            build_dir = app.config.build_dir

            # ── Step 1: Locate firmware source ──────────────────────────
            on_step("Looking for firmware source...", 2)

            system_image: Path | None = None
            boot_image: Path | None = None
            source_label = ""

            # 1a. Check for existing backup images (correct naming: mtd4_system0.img)
            system_image = self._find_backup_image(
                backup_dir,
                prefixes=["mtd4", "mtd5"],
                suffixes=["_system0", "_system1", ""],
            )

            if system_image:
                source_label = f"backup ({system_image.name})"
                log.write(f"  [green]✓[/] Found system backup: {system_image.name}")

                # Also look for boot backup
                boot_image = self._find_backup_image(
                    backup_dir,
                    prefixes=["mtd2", "mtd3"],
                    suffixes=["_boot0", "_boot1", ""],
                )
                if boot_image:
                    log.write(f"  [green]✓[/] Found boot backup: {boot_image.name}")
            else:
                # 1b. No backup — try extracting directly from device
                log.write("  No backup images found in backup directory.")

                if app.device is not None:
                    log.write("  Device is connected — attempting direct extraction...")
                    on_step("Extracting firmware directly from device...", 5)

                    system_image, boot_image = await self._extract_from_device(
                        app, log, build_dir,
                    )
                    source_label = f"device (direct extraction)"
                else:
                    # 1c. No backup AND no device — cannot proceed
                    log.write("")
                    log.write("[bold red]✗ No firmware source available![/]")
                    log.write("")
                    log.write("[yellow]Options:[/]")
                    log.write("  1. Go back and run backup first (recommended)")
                    log.write("  2. Connect your device via USB and try again")
                    log.write("")
                    log.write("[red]Either run backup first or connect your device.[/]")
                    self.query_one("#start-btn", Button).disabled = False
                    return

            log.write(f"  Firmware source: {source_label}")
            log.write("")

            # ── Step 2: Create pipeline with correct paths ───────────────
            on_step("Preparing customization pipeline...", 10)

            pipeline = app.get_firmware_pipeline(
                system_dump_override=system_image,
                boot_dump_override=boot_image,
            )

            # ── Step 3: Run the full pipeline ────────────────────────────
            on_step("Running customization pipeline...", 15)

            result = await pipeline.run_pipeline(
                on_output=lambda s, l: log.write(f"  [{s}] {l}"),
            )

            if not result.success:
                log.write(f"\n[bold red]Pipeline failed:[/] {', '.join(result.steps_failed)}")
                self.query_one("#start-btn", Button).disabled = False
                return

            on_step("Firmware build complete!", 100)
            log.write(f"\n[bold green]Custom firmware built successfully![/]")
            if result.output_system:
                log.write(f"  Output: {result.output_system}")
                log.write(f"  Size: {result.output_system.stat().st_size:,} bytes")
            if result.warnings:
                log.write(f"\n[yellow]Warnings ({len(result.warnings)}):[/]")
                for w in result.warnings:
                    log.write(f"  [yellow]•[/] {w}")

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
