"""
lx06_tool/utils/amlogic.py
---------------------------
Wrapper around Radxa's `aml-flash-tool` (the binary is named `update` on Linux,
historically called `update.exe` in the docs — same binary, just no .exe).

All calls are async; the caller is responsible for providing the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from lx06_tool.exceptions import UpdateExeError
from lx06_tool.utils.runner import RunResult, run, run_streaming


# ─── Device Info ──────────────────────────────────────────────────────────────

@dataclass
class AmlogicDeviceInfo:
    """Parsed output from `update identify`."""
    raw: str
    chip: str = ""
    firmware_version: str = ""
    serial: str = ""

    @property
    def identified(self) -> bool:
        return bool(self.chip or self.firmware_version)


def parse_identify_output(output: str) -> AmlogicDeviceInfo:
    """
    Parse the output of `update identify`.

    Example output:
        AmlUsbIdentifyHost
        This firmware version is 0-7-0-16-0-0-0-0
    """
    info = AmlogicDeviceInfo(raw=output)

    version_match = re.search(
        r"firmware version is\s+([\d\-]+)", output, re.IGNORECASE
    )
    if version_match:
        info.firmware_version = version_match.group(1)

    # Chip is reported by some tool versions
    chip_match = re.search(r"chip(?:type)?\s*[:\s]+(\w+)", output, re.IGNORECASE)
    if chip_match:
        info.chip = chip_match.group(1).upper()

    # Presence of "AmlUsb" prefix indicates success
    if "AmlUsb" in output or info.firmware_version:
        info.chip = info.chip or "AXG"  # LX06 is AXG; default if not reported

    return info


# ─── Amlogic Tool Wrapper ─────────────────────────────────────────────────────

class AmlogicTool:
    """
    Wrapper around the `update` binary from aml-flash-tool.

    The binary requires libusb-0.1 (libusb-compat on Arch/CachyOS).
    """

    def __init__(self, update_exe: Path) -> None:
        if not update_exe.exists():
            raise FileNotFoundError(f"update binary not found: {update_exe}")
        self._exe = update_exe

    # ─── Identification ────────────────────────────────────────────────

    async def identify(self, timeout: int = 5) -> AmlogicDeviceInfo:
        """
        Run `update identify` — succeeds only inside the ~2-second
        Amlogic USB bootloader window.
        """
        result = await run([self._exe, "identify"], timeout=timeout)
        info = parse_identify_output(result.stdout + result.stderr)
        return info

    # ─── Bootloader ────────────────────────────────────────────────────

    async def bulk_cmd(self, cmd: str, timeout: int = 10) -> RunResult:
        """Send a U-boot command via `update bulkcmd`."""
        result = await run([self._exe, "bulkcmd", cmd], timeout=timeout)
        if result.returncode != 0:
            raise UpdateExeError(
                f"bulkcmd '{cmd}' failed: {result.stderr}",
                returncode=result.returncode,
            )
        return result

    async def setenv(self, key: str, value: str) -> None:
        """Set a U-boot environment variable."""
        await self.bulk_cmd(f"setenv {key} {value}")

    async def saveenv(self) -> None:
        """Persist U-boot environment to NAND."""
        await self.bulk_cmd("saveenv")

    # ─── Partition Read/Write ──────────────────────────────────────────

    async def mread(
        self,
        partition: str,
        output_path: Path,
        timeout: int = 180,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Dump a partition to a file.
          update mread store <partition> normal <output_file>
        """
        result = await run_streaming(
            [self._exe, "mread", "store", partition, "normal", str(output_path)],
            timeout=timeout,
            on_stdout=on_progress,
            on_stderr=on_progress,
        )
        if result.returncode != 0:
            raise UpdateExeError(
                f"mread of '{partition}' failed: {result.stderr}",
                returncode=result.returncode,
            )
        return result

    async def partition(
        self,
        partition_name: str,
        image_path: Path,
        timeout: int = 300,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Flash an image to a named partition.
          update partition <name> <image>
        """
        result = await run_streaming(
            [self._exe, "partition", partition_name, str(image_path)],
            timeout=timeout,
            on_stdout=on_progress,
            on_stderr=on_progress,
        )
        if result.returncode != 0:
            raise UpdateExeError(
                f"flash of '{partition_name}' from '{image_path}' failed: {result.stderr}",
                returncode=result.returncode,
            )
        return result

    async def write(
        self,
        partition: str,
        image_path: Path,
        timeout: int = 300,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Alternative flash method via mwrite:
          update mwrite <image> store <partition> normal
        """
        result = await run_streaming(
            [self._exe, "mwrite", str(image_path), "store", partition, "normal"],
            timeout=timeout,
            on_stdout=on_progress,
            on_stderr=on_progress,
        )
        if result.returncode != 0:
            raise UpdateExeError(
                f"mwrite to '{partition}' failed: {result.stderr}",
                returncode=result.returncode,
            )
        return result
