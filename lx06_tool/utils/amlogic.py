"""
Amlogic update.exe CLI wrapper for LX06 Flash Tool.

Provides a typed, async interface to the aml-flash-tool's `update` binary
for device identification, partition operations, and firmware flashing.

The `update` binary (confusingly named update.exe despite being a Linux ELF)
is the core tool for communicating with Amlogic SoCs in USB burning mode.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lx06_tool.constants import (
    HANDSHAKE_POLL_INTERVAL_SEC,
    HANDSHAKE_TIMEOUT_SEC,
)
from lx06_tool.exceptions import (
    AmlogicToolError,
    HandshakeTimeoutError,
    DeviceDisconnectedError,
)
from lx06_tool.utils.runner import AsyncRunner, CommandResult

logger = logging.getLogger(__name__)


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class DeviceInfo:
    """Parsed device identification from update.exe identify."""

    serial: str = ""
    chip_id: str = ""
    board_name: str = ""
    raw_output: str = ""


@dataclass
class PartitionInfo:
    """Information about a device partition."""

    name: str
    offset: int = 0
    size: int = 0
    part_type: str = ""


# ── Amlogic Tool Wrapper ────────────────────────────────────────────────────


class AmlogicTool:
    """High-level async wrapper around the Amlogic update.exe CLI tool.

    Handles:
    - Device identification (USB handshake)
    - Bootloader unlock (setenv/saveenv)
    - Partition read (mread) and write (partition)
    - Bulk commands (bulkcmd)

    Usage:
        tool = AmlogicTool(update_exe_path=Path("./tools/aml-flash-tool/update"))
        device = await tool.identify(timeout=60)
        print(f"Found device: {device.serial}")
    """

    def __init__(self, update_exe_path: Path, runner: AsyncRunner | None = None):
        self._update_exe = update_exe_path
        self._runner = runner or AsyncRunner(default_timeout=60.0)

        if not update_exe_path.exists():
            raise AmlogicToolError(
                f"update.exe not found at: {update_exe_path}. "
                f"Run the environment setup to download aml-flash-tool."
            )

    @property
    def update_exe(self) -> Path:
        """Path to the update.exe binary."""
        return self._update_exe

    # ── Device Identification ────────────────────────────────────────────────

    async def identify(self, timeout: float | None = None) -> DeviceInfo:
        """Run update.exe identify once and parse the result.

        Args:
            timeout: Command timeout in seconds.

        Returns:
            DeviceInfo with parsed device details.

        Raises:
            AmlogicToolError: If identify fails (device not in burning mode).
        """
        result = await self._run_update("identify", timeout=timeout or 10)
        if not result.success:
            raise AmlogicToolError(
                f"Device identification failed: {result.stderr}",
                details="Ensure the device is in USB burning mode",
            )
        return self._parse_identify_output(result.combined_output)

    async def handshake_loop(
        self,
        timeout: float = HANDSHAKE_TIMEOUT_SEC,
        poll_interval: float = HANDSHAKE_POLL_INTERVAL_SEC,
        on_attempt: Callable[[int, float], None] | None = None,
    ) -> DeviceInfo:
        """Poll update.exe identify until the device is detected.

        The LX06 bootloader enters USB burning mode for ~2 seconds after
        power-on when test pads are shorted. This loop catches that window.

        Args:
            timeout: Maximum seconds to poll.
            poll_interval: Seconds between identify attempts (default 100ms).
            on_attempt: Callback(attempt_number, elapsed_seconds) for UI updates.

        Returns:
            DeviceInfo once the device is detected.

        Raises:
            HandshakeTimeoutError: If the device is not detected within timeout.
        """
        start = asyncio.get_event_loop().time()
        attempt = 0

        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= timeout:
                raise HandshakeTimeoutError(
                    f"Device not detected after {timeout:.0f}s "
                    f"({attempt} attempts)",
                    details=(
                        "Make sure the speaker is connected via USB and "
                        "powered on with test pads shorted for burning mode."
                    ),
                )

            attempt += 1
            try:
                result = await self._run_update("identify", timeout=5)
                if result.success:
                    device = self._parse_identify_output(result.combined_output)
                    logger.info(
                        "Device detected after %d attempts (%.1fs): %s",
                        attempt, elapsed, device.serial or "unknown",
                    )
                    return device
            except AmlogicToolError:
                pass  # Expected when device is not in burning mode
            except Exception as exc:
                logger.debug("Identify attempt %d failed: %s", attempt, exc)

            if on_attempt:
                try:
                    on_attempt(attempt, elapsed)
                except Exception:
                    pass

            await asyncio.sleep(poll_interval)

    # ── Bootloader Operations ────────────────────────────────────────────────

    async def bulkcmd(self, command: str, timeout: float = 30) -> CommandResult:
        """Send a bulk command to the device via update.exe bulkcmd.

        Used for U-boot environment manipulation (setenv, saveenv).

        Args:
            command: The command string to send.
            timeout: Command timeout.

        Returns:
            CommandResult from the execution.
        """
        result = await self._run_update("bulkcmd", command, timeout=timeout)
        if not result.success:
            raise AmlogicToolError(
                f"bulkcmd '{command}' failed: {result.stderr}"
            )
        return result

    async def unlock_bootloader(self, bootdelay: int = 15) -> None:
        """Unlock the U-boot bootloader by setting bootdelay.

        This allows interrupting boot for recovery if the device is bricked.

        Args:
            bootdelay: Seconds to wait at U-boot prompt (default 15).
        """
        logger.info("Unlocking bootloader (bootdelay=%d)...", bootdelay)
        await self.bulkcmd(f"setenv bootdelay {bootdelay}")
        await self.bulkcmd("saveenv")
        logger.info("Bootloader unlocked successfully")

    # ── Partition Read (Backup) ──────────────────────────────────────────────

    async def mread(self, partition: str, output_file: Path) -> Path:
        """Read a partition from the device to a file.

        Args:
            partition: Partition name (e.g. "mtd0", "system0").
            output_file: Destination file path.

        Returns:
            Path to the dump file.

        Raises:
            AmlogicToolError: If the read fails.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Reading partition %s → %s", partition, output_file.name)

        result = await self._run_update(
            "mread", partition, str(output_file),
            timeout=120,  # Larger partitions take time
        )

        if not result.success:
            raise AmlogicToolError(
                f"Failed to read partition {partition}: {result.stderr}"
            )

        if not output_file.exists():
            raise AmlogicToolError(
                f"Partition dump file not created: {output_file}"
            )

        size = output_file.stat().st_size
        logger.info("Read partition %s: %d bytes", partition, size)
        return output_file

    # ── Partition Write (Flash) ──────────────────────────────────────────────

    async def write_partition(
        self,
        partition_name: str,
        image_file: Path,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult:
        """Write an image file to a device partition.

        Args:
            partition_name: Target partition (e.g. "boot0", "system0").
            image_file: Source image file path.
            on_output: Callback for real-time output (progress tracking).

        Returns:
            CommandResult from the flash operation.

        Raises:
            AmlogicToolError: If the write fails.
        """
        if not image_file.exists():
            raise AmlogicToolError(f"Image file not found: {image_file}")

        logger.info(
            "Flashing %s (%d bytes) → %s",
            image_file.name, image_file.stat().st_size, partition_name,
        )

        result = await self._run_update(
            "partition", partition_name, str(image_file),
            timeout=300,  # Large images take time
            on_output=on_output,
        )

        if not result.success:
            raise AmlogicToolError(
                f"Failed to flash {partition_name}: {result.stderr}"
            )

        logger.info("Successfully flashed %s", partition_name)
        return result

    # ── Internal Helpers ─────────────────────────────────────────────────────

    async def _run_update(
        self,
        *args: str,
        timeout: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult:
        """Execute the update binary with given arguments."""
        cmd = [str(self._update_exe), *args]
        return await self._runner.run(
            cmd,
            timeout=timeout,
            on_output=on_output,
            sudo=True,  # update.exe typically needs root for USB access
        )

    @staticmethod
    def _parse_identify_output(output: str) -> DeviceInfo:
        """Parse the output of update.exe identify.

        Expected output format varies by firmware version but typically includes:
            serial=...
            chip_id=...
            board=...
        """
        info = DeviceInfo(raw_output=output)

        for line in output.splitlines():
            line = line.strip()
            # Match common identify output patterns
            if "serial" in line.lower() and "=" in line:
                info.serial = line.split("=", 1)[1].strip()
            elif "chip" in line.lower() and "=" in line:
                info.chip_id = line.split("=", 1)[1].strip()
            elif "board" in line.lower() and "=" in line:
                info.board_name = line.split("=", 1)[1].strip()

        # If no structured data found, try to extract from raw output
        if not info.serial:
            # Some versions just print a hex string
            hex_match = re.search(r"[0-9a-fA-F]{12,}", output)
            if hex_match:
                info.serial = hex_match.group(0)

        return info
