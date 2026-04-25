"""
lx06_tool/utils/amlogic.py
---------------------------
Wrapper around the Amlogic `update` binary from aml-flash-tool.

Command syntax derived from the official Radxa aml-flash-tool source:
https://github.com/radxa/aml-flash-tool/blob/master/aml-flash-tool.sh

Key commands
~~~~~~~~~~~~
- ``update identify 7``               — detect device in USB burning mode
- ``update bulkcmd "<uboot_cmd>"``    — send U-Boot command
- ``update partition <name> <file>``  — flash partition by name
- ``update mread mem <addr> normal <size> <file>`` — dump device RAM to host
- ``update mwrite <file> mem <addr> normal``        — write host file to device RAM

IMPORTANT: ``mread`` does NOT read partitions directly.  It transfers bytes
from the device's memory to the host.  To dump a NAND partition, you must:

  1. ``bulkcmd "store read.part <label> <addr> 0 <size>"``  (NAND → device RAM)
  2. ``mread mem <addr> normal <size> <file>``              (device RAM → host)

The ``bulkcmd`` arguments are prefixed with 5 spaces, matching the official
aml-flash-tool.sh ``run_update_return()`` wrapper.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lx06_tool.exceptions import UpdateExeError
from lx06_tool.utils.runner import RunResult, run, run_streaming


# ─── Device Info ──────────────────────────────────────────────────────────────

@dataclass
class AmlogicDeviceInfo:
    """Parsed output from `update identify 7`."""
    raw: str
    chip: str = ""
    firmware_version: str = ""
    serial: str = ""

    @property
    def identified(self) -> bool:
        return bool(self.chip or self.firmware_version)


def parse_identify_output(output: str) -> AmlogicDeviceInfo:
    """
    Parse the output of `update identify 7`.

    Example successful output:
        AmlUsbIdentifyHost
        This firmware version is 0-7-0-16-0-0-0-0

    The official aml-flash-tool.sh checks for the string "firmware"
    (case-insensitive) to confirm device detection.
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

    # Presence of "AmlUsb" prefix or firmware version indicates success
    if "AmlUsb" in output or info.firmware_version:
        info.chip = info.chip or "AXG"  # LX06 is AXG; default if not reported

    return info


# ─── Amlogic Tool Wrapper ─────────────────────────────────────────────────────

