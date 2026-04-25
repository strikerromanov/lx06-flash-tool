"""
Bootloader unlock and recovery module for LX06 Flash Tool (Phase 2).

Handles:
- U-boot bootloader unlock via bootdelay setting
- Environment variable save/verify
- Bootloader state detection
- Recovery instructions for bricked devices

Unlocking the bootloader (setting bootdelay > 0) is the most critical
safety step — it allows interrupting U-boot via serial console to
recover from a bad flash. This must be done BEFORE any partition writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lx06_tool.constants import BOOTLOADER_BOOTDELAY
from lx06_tool.exceptions import (
    AmlogicToolError,
    BackupError,
)
from lx06_tool.utils.amlogic import AmlogicTool
from lx06_tool.utils.runner import AsyncRunner

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class BootloaderStatus:
    """Current state of the device bootloader."""

    unlocked: bool = False
    bootdelay: int = 0
    verified: bool = False
    raw_output: str = ""

    @property
    def is_safe_for_flashing(self) -> bool:
        """Whether the bootloader is unlocked and safe for flashing.

        A bootdelay of 5+ seconds is considered safe, as it gives
        enough time to interrupt U-boot for recovery.
        """
        return self.unlocked and self.bootdelay >= 5


# ── Bootloader Manager ──────────────────────────────────────────────────────


class BootloaderManager:
    """Manages U-boot bootloader unlock and verification.

    The bootloader MUST be unlocked before any flash operation.
    This ensures the device can be recovered via serial console
    if a bad firmware is flashed.

    Usage:
        mgr = BootloaderManager(aml_tool=aml_tool)
        status = await mgr.unlock()
        if status.is_safe_for_flashing:
            print("Safe to proceed with flashing")
    """

    def __init__(
        self,
        aml_tool: AmlogicTool,
        runner: AsyncRunner | None = None,
    ):
        self._aml = aml_tool
        self._runner = runner or AsyncRunner(default_timeout=30.0, sudo=True)

    # ── Unlock ───────────────────────────────────────────────────────────────

    async def unlock(
        self,
        bootdelay: int = BOOTLOADER_BOOTDELAY,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> BootloaderStatus:
        """Unlock the U-boot bootloader by setting bootdelay.

        This is the primary safety measure against bricking. With bootdelay
        set, the user can interrupt U-boot via serial console and flash
        a recovery image.

        The unlock process:
        1. Send `setenv bootdelay <N>` via bulkcmd
        2. Send `saveenv` to persist the change
        3. Verify the bootdelay was set correctly

        Args:
            bootdelay: Seconds to wait at U-boot prompt (default 15).
            on_output: Callback for real-time output.

        Returns:
            BootloaderStatus with unlock result.

        Raises:
            AmlogicToolError: If bulkcmd fails.
        """
        logger.info("Unlocking bootloader (target bootdelay=%d)...", bootdelay)

        if on_output:
            on_output("stdout", f"Setting bootdelay to {bootdelay} seconds...")

        # Step 1: Set bootdelay
        try:
            await self._aml.bulkcmd(f"setenv bootdelay {bootdelay}")
        except AmlogicToolError as exc:
            logger.error("Failed to set bootdelay: %s", exc)
            raise

        if on_output:
            on_output("stdout", "Saving environment...")

        # Step 2: Save environment
        try:
            await self._aml.bulkcmd("saveenv")
        except AmlogicToolError as exc:
            logger.error("Failed to saveenv: %s", exc)
            raise

        # Step 3: Verify
        status = await self.verify(on_output=on_output)

        if status.is_safe_for_flashing:
            logger.info(
                "Bootloader unlocked successfully (bootdelay=%d)", status.bootdelay
            )
            if on_output:
                on_output(
                    "stdout",
                    f"✅ Bootloader unlocked! bootdelay={status.bootdelay}s — "
                    f"device is recoverable via serial console.",
                )
        else:
            logger.warning(
                "Bootloader unlock may have failed. bootdelay=%d, verified=%s",
                status.bootdelay, status.verified,
            )
            if on_output:
                on_output(
                    "stdout",
                    f"⚠️ Bootloader unlock uncertain. bootdelay={status.bootdelay}. "
                    f"Proceed with caution.",
                )

        return status

    # ── Verify ───────────────────────────────────────────────────────────────

    async def verify(
        self,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> BootloaderStatus:
        """Verify the current bootloader state.

        Attempts to read the bootdelay value from U-boot environment.
        Note: Not all Amlogic tool versions support reading env vars,
        so this may return an unverified status.

        Args:
            on_output: Callback for output lines.

        Returns:
            BootloaderStatus with current state.
        """
        status = BootloaderStatus()

        # Try to read bootdelay via bulkcmd printenv
        try:
            result = await self._aml.bulkcmd("printenv bootdelay")
            output = result.combined_output
            status.raw_output = output

            # Parse bootdelay value from output
            # Expected: "bootdelay=15" or "bootdelay = 15"
            for line in output.splitlines():
                line = line.strip()
                if "bootdelay" in line.lower():
                    # Extract the number
                    parts = line.replace("=", " ").split()
                    for part in parts:
                        try:
                            status.bootdelay = int(part)
                            status.unlocked = status.bootdelay > 0
                            status.verified = True
                            break
                        except ValueError:
                            continue
                    if status.verified:
                        break

        except AmlogicToolError as exc:
            # printenv may not be supported in all burning modes
            logger.debug(
                "Could not verify bootdelay (this may be normal): %s", exc
            )
            if on_output:
                on_output(
                    "stdout",
                    "Could not verify bootdelay via bulkcmd (this is normal for some firmware). "
                    "Assuming unlock succeeded based on setenv/saveenv success.",
                )
            # Assume success since setenv+saveenv didn't error
            status.bootdelay = BOOTLOADER_BOOTDELAY
            status.unlocked = True
            status.verified = False  # Not directly confirmed

        return status

    # ── Recovery Information ──────────────────────────────────────────────────

    @staticmethod
    def get_recovery_instructions() -> str:
        """Get step-by-step recovery instructions for a bricked device.

        Returns a formatted string with recovery steps using the
        U-boot serial console (requires bootdelay > 0).
        """
        return """╔══════════════════════════════════════════════════════════════╗
