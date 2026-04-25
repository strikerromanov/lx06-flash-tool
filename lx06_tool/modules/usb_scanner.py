"""
USB scanner and handshake module for LX06 Flash Tool (Phase 1).

Handles:
- udev rules injection for Amlogic USB burning mode detection
- udev rules reload (no reboot required)
- USB device presence monitoring
- Orchestration of the Amlogic handshake loop
- User guidance for physical device connection

The LX06 enters USB burning mode for ~2 seconds after power-on when
test pads are shorted. The handshake loop polls at 100ms intervals
to catch this brief window.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from lx06_tool.constants import (
    AML_USB_PRODUCT_ID,
    AML_USB_VENDOR_ID,
    HANDSHAKE_POLL_INTERVAL_SEC,
    HANDSHAKE_TIMEOUT_SEC,
    UDEV_RULES_CONTENT,
    UDEV_RULES_FILENAME,
)
from lx06_tool.exceptions import (
    HandshakeTimeoutError,
    PermissionDeniedError,
    USBError,
)
from lx06_tool.utils.amlogic import AmlogicTool, DeviceInfo
from lx06_tool.utils.runner import AsyncRunner

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────────

UDEV_RULES_DIR = Path("/lib/udev/rules.d")
UDEV_RULES_DIR_ALT = Path("/etc/udev/rules.d")


class USBScanner:
    """Manages USB device detection, udev rules, and the Amlogic handshake.

    This module orchestrates the complete USB connection workflow:
    1. Install udev rules for Amlogic USB burning mode
    2. Reload udev so rules take effect immediately
    3. Wait for user to plug in the device
    4. Run the handshake loop to catch the 2-second bootloader window

    Usage:
        scanner = USBScanner(runner=runner)
        await scanner.install_udev_rules()
        device = await scanner.wait_for_device(
            update_exe_path=Path("./tools/aml-flash-tool/update"),
            on_status=ui_status_callback,
            on_attempt=ui_attempt_callback,
        )
        print(f"Connected: {device.serial}")
    """

    def __init__(self, runner: AsyncRunner | None = None):
        self._runner = runner or AsyncRunner(default_timeout=30.0, sudo=True)
        self._udev_installed = False
        self._device_connected = False

    @property
    def udev_installed(self) -> bool:
        """Whether udev rules have been installed."""
        return self._udev_installed

    @property
    def device_connected(self) -> bool:
        """Whether a device is currently connected."""
        return self._device_connected

    # ── udev Rules Management ────────────────────────────────────────────────

    async def install_udev_rules(
        self,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> Path:
        """Install udev rules for Amlogic USB burning mode detection.

        Writes the rules file to /lib/udev/rules.d/ (or /etc/udev/rules.d/ as fallback),
        then reloads udev so the rules take effect immediately without a reboot.

        Args:
            on_output: Callback for status messages.

        Returns:
            Path to the installed rules file.

        Raises:
            PermissionDeniedError: If we can't write to the udev directory.
            USBError: If udev reload fails.
        """
        # Determine target directory
        rules_dir = UDEV_RULES_DIR if UDEV_RULES_DIR.exists() else UDEV_RULES_DIR_ALT
        if not rules_dir.exists():
            raise USBError(
                f"udev rules directory not found. Tried: {UDEV_RULES_DIR}, {UDEV_RULES_DIR_ALT}"
            )

        rules_file = rules_dir / UDEV_RULES_FILENAME

        logger.info("Installing udev rules to %s", rules_file)
        if on_output:
            on_output("stdout", f"Installing udev rules to {rules_file}...")

        # Write rules file (needs sudo for /lib/udev/rules.d)
        result = await self._runner.run(
            ["tee", str(rules_file)],
            input_text=UDEV_RULES_CONTENT,
            sudo=True,
        )

        if not result.success:
            raise PermissionDeniedError(
                f"Failed to write udev rules to {rules_file}",
                details=f"Error: {result.stderr}. Try running with sudo.",
            )

        # Reload udev rules
        await self._reload_udev(on_output=on_output)

        self._udev_installed = True
        logger.info("udev rules installed and reloaded successfully")
        if on_output:
            on_output("stdout", "udev rules installed. No reboot required.")

        return rules_file

    async def _reload_udev(
        self,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Reload udev rules so they take effect immediately.

        Runs:
            sudo udevadm control --reload-rules
            sudo udevadm trigger
        """
        logger.info("Reloading udev rules...")
        if on_output:
            on_output("stdout", "Reloading udev rules...")

        # Reload rules
        result = await self._runner.run(
            ["udevadm", "control", "--reload-rules"],
            timeout=10,
            sudo=True,
        )
        if not result.success:
            raise USBError(
                f"Failed to reload udev rules: {result.stderr}",
                details="Ensure udevadm is installed and you have root permissions.",
            )

        # Trigger to apply to existing devices
        result = await self._runner.run(
            ["udevadm", "trigger"],
            timeout=10,
            sudo=True,
        )
        if not result.success:
            logger.warning("udevadm trigger failed (non-fatal): %s", result.stderr)
            # Not fatal — rules will apply to new devices regardless

        logger.info("udev rules reloaded")

    async def check_udev_rules(self) -> bool:
        """Check if udev rules are already installed.

        Returns:
            True if the rules file exists with correct content.
        """
        for rules_dir in [UDEV_RULES_DIR, UDEV_RULES_DIR_ALT]:
            rules_file = rules_dir / UDEV_RULES_FILENAME
            if rules_file.exists():
                try:
                    content = rules_file.read_text()
                    if AML_USB_VENDOR_ID in content and AML_USB_PRODUCT_ID in content:
                        logger.debug("udev rules found at %s", rules_file)
                        self._udev_installed = True
                        return True
                except Exception:
                    pass
        return False

    # ── USB Device Detection ─────────────────────────────────────────────────

    async def check_usb_device_present(self) -> bool:
        """Check if an Amlogic device is currently connected via USB.

        Uses lsusb to scan for the Amlogic vendor/product ID.

        Returns:
            True if the device is detected in lsusb output.
        """
        result = await self._runner.run(
            ["lsusb"],
            timeout=5,
            sudo=False,
        )
        if result.success:
            # Look for Amlogic device: 1b8e:c003
            for line in result.stdout.splitlines():
                if AML_USB_VENDOR_ID in line.lower() and AML_USB_PRODUCT_ID in line.lower():
                    logger.debug("Amlogic USB device detected via lsusb")
                    return True
        return False

    async def monitor_usb_presence(
        self,
        poll_interval: float = 1.0,
        timeout: float = 300.0,
        *,
        on_status: Callable[[bool], None] | None = None,
    ) -> bool:
        """Monitor USB device presence until it appears or timeout.

        Useful for detecting when the user plugs in the device.

        Args:
            poll_interval: Seconds between checks.
            timeout: Maximum seconds to wait.
            on_status: Callback(present) for each check.

        Returns:
            True if device was detected, False on timeout.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            present = await self.check_usb_device_present()
            if on_status:
                try:
                    on_status(present)
                except Exception:
                    pass
            if present:
                return True
            await asyncio.sleep(poll_interval)
        return False

    # ── Device Handshake ─────────────────────────────────────────────────────

    async def wait_for_device(
        self,
        update_exe_path: Path,
        *,
        timeout: float = HANDSHAKE_TIMEOUT_SEC,
        poll_interval: float = HANDSHAKE_POLL_INTERVAL_SEC,
        on_status: Callable[[str], None] | None = None,
        on_attempt: Callable[[int, float], None] | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> DeviceInfo:
        """Wait for the user to connect the device and complete the USB handshake.

        This is the main entry point for the USB connection workflow:
        1. Instruct the user to plug in the device
        2. Run the Amlogic handshake loop to catch the bootloader window
        3. Return device info on success

        Args:
            update_exe_path: Path to the update.exe binary.
            timeout: Maximum seconds to wait for handshake.
            poll_interval: Seconds between identify attempts.
            on_status: Callback(status_message) for user-facing instructions.
            on_attempt: Callback(attempt_number, elapsed_seconds) for progress.
            on_output: Callback(stream, line) for raw output.

        Returns:
            DeviceInfo with device details.

        Raises:
            HandshakeTimeoutError: If device not detected within timeout.
            AmlogicToolError: If the update binary fails.
        """
        if on_status:
            on_status(
                "🔌 Plug in your LX06 speaker via USB now.\n"
                "   The tool will automatically detect the device\n"
                "   when it enters USB burning mode.\n\n"
                "   If the device is already plugged in, try:\n"
                "   1. Unplug the USB cable\n"
                "   2. Short the test pads on the PCB\n"
                "   3. Plug in the USB cable while holding the short"
            )

        logger.info("Waiting for LX06 device connection (timeout=%ds)...", timeout)

        # Create the Amlogic tool
        aml_tool = AmlogicTool(update_exe_path, runner=self._runner)

        # Run the handshake loop
        try:
            device = await aml_tool.handshake_loop(
                timeout=timeout,
                poll_interval=poll_interval,
                on_attempt=on_attempt,
            )
        except HandshakeTimeoutError:
            if on_status:
                on_status(
                    "❌ Device not detected within timeout.\n\n"
                    "Troubleshooting:\n"
                    "1. Ensure the speaker is powered off\n"
                    "2. Open the speaker case and locate the test pads\n"
                    "3. Short the test pads with tweezers\n"
                    "4. While holding the short, plug in USB power\n"
                    "5. Release the short after 3 seconds\n"
                    "6. The device should now be in USB burning mode"
                )
            raise

        self._device_connected = True
        if on_status:
            on_status(
                f"✅ Device detected!\n"
                f"   Serial: {device.serial or 'N/A'}\n"
                f"   Chip:   {device.chip_id or 'Amlogic AXG'}\n"
                f"   Board:  {device.board_name or 'LX06'}"
            )

        logger.info(
            "Device connected: serial=%s, chip=%s",
            device.serial, device.chip_id,
        )
        return device

    # ── Disconnect Detection ─────────────────────────────────────────────────

    async def wait_for_disconnect(
        self,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """Wait for the device to be disconnected.

        Useful for detecting when to proceed to the next phase
        (e.g., user needs to reboot the device after flashing).

        Args:
            poll_interval: Seconds between checks.
            timeout: Maximum seconds to wait.
            on_status: Callback for status messages.
        """
        if on_status:
            on_status("Waiting for device disconnect...")

        was_present = await self.check_usb_device_present()
        if not was_present:
            if on_status:
                on_status("Device is not connected (already disconnected).")
            self._device_connected = False
            return

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            present = await self.check_usb_device_present()
            if was_present and not present:
                # Device was present but now gone — disconnected
                self._device_connected = False
                if on_status:
                    on_status("Device disconnected.")
                logger.info("Device disconnected")
                return
            was_present = present
            await asyncio.sleep(poll_interval)

        logger.warning("Timed out waiting for device disconnect")

    # ── Reset ────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset scanner state for a new connection attempt."""
        self._device_connected = False

    async def remove_udev_rules(self) -> None:
        """Remove installed udev rules (cleanup on uninstall)." """
        for rules_dir in [UDEV_RULES_DIR, UDEV_RULES_DIR_ALT]:
            rules_file = rules_dir / UDEV_RULES_FILENAME
            if rules_file.exists():
                await self._runner.run(
                    ["rm", "-f", str(rules_file)],
                    sudo=True,
                )
                logger.info("Removed udev rules from %s", rules_file)

        await self._reload_udev()
        self._udev_installed = False
