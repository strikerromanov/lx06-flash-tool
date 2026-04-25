"""Backup screen — bootloader unlock and partition dumping."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Markdown, ProgressBar, RichLog

from lx06_tool.app import LX06App
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
        """Run the full backup sequence using new standalone functions."""
        self.backup_started = True
        log = self.query_one(RichLog)
        progress = self.query_one("#backup-progress", ProgressBar)
        app = self.app

        if not isinstance(app, LX06App):
            return

        try:
            tool = app.get_aml_tool()
            backup_dir = app.config.backup_dir
            backup_dir.mkdir(parents=True, exist_ok=True)

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

            def on_partition_start(mtd_name: str, label: str) -> None:
                log.write(f"\n  Dumping {mtd_name} ({label})...")

            def on_line(line: str) -> None:
                pass  # Suppress noisy output

            backup_set = await dump_all_partitions(
                tool=tool,
                backup_dir=backup_dir,
                on_partition_start=on_partition_start,
                on_line=on_line,
            )

            # Update progress for each partition dumped
            num_partitions = len(backup_set.partitions)
            for i, (mtd_name, part) in enumerate(backup_set.partitions.items()):
                log.write(f"  [green]\u2713[/] {mtd_name} ({part.label}): {part.size_bytes:,} bytes")
                progress.update(progress=15 + int(50 * (i + 1) / max(num_partitions, 1)))

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

    async def _go_next(self) -> None:
        app = self.app
        if isinstance(app, LX06App):
            await app.on_backup_done(self.backup_complete)
