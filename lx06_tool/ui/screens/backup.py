"""Backup screen — bootloader unlock and partition dumping."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Markdown, ProgressBar, RichLog

from lx06_tool.app import LX06App

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
    """

    backup_started: reactive[bool] = reactive(False)
    backup_complete: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Markdown(BACKUP_INFO)
        yield ProgressBar(total=100, id="backup-progress")
        yield RichLog(id="backup-log", highlight=True, markup=True)
        with Vertical(id="backup-actions"):
            yield Button("Start Backup", variant="primary", id="start-btn")
            yield Button("Continue", variant="success", id="continue-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one(RichLog).write("Ready to back up your device. Click 'Start Backup' to begin.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.app.run_worker(self._run_backup())
        elif event.button.id == "continue-btn":
            self.app.run_worker(self._go_next())

    async def _run_backup(self) -> None:
        """Run the full backup sequence."""
        self.backup_started = True
        log = self.query_one(RichLog)
        progress = self.query_one("#backup-progress", ProgressBar)
        app = self.app

        if not isinstance(app, LX06App):
            return

        try:
            # Step 1: Unlock bootloader
            log.write("[bold blue]Step 1: Unlocking bootloader...[/]")
            progress.update(progress=5)

            bl_mgr = app.get_bootloader_mgr()
            status = await bl_mgr.unlock(on_output=lambda s, l: log.write(f"  [{s}] {l}"))

            if status.is_safe_for_flashing:
                log.write(f"[green]Bootloader unlocked (bootdelay={status.bootdelay})[/]")
            else:
                log.write("[yellow]Warning: Bootdelay is low. Device may be harder to recover.[/]")

            progress.update(progress=15)

            # Step 2: Dump partitions
            log.write("\n[bold blue]Step 2: Dumping MTD partitions...[/]")
            backup_mgr = app.get_backup_mgr()

            partitions = ["mtd0", "mtd1", "mtd2", "mtd3", "mtd4", "mtd5", "mtd6"]

            async def on_dump_status(mtd: str, status_msg: str) -> None:
                log.write(f"  [{mtd}] {status_msg}")

            backup_set = await backup_mgr.dump_all_partitions(
                partitions=partitions,
                on_partition_start=lambda mtd: log.write(f"\n  Dumping {mtd}..."),
                on_partition_done=lambda mtd: progress.update(progress=15 + int(60 * partitions.index(mtd) / len(partitions))),
                on_output=lambda s, l: None,
            )

            progress.update(progress=80)

            # Step 3: Verify backups
            log.write("\n[bold blue]Step 3: Verifying backup integrity...[/]")
            is_valid = await backup_mgr.verify_backup(backup_set)

            if is_valid:
                log.write("[bold green]All backups verified successfully![/]")
            else:
                log.write("[bold yellow]Some partitions failed verification. Check details above.[/]")

            # Save manifest
            manifest_path = await backup_mgr.save_manifest(backup_set)
            log.write(f"\nBackup manifest saved to: {manifest_path}")

            progress.update(progress=100)
            self.backup_complete = True
            self.query_one("#start-btn", Button).disabled = True
            self.query_one("#continue-btn", Button).disabled = False
            app.update_status("Backup complete")

        except Exception as exc:
            log.write(f"\n[bold red]Backup error:[/] {exc}")
            logger.error("Backup failed: %s", exc, exc_info=True)
            app.update_status(f"Backup failed: {exc}")

    async def _go_next(self) -> None:
        app = self.app
        if isinstance(app, LX06App):
            await app.on_backup_done(self.backup_complete)
