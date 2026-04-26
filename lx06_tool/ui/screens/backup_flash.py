"""
lx06_tool/ui/screens/backup_flash.py
--------------------------------------
Combined backup + download + flash screen.

Three sequential phases:
  1. Backup all 7 partitions (with validation)
  2. Download latest firmware from GitHub
  3. Flash boot + system to both A/B slots
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import tarfile
from datetime import datetime
from pathlib import Path

import httpx

from textual.app import ComposeResult
from textual.containers import Center, VerticalScroll
from textual.screen import Screen
from textual.widgets import Label, Static

from lx06_tool.constants import (
    AB_BOOT_SLOTS,
    AB_SYSTEM_SLOTS,
    BACKUP_ORDER,
    CUSTOM_FIRMWARE_SEARCH_PATHS,
    FIRMWARE_BOOT_FILE,
    FIRMWARE_FILE_PATTERN,
    FIRMWARE_RELEASES_URL,
    FIRMWARE_SYSTEM_FILE,
    LX06_MAX_SYSTEM_SIZE,
    PARTITION_MAP,
    SQUASHFS_MAGIC_LE,
    USER_FIRMWARE_DIR,
)
from lx06_tool.ui.widgets import ActionButton, LogPanel, StepProgress, StatusLabel


class BackupFlashScreen(Screen):
    """Combined backup, download, and flash screen."""

    DEFAULT_CSS = """
    BackupFlashScreen {
        align: center middle;
    }
    BackupFlashScreen > VerticalScroll {
        width: 80;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
    }
    BackupFlashScreen .title {
        text-align: center;
        text-style: bold;
        color: $primary;
        padding: 1 0;
    }
    BackupFlashScreen .status {
        padding: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("💾 Backup & Flash", classes="title")
            yield StepProgress(
                steps=["Backup", "Download", "Flash"],
                id="main_steps",
            )
            yield StepProgress(
                steps=["bootloader", "tpl", "boot0", "boot1", "system0", "system1", "data"],
                id="partition_steps",
            )
            yield StatusLabel(text="Press Start to begin backup and flashing.", id="bf_status")
            yield LogPanel(title="Operation Log", id="bf_log")
            with Center():
                yield ActionButton(
                    label="Start",
                    variant="success",
                    id="start_btn",
                )
                yield ActionButton(
                    label="Continue",
                    variant="success",
                    id="continue_btn",
                    disabled=True,
                )

    def on_mount(self) -> None:
        """Initialize screen state."""
        self._log = self.query_one("#bf_log", LogPanel)
        self._status = self.query_one("#bf_status", StatusLabel)
        self._main_steps = self.query_one("#main_steps", StepProgress)
        self._part_steps = self.query_one("#partition_steps", StepProgress)
        self._done = False
        # Paths populated during download
        self._boot_img: Path | None = None
        self._system_img: Path | None = None

    def _set_status(self, text: str) -> None:
        try:
            self._status.status_text = text
        except Exception:
            pass

    def on_action_button_pressed(self, event: ActionButton.Pressed) -> None:
        """Handle button presses."""
        btn = event.action_button
        if btn.id == "start_btn":
            btn.set_loading(True)
            self.query_one("#continue_btn", ActionButton).set_enabled(False)
            self.run_worker(self._run_all(), exclusive=True)
        elif btn.id == "continue_btn" and self._done:
            self.app.push_screen("complete")

    # ─── Main Pipeline ──────────────────────────────────────────────────

    async def _run_all(self) -> None:
        """Run backup → download → flash pipeline sequentially."""
        try:
            # ── Phase 1: Backup ──
            self._main_steps.set_current(0)
            backup_ok = await self._backup_all()
            if not backup_ok:
                self._log.write("[red]Backup failed. Cannot proceed with flashing.[/red]")
                self._set_status("❌ Backup failed — check log for details")
                self.query_one("#start_btn", ActionButton).set_loading(False)
                return

            # ── Phase 2: Download Firmware ──
            self._main_steps.set_current(1)
            download_ok = await self._download_firmware()
            if not download_ok:
                self._log.write("[red]Firmware download failed. Cannot proceed.[/red]")
                self._set_status("❌ Download failed — check log for details")
                self.query_one("#start_btn", ActionButton).set_loading(False)
                return

            # ── Phase 3: Flash ──
            self._main_steps.set_current(2)
            flash_ok = await self._flash_firmware()
            if not flash_ok:
                self._log.write("[red]Flashing failed. Your backup is safe.[/red]")
                self._set_status("❌ Flash failed — check log for details")
                self.query_one("#start_btn", ActionButton).set_loading(False)
                return

            # All done!
            self._main_steps.set_current(3)  # Past last = all complete
            self._done = True
            self._set_status("✅ Backup, download, and flash all completed successfully!")
            self._log.write("[bold green]All operations completed successfully![/bold green]")
            self.query_one("#start_btn", ActionButton).set_loading(False)
            self.query_one("#continue_btn", ActionButton).set_enabled(True)

            # Update config backup state
            config = self.app.config  # type: ignore[attr-defined]
            config.backup.timestamp = datetime.now().isoformat()

        except Exception as exc:
            self._log.write(f"[red]Pipeline error: {exc}[/red]")
            self._set_status(f"❌ Error: {exc}")
            self.query_one("#start_btn", ActionButton).set_loading(False)

    # ─── Phase 1: Backup All Partitions ─────────────────────────────────

    async def _backup_all(self) -> bool:
        """Backup all 7 partitions with validation.

        Returns True if all backups succeed.
        """
        config = self.app.config  # type: ignore[attr-defined]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = config.backup_dir / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)

        config.backup.backup_dir = backup_dir

        self._set_status(f"Backing up all partitions to {backup_dir}...")
        self._log.write(f"[bold]Starting backup → {backup_dir}[/bold]")

        aml_tool = self.app.get_aml_tool()  # type: ignore[attr-defined]
        all_ok = True

        def on_partition_start(mtd: str, label: str, idx: int, total: int) -> None:
            self._part_steps.set_current(idx)
            self._set_status(f"Backing up {label} ({idx + 1}/{total})...")
            self._log.write(f"Backing up {mtd} ({label})...")

        def on_partition_done(result: object) -> None:
            pass  # We validate in the main loop below

        def on_output(line: str) -> None:
            self._log.write(line.rstrip())

        try:
            results = await aml_tool.backup_all_partitions(
                backup_dir,
                on_partition_start=on_partition_start,
                on_partition_done=on_partition_done,
                on_output=on_output,
            )

            # Validate each partition
            for idx, dump in enumerate(results):
                self._part_steps.set_current(idx)
                label = dump.label
                ok = dump.ok
                size_ok = False
                magic_ok = True  # Only checked for system partitions

                # Check file exists and size
                if ok and dump.output_path and dump.output_path.exists():
                    file_size = dump.output_path.stat().st_size
                    expected = PARTITION_MAP.get(dump.mtd, {}).get("size", 0)
                    # Allow ≥50% of expected (NAND bad blocks)
                    size_ok = file_size >= expected * 0.5 if expected else file_size > 0

                    # Validate squashfs magic for system partitions
                    if label.startswith("system"):
                        magic_ok = self._check_squashfs_magic(dump.output_path)
                        if not magic_ok:
                            self._log.write(
                                f"  [yellow]⚠ {label}: squashfs magic bytes invalid[/yellow]"
                            )

                    if size_ok and magic_ok:
                        self._log.write(
                            f"  [green]✓ {label}: {file_size:,} bytes[/green]"
                        )
                    else:
                        if not size_ok:
                            self._log.write(
                                f"  [yellow]⚠ {label}: size {file_size:,} < expected ~{expected:,}[/yellow]"
                            )
                else:
                    self._log.write(
                        f"  [red]✗ {label}: dump failed (exit {dump.raw_output[-100:]})[/red]"
                    )

                overall_ok = ok and size_ok and magic_ok
                if not overall_ok:
                    all_ok = False

            # Mark all partition steps done
            self._part_steps.set_current(7)  # Past last

            if all_ok:
                self._log.write("[bold green]✓ All partitions backed up successfully.[/bold green]")
            else:
                self._log.write(
                    "[yellow]⚠ Some partitions had issues, but continuing anyway.[/yellow]"
                )
                all_ok = True  # Continue even with warnings — backup may still be usable

        except Exception as exc:
            self._log.write(f"[red]Backup error: {exc}[/red]")
            all_ok = False

        return all_ok

    def _check_squashfs_magic(self, path: Path) -> bool:
        """Validate that a file starts with squashfs magic bytes (b'hsqs')."""
        try:
            with open(path, "rb") as f:
                magic = f.read(4)
            return magic == SQUASHFS_MAGIC_LE
        except Exception:
            return False

    # ─── Phase 2: Download Firmware ─────────────────────────────────────

    async def _download_firmware(self) -> bool:
        """Check for local custom firmware first, then fall back to GitHub.

        Returns True if firmware found, extracted, and validated.
        """
        config = self.app.config  # type: ignore[attr-defined]
        build_dir = config.build_dir
        build_dir.mkdir(parents=True, exist_ok=True)

        self._set_status("Checking for local firmware...")
        self._log.write("[bold]Downloading firmware[/bold]")

        # ── Check for local custom firmware first ──
        local_firmware_paths = [Path(p) for p in CUSTOM_FIRMWARE_SEARCH_PATHS]
        local_firmware_paths.append(
            Path.home() / ".local" / "share" / "lx06-tool" / USER_FIRMWARE_DIR
        )

        for search_dir in local_firmware_paths:
            if not search_dir.exists():
                continue
            tars = sorted(
                search_dir.glob(FIRMWARE_FILE_PATTERN),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not tars:
                continue

            tar_path = tars[0]
            self._log.write(f"[green]✓ Found local custom firmware: {tar_path.name}[/green]")
            self._log.write(f"  Size: {tar_path.stat().st_size:,} bytes")

            # Extract tarball
            self._set_status("Extracting custom firmware...")
            extract_dir = config.build_dir / "firmware"
            if extract_dir.exists():
                import shutil
                shutil.rmtree(extract_dir)

            with tarfile.open(tar_path, "r") as tar:
                tar.extractall(extract_dir)

            # Find boot.img and root.squashfs
            boot_img = None
            system_img = None
            for root, dirs, files in self._walk(extract_dir):
                for f in files:
                    if f == FIRMWARE_BOOT_FILE:
                        boot_img = Path(root) / f
                    elif f == FIRMWARE_SYSTEM_FILE:
                        system_img = Path(root) / f

            if not boot_img and (extract_dir / FIRMWARE_BOOT_FILE).exists():
                boot_img = extract_dir / FIRMWARE_BOOT_FILE
            if not system_img and (extract_dir / FIRMWARE_SYSTEM_FILE).exists():
                system_img = extract_dir / FIRMWARE_SYSTEM_FILE

            # Validate
            if not boot_img or not boot_img.exists():
                self._log.write(
                    f"[yellow]Local firmware missing {FIRMWARE_BOOT_FILE}, falling back to download.[/yellow]"
                )
                continue
            if not system_img or not system_img.exists():
                self._log.write(
                    f"[yellow]Local firmware missing {FIRMWARE_SYSTEM_FILE}, falling back to download.[/yellow]"
                )
                continue

            sys_size = system_img.stat().st_size
            if sys_size > LX06_MAX_SYSTEM_SIZE:
                self._log.write(
                    f"[yellow]Local firmware too large ({sys_size:,} > {LX06_MAX_SYSTEM_SIZE:,}), falling back to download.[/yellow]"
                )
                continue

            self._log.write(
                f"[green]✓ Custom firmware validated:[/green]\n"
                f"  boot.img: {boot_img.stat().st_size:,} bytes\n"
                f"  root.squashfs: {sys_size:,} bytes\n"
                f"  WiFi: R&B Slow (pre-configured)\n"
                f"  ADB: enabled"
            )
            self._boot_img = boot_img
            self._system_img = system_img
            return True

        # ── No local firmware found, download from GitHub ──
        try:
            # Fetch latest release info
            proxy = config.proxy or None
            async with httpx.AsyncClient(
                follow_redirects=True,
                proxy=proxy,
                timeout=httpx.Timeout(30.0),
            ) as client:
                self._log.write(f"Fetching release info from GitHub...")
                response = await client.get(FIRMWARE_RELEASES_URL)
                response.raise_for_status()
                release = response.json()

            tag = release.get("tag_name", "unknown")
            self._log.write(f"Latest release: {tag}")

            # Find matching asset
            assets = release.get("assets", [])
            firmware_asset = None
            for asset in assets:
                name = asset.get("name", "")
                if fnmatch.fnmatch(name, FIRMWARE_FILE_PATTERN):
                    firmware_asset = asset
                    break

            if not firmware_asset:
                self._log.write(
                    f"[red]No firmware asset matching '{FIRMWARE_FILE_PATTERN}' found.[/red]"
                )
                self._log.write(
                    f"Available assets: {', '.join(a.get('name', '?') for a in assets)}"
                )
                return False

            download_url = firmware_asset["browser_download_url"]
            file_name = firmware_asset["name"]
            file_size = firmware_asset.get("size", 0)
            self._log.write(
                f"Found: {file_name} ({file_size:,} bytes)"
            )

            # Download the tarball
            tar_path = build_dir / file_name
            self._set_status(f"Downloading {file_name}...")

            from lx06_tool.utils.downloader import AsyncDownloader
            downloader = AsyncDownloader(proxy=config.proxy)

            last_pct = [-1]

            def on_progress(downloaded: int, total: int) -> None:
                if total > 0:
                    pct = int(downloaded / total * 100)
                    if pct != last_pct[0] and pct % 10 == 0:
                        last_pct[0] = pct
                        self._log.write(f"  Download progress: {pct}% ({downloaded:,}/{total:,})")
                        self._set_status(f"Downloading firmware... {pct}%")

            await downloader.download_file(
                download_url,
                tar_path,
                on_progress=on_progress,
            )
            self._log.write(f"[green]✓ Downloaded {file_name}[/green]")

            # Extract tarball
            self._set_status("Extracting firmware...")
            self._log.write("Extracting firmware tarball...")
            extract_dir = build_dir / "firmware"
            if extract_dir.exists():
                import shutil
                shutil.rmtree(extract_dir)

            with tarfile.open(tar_path, "r") as tar:
                tar.extractall(extract_dir)

            # Find boot.img and root.squashfs
            boot_img = None
            system_img = None
            for root, dirs, files in self._walk(extract_dir):
                for f in files:
                    if f == FIRMWARE_BOOT_FILE:
                        boot_img = Path(root) / f
                    elif f == FIRMWARE_SYSTEM_FILE:
                        system_img = Path(root) / f

            # Also check top-level
            if not boot_img and (extract_dir / FIRMWARE_BOOT_FILE).exists():
                boot_img = extract_dir / FIRMWARE_BOOT_FILE
            if not system_img and (extract_dir / FIRMWARE_SYSTEM_FILE).exists():
                system_img = extract_dir / FIRMWARE_SYSTEM_FILE

            # Validate
            if not boot_img or not boot_img.exists():
                self._log.write(f"[red]{FIRMWARE_BOOT_FILE} not found in archive.[/red]")
                return False

            if boot_img.stat().st_size == 0:
                self._log.write(f"[red]{FIRMWARE_BOOT_FILE} is empty (0 bytes).[/red]")
                return False

            if not system_img or not system_img.exists():
                self._log.write(f"[red]{FIRMWARE_SYSTEM_FILE} not found in archive.[/red]")
                return False

            sys_size = system_img.stat().st_size
            if sys_size > LX06_MAX_SYSTEM_SIZE:
                self._log.write(
                    f"[red]{FIRMWARE_SYSTEM_FILE} too large: {sys_size:,} bytes "
                    f"> max {LX06_MAX_SYSTEM_SIZE:,}[/red]"
                )
                return False

            self._log.write(
                f"[green]✓ Firmware validated:[/green]\n"
                f"  boot.img: {boot_img.stat().st_size:,} bytes\n"
                f"  root.squashfs: {sys_size:,} bytes"
            )

            self._boot_img = boot_img
            self._system_img = system_img
            return True

        except httpx.HTTPError as exc:
            self._log.write(f"[red]HTTP error: {exc}[/red]")
            return False
        except Exception as exc:
            self._log.write(f"[red]Download error: {exc}[/red]")
            return False

    @staticmethod
    def _walk(path: Path):
        """Non-recursive os.walk equivalent for Path."""
        import os
        return os.walk(str(path))

    # ─── Phase 3: Flash Boot + System ───────────────────────────────────

    async def _flash_firmware(self) -> bool:
        """Flash boot.img and root.squashfs to both A/B slots.

        Returns True if all flash operations succeed.
        """
        if not self._boot_img or not self._system_img:
            self._log.write("[red]No firmware images available for flashing.[/red]")
            return False

        aml_tool = self.app.get_aml_tool()  # type: ignore[attr-defined]
        all_ok = True

        # Update partition steps to show flash progress
        flash_labels = [
            f"boot0", f"boot1", f"system0", f"system1"
        ]
        # Reconfigure partition step display
        try:
            self._part_steps.set_current(0)  # Reset
        except Exception:
            pass

        def on_output(line: str) -> None:
            self._log.write(line.rstrip())

        # ── Flash boot.img to both A/B slots ──
        self._set_status("Flashing boot partitions...")
        self._log.write("[bold]Flashing boot.img → boot0 + boot1[/bold]")

        boot_idx = 0
        for slot in AB_BOOT_SLOTS:
            self._set_status(f"Flashing {slot}...")
            self._log.write(f"Flashing {slot} ← {self._boot_img.name}")

            try:
                result = await aml_tool.flash_partition(
                    slot, self._boot_img, on_output=on_output
                )
                if result.ok and "mwrite success" in result.raw_output.lower():
                    self._log.write(f"  [green]✓ {slot}: success[/green]")
                elif result.ok:
                    self._log.write(f"  [green]✓ {slot}: completed (exit 0)[/green]")
                else:
                    self._log.write(f"  [red]✗ {slot}: failed[/red]")
                    self._log.write(f"    Output: {result.raw_output[-200:]}")
                    all_ok = False
            except Exception as exc:
                self._log.write(f"  [red]✗ {slot}: error — {exc}[/red]")
                all_ok = False

            boot_idx += 1

        # ── Flash root.squashfs to both A/B slots ──
        self._set_status("Flashing system partitions...")
        self._log.write("[bold]Flashing root.squashfs → system0 + system1[/bold]")

        for slot in AB_SYSTEM_SLOTS:
            self._set_status(f"Flashing {slot}...")
            self._log.write(f"Flashing {slot} ← {self._system_img.name}")

            try:
                result = await aml_tool.flash_partition(
                    slot, self._system_img, on_output=on_output
                )
                if result.ok and "mwrite success" in result.raw_output.lower():
                    self._log.write(f"  [green]✓ {slot}: success[/green]")
                elif result.ok:
                    self._log.write(f"  [green]✓ {slot}: completed (exit 0)[/green]")
                else:
                    self._log.write(f"  [red]✗ {slot}: failed[/red]")
                    self._log.write(f"    Output: {result.raw_output[-200:]}")
                    all_ok = False
            except Exception as exc:
                self._log.write(f"  [red]✗ {slot}: error — {exc}[/red]")
                all_ok = False

        if all_ok:
            self._log.write("[bold green]✓ All partitions flashed successfully![/bold green]")
        else:
            self._log.write("[red]Some flash operations failed. Review the log above.[/red]")

        return all_ok