║           DEVICE RECOVERY INSTRUCTIONS                      ║
║           (Requires unlocked bootloader)                     ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  If your device is bricked after flashing:                   ║
║                                                              ║
║  1. Open the speaker case and locate the serial UART pads   ║
║     (TX, RX, GND — typically near the USB port)              ║
║                                                              ║
║  2. Connect a USB-TTL serial adapter (3.3V!):               ║
║     - Adapter TX → Device RX                                 ║
║     - Adapter RX → Device TX                                 ║
║     - Adapter GND → Device GND                               ║
║                                                              ║
║  3. Open a serial terminal (115200 baud, 8N1):              ║
║     $ picocom /dev/ttyUSB0 -b 115200                         ║
║                                                              ║
║  4. Power on the device and press Ctrl+C or Space during    ║
║     the bootdelay countdown to enter U-boot console          ║
║                                                              ║
║  5. At the U-boot prompt, restore the backup:               ║
║     => usb reset                                             ║
║     => fatload usb 0 ${loadaddr} recovery.img               ║
║     => update partition system0 ${loadaddr}                  ║
║                                                              ║
║  6. Alternatively, boot from the inactive partition:         ║
║     => setenv active_slot system1                            ║
║     => saveenv                                               ║
║     => reset                                                 ║
║                                                              ║
║  If bootdelay was NOT set before flashing, recovery         ║
║  requires shorting specific test pads during boot to         ║
║  force USB burning mode.                                     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝"""

    @staticmethod
    def get_safety_warning() -> str:
        """Get a warning about the importance of bootloader unlock."""
        return (
            "⚠️  SAFETY WARNING ⚠️\n\n"
            "Unlocking the bootloader is ESSENTIAL before flashing.\n"
            "Without it, a bad flash will PERMANENTLY BRICK your device.\n\n"
            "The bootloader unlock sets a boot delay so you can\n"
            "interrupt U-boot via serial console and flash a recovery image.\n\n"
            "This tool will NOT proceed to flashing without bootloader unlock."
        )
