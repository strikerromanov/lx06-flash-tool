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
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional

from lx06_tool.constants import (
    AMLOGIC_USB_PRODUCT_ID,
    AMLOGIC_USB_VENDOR_ID,
    FAST_POLL_INTERVAL_S,
    HANDSHAKE_DEFAULT_TIMEOUT_S,
    HANDSHAKE_POLL_INTERVAL_S,
    OLD_UDEV_RULES,
    UPDATE_EXE_RELPATH,
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

    Cleans up old/conflicting rules first, then writes the new rule.
    Uses PTY-based sudo for reliable password authentication on all distros.
    """
    # ── Clean up old/conflicting udev rules ──────────────────────────────
    cleaned = []
    for old_rule in OLD_UDEV_RULES:
        old_path = Path(old_rule)
        if old_path.exists():
            try:
                result = await sudo_run(
                    ["rm", "-f", old_rule],
                    password=sudo_password,
                    timeout=5,
                )
                if result.ok:
                    cleaned.append(old_rule)
                    logger.info("Removed old udev rule: %s", old_rule)
                else:
                    logger.warning(
                        "Failed to remove old udev rule %s: %s",
                        old_rule, result.output,
                    )
            except Exception as exc:
                logger.warning(
                    "Error removing old udev rule %s: %s", old_rule, exc
                )

    # ── Write rules file via PTY-based sudo ──────────────────────────────
    result = await sudo_write_file(
        UDEV_RULE_CONTENT, dest, password=sudo_password, timeout=10,
    )
    if not result.ok:
        raise UdevRulesError(
            f"Failed to write udev rules to {dest}: {result.output}"
        )

    # ── Reload and trigger udev rules ────────────────────────────────────
    for cmd in (
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=usb"],
    ):
        result = await sudo_run(cmd, password=sudo_password, timeout=15)
        if not result.ok:
            raise UdevRulesError(
                f"udevadm failed: sudo {' '.join(cmd)}\n{result.output}"
            )


async def install_udev_rules_safe(
    dest: str = UDEV_RULES_DEST,
    *,
    sudo_password: str = "",
) -> tuple[bool, list[str]]:
    """
    Non-throwing version of install_udev_rules.

    Returns (success, messages) where messages is a list of log lines.
    """
    msgs: list[str] = []
    try:
        # Clean old rules
        for old_rule in OLD_UDEV_RULES:
            if Path(old_rule).exists():
                result = await sudo_run(
                    ["rm", "-f", old_rule],
                    password=sudo_password, timeout=5,
                )
                if result.ok:
                    msgs.append(f"Removed old rule: {old_rule}")
                else:
                    msgs.append(f"Warning: could not remove {old_rule}")

        # Write new rule
        result = await sudo_write_file(
            UDEV_RULE_CONTENT, dest,
            password=sudo_password, timeout=10,
        )
        if not result.ok:
            msgs.append(f"Failed to write udev rules: {result.output}")
            return False, msgs

        # Reload
        for cmd in (
            ["udevadm", "control", "--reload-rules"],
            ["udevadm", "trigger", "--subsystem-match=usb"],
        ):
            result = await sudo_run(
                cmd, password=sudo_password, timeout=15,
            )
            if not result.ok:
                msgs.append(f"udevadm failed: {' '.join(cmd)}")
                return False, msgs

        msgs.append("udev rules installed and reloaded.")
        return True, msgs
    except Exception as exc:
        msgs.append(f"Error: {exc}")
        return False, msgs


def udev_rules_installed(dest: str = UDEV_RULES_DEST) -> bool:
    """Return True if the udev rules file already exists."""
    return Path(dest).exists()


def get_udev_rules_content(dest: str = UDEV_RULES_DEST) -> str:
    """Read the current udev rules file content, or empty string if missing."""
    try:
        return Path(dest).read_text().strip()
    except OSError:
        return ""


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


# ─── USB Diagnostics ──────────────────────────────────────────────────────────

async def test_usb_detection(
    update_exe_path: str | Path | None = None,
    sudo_password: str = "",
) -> dict[str, Any]:
    """
    Run all USB detection checks and return a comprehensive report.

    This function is designed to be called from the UI to show a
    pre-flight diagnostic checklist before starting the USB scan.

    Returns a dict with all check results and diagnostic info.
    """
    from lx06_tool.utils.runner import run as async_run

    results: dict[str, Any] = {
        # Basic system checks
        "sysfs_available": Path("/sys/bus/usb/devices").is_dir(),
        "lsusb_installed": shutil.which("lsusb") is not None,

        # Device detection
        "device_present_sysfs": _check_sysfs(),
        "device_present_lsusb": False,

        # udev rules
        "udev_rules_installed": udev_rules_installed(),
        "udev_rules_path": UDEV_RULES_DEST,
        "udev_rules_content": get_udev_rules_content(),

        # Binary checks
        "update_exe_found": False,
        "update_exe_path": None,
        "update_exe_executable": False,
        "update_exe_test_output": "",

        # Library checks
        "libusb_available": False,
        "libusb_detail": "",

        # Old rules cleanup
        "old_rules_found": [],

        # Overall
        "ready_to_scan": False,
        "issues": [],
    }

    # ── Check for old conflicting rules ──────────────────────────────────
    for old_rule in OLD_UDEV_RULES:
        if Path(old_rule).exists():
            results["old_rules_found"].append(old_rule)

    # ── Check device via lsusb ───────────────────────────────────────────
    results["device_present_lsusb"] = await _check_lsusb()

    # ── Locate the update binary ─────────────────────────────────────────
    exe_candidates = []
    if update_exe_path:
        exe_candidates.append(Path(update_exe_path))

    # Check XDG data dir location
    try:
        from platformdirs import user_data_path
        from lx06_tool.constants import APP_NAME, TOOLS_SUBDIR
        xdg_tools = user_data_path(APP_NAME) / TOOLS_SUBDIR
        exe_candidates.append(xdg_tools / "aml-flash-tool" / UPDATE_EXE_RELPATH)
    except Exception:
        pass

    # Common fallback paths
    exe_candidates.extend([
        Path("/usr/local/bin/aml-flash-tool/update"),
        Path("/usr/local/bin/aml-flash-tool/tools/linux-x86/update"),
        Path("tools/aml-flash-tool/tools/linux-x86/update"),
    ])

    for candidate in exe_candidates:
        if candidate.exists():
            results["update_exe_found"] = True
            results["update_exe_path"] = str(candidate)
            results["update_exe_executable"] = os.access(candidate, os.X_OK)
            break

    # ── Check libusb ─────────────────────────────────────────────────────
    # Check for libusb-0.1 / libusb-compat which the Amlogic binary needs
    libusb_checks = [
        # Direct library path checks
        "/usr/lib/libusb.so",
        "/usr/lib/libusb-0.1.so",
        "/usr/lib/libusb-1.0.so",
        "/usr/lib/x86_64-linux-gnu/libusb.so",
        "/usr/lib/x86_64-linux-gnu/libusb-0.1.so",
    ]
    # Also check via ldconfig
    try:
        ldconfig = await async_run(["ldconfig", "-p"], timeout=5)
        if ldconfig.returncode == 0:
            for line in ldconfig.stdout.splitlines():
                if "libusb" in line.lower() and ".so" in line:
                    results["libusb_detail"] = line.strip()
                    # libusb-0.1 is what the Amlogic binary needs
                    if "libusb-0.1" in line or "libusb.so" in line:
                        results["libusb_available"] = True
                        break
    except Exception:
        pass

    # Fallback: check if the library files exist directly
    if not results["libusb_available"]:
        for lib_path in libusb_checks:
            if Path(lib_path).exists() or Path(lib_path + ".4").exists():
                results["libusb_available"] = True
                results["libusb_detail"] = lib_path
                break

    # Also check via pacman on Arch
    if not results["libusb_available"]:
        try:
            pacman_check = await async_run(
                ["pacman", "-Q", "libusb-compat"], timeout=5,
            )
            if pacman_check.returncode == 0:
                results["libusb_available"] = True
                results["libusb_detail"] = pacman_check.stdout.strip()
        except Exception:
            pass

    # ── Try running the binary ───────────────────────────────────────────
    if results["update_exe_found"] and results["update_exe_executable"]:
        exe = Path(results["update_exe_path"])
        try:
            # Try 'update help' first — doesn't need a device
            test_result = await async_run(
                [str(exe), "help"], timeout=5,
            )
            output = (test_result.stdout + test_result.stderr).strip()
            if output:
                results["update_exe_test_output"] = f"OK: {output[:200]}"
            else:
                # Some versions don't have 'help', try running without args
                test_result = await async_run(
                    [str(exe)], timeout=5,
                )
                output = (test_result.stdout + test_result.stderr).strip()
                results["update_exe_test_output"] = (
                    f"RC={test_result.returncode}: {output[:200]}"
                )
        except Exception as exc:
            results["update_exe_test_output"] = f"ERROR: {exc}"

            # Check for common issues
            exc_str = str(exc).lower()
            if "cannot execute" in exc_str or "exec format" in exc_str:
                results["issues"].append(
                    "Binary is wrong architecture (e.g. ARM binary on x86_64)"
                )
            elif "shared library" in exc_str or "not found" in exc_str:
                results["issues"].append(
                    f"Missing shared library: {exc}. Install libusb-compat."
                )

    # ── Determine overall readiness ──────────────────────────────────────
    issues = results["issues"]

    if not results["sysfs_available"] and not results["lsusb_installed"]:
        issues.append("No USB detection method available (no sysfs or lsusb)")

    if not results["update_exe_found"]:
        issues.append(
            "AML update binary not found. Run Environment Setup to download it."
        )
    elif not results["update_exe_executable"]:
        issues.append(
            "AML update binary is not executable. Check file permissions."
        )

    if not results["libusb_available"]:
        issues.append(
            "libusb not detected. Install libusb-compat (Arch) or libusb-0.1-4 (Debian)."
        )

    if results["old_rules_found"]:
        issues.append(
            f"Old conflicting udev rules found: {', '.join(results['old_rules_found'])}. "
            "Click Start USB Scan to clean them up."
        )

    results["ready_to_scan"] = (
        results["update_exe_found"]
        and results["update_exe_executable"]
        and results["libusb_available"]
        and (results["sysfs_available"] or results["lsusb_installed"])
        and len([i for i in issues if "Old conflicting" not in i]) == 0
    )

    return results


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
