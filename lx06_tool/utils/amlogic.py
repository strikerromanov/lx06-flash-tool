"""
lx06_tool/utils/amlogic.py
---------------------------
Wrapper around Radxa's `aml-flash-tool` (the binary is named `update` on Linux,
historically called `update.exe` in the docs — same binary, just no .exe).

All calls are async; the caller is responsible for providing the path.

On Arch/CachyOS the `update` binary may need sudo for direct USB access
if udev rules haven't been applied yet. The identify() method automatically
retries with sudo if the initial attempt fails with a permission error.
"""

from __future__ import annotations

import logging
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

    def __init__(self, update_exe: Path | str) -> None:
        update_exe = Path(update_exe)
        if not update_exe.exists():
            raise FileNotFoundError(f"update binary not found: {update_exe}")
        self._exe = update_exe

    # ─── Identification ────────────────────────────────────────────────

    async def identify(
        self,
        timeout: int = 5,
        *,
        sudo_password: str = "",
    ) -> AmlogicDeviceInfo:
        """
        Run `update identify` — succeeds only inside the ~2-second
        Amlogic USB bootloader window.

        If the initial run fails with a permission/libusb error, automatically
        retries with sudo (useful on Arch/CachyOS before udev rules are active).
        """
        log = logging.getLogger(__name__)

        try:
            result = await run([self._exe, "identify"], timeout=timeout)
        except Exception as exc:
            log.warning("update identify raised exception: %s [%s]", exc, type(exc).__name__)
            # Could be a timeout, missing binary, or library error
            exc_lower = str(exc).lower()
            if "cannot execute" in exc_lower or "exec format" in exc_lower:
                log.error("Binary is wrong architecture for this system")
            elif "not found" in exc_lower:
                log.error("Binary or shared library missing: %s", exc)
            return AmlogicDeviceInfo(raw=f"ERROR: {exc}")

        combined = (result.stdout + "\n" + result.stderr).strip()
        log.debug(
            "update identify RC=%d stdout=%s stderr=%s",
            result.returncode,
            result.stdout[:100] if result.stdout else "<empty>",
            result.stderr[:100] if result.stderr else "<empty>",
        )

        info = parse_identify_output(combined)

        if not info.identified:
            log.debug(
                "update identify did not detect device (RC=%d, raw=%s)",
                result.returncode,
                combined[:200] if combined else "<empty>",
            )

        # If identify failed, try with sudo as fallback for USB permissions
        if not info.identified and sudo_password:
            log.debug("Retrying update identify with sudo...")
            try:
                from lx06_tool.utils.sudo import sudo_run
                sudo_result = await sudo_run(
                    [str(self._exe), "identify"],
                    password=sudo_password,
                    timeout=timeout,
                )
                if sudo_result.ok:
                    log.debug("sudo update identify output: %s", sudo_result.output[:200])
                    info = parse_identify_output(sudo_result.output)
                    if info.identified:
                        log.info("Device identified via sudo fallback")
                else:
                    log.debug(
                        "sudo update identify failed: RC=%s output=%s",
                        sudo_result.returncode, sudo_result.output[:200],
                    )
            except Exception as exc:
                log.debug("sudo update identify exception: %s", exc)

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

        Tries multiple mread syntaxes since different Amlogic tool versions
        and device configurations expect different argument formats:

          1. update mread store <partition> normal <output>  (original)
          2. update mread <partition> <output>               (simple 2-arg)
          3. update mread store mtd<N> normal <output>        (mtd name)
          4. update mread <mtd-index> <output>                (numeric index)
        """
        log = logging.getLogger(__name__)

        # Build candidate command variants to try
        candidates = [
            # Variant 1: full syntax with store/normal
            ([self._exe, "mread", "store", partition, "normal", str(output_path)],
             f"mread store {partition} normal"),
            # Variant 2: simple 2-arg syntax (as in Radxa dump script)
            ([self._exe, "mread", partition, str(output_path)],
             f"mread {partition}"),
        ]

        # Try to extract mtdN index from partition name (e.g. "bootloader" -> "mtd0")
        mtd_name = self._resolve_mtd_name(partition)
        if mtd_name and mtd_name != partition:
            candidates.append(
                # Variant 3: store/mtd-name/normal
                ([self._exe, "mread", "store", mtd_name, "normal", str(output_path)],
                 f"mread store {mtd_name} normal"),
            )
            candidates.append(
                # Variant 4: simple mtd name
                ([self._exe, "mread", mtd_name, str(output_path)],
                 f"mread {mtd_name}"),
            )

        # Try numeric index if mtdN
        mtd_index = self._extract_mtd_index(partition) or self._extract_mtd_index(mtd_name or "")
        if mtd_index is not None:
            candidates.append(
                ([self._exe, "mread", str(mtd_index), str(output_path)],
                 f"mread {mtd_index}"),
            )

        last_error: Exception | None = None
        for cmd, desc in candidates:
            log.debug("Trying mread variant: %s", desc)
            try:
                result = await run_streaming(
                    cmd,
                    timeout=timeout,
                    on_stdout=on_progress,
                    on_stderr=on_progress,
                )
                if result.returncode == 0:
                    log.debug("mread succeeded with variant: %s", desc)
                    return result
                else:
                    last_error = UpdateExeError(
                        f"mread variant '{desc}' failed: {result.stderr}",
                        returncode=result.returncode,
                    )
                    log.debug("mread variant '%s' failed (RC=%d): %s",
                              desc, result.returncode, result.stderr[:200])
            except Exception as exc:
                last_error = exc
                log.debug("mread variant '%s' exception: %s", desc, exc)

        # All variants failed
        raise UpdateExeError(
            f"mread of '{partition}' failed after trying {len(candidates)} syntax variants. "
            f"Last error: {last_error}",
            returncode=-1,
        )

    @staticmethod
    def _resolve_mtd_name(partition: str) -> str | None:
        """Map a partition label to its mtd device name, or return as-is if already mtdN."""
        from lx06_tool.constants import PARTITION_MAP
        if partition.startswith("mtd"):
            return partition
        for mtd_name, meta in PARTITION_MAP.items():
            if meta.get("label") == partition:
                return mtd_name
        return None

    @staticmethod
    def _extract_mtd_index(name: str) -> int | None:
        """Extract numeric index from mtd name like 'mtd0' -> 0."""
        if name.startswith("mtd") and name[3:].isdigit():
            return int(name[3:])
        return None

    async def list_partitions(self, timeout: int = 10) -> str:
        """
        Query the device for partition information.

        Tries various commands to discover the partition layout:
          - update partition (list partitions)
          - update info (device info)
          - bulkcmd 'printenv partitions'
        Returns the combined output for parsing.
        """
        log = logging.getLogger(__name__)
        outputs: list[str] = []

        # Try 'update partition'
        try:
            result = await run([self._exe, "partition"], timeout=timeout)
            if result.ok:
                outputs.append(f"[update partition]\n{result.stdout}")
        except Exception as exc:
            log.debug("update partition query failed: %s", exc)

        # Try 'update info'
        try:
            result = await run([self._exe, "info"], timeout=timeout)
            if result.ok:
                outputs.append(f"[update info]\n{result.stdout}")
        except Exception as exc:
            log.debug("update info query failed: %s", exc)

        # Try bulkcmd printenv partitions
        try:
            result = await self.bulk_cmd("printenv partitions", timeout=timeout)
            outputs.append(f"[bulkcmd printenv partitions]\n{result.stdout}")
        except Exception as exc:
            log.debug("bulkcmd printenv partitions failed: %s", exc)

        return "\n\n".join(outputs) if outputs else "(no partition info available)"

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