class AmlogicTool:
    """
    Wrapper around the `update` binary from aml-flash-tool.

    Command syntax follows the official Radxa aml-flash-tool.sh:
    - identify uses argument '7'
    - bulkcmd arguments are prefixed with 5 spaces
    - mread dumps device memory (not partitions directly)
    - partition flashing uses partition names, not mtd indices
    """

    # Device RAM address used as a work buffer for partition operations.
    # Must be in a region that U-Boot has initialized (typically 0x03000000).
    NAND_WORK_ADDR: int = 0x03000000

    # The official aml-flash-tool.sh prepends 5 spaces to bulkcmd arguments.
    BULKCMD_SPACE_PREFIX: str = "     "

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
        Run `update identify 7` — succeeds only when an Amlogic SoC is
        in USB burning (maskrom / WorldCup) mode.

        The '7' argument is required by the official aml-flash-tool.
        The official script checks for "firmware" (case-insensitive)
        in the output to confirm detection.

        If the initial run fails with a permission/libusb error, automatically
        retries with sudo (useful on Arch/CachyOS before udev rules are active).
        """
        log = logging.getLogger(__name__)

        try:
            # Official syntax: update identify 7
            result = await run([self._exe, "identify", "7"], timeout=timeout)
        except Exception as exc:
            log.warning("update identify 7 raised exception: %s [%s]", exc, type(exc).__name__)
            exc_lower = str(exc).lower()
            if "cannot execute" in exc_lower or "exec format" in exc_lower:
                log.error("Binary is wrong architecture for this system")
            elif "not found" in exc_lower:
                log.error("Binary or shared library missing: %s", exc)
            return AmlogicDeviceInfo(raw=f"ERROR: {exc}")

        combined = (result.stdout + "\n" + result.stderr).strip()
        log.debug(
            "update identify 7 RC=%d stdout=%s stderr=%s",
            result.returncode,
            result.stdout[:100] if result.stdout else "<empty>",
            result.stderr[:100] if result.stderr else "<empty>",
        )

        info = parse_identify_output(combined)

        if not info.identified:
            log.debug(
                "update identify 7 did not detect device (RC=%d, raw=%s)",
                result.returncode,
                combined[:200] if combined else "<empty>",
            )

        # If identify failed, try with sudo as fallback for USB permissions
        if not info.identified and sudo_password:
            log.debug("Retrying update identify 7 with sudo...")
            try:
                from lx06_tool.utils.sudo import sudo_run
                sudo_result = await sudo_run(
                    [str(self._exe), "identify", "7"],
                    password=sudo_password,
                    timeout=timeout,
                )
                if sudo_result.ok:
                    log.debug("sudo update identify 7 output: %s", sudo_result.output[:200])
                    info = parse_identify_output(sudo_result.output)
                    if info.identified:
                        log.info("Device identified via sudo fallback")
                else:
                    log.debug(
                        "sudo update identify 7 failed: RC=%s output=%s",
                        sudo_result.returncode, sudo_result.output[:200],
                    )
            except Exception as exc:
                log.debug("sudo update identify 7 exception: %s", exc)

        return info

    # ─── Bulk Command (U-Boot) ─────────────────────────────────────────

    async def bulkcmd(self, cmd: str, timeout: int = 10) -> RunResult:
        """Send a U-Boot command via `update bulkcmd`.

        The official aml-flash-tool.sh prepends 5 spaces to the bulkcmd
        argument.  This is replicated here for compatibility.

        Examples::

            await tool.bulkcmd("setenv bootdelay 15")
            await tool.bulkcmd("saveenv")
            await tool.bulkcmd("printenv partitions")
            await tool.bulkcmd("store read.part boot0 0x03000000 0 0x800000")
        """
        log = logging.getLogger(__name__)
        # Official aml-flash-tool.sh prepends 5 spaces to bulkcmd arguments
        spaced_cmd = self.BULKCMD_SPACE_PREFIX + cmd
        log.debug("bulkcmd: %s", cmd)

        result = await run([self._exe, "bulkcmd", spaced_cmd], timeout=timeout)

        # Official tool checks for "ERR" in output (even if RC=0)
        combined = (result.stdout + "\n" + result.stderr).strip()
        if "ERR" in combined.upper():
            log.warning("bulkcmd '%s' reported error: %s", cmd, combined[:200])

        return result

    # Backward-compatible alias
    async def bulk_cmd(self, cmd: str, timeout: int = 10) -> RunResult:
        """Deprecated alias for bulkcmd()."""
        return await self.bulkcmd(cmd, timeout=timeout)

    async def setenv(self, key: str, value: str) -> None:
        """Set a U-boot environment variable."""
        result = await self.bulkcmd(f"setenv {key} {value}")
        if result.returncode != 0:
            raise UpdateExeError(
                f"setenv {key}={value} failed: {result.stderr}",
                returncode=result.returncode,
            )

    async def saveenv(self) -> None:
        """Persist U-boot environment to storage.

        Uses ``saveenv`` (standard U-Boot command).  The official
        aml-flash-tool.sh uses ``save`` which is an alias on most
        Amlogic builds.
        """
        result = await self.bulkcmd("saveenv")
        if result.returncode != 0:
            # Try 'save' as fallback (some Amlogic builds use this)
            result2 = await self.bulkcmd("save")
            if result2.returncode != 0:
                raise UpdateExeError(
                    f"saveenv failed: {result.stderr}",
                    returncode=result.returncode,
                )

    # ─── Low-level Memory Transfer ─────────────────────────────────────

    async def mread_mem(
        self,
        addr: int,
        size: int,
        output_path: Path,
        *,
        timeout: int = 180,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Dump device memory to a host file.

        Real syntax (from aml-flash-tool.sh help)::

            update mread mem 0x03000000 normal 512 emmc.bin

        Parameters
        ----------
        addr : int
            Device memory address to read from (e.g. 0x03000000).
        size : int
            Number of bytes to transfer.
        output_path : Path
            Host file to write the data into.
        """
        log = logging.getLogger(__name__)
        addr_str = f"0x{addr:08X}"
        log.debug("mread mem %s normal %d %s", addr_str, size, output_path)

        result = await run_streaming(
            [self._exe, "mread", "mem", addr_str, "normal", str(size), str(output_path)],
            timeout=timeout,
            on_stdout=on_progress,
            on_stderr=on_progress,
        )

        if result.returncode != 0:
            raise UpdateExeError(
                f"mread mem {addr_str} normal {size} failed: {result.stderr}",
                returncode=result.returncode,
            )
        return result

    # ─── Partition Dump (High-Level) ───────────────────────────────────

    async def dump_partition(
        self,
        partition_label: str,
        size: int,
        output_path: Path,
        *,
        timeout: int = 180,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Dump a NAND partition to a host file using the two-step process.

        This is the CORRECT way to dump partitions per the official
        aml-flash-tool source code:

        1. ``bulkcmd "store read.part <label> <addr> 0 <size>"`` — NAND → device RAM
        2. ``mread mem <addr> normal <size> <file>`` — device RAM → host file

        Falls back to alternative NAND read commands if ``store read.part``
        is not supported by the device's U-Boot.

        Parameters
        ----------
        partition_label : str
            Partition name as known to U-Boot (e.g. "bootloader", "system0").
        size : int
            Expected partition size in bytes (from PARTITION_MAP).
        output_path : Path
            Host file to write the dump into.
        """
        log = logging.getLogger(__name__)
        addr = self.NAND_WORK_ADDR
        addr_str = f"0x{addr:08X}"
        size_hex = f"0x{size:X}"

        log.info(
            "Dumping partition '%s' (%d bytes) via two-step process",
            partition_label, size,
        )

        # Step 1: Read NAND partition into device RAM
        # Try multiple U-Boot command variants for compatibility
        read_cmds = [
            # Modern Amlogic store commands (AXG, G12A, etc.)
            f"store read.part {partition_label} {addr_str} 0 {size_hex}",
            # Alternative store syntax without explicit offset
            f"store read.part {partition_label} {addr_str} {size_hex}",
            # NAND-specific read command
            f"nand read.part {partition_label} {addr_str} {size_hex}",
            # Another common variant
            f"nand read {addr_str} {partition_label} {size_hex}",
        ]

        read_ok = False
        last_error: str = ""

        for i, cmd in enumerate(read_cmds):
            try:
                log.debug("Trying read command [%d]: %s", i, cmd)
                result = await self.bulkcmd(cmd, timeout=30)
                combined = (result.stdout + "\n" + result.stderr).strip()

                if result.returncode == 0 and "ERR" not in combined.upper():
                    log.debug("Read command [%d] succeeded: %s", i, cmd)
                    read_ok = True
                    break
                else:
                    last_error = combined[:200]
                    log.debug(
                        "Read command [%d] returned error (RC=%d): %s",
                        i, result.returncode, last_error,
                    )
            except Exception as exc:
                last_error = str(exc)
                log.debug("Read command [%d] exception: %s", i, exc)

        if not read_ok:
            raise UpdateExeError(
                f"Failed to read partition '{partition_label}' into device RAM. "
                f"Tried {len(read_cmds)} command variants. Last error: {last_error}",
                returncode=-1,
            )

        # Step 2: Dump device memory to host file
        if on_progress:
            on_progress(f"Dumping {partition_label} from device memory...")

        result = await self.mread_mem(
            addr=addr,
            size=size,
            output_path=output_path,
            timeout=timeout,
            on_progress=on_progress,
        )

        log.info(
            "Partition '%s' dumped: %d bytes -> %s",
            partition_label, size, output_path,
        )
        return result

    async def mread(
        self,
        partition: str,
        output_path: Path,
        *,
        size: int = 0,
        timeout: int = 180,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Dump a partition to a host file.

        High-level wrapper that resolves the partition label and size,
        then delegates to dump_partition() for the two-step NAND dump.

        Parameters
        ----------
        partition : str
            Partition label (e.g. "bootloader", "system0") or mtd name ("mtd0").
        output_path : Path
            Host file for the dump.
        size : int
            Expected size in bytes.  If 0, looked up from PARTITION_MAP.
        """
        log = logging.getLogger(__name__)
        label = partition

        if not size:
            # Look up size from PARTITION_MAP
            from lx06_tool.constants import PARTITION_MAP
            for mtd_name, meta in PARTITION_MAP.items():
                if meta.get("label") == partition or mtd_name == partition:
                    label = str(meta["label"])
                    size = int(meta["size"])  # type: ignore[arg-type]
                    break
            if not size:
                raise UpdateExeError(
                    f"Unknown partition '{partition}' and no size specified. "
                    f"Known partitions: {list(PARTITION_MAP.keys())}",
                    returncode=-1,
                )

        log.debug("mread: partition='%s' label='%s' size=%d", partition, label, size)
        return await self.dump_partition(
            partition_label=label,
            size=size,
            output_path=output_path,
            timeout=timeout,
            on_progress=on_progress,
        )

    # ─── Partition Listing ─────────────────────────────────────────────

    async def list_partitions(self, timeout: int = 10) -> str:
        """
        Query the device for partition information.

        The official aml-flash-tool does NOT query the device for partition
        layout — it reads partition info from the image configuration file.
        However, we can query U-Boot environment variables for the partition
        table string.

        Tries:
          - ``bulkcmd "printenv partitions"``
          - ``bulkcmd "printenv"`` (full env for parsing)
          - ``bulkcmd "store list"`` (may work on some builds)
        """
        log = logging.getLogger(__name__)
        outputs: list[str] = []

        # Try printenv partitions
        try:
            result = await self.bulkcmd("printenv partitions", timeout=timeout)
            if result.returncode == 0:
                outputs.append(f"[printenv partitions]\n{result.stdout}")
        except Exception as exc:
            log.debug("printenv partitions failed: %s", exc)

        # Try full printenv for partition-related variables
        try:
            result = await self.bulkcmd("printenv", timeout=timeout)
            if result.returncode == 0:
                combined = result.stdout + "\n" + result.stderr
                # Extract partition-related lines
                part_lines = [
                    line for line in combined.splitlines()
                    if any(kw in line.lower() for kw in ["part", "boot", "mtd", "system"])
                ]
                if part_lines:
                    outputs.append("[printenv filtered]\n" + "\n".join(part_lines[:20]))
        except Exception as exc:
            log.debug("printenv failed: %s", exc)

        # Try store list
        try:
            result = await self.bulkcmd("store list", timeout=timeout)
            if result.returncode == 0:
                outputs.append(f"[store list]\n{result.stdout}")
        except Exception as exc:
            log.debug("store list failed: %s", exc)

        return "\n\n".join(outputs) if outputs else "(no partition info available)"

    # ─── Partition Flash ───────────────────────────────────────────────

    async def partition(
        self,
        partition_name: str,
        image_path: Path,
        *,
        partition_type: str = "normal",
        timeout: int = 300,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Flash an image to a named partition.

        Real syntax (from aml-flash-tool.sh)::

            update partition <name> <image> [type]

        The ``partition_type`` is typically "normal", "sparse", or "dtb".
        """
        log = logging.getLogger(__name__)
        log.info("Flashing partition '%s' from %s (type=%s)", partition_name, image_path, partition_type)

        result = await run_streaming(
            [self._exe, "partition", partition_name, str(image_path), partition_type],
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

    # ─── Legacy / Compatibility ────────────────────────────────────────

    async def write(
        self,
        partition: str,
        image_path: Path,
        *,
        timeout: int = 300,
        on_progress: Optional[callable] = None,    # type: ignore[type-arg]
    ) -> RunResult:
        """
        Flash an image to a partition (convenience wrapper).

        Delegates to ``partition()`` with the default type "normal".
        The old ``mwrite store`` syntax was incorrect; the official tool
        uses ``partition`` for flashing.
        """
        return await self.partition(
            partition_name=partition,
            image_path=image_path,
            partition_type="normal",
            timeout=timeout,
            on_progress=on_progress,
        )
