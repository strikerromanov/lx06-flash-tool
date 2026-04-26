"""
lx06_tool/utils/amlogic.py
---------------------------
Thin async wrapper around the Amlogic update binary (update.exe / update).

All commands go through the update tool from aml-flash-tool.
USB operations require sudo for device access.

Command reference (from official xiaoai-patch guide):
    update identify                                    — Detect device in USB burning mode
    update bulkcmd "setenv bootdelay 15"               — Set U-Boot bootdelay
    update bulkcmd "saveenv"                           — Save U-Boot environment
    update mread store <label> normal <hex_size> <out>  — Dump partition to file
    update partition <label> <image>                    — Flash partition from image
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from lx06_tool.constants import (
    BACKUP_ORDER,
    DEFAULT_FLASH_TIMEOUT,
    DEFAULT_PARTITION_TIMEOUT,
    FLASH_TIMEOUTS,
    PARTITION_MAP,
    PARTITION_TIMEOUTS,
)
from lx06_tool.utils.debug_log import log_debug, log_err, log_ok
from lx06_tool.utils.runner import RunResult, run, run_streaming
from lx06_tool.utils.sudo import SudoContext


# ─── Result Types ──────────────────────────────────────────────────────────────

class IdentifyResult:
    """Parsed output from `update identify`."""
    def __init__(self, raw_output: str) -> None:
        self.raw = raw_output
        self.success = "AmlUsbIdentifyHost" in raw_output
        # Parse firmware version line like: "This firmware version is 0-7-0-16-0-0-0-0"
        match = re.search(r"firmware version is ([\d-]+)", raw_output)
        self.firmware_version = match.group(1) if match else "unknown"


class PartitionDumpResult:
    """Result from dumping a single partition."""
    def __init__(
        self,
        mtd: str,
        label: str,
        output_path: Path,
        result: RunResult,
    ) -> None:
        self.mtd = mtd
        self.label = label
        self.output_path = output_path
        self.ok = result.ok
        self.raw_output = result.combined_output
        # Parse transferred size from output like: "[Uploading]OK:6MB in 1Sec"
        match = re.search(r"OK:(\d+)MB", result.combined_output)
        self.transferred_mb = int(match.group(1)) if match else 0


class FlashResult:
    """Result from flashing a partition."""
    def __init__(
        self,
        partition: str,
        image_path: Path,
        result: RunResult,
    ) -> None:
        self.partition = partition
        self.image_path = image_path
        self.ok = "mwrite success" in result.combined_output or result.ok
        self.raw_output = result.combined_output
        # Parse transfer size: "[update]:Transfer size 0x600000B(6MB)"
        match = re.search(r"Transfer size (\S+)", result.combined_output)
        self.transfer_size = match.group(1) if match else ""


# ─── AmlogicTool — Main Interface ─────────────────────────────────────────────

class AmlogicTool:
    """Async wrapper around the Amlogic update binary.

    Usage::

        tool = AmlogicTool(update_path="/path/to/update", sudo_ctx=SudoContext("pw"))
        result = await tool.identify()
        await tool.backup_partition("mtd4", Path("mtd4.img"))
        await tool.flash_partition("boot0", Path("boot.img"))
    """

    def __init__(
        self,
        update_path: str | Path,
        sudo_ctx: SudoContext | None = None,
    ) -> None:
        self._update = str(update_path)
        self._sudo = sudo_ctx

    # ─── Identify ───────────────────────────────────────────────────────

    async def identify(self, timeout: int = 3) -> IdentifyResult:
        """Run `update identify` to detect device in USB burning mode."""
        result = await self._run_sudo([self._update, "identify"], timeout=timeout)
        return IdentifyResult(result.combined_output)

    async def identify_loop(
        self,
        timeout_seconds: int = 120,
        on_status: object = None,
    ) -> IdentifyResult:
        """Two-phase device detection.

        Phase 1: Fast sysfs polling (no subprocess) at 50ms intervals.
        Phase 2: Run `update identify` to confirm once USB device appears.

        The USB burning mode window is ~2 seconds after power-on.
        """
        import time
        deadline = time.monotonic() + timeout_seconds
        attempt = 0
        phase = 1

        # Phase 1: Fast sysfs polling — no subprocess overhead
        sysfs_path = Path("/sys/bus/usb/devices")
        aml_vids = {"1b8e"}  # Amlogic vendor ID

        while time.monotonic() < deadline:
            attempt += 1

            if phase == 1:
                # Fast poll: just check if Amlogic USB device exists in sysfs
                try:
                    if sysfs_path.exists():
                        for dev_dir in sysfs_path.iterdir():
                            vid_file = dev_dir / "idVendor"
                            if vid_file.exists():
                                try:
                                    vid = vid_file.read_text().strip()
                                    if vid in aml_vids:
                                        log_ok("sysfs", 0, f"Amlogic device detected: {dev_dir.name}")
                                        phase = 2  # Switch to identify phase
                                        break
                                except Exception:
                                    pass
                except Exception:
                    pass

                if phase == 1:
                    if on_status and callable(on_status):
                        remaining = int(deadline - time.monotonic())
                        on_status(attempt, max(0, remaining))
                    await asyncio.sleep(0.05)  # 50ms fast poll
                    continue

            # Phase 2: Run identify to confirm and get firmware version
            if on_status and callable(on_status):
                remaining = int(deadline - time.monotonic())
                on_status(attempt, max(0, remaining))

            result = await self.identify(timeout=3)
            if result.success:
                log_ok("identify", 0, f"Device confirmed after {attempt} attempts")
                return result

            # Device disappeared from identify but was in sysfs — keep trying
            await asyncio.sleep(0.05)

        log_err("identify", -1, f"Timeout after {timeout_seconds}s ({attempt} attempts)")
        return IdentifyResult(f"Timeout after {attempt} attempts")

    # ─── Bootloader (bulkcmd) ───────────────────────────────────────────

    async def set_bootdelay(self, delay: int = 15) -> RunResult:
        """Set U-Boot bootdelay via bulkcmd.

        Uses 5-space prefix as required by some Amlogic SoCs.
        """
        result = await self.bulkcmd(f"setenv bootdelay {delay}")
        if result.ok:
            result2 = await self.bulkcmd("saveenv")
            return result2
        return result

    async def bulkcmd(self, command: str) -> RunResult:
        """Run a bulk command via `update bulkcmd`.

        The Amlogic bulkcmd protocol requires 5 spaces before the command.
        """
        # 5-space prefix per Amlogic protocol
        padded = f"     {command}"
        return await self._run_sudo(
            [self._update, "bulkcmd", padded],
            timeout=15,
        )

    # ─── Backup (mread) ─────────────────────────────────────────────────

    async def backup_partition(
        self,
        mtd: str,
        output_path: Path,
        *,
        on_output: object = None,
    ) -> PartitionDumpResult:
        """Dump a single partition to a file.

        Command: update mread store <label> normal <hex_size> <output>
        """
        info = PARTITION_MAP.get(mtd)
        if not info:
            raise ValueError(f"Unknown partition: {mtd}")

        label = info["label"]
        hex_size = info["size"]
        timeout = PARTITION_TIMEOUTS.get(label, DEFAULT_PARTITION_TIMEOUT)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [self._update, "mread", "store", label, "normal", hex(hex_size), str(output_path)]
        log_debug("INFO", f"Backing up {mtd} ({label}, {hex_size:#x}) → {output_path.name}")

        result = await self._run_sudo_streaming(
            cmd,
            timeout=timeout,
            on_output=on_output,
        )

        return PartitionDumpResult(mtd=mtd, label=label, output_path=output_path, result=result)

    async def backup_all_partitions(
        self,
        output_dir: Path,
        *,
        on_partition_start: object = None,
        on_partition_done: object = None,
        on_output: object = None,
    ) -> list[PartitionDumpResult]:
        """Backup all 7 partitions in MTD order.

        Calls on_partition_start(mtd, label, index, total) before each partition.
        Calls on_partition_done(result) after each partition completes.
        """
        results: list[PartitionDumpResult] = []
        total = len(BACKUP_ORDER)

        for idx, mtd in enumerate(BACKUP_ORDER):
            info = PARTITION_MAP[mtd]
            label = info["label"]
            output_path = output_dir / f"{mtd}.img"

            if on_partition_start and callable(on_partition_start):
                on_partition_start(mtd, label, idx, total)

            dump = await self.backup_partition(mtd, output_path, on_output=on_output)
            results.append(dump)

            if on_partition_done and callable(on_partition_done):
                on_partition_done(dump)

        return results

    # ─── Flash (partition write) ────────────────────────────────────────

    async def flash_partition(
        self,
        partition: str,
        image_path: Path,
        *,
        on_output: object = None,
    ) -> FlashResult:
        """Flash a partition from an image file.

        Command: update partition <label> <image>
        """
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        timeout = FLASH_TIMEOUTS.get(partition, DEFAULT_FLASH_TIMEOUT)

        cmd = [self._update, "partition", partition, str(image_path)]
        log_debug("INFO", f"Flashing {partition} ← {image_path.name}")

        result = await self._run_sudo_streaming(
            cmd,
            timeout=timeout,
            on_output=on_output,
        )

        return FlashResult(partition=partition, image_path=image_path, result=result)

    async def flash_boot_slots(
        self,
        boot_img: Path,
        *,
        on_output: object = None,
        on_slot_start: object = None,
        on_slot_done: object = None,
    ) -> list[FlashResult]:
        """Flash boot.img to both A/B boot slots."""
        from lx06_tool.constants import AB_BOOT_SLOTS
        results = []
        for slot in AB_BOOT_SLOTS:
            if on_slot_start and callable(on_slot_start):
                on_slot_start(slot)
            r = await self.flash_partition(slot, boot_img, on_output=on_output)
            results.append(r)
            if on_slot_done and callable(on_slot_done):
                on_slot_done(r)
        return results

    async def flash_system_slots(
        self,
        system_img: Path,
        *,
        on_output: object = None,
        on_slot_start: object = None,
        on_slot_done: object = None,
    ) -> list[FlashResult]:
        """Flash root.squashfs to both A/B system slots."""
        from lx06_tool.constants import AB_SYSTEM_SLOTS
        results = []
        for slot in AB_SYSTEM_SLOTS:
            if on_slot_start and callable(on_slot_start):
                on_slot_start(slot)
            r = await self.flash_partition(slot, system_img, on_output=on_output)
            results.append(r)
            if on_slot_done and callable(on_slot_done):
                on_slot_done(r)
        return results

    # ─── Internal helpers ───────────────────────────────────────────────

    async def _run_sudo(self, cmd: list[str], timeout: int = 30) -> RunResult:
        """Run a command with sudo if a SudoContext is available."""
        if self._sudo and self._sudo.has_password:
            sr = await self._sudo.sudo_run(cmd, timeout=timeout)
            return RunResult(
                cmd=cmd,
                returncode=sr.returncode,
                stdout=sr.output,
                stderr="",
            )
        # Try plain sudo first (works for NOPASSWD / root)
        return await run(["sudo"] + cmd, timeout=timeout)

    async def _run_sudo_streaming(
        self,
        cmd: list[str],
        timeout: int = 300,
        on_output: object = None,
    ) -> RunResult:
        """Run a command with sudo, streaming output."""
        actual_cmd = ["sudo"] + cmd

        def _on_stdout(line: str) -> None:
            if on_output and callable(on_output):
                on_output(line)

        return await run_streaming(
            actual_cmd,
            timeout=timeout,
            on_stdout=_on_stdout,
        )
