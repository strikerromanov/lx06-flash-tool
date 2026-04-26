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
import glob
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lx06_tool.constants import (
    AMLOGIC_USB_PRODUCT_ID,
    AMLOGIC_USB_VENDOR_ID,
    FAST_POLL_INTERVAL_S,
    HANDSHAKE_DEFAULT_TIMEOUT_S,
    HANDSHAKE_POLL_INTERVAL_S,
    OLD_UDEV_RULES,
    UDEV_GLOB_DIRS,
    UDEV_GLOB_PATTERNS,
    UDEV_RULE_LINE,
    UDEV_RULES_DEST,
    UPDATE_EXE_RELPATH,
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


def _glob_old_rules() -> list[str]:
    """
    Glob-search all known udev rules directories for files matching
    Amlogic-related patterns. Returns list of matching file paths.

    This catches rules that were installed by older versions,
    manual installs, or distro-specific packages.
    """
    found: list[str] = []
    for rules_dir in UDEV_GLOB_DIRS:
        rules_path = Path(rules_dir)
        if not rules_path.is_dir():
            continue
        for pattern in UDEV_GLOB_PATTERNS:
            for match in glob.glob(str(rules_path / pattern)):
                # Skip our own rule file
                if match == UDEV_RULES_DEST:
                    continue
                if match not in found:
                    found.append(match)
    return sorted(found)


async def _remove_old_rules(
    rules: list[str],
    sudo_password: str = "",
) -> list[dict[str, str]]:
    """
    Attempt to remove each old udev rule file. Returns a list of
    {path, status, detail} dicts for logging/reporting.
    """
    results: list[dict[str, str]] = []
    for rule_path in rules:
        entry: dict[str, str] = {
            "path": rule_path,
            "status": "skipped",
            "detail": "",
        }
        if not Path(rule_path).exists():
            entry["status"] = "already_gone"
            entry["detail"] = "File does not exist"
            results.append(entry)
            continue

        # Read content for logging before removal
        try:
            content = Path(rule_path).read_text().strip()
            logger.info("Old rule %s content: %s", rule_path, content[:200])
        except OSError as exc:
            logger.warning("Cannot read old rule %s: %s", rule_path, exc)

        try:
            result = await sudo_run(
                ["rm", "-f", rule_path],
                password=sudo_password,
                timeout=5,
            )
            if result.ok:
                # Verify removal
                if not Path(rule_path).exists():
                    entry["status"] = "removed"
                    logger.info("Successfully removed old udev rule: %s", rule_path)
                else:
                    entry["status"] = "failed"
                    entry["detail"] = f"rm succeeded but file still exists! output={result.output}"
                    logger.error(
                        "BUG: rm -f %s reported success but file still exists", rule_path,
                    )
            else:
                entry["status"] = "failed"
                entry["detail"] = result.output or f"exit code {result.returncode}"
                logger.warning(
                    "Failed to remove old udev rule %s: %s", rule_path, result.output,
                )
        except Exception as exc:
            entry["status"] = "error"
            entry["detail"] = str(exc)
            logger.error("Exception removing old udev rule %s: %s", rule_path, exc)

        results.append(entry)
    return results


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
    # Phase 1: Known paths from OLD_UDEV_RULES list
    all_old = list(OLD_UDEV_RULES)

    # Phase 2: Glob for any file matching Amlogic patterns
    glob_found = _glob_old_rules()
    for g in glob_found:
        if g not in all_old:
            all_old.append(g)
            logger.info("Glob found additional old rule: %s", g)

    if all_old:
        logger.info("Cleaning up %d old/conflicting udev rules", len(all_old))
        removal_results = await _remove_old_rules(all_old, sudo_password)
        for r in removal_results:
            if r["status"] not in ("removed", "already_gone", "skipped"):
                logger.warning(
                    "Old rule cleanup issue: %s → %s (%s)",
                    r["path"], r["status"], r["detail"],
                )

    # ── Write rules file via PTY-based sudo ──────────────────────────────
    logger.info("Writing udev rules to %s", dest)
    result = await sudo_write_file(
        UDEV_RULE_CONTENT, dest, password=sudo_password, timeout=10,
    )
    if not result.ok:
        raise UdevRulesError(
            f"Failed to write udev rules to {dest}: {result.output}"
        )
    logger.info("Successfully wrote udev rules to %s", dest)

    # ── Reload and trigger udev rules ────────────────────────────────────
    for cmd in (
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=usb"],
    ):
        logger.debug("Running: sudo %s", " ".join(cmd))
        result = await sudo_run(cmd, password=sudo_password, timeout=15)
        if not result.ok:
            raise UdevRulesError(
                f"udevadm failed: sudo {' '.join(cmd)}\n{result.output}"
            )
    logger.info("udev rules reloaded and triggered")


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
        # Clean old rules — both known paths and glob matches
        all_old = list(OLD_UDEV_RULES)
        glob_found = _glob_old_rules()
        for g in glob_found:
            if g not in all_old:
                all_old.append(g)

        for old_rule in all_old:
            if Path(old_rule).exists():
                result = await sudo_run(
                    ["rm", "-f", old_rule],
                    password=sudo_password, timeout=5,
                )
                if result.ok:
                    still_exists = Path(old_rule).exists()
                    if still_exists:
                        msgs.append(f"WARNING: rm succeeded but {old_rule} still exists!")
                    else:
                        msgs.append(f"Removed old rule: {old_rule}")
                else:
                    msgs.append(f"Warning: could not remove {old_rule}: {result.output}")

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


def _check_sysfs_all_devices() -> list[dict[str, str]]:
    """
    List ALL USB devices visible via sysfs.
    Returns list of dicts with vendor, product, busnum, devnum, product_name.
    """
    devices: list[dict[str, str]] = []
    sysfs_usb = Path("/sys/bus/usb/devices")
    if not sysfs_usb.is_dir():
        return devices

    try:
        for dev_path in sysfs_usb.iterdir():
            if ":" in dev_path.name:
                continue
            info: dict[str, str] = {"sysfs_path": str(dev_path)}
            try:
                info["idVendor"] = (dev_path / "idVendor").read_text().strip()
            except OSError:
                continue
            try:
                info["idProduct"] = (dev_path / "idProduct").read_text().strip()
            except OSError:
                pass
            try:
                info["product"] = (dev_path / "product").read_text().strip()
            except OSError:
                pass
            try:
                info["busnum"] = (dev_path / "busnum").read_text().strip()
            except OSError:
                pass
            try:
                info["devnum"] = (dev_path / "devnum").read_text().strip()
            except OSError:
                pass
            devices.append(info)
    except OSError:
        pass
    return devices


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


async def _lsusb_full() -> str:
    """
    Run lsusb and return full output listing all USB devices.
    """
    from lx06_tool.utils.runner import run as async_run
    try:
        result = await async_run(["lsusb"], timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


async def _lsusb_vid_filter(vid: str) -> str:
    """
    Run lsusb filtering for a specific vendor ID.
    Returns matching lines or empty string.
    """
    from lx06_tool.utils.runner import run as async_run
    try:
        result = await async_run(["lsusb", "-d", f"{vid}:"], timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


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

    Returns a dict with all check results and diagnostic info, including:
    - Full lsusb output and device listing
    - Binary architecture and library dependency checks
    - update identify test output
    - Old udev rule cleanup with results
    - Actionable advice for each failure
    """
    from lx06_tool.utils.runner import run as async_run

    results: dict[str, Any] = {
        # Basic system checks
        "sysfs_available": Path("/sys/bus/usb/devices").is_dir(),
        "lsusb_installed": shutil.which("lsusb") is not None,

        # Device detection
        "device_present_sysfs": _check_sysfs(),
        "device_present_lsusb": False,

        # Full USB device listing
        "all_usb_devices_sysfs": _check_sysfs_all_devices(),
        "all_usb_devices_lsusb": "",
        "amlogic_vid_devices": "",

        # udev rules
        "udev_rules_installed": udev_rules_installed(),
        "udev_rules_path": UDEV_RULES_DEST,
        "udev_rules_content": get_udev_rules_content(),

        # Binary checks
        "update_exe_found": False,
        "update_exe_path": None,
        "update_exe_executable": False,
        "update_exe_arch": "",
        "update_exe_ldd": "",
        "update_exe_file_detail": "",
        "update_exe_test_output": "",
        "update_identify_output": "",

        # Library checks
        "libusb_available": False,
        "libusb_detail": "",

        # Old rules cleanup
        "old_rules_found": [],
        "old_rules_contents": {},
        "old_rules_cleanup_results": [],

        # Overall
        "ready_to_scan": False,
        "issues": [],
        "advice": [],
    }

    # ── Full USB device listing ───────────────────────────────────────────
    if results["lsusb_installed"]:
        results["all_usb_devices_lsusb"] = await _lsusb_full()
        results["amlogic_vid_devices"] = await _lsusb_vid_filter(AMLOGIC_USB_VENDOR_ID)

    # ── Check for old conflicting rules (known paths + glob) ──────────────
    all_old_rules = list(OLD_UDEV_RULES)
    glob_found = _glob_old_rules()
    for g in glob_found:
        if g not in all_old_rules:
            all_old_rules.append(g)

    for old_rule in all_old_rules:
        if Path(old_rule).exists():
            results["old_rules_found"].append(old_rule)
            # Read content for reporting
            try:
                content = Path(old_rule).read_text().strip()
                results["old_rules_contents"][old_rule] = content
            except OSError:
                results["old_rules_contents"][old_rule] = "<cannot read>"

    # Try to remove old rules right now if found
    if results["old_rules_found"]:
        cleanup = await _remove_old_rules(results["old_rules_found"], sudo_password)
        results["old_rules_cleanup_results"] = cleanup
        # Check which ones are still there after cleanup
        remaining = [r["path"] for r in cleanup if r["status"] != "removed" and r["status"] != "already_gone"]
        if remaining:
            results["issues"].append(
                f"Could not remove old rules: {', '.join(remaining)}. "
                "They may need manual sudo removal."
            )

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
            results["update_exe_path"] = str(candidate.resolve())
            results["update_exe_executable"] = os.access(candidate, os.X_OK)
            break

    # ── Binary architecture and library checks ────────────────────────────
    if results["update_exe_found"]:
        exe = Path(results["update_exe_path"])

        # Run `file` on the binary to check architecture
        try:
            file_result = await async_run(
                ["file", "-b", str(exe)], timeout=5,
            )
            if file_result.returncode == 0:
                results["update_exe_file_detail"] = file_result.stdout.strip()
                arch_info = file_result.stdout.lower()
                if "32-bit" in arch_info:
                    results["update_exe_arch"] = "32-bit"
                elif "64-bit" in arch_info:
                    results["update_exe_arch"] = "64-bit"
                elif "arm" in arch_info:
                    results["update_exe_arch"] = "ARM"
                else:
                    results["update_exe_arch"] = "unknown"
        except Exception as exc:
            results["update_exe_file_detail"] = f"Error: {exc}"

        # Run `ldd` on the binary to check shared library deps
        try:
            ldd_result = await async_run(
                ["ldd", str(exe)], timeout=5,
            )
            if ldd_result.returncode == 0:
                ldd_output = ldd_result.stdout.strip()
                results["update_exe_ldd"] = ldd_output
                # Check for missing libraries
                if "not found" in ldd_output:
                    missing = []
                    for line in ldd_output.splitlines():
                        if "not found" in line:
                            missing.append(line.strip())
                    results["issues"].append(
                        f"Missing shared libraries: {'; '.join(missing)}"
                    )
                    results["advice"].append(
                        "Install missing libraries. On Arch/CachyOS: "
                        "sudo pacman -S libusb-compat lib32-libusb-compat"
                    )
            else:
                results["update_exe_ldd"] = f"ldd failed: {ldd_result.stderr.strip()}"
        except Exception as exc:
            results["update_exe_ldd"] = f"Error: {exc}"

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

    # Also check for 32-bit libusb if the binary is 32-bit
    if results["update_exe_arch"] == "32-bit" and not results["libusb_available"]:
        try:
            pacman_check = await async_run(
                ["pacman", "-Q", "lib32-libusb-compat"], timeout=5,
            )
            if pacman_check.returncode == 0:
                results["libusb_available"] = True
                results["libusb_detail"] = f"32-bit: {pacman_check.stdout.strip()}"
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
                results["update_exe_test_output"] = f"OK: {output[:300]}"
            else:
                # Some versions don't have 'help', try running without args
                test_result = await async_run(
                    [str(exe)], timeout=5,
                )
                output = (test_result.stdout + test_result.stderr).strip()
                results["update_exe_test_output"] = (
                    f"RC={test_result.returncode}: {output[:300]}"
                )
        except Exception as exc:
            results["update_exe_test_output"] = f"ERROR: {exc}"

            # Check for common issues
            exc_str = str(exc).lower()
            if "cannot execute" in exc_str or "exec format" in exc_str:
                results["issues"].append(
                    "Binary is wrong architecture (e.g. ARM binary on x86_64)"
                )
                results["advice"].append(
                    "The update binary architecture doesn't match your system. "
                    "Check if a 64-bit version is available, or install "
                    "multilib support: sudo pacman -S lib32-libusb-compat"
                )
            elif "shared library" in exc_str or "not found" in exc_str:
                results["issues"].append(
                    f"Missing shared library: {exc}. Install libusb-compat."
                )
                results["advice"].append(
                    "Install missing libraries: "
                    "sudo pacman -S libusb-compat lib32-libusb-compat"
                )

        # Try `update identify 7` — the official handshake command
        try:
            identify_result = await async_run(
                [str(exe), "identify", "7"], timeout=5,
            )
            id_output = (identify_result.stdout + "\n" + identify_result.stderr).strip()
            rc = identify_result.returncode
            results["update_identify_output"] = f"RC={rc}: {id_output[:300]}"
            if rc != 0:
                results["advice"].append(
                    f"update identify returned exit code {rc}. "
                    "This is expected if no device is connected. "
                    "If a device IS connected, check USB cable and try sudo."
                )
        except Exception as exc:
            results["update_identify_output"] = f"ERROR: {exc}"

    elif results["update_exe_found"] and not results["update_exe_executable"]:
        results["advice"].append(
            f"Run: chmod +x {results['update_exe_path']}"
        )

    # ── Determine overall readiness ──────────────────────────────────────
    issues = results["issues"]
    advice = results["advice"]

    if not results["sysfs_available"] and not results["lsusb_installed"]:
        issues.append("No USB detection method available (no sysfs or lsusb)")
        advice.append("Ensure /sys is mounted and usbutils is installed.")

    if not results["update_exe_found"]:
        issues.append(
            "AML update binary not found. Run Environment Setup to download it."
        )
        advice.append(
            "Go to Environment Setup screen and click 'Download Tools' to "
            "clone aml-flash-tool from Radxa."
        )
    elif not results["update_exe_executable"]:
        issues.append(
            "AML update binary is not executable. Check file permissions."
        )
    elif results["update_exe_arch"] == "32-bit":
        issues.append(
            "AML update binary is 32-bit. May need lib32 compatibility libraries."
        )
        advice.append(
            "The Radxa update binary is 32-bit x86. On 64-bit Arch/CachyOS, "
            "install: sudo pacman -S lib32-libusb-compat lib32-glibc\n"
            "Enable multilib in /etc/pacman.conf first if not already enabled."
        )

    if not results["libusb_available"]:
        issues.append(
            "libusb not detected. Install libusb-compat (Arch) or libusb-0.1-4 (Debian)."
        )
        advice.append(
            "On Arch/CachyOS: sudo pacman -S libusb-compat\n"
            "If the binary is 32-bit: sudo pacman -S lib32-libusb-compat"
        )

    # Re-check old rules after cleanup attempt
    remaining_old = []
    for old_rule in results["old_rules_found"]:
        if Path(old_rule).exists():
            remaining_old.append(old_rule)
    if remaining_old:
        issues.append(
            f"Old conflicting udev rules STILL present: {', '.join(remaining_old)}. "
            "Manual removal may be needed."
        )
        advice.append(
            f"Run manually: sudo rm -f {' '.join(remaining_old)}\n"
            "Then: sudo udevadm control --reload-rules"
        )

    # If device is detected but identify fails, give specific advice
    if results["device_present_sysfs"] or results["device_present_lsusb"]:
        if "RC=" in results.get("update_identify_output", ""):
            import re
            rc_match = re.search(r"RC=(\d+)", results["update_identify_output"])
            if rc_match and int(rc_match.group(1)) != 0:
                issues.append(
                    "USB device detected in sysfs/lsusb but update identify failed."
                )
                advice.append(
                    "The device is visible to the kernel but the Amlogic tool can't "
                    "communicate with it. Try:\n"
                    "1. Run with sudo: sudo update identify\n"
                    "2. Check udev rules are applied: udevadm info /sys/bus/usb/devices/*\n"
                    "3. Try a different USB cable (some cables are charge-only)\n"
                    "4. Try a different USB port (prefer USB 2.0)\n"
                    "5. If using a hub, connect directly to the computer"
                )

    results["ready_to_scan"] = (
        results["update_exe_found"]
        and results["update_exe_executable"]
        and results["libusb_available"]
        and (results["sysfs_available"] or results["lsusb_installed"])
        and len([i for i in issues if "Old conflicting" not in i and "STILL present" not in i]) == 0
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
    on_attempt: Callable[[int, int, str], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
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
            else:
                logger.debug(
                    "identify attempt %d: no device (raw=%s)",
                    identify_attempts,
                    info.raw[:100] if info.raw else "<empty>",
                )
        except Exception as exc:
            # identify() can raise on USB errors — keep retrying
            logger.debug(
                "identify attempt %d failed: %s [%s]", identify_attempts, exc, type(exc).__name__,
            )

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
            return
        await asyncio.sleep(check_interval)
    raise DeviceDisconnectedError(timeout)
