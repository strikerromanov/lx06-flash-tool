"""
lx06_tool/modules/usb_scanner.py
----------------------------------
Phase 1: udev rules installation and USB handshake orchestration.

The Amlogic AXG (LX06) enters USB burning mode for approximately 2 seconds
after power-on with the test-pad shorted. We use a **two-phase** detection
strategy to reliably catch this narrow window:

  Phase 1 — Fast sysfs/lsusb polling (50 ms interval)
      Reads /sys/bus/usb/devices/*/idVendor directly — no subprocess overhead.
      Falls back to `lsusb` if sysfs is unavailable.
      Detects the Amlogic VID:PID (1b8e:c003) within milliseconds.

  Phase 2 — `update identify` handshake
      Once the USB device is visible to the kernel, we immediately run the
      Amlogic `update identify` command to complete the USB handshake and
      keep the device in burning mode beyond the 2-second window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from lx06_tool.constants import (
    AMLOGIC_USB_PRODUCT_ID,
    AMLOGIC_USB_VENDOR_ID,
    FAST_POLL_INTERVAL_S,
    HANDSHAKE_DEFAULT_TIMEOUT_S,
    HANDSHAKE_POLL_INTERVAL_S,
    UDEV_RULE_LINE,
    UDEV_RULES_DEST,
)
from lx06_tool.exceptions import (
    DeviceDisconnectedError,
    HandshakeTimeoutError,
    UdevRulesError,
)
from lx06_tool.utils.amlogic import AmlogicDeviceInfo, AmlogicTool
from lx06_tool.utils.sudo import sudo_run, sudo_write_file

logger = logging.getLogger(__name__)


# ─── udev Rules ───────────────────────────────────────────────────────────────

UDEV_RULE_CONTENT = f"""\
# Amlogic USB burning mode — LX06 flash tool
# Compatible with systemd-udevd (Arch/CachyOS) and eudev (Gentoo)
# Uses MODE=0666 for universal access + uaccess tag for console users
{UDEV_RULE_LINE}
"""


async def install_udev_rules(
    dest: str = UDEV_RULES_DEST,
    *,
    sudo_password: str = "",
) -> None:
    """
    Write the Amlogic udev rule and reload the rule database.

    Uses PTY-based sudo for reliable password authentication on all distros.
    """
    # Write rules file via PTY-based sudo
    result = await sudo_write_file(
        UDEV_RULE_CONTENT, dest, password=sudo_password, timeout=10,
    )
    if not result.ok:
        raise UdevRulesError(
            f"Failed to write udev rules to {dest}: {result.output}"
        )

    # Reload and trigger udev rules
    for cmd in (
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=usb"],
    ):
        result = await sudo_run(cmd, password=sudo_password, timeout=15)
        if not result.ok:
            raise UdevRulesError(
                f"udevadm failed: sudo {' '.join(cmd)}\n{result.output}"
            )


def udev_rules_installed(dest: str = UDEV_RULES_DEST) -> bool:
    """Return True if the udev rules file already exists."""
    return Path(dest).exists()


# ─── Fast USB Device Detection ────────────────────────────────────────────────

def _check_sysfs() -> bool:
    """
    Check /sys/bus/usb/devices/ for the Amlogic VID:PID.

    This is the fastest detection method — pure filesystem reads, no subprocess.
    Returns True if a device matching 1b8e:c003 is found.
    """
    sysfs_usb = Path("/sys/bus/usb/devices")
    if not sysfs_usb.is_dir():
        return False

    try:
        for dev_path in sysfs_usb.iterdir():
            # Skip interfaces — we only want device directories
            if ":" in dev_path.name:
                continue
            vendor_file = dev_path / "idVendor"
            product_file = dev_path / "idProduct"
            try:
                vendor = vendor_file.read_text().strip().lower()
                product = product_file.read_text().strip().lower()
                if vendor == AMLOGIC_USB_VENDOR_ID and product == AMLOGIC_USB_PRODUCT_ID:
                    logger.debug("Found Amlogic device at %s", dev_path)
                    return True
            except (OSError, FileNotFoundError):
                continue
    except OSError:
        pass
    return False


async def _check_lsusb() -> bool:
    """
    Fallback: run `lsusb` and grep for the Amlogic VID:PID.
    Slower than sysfs but works if /sys is not mounted.
    """
    from lx06_tool.utils.runner import run as async_run
    try:
        result = await async_run(
            ["lsusb", "-d", f"{AMLOGIC_USB_VENDOR_ID}:{AMLOGIC_USB_PRODUCT_ID}"],
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_usb_device_present() -> bool:
    """
    Synchronous fast check: is the Amlogic USB device visible to the kernel?

    Uses multiple detection strategies:
      1. /sys/bus/usb/devices/ sysfs scan (fastest, no subprocess)
      2. lsusb fallback (slower, spawns subprocess)

    Returns True if the device with VID=1b8e PID=c003 is present.
    """
    # Phase 1: sysfs (instant)
    if _check_sysfs():
        return True

    # Phase 2: lsusb (subprocess, ~100ms)
    # Can't call async from sync, so we return False here.
    # The async handshake loop will use _check_lsusb as fallback.
    return False


async def check_usb_device_present_async() -> bool:
    """
    Async version: check if Amlogic USB device is present.

    Uses sysfs first (instant), falls back to lsusb (subprocess).
    """
    if _check_sysfs():
        return True
    return await _check_lsusb()


# ─── Two-Phase Handshake Loop ─────────────────────────────────────────────────

async def handshake_loop(
    tool: AmlogicTool,
    *,
    timeout: int = HANDSHAKE_DEFAULT_TIMEOUT_S,
    fast_poll: float = FAST_POLL_INTERVAL_S,
    identify_poll: float = HANDSHAKE_POLL_INTERVAL_S,
    sudo_password: str = "",
    on_attempt: Optional[Callable[[int, int, str], None]] = None,
    on_phase: Optional[Callable[[str], None]] = None,
) -> AmlogicDeviceInfo:
    """
    Two-phase USB handshake for the Amlogic burning mode.

    The bootloader USB window is only ~2 seconds, so we must detect the
    device *immediately* and run `update identify` to hold it open.

    **Phase 1 — Fast device detection**
    Polls sysfs/lsusb every `fast_poll` seconds (default 50 ms).
    This catches the kernel-level USB enumeration with minimal latency.

    **Phase 2 — Amlogic handshake**
    Once the USB device appears, immediately run `update identify`.
    This completes the Amlogic-specific handshake and extends the
    bootloader window indefinitely (device stays in burning mode).
    Retries `identify` at `identify_poll` intervals if the first call
    fails (the device may still be initializing).

    identify_poll : Seconds between identify retries in phase 2 (default 100 ms).
    sudo_password : Password for sudo fallback if identify needs elevated privileges.
    on_attempt    : Called with (attempt_number, elapsed_seconds, phase_name).
    on_phase      : Called with ("fast" | "identify") when phase changes.

    Returns
    -------
    AmlogicDeviceInfo on success.

    Raises
    ------
    HandshakeTimeoutError if the device is not identified within `timeout`.
    """
    start = time.monotonic()
    attempt = 0
    phase = "fast"
    device_seen = False
    identify_attempts = 0
    max_identify_attempts = 40  # 40 × 100ms = 4 seconds of identify retries

    while True:
        elapsed = time.monotonic() - start
        elapsed_int = int(elapsed)

        if elapsed >= timeout:
            raise HandshakeTimeoutError(timeout)

        attempt += 1

        # ── Phase 1: Fast device detection via sysfs ──────────────────────
        if not device_seen:
            if on_attempt:
                on_attempt(attempt, elapsed_int, "fast")

            # Fast sysfs check (no subprocess)
            found = _check_sysfs()

            # Fallback to lsusb every 5th attempt (reduces subprocess overhead)
            if not found and attempt % 5 == 0:
                found = await _check_lsusb()

            if found:
                device_seen = True
                phase = "identify"
                identify_attempts = 0
                logger.info(
                    "Amlogic USB device detected at %.1fs — starting identify",
                    elapsed,
                )
                if on_phase:
                    on_phase("identify")
                # Fall through to phase 2 immediately — no sleep!
            else:
                await asyncio.sleep(fast_poll)
                continue

        # ── Phase 2: Amlogic identify handshake ──────────────────────────
        identify_attempts += 1

        if on_attempt:
            on_attempt(attempt, elapsed_int, "identify")

        if identify_attempts > max_identify_attempts:
            # Device vanished or identify can't connect — reset to phase 1
            logger.warning(
                "identify failed %d times — resetting to fast poll",
                identify_attempts,
            )
            device_seen = False
            phase = "fast"
            if on_phase:
                on_phase("fast")
            await asyncio.sleep(fast_poll)
            continue
        try:
            info = await tool.identify(timeout=2, sudo_password=sudo_password)
            if info.identified:
                logger.info(
                    "Device identified after %.1fs (identify attempt %d)",
                    elapsed,
                    identify_attempts,
                )
                return info
        except Exception as exc:
            # identify() can raise on USB errors — keep retrying
            logger.debug("identify attempt %d failed: %s", identify_attempts, exc)

        await asyncio.sleep(identify_poll)


# ─── Disconnect Detection ─────────────────────────────────────────────────────

async def wait_for_disconnect(
    tool: AmlogicTool,
    *,
    check_interval: float = 1.0,
    timeout: int = 30,
) -> None:
    """
    Poll until the device stops responding to `identify`.
    Useful for detecting safe removal after a flash operation.

    Raises DeviceDisconnectedError if still connected after `timeout`.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            info = await tool.identify(timeout=2)
            if not info.identified:
                return
        except Exception:
            return  # Exception = device gone = success
        await asyncio.sleep(check_interval)

    raise DeviceDisconnectedError(
        f"Device still connected after {timeout}s — expected disconnection."
    )
