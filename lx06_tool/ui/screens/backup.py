"""Backup screen — bootloader unlock and partition dumping."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Markdown, ProgressBar, RichLog

from lx06_tool.app import LX06App
from lx06_tool.utils.debug_log import RichLogSink, register_sink, unregister_sink
from lx06_tool.modules.backup import (
    dump_all_partitions,
    compute_checksums,
    verify_backup,
    generate_backup_report,
)

logger = logging.getLogger(__name__)

BACKUP_INFO = """## Phase 2: Backup & Safety

This phase will:

1. **Unlock the bootloader** — Set bootdelay=15 for recovery access
2. **Dump all 7 MTD partitions** — bootloader, tpl, boot0/1, system0/1, data
3. **Verify backup integrity** — SHA256 + MD5 checksums

Your backup is your safety net. If anything goes wrong, you can always
restore from these dumps.
"""


class BackupScreen(Screen):
    """Backup screen — bootloader unlock and partition dump."""

    DEFAULT_CSS = """
    BackupScreen { padding: 1 2; }
    #backup-log { height: 1fr; border: solid $primary; margin: 1 0; }
    #backup-progress { height: 1; margin: 0 0 1 0; }
    #backup-actions { height: auto; align: center middle; padding: 1 0; }
    #backup-actions Button { margin: 0 1; }
    #skip-warning { color: $warning; text-align: center; padding: 1 2; background: $warning-darken-3; }
    """

    backup_started: reactive[bool] = reactive(False)
    backup_complete: reactive[bool] = reactive(False)
    _skip_pending: bool = False

    def compose(self) -> ComposeResult:
        yield Markdown(BACKUP_INFO)
        yield ProgressBar(total=100, id="backup-progress")
        yield RichLog(id="backup-log", highlight=True, markup=True)
        with Vertical(id="backup-actions"):
            yield Button("Start Backup", variant="primary", id="start-btn")
            yield Button("Skip Backup", variant="warning", id="skip-btn")
            yield Button("Continue", variant="success", id="continue-btn", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("Ready to back up your device. Click 'Start Backup' to begin.")
        log.write("[dim]You can also skip backup, but this is NOT recommended.[/]")
        self._debug_sink = RichLogSink(log)
        register_sink(self._debug_sink)

    def on_unmount(self) -> None:
        unregister_sink(self._debug_sink)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.app.run_worker(self._run_backup())
        elif event.button.id == "skip-btn":
            self._handle_skip()
        elif event.button.id == "continue-btn":
            self.app.run_worker(self._go_next())

    def _handle_skip(self) -> None:
        """Handle the Skip Backup button with confirmation flow."""
        log = self.query_one(RichLog)

        if not self._skip_pending:
            # First click: show warning and require second click
            self._skip_pending = True
            log.write("\n[bold red]⚠ WARNING: Skipping backup is dangerous![/]")
            log.write("[yellow]If flashing fails, you will have NO way to recover your device.[/]")
            log.write("[yellow]A backup is your only safety net against a bricked device.[/]")
            log.write("[yellow]Click 'Skip Backup' again to confirm, or 'Start Backup' to proceed safely.[/]")

            # Update button to show confirmation state
            skip_btn = self.query_one("#skip-btn", Button)
            skip_btn.label = "⚠ Confirm Skip (click again)"
            skip_btn.variant = "error"
        else:
            # Second click: confirmed — proceed without backup
            self._do_skip()

    def _do_skip(self) -> None:
        """Execute the skip — mark backup as skipped and proceed."""
        log = self.query_one(RichLog)
        app = self.app

        if not isinstance(app, LX06App):
            return

        # Mark backup as skipped in app config
        app.backup_skipped = True

        log.write("\n[bold yellow]Backup skipped by user.[/]")
        log.write("[dim]Proceeding to customization. You can go back and run backup later.[/]")
        log.write("[bold red]WARNING: Do NOT proceed to flash without a backup![/]")

        # Disable all action buttons
        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#skip-btn", Button).disabled = True
        self.query_one("#continue-btn", Button).disabled = False

        self.app.update_status("Backup SKIPPED — no recovery if flash fails!")

    async def _run_backup(self) -> None:
        """Run the full backup sequence using new standalone functions."""
        self.backup_started = True
        log = self.query_one(RichLog)
        progress = self.query_one("#backup-progress", ProgressBar)
        app = self.app

        if not isinstance(app, LX06App):
            return

        # Disable skip button once backup starts
        self.query_one("#skip-btn", Button).disabled = True

        try:
            tool = app.get_aml_tool()
            backup_dir = app.config.backup_dir
            backup_dir.mkdir(parents=True, exist_ok=True)

            # Step 0: Query partition info for debugging
            log.write("[dim]Querying device partition info...[/]")
            try:
                part_info = await tool.list_partitions()
                log.write(f"[dim]{part_info}[/]")
            except Exception as exc:
                log.write(f"[dim]Partition info query failed (non-fatal): {exc}[/]")

            # Step 1: Unlock bootloader via AmlogicTool directly
            log.write("[bold blue]Step 1: Unlocking bootloader...[/]")
            progress.update(progress=5)

            try:
                # Set bootdelay for recovery access
                await tool.setenv("bootdelay", "15")
                await tool.saveenv()

                # Verify by reading back
                from lx06_tool.utils.runner import run
                result = await run(
                    [str(tool._exe), "bulkcmd", "printenv bootdelay"],
                    timeout=10,
                )

                bootdelay = 0
                if result.ok:
                    output = result.stdout + result.stderr
                    if "bootdelay=15" in output:
                        bootdelay = 15
                    elif "bootdelay=" in output:
                        try:
                            val = output.split("bootdelay=")[1].split()[0].strip()
                            bootdelay = int(val)
                        except (ValueError, IndexError):
                            pass

                if bootdelay >= 5:
                    log.write(f"[green]Bootloader unlocked (bootdelay={bootdelay})[/]")
                else:
                    log.write("[yellow]Warning: Bootdelay is low. Device may be harder to recover.[/]")

            except Exception as exc:
                log.write(f"[yellow]Bootloader unlock warning: {exc}[/]")
                log.write("[dim]Continuing with backup — bootloader may already be unlocked.[/]")

            progress.update(progress=15)

            # Step 2: Dump all partitions
            log.write("\n[bold blue]Step 2: Dumping MTD partitions...[/]")

            skipped: list[str] = []

            def on_partition_start(mtd_name: str, label: str) -> None:
                log.write(f"\n  Dumping {mtd_name} ({label})...")

            def on_line(line: str) -> None:
                pass  # Suppress noisy output

            def on_partition_skip(mtd_name: str, reason: str) -> None:
                skipped.append(mtd_name)
                log.write(f"  [yellow]⚠ Skipped {mtd_name}: {reason}[/]")

            backup_set = await dump_all_partitions(
                tool=tool,
                backup_dir=backup_dir,
                on_partition_start=on_partition_start,
                on_line=on_line,
                on_partition_skip=on_partition_skip,
            )

            # Update progress for each partition dumped
            num_partitions = len(backup_set.partitions)
            for i, (mtd_name, part) in enumerate(backup_set.partitions.items()):
                log.write(f"  [green]\u2713[/] {mtd_name} ({part.label}): {part.size_bytes:,} bytes")
                progress.update(progress=15 + int(50 * (i + 1) / max(num_partitions, 1)))

            if skipped:
                log.write(f"\n[yellow]⚠ {len(skipped)} partition(s) could not be dumped: {', '.join(skipped)}[/]")
                log.write("[yellow]You may still proceed, but recovery options are limited for those partitions.[/]")

            progress.update(progress=70)

            # Step 3: Compute checksums
            log.write("\n[bold blue]Step 3: Computing checksums...[/]")

            def on_checksum(mtd_name: str, checksums: object) -> None:
                log.write(f"  {mtd_name}: SHA256 OK")

            await compute_checksums(backup_set, on_partition=on_checksum)

            progress.update(progress=85)

            # Step 4: Verify backups
            log.write("\n[bold blue]Step 4: Verifying backup integrity...[/]")
            await verify_backup(backup_set)

            if backup_set.all_verified:
                log.write("[bold green]All backups verified successfully![/]")
            else:
                log.write("[bold yellow]Some partitions failed verification. Check details above.[/]")

            # Store backup set in app config
            app.config.backup = backup_set

            # Generate and display report
            report = generate_backup_report(backup_set)
            log.write(f"\n[dim]{report}[/]")

            progress.update(progress=100)
            self.backup_complete = True
            self.query_one("#start-btn", Button).disabled = True
            self.query_one("#continue-btn", Button).disabled = False
            app.update_status("Backup complete")

        except Exception as exc:
            log.write(f"\n[bold red]Backup error:[/] {exc}")
            logger.error("Backup failed: %s", exc, exc_info=True)
            app.update_status(f"Backup failed: {exc}")
            # Still allow proceeding - user may want to skip
            self.query_one("#continue-btn", Button).disabled = False
            self.query_one("#skip-btn", Button).disabled = False

    async def _go_next(self) -> None:
        app = self.app
        if isinstance(app, LX06App):
            await app.on_backup_done(self.backup_complete)
