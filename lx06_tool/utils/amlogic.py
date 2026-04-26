"""
lx06_tool/utils/amlogic.py
---------------------------
Wrapper around the Amlogic `update` binary from aml-flash-tool.

Command syntax derived from the official Radxa aml-flash-tool source:
https://github.com/radxa/aml-flash-tool/blob/master/aml-flash-tool.sh

Key commands
~~~~~~~~~~~~
- ``update identify [7]``             — detect device in USB burning mode
- ``update bulkcmd "<uboot_cmd>"``    — send U-Boot command (setenv/saveenv ONLY)
- ``update partition <name> <file>``  — flash partition by name
- ``update mread store <label> normal <file>`` — direct NAND→host partition dump
- ``update mwrite <file> mem <addr> normal``   — write host file to device RAM

IMPORTANT: Partition dumps use DIRECT NAND→host transfer:

    update mread store <label> normal <file>

This transfers partition data directly from NAND to the host file, avoiding
the broken two-step RAM approach (store read.part → mread mem) which caused
heap corruption at address 0x03000000 on AXG SoCs.

The ``bulkcmd`` command is reserved ONLY for:
- ``setenv bootdelay 15`` / ``saveenv`` — bootloader unlock
- ``store list_part`` / ``store list`` — partition table queries
- ``printenv`` — environment variable queries

DO NOT use bulkcmd for partition reads — use mread store instead.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from lx06_tool.constants import (
    DEFAULT_PARTITION_TIMEOUT,
    PARTITION_TIMEOUTS,
    SQUASHFS_MAGIC_BE,
    SQUASHFS_MAGIC_LE,
)
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
    Parse the output of `update identify [7]`.

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


# ─── Magic Byte Validation ───────────────────────────────────────────────────

def validate_dump_magic(path: Path, label: str) -> str:
    """Validate first bytes of a dump file. Returns status string.

    Returns one of:
        'squashfs_le'  — valid little-endian squashfs (hsqs)
        'squashfs_be'  — valid big-endian squashfs (sqsh)
        'empty'        — all zeros (partition may be blank)
        'unreadable'   — 0xFF padded (unread NAND)
        'ubi'          — UBI format
        'gzip'         — gzip compressed
        'unknown'      — unrecognized format
    """
    log = logging.getLogger(__name__)
    if not path.exists():
        return "missing"

    try:
        with open(path, 'rb') as f:
            header = f.read(32)
    except OSError as exc:
        log.warning("[MAGIC] Cannot read %s: %s", path, exc)
        return "unreadable"

    if len(header) < 4:
        return "too_small"

    magic4 = header[:4]

    if magic4 == SQUASHFS_MAGIC_LE:
        log.info("[MAGIC] '%s': valid squashfs (little-endian 'hsqs')", label)
        return "squashfs_le"
    if magic4 == SQUASHFS_MAGIC_BE:
        log.info("[MAGIC] '%s': valid squashfs (big-endian 'sqsh')", label)
        return "squashfs_be"
    if magic4 == b'\x00\x00\x00\x00':
        log.warning("[MAGIC] '%s': all zeros — partition may be empty", label)
        return "empty"
    if magic4[:2] == b'\xff\xff':
        log.warning("[MAGIC] '%s': 0xFF padded — likely unread NAND", label)
        return "unreadable"
    if magic4 == b'UBI#':
        log.warning("[MAGIC] '%s': UBI format — partition uses UBI/UBIFS", label)
        return "ubi"
    if header[:2] == b'\x1f\x8b':
        log.warning("[MAGIC] '%s': gzip compressed", label)
        return "gzip"

    log.warning(
        "[MAGIC] '%s': unknown format — first 4 bytes: %s",
        label, magic4.hex(),
    )
    return "unknown"


# ─── Amlogic Tool Wrapper ─────────────────────────────────────────────────────

class AmlogicTool:
    """
    Wrapper around the `update` binary from aml-flash-tool.

    Partition dumps use DIRECT NAND→host transfer:
        update mread store <label> normal <file>

    This avoids the broken two-step RAM approach that caused heap corruption
    on AXG SoCs when cramming 32 MB into address 0x03000000.

    bulkcmd is reserved for setenv/saveenv and partition table queries ONLY.
    """

    # The official aml-flash-tool.sh prepends 5 spaces to bulkcmd arguments.
    BULKCMD_SPACE_PREFIX: str = "     "

    # Chunk size for fallback reads (4 MB)
    CHUNK_SIZE: int = 4 * 1024 * 1024

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
        Run `update identify` — succeeds only when an Amlogic SoC is
        in USB burning (maskrom / WorldCup) mode.

        Probes both `identify 7` and `identify` without argument, since
        some firmware variants reject the numeric argument.

        The official script checks for "firmware" (case-insensitive)
        in the output to confirm detection.

        If the initial run fails with a permission/libusb error, automatically
        retries with sudo (useful on Arch/CachyOS before udev rules are active).
        """
        log = logging.getLogger(__name__)

        # Try with '7' first (official syntax)
        try:
            result = await run([self._exe, "identify", "7"], timeout=timeout)
        except Exception as exc:
            log.warning("update identify 7 raised: %s [%s]", exc, type(exc).__name__)
            exc_lower = str(exc).lower()
            if "cannot execute" in exc_lower or "exec format" in exc_lower:
                log.error("Binary is wrong architecture for this system")
            elif "not found" in exc_lower:
                log.error("Binary or shared library missing: %s", exc)
            result = None

        combined = ""
        if result:
            combined = (result.stdout + "\n" + result.stderr).strip()
            log.debug(
                "update identify 7 RC=%d stdout=%s stderr=%s",
                result.returncode,
                result.stdout[:100] if result.stdout else "<empty>",
                result.stderr[:100] if result.stderr else "<empty>",
            )

        info = parse_identify_output(combined) if combined else AmlogicDeviceInfo(raw="")

        # If identify 7 didn't work, try without argument
        if not info.identified:
            log.debug("identify 7 failed, trying without argument...")
            try:
                result2 = await run([self._exe, "identify"], timeout=timeout)
                combined2 = (result2.stdout + "\n" + result2.stderr).strip()
                info2 = parse_identify_output(combined2)
                if info2.identified:
                    log.info("Device identified via 'identify' (without '7')")
                    info = info2
            except Exception as exc:
                log.debug("identify (no arg) also failed: %s", exc)

        # If identify failed, try with sudo as fallback for USB permissions
        if not info.identified and sudo_password:
            log.debug("Retrying update identify with sudo...")
            for cmd_args in (["identify", "7"], ["identify"]):
                try:
                    from lx06_tool.utils.sudo import sudo_run
                    sudo_result = await sudo_run(
                        [str(self._exe)] + cmd_args,
                        password=sudo_password,
                        timeout=timeout,
                    )
                    if sudo_result.ok:
                        log.debug("sudo update %s output: %s",
                                  " ".join(cmd_args), sudo_result.output[:200])
                        info = parse_identify_output(sudo_result.output)
                        if info.identified:
                            log.info("Device identified via sudo fallback (%s)",
                                     " ".join(cmd_args))
                            break
                except Exception as exc:
                    log.debug("sudo identify retry failed: %s", exc)

        if not info.identified:
            log.debug("Device not identified after all attempts")
        else:
            if not info.raw:
                info.raw = combined

        return info

    # ─── Bulk Command (U-Boot) ─────────────────────────────────────────

    async def bulkcmd(
        self,
        cmd: str,
        timeout: int = 10,
        *,
        sudo_password: str = "",
    ) -> RunResult:
        """Send a U-Boot command via `update bulkcmd`.

        NOTE: bulkcmd is reserved for setenv/saveenv and partition table
        queries ONLY. Do NOT use for partition reads — use dump_partition()
        which uses direct NAND→host transfer.

        The official aml-flash-tool.sh prepends 5 spaces to the bulkcmd
        argument.

        Examples::

            await tool.bulkcmd("setenv bootdelay 15")
            await tool.bulkcmd("saveenv")
            await tool.bulkcmd("printenv partitions")
            await tool.bulkcmd("store list_part")
        """
        from lx06_tool.utils.validation import sanitize_command_input

        log = logging.getLogger(__name__)

        # SECURITY: Validate command input to prevent injection
        try:
            safe_cmd = sanitize_command_input(cmd)
        except ValueError as exc:
            raise UpdateExeError(
                f"Invalid command input: {exc}",
                returncode=-1,
            ) from exc

        spaced_cmd = self.BULKCMD_SPACE_PREFIX + safe_cmd
        log.info("[BULKCMD] Sending: '%s'", safe_cmd)

        full_cmd = [self._exe, "bulkcmd", spaced_cmd]
        result = await run(full_cmd, timeout=timeout)

        combined = (result.stdout + "\n" + result.stderr).strip()
        log.info(
            "[BULKCMD] Result: RC=%d, stdout='%s', stderr='%s'",
            result.returncode,
            result.stdout[:300] if result.stdout else "",
            result.stderr[:300] if result.stderr else "",
        )
        if "ERR" in combined.upper():
            log.warning("[BULKCMD] '%s' reported error: %s", cmd, combined[:200])

        # Retry with sudo if initial attempt failed and password is available
        if not result.ok and sudo_password:
            try:
                from lx06_tool.utils.sudo import sudo_run
                sudo_result = await sudo_run(
                    [str(self._exe), "bulkcmd", spaced_cmd],
                    password=sudo_password,
                    timeout=timeout,
                )
                if sudo_result.ok:
                    log.debug("bulkcmd succeeded via sudo fallback")
                    return RunResult(
                        cmd=[str(self._exe), "bulkcmd", spaced_cmd],
                        returncode=0,
                        stdout=sudo_result.output,
                        stderr="",
                    )
            except Exception as exc:
                log.debug("sudo bulkcmd retry failed: %s", exc)

        return result

    # Backward-compatible alias
    async def bulk_cmd(self, cmd: str, timeout: int = 10, *, sudo_password: str = "") -> RunResult:
        """Deprecated alias for bulkcmd()."""
        return await self.bulkcmd(cmd, timeout=timeout, sudo_password=sudo_password)

    async def setenv(self, key: str, value: str, *, sudo_password: str = "") -> None:
        """Set a U-boot environment variable."""
        result = await self.bulkcmd(f"setenv {key} {value}", sudo_password=sudo_password)
        if result.returncode != 0:
            raise UpdateExeError(
                f"setenv {key}={value} failed: {result.stderr}",
                returncode=result.returncode,
            )

    async def saveenv(self, *, sudo_password: str = "") -> None:
        """Persist U-boot environment to storage.

        Uses ``saveenv`` (standard U-Boot command).  The official
        aml-flash-tool.sh uses ``save`` which is an alias on most
        Amlogic builds.
        """
        result = await self.bulkcmd("saveenv", sudo_password=sudo_password)
        if result.returncode != 0:
            # Try 'save' as fallback (some Amlogic builds use this)
            result2 = await self.bulkcmd("save", sudo_password=sudo_password)
            if result2.returncode != 0:
                raise UpdateExeError(
                    f"saveenv failed: {result.stderr}",
                    returncode=result.returncode,
                )

    # ─── Partition Dump (Direct NAND→Host) ──────────────────────────────

    async def dump_partition(
        self,
        partition_label: str,
        output_path: Path,
        *,
        size: int = 0,
        timeout: int = 0,
        on_progress: callable | None = None,    # type: ignore[type-arg]
        sudo_password: str = "",
    ) -> Path:
        """
        Dump a NAND partition directly to a host file.

        Uses direct NAND→host transfer:
            update mread store <label> normal <file>

        This avoids the broken two-step RAM approach (store read.part → mread mem)
        which caused heap corruption at address 0x03000000 on AXG SoCs.

        Fallback chain:
          1. mread store <label> normal <file>     — standard syntax
          2. mread <label> <file>                  — simplified syntax
          3. mread store <label> normal <size> <file> — with explicit size
          4. Chunked read via bulkcmd store read.part + mread mem
             (only if direct reads fail, uses safe high address 0x50000000)

        Parameters
        ----------
        partition_label : str
            Partition name as known to U-Boot (e.g. "bootloader", "system0").
        output_path : Path
            Host file to write the dump into.
        size : int
            Expected partition size in bytes (from PARTITION_MAP).
            Used for chunked fallback and validation.
        timeout : int
            Timeout in seconds. 0 = use per-partition default from constants.
        on_progress : callable
            Optional progress callback.
        sudo_password : str
            Optional sudo password for USB permission issues.

        Returns
        -------
        Path to the dump file.

        Raises
        ------
        UpdateExeError
            If all dump methods fail.
        """
        log = logging.getLogger(__name__)

        # Resolve timeout from per-partition constants if not specified
        if timeout <= 0:
            timeout = PARTITION_TIMEOUTS.get(
                partition_label, DEFAULT_PARTITION_TIMEOUT,
            )

        log.info(
            "[DUMP] Starting direct NAND→host dump of '%s' -> %s (timeout=%ds)",
            partition_label, output_path, timeout,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Attempt 1: Standard mread store syntax ──────────────────────
        cmd = [str(self._exe), "mread", "store", partition_label, "normal", str(output_path)]
        log.info("[DUMP] Attempt 1: %s", cmd)
        result = await self._run_dump(cmd, timeout, on_progress, sudo_password)

        if result.ok and output_path.exists() and output_path.stat().st_size > 0:
            log.info("[DUMP] Attempt 1 SUCCEEDED: %d bytes", output_path.stat().st_size)
            self._post_validate(output_path, partition_label)
            return output_path

        log.warning("[DUMP] Attempt 1 failed (RC=%s, exists=%s): %s",
                    result.returncode if result else "?",
                    output_path.exists(),
                    (result.stderr or "")[:200] if result else "no result")

        # ── Attempt 2: Simplified mread syntax ──────────────────────────
        cmd = [str(self._exe), "mread", partition_label, str(output_path)]
        log.info("[DUMP] Attempt 2: %s", cmd)
        result = await self._run_dump(cmd, timeout, on_progress, sudo_password)

        if result.ok and output_path.exists() and output_path.stat().st_size > 0:
            log.info("[DUMP] Attempt 2 SUCCEEDED: %d bytes", output_path.stat().st_size)
            self._post_validate(output_path, partition_label)
            return output_path

        log.warning("[DUMP] Attempt 2 failed (RC=%s)",
                    result.returncode if result else "?")

        # ── Attempt 3: mread with explicit size ─────────────────────────
        if size > 0:
            cmd = [str(self._exe), "mread", "store", partition_label,
                   "normal", str(size), str(output_path)]
            log.info("[DUMP] Attempt 3 (with size %d): %s", size, cmd)
            result = await self._run_dump(cmd, timeout, on_progress, sudo_password)

            if result.ok and output_path.exists() and output_path.stat().st_size > 0:
                log.info("[DUMP] Attempt 3 SUCCEEDED: %d bytes", output_path.stat().st_size)
                self._post_validate(output_path, partition_label)
                return output_path

            log.warning("[DUMP] Attempt 3 failed (RC=%s)",
                        result.returncode if result else "?")

        # ── Attempt 4: Chunked fallback via RAM ─────────────────────────
        # Uses a HIGH safe address (0x50000000) far above U-Boot heap
        if size > 0:
            log.warning("[DUMP] Direct reads failed, trying chunked fallback...")
            try:
                await self._chunked_dump(
                    partition_label, size, output_path,
                    timeout=timeout, on_progress=on_progress,
                    sudo_password=sudo_password,
                )
                if output_path.exists() and output_path.stat().st_size > 0:
                    log.info("[DUMP] Chunked fallback SUCCEEDED: %d bytes",
                             output_path.stat().st_size)
                    self._post_validate(output_path, partition_label)
                    return output_path
            except Exception as exc:
                log.error("[DUMP] Chunked fallback failed: %s", exc)

        # ── All methods failed ──────────────────────────────────────────
        raise UpdateExeError(
            f"Failed to dump partition '{partition_label}'. "
            f"Tried direct NAND→host and chunked fallback. "
            f"Last error: {(result.stderr or 'unknown')[:300] if result else 'no result'}",
            returncode=result.returncode if result else -1,
        )

    async def _run_dump(
        self,
        cmd: list[str],
        timeout: int,
        on_progress: callable | None,    # type: ignore[type-arg]
        sudo_password: str,
    ) -> RunResult:
        """Execute a dump command with sudo fallback."""
        log = logging.getLogger(__name__)
        try:
            result = await run_streaming(
                cmd,
                timeout=timeout,
                on_stdout=on_progress,
                on_stderr=on_progress,
            )
            if result.ok:
                return result
        except Exception as exc:
            log.debug("Dump command exception: %s", exc)
            if not sudo_password:
                return RunResult(cmd=cmd, returncode=-1, stdout="", stderr=str(exc))

        # Retry with sudo
        if sudo_password:
            try:
                from lx06_tool.utils.sudo import sudo_run
                sudo_result = await sudo_run(
                    cmd, password=sudo_password, timeout=timeout,
                )
                if sudo_result.ok:
                    log.debug("Dump succeeded via sudo fallback")
                    return RunResult(
                        cmd=cmd, returncode=0,
                        stdout=sudo_result.output, stderr="",
                    )
                return RunResult(
                    cmd=cmd, returncode=sudo_result.returncode,
                    stdout=sudo_result.output, stderr="",
                )
            except Exception as exc:
                log.debug("sudo dump retry failed: %s", exc)

        return result  # type: ignore[return-value]

    async def _chunked_dump(
        self,
        partition_label: str,
        total_size: int,
        output_path: Path,
        *,
        timeout: int = 600,
        on_progress: callable | None = None,    # type: ignore[type-arg]
        sudo_password: str = "",
    ) -> None:
        """Fallback chunked dump using RAM transfer.

        Uses a SAFE high address (0x50000000) well above U-Boot heap.
        Only used when direct NAND→host transfer fails.

        Reads in CHUNK_SIZE (4 MB) chunks:
          1. bulkcmd "store read.part <label> <addr> <offset> <chunk_size>"
          2. mread mem <addr> normal <chunk_size> <temp_file>
          3. Append temp_file to output_path
        """
        log = logging.getLogger(__name__)
        # Safe RAM address — well above U-Boot heap on AXG
        safe_addr = 0x50000000
        addr_str = f"0x{safe_addr:08X}"
        chunk_size = self.CHUNK_SIZE
        offset = 0

        log.info(
            "[CHUNK] Starting chunked dump of '%s': total=%d, chunk=%d, addr=%s",
            partition_label, total_size, chunk_size, addr_str,
        )

        # Remove existing output to start fresh
        if output_path.exists():
            output_path.unlink()

        import tempfile
        chunk_num = 0
        while offset < total_size:
            remaining = total_size - offset
            this_chunk = min(chunk_size, remaining)
            chunk_num += 1
            pct = int(offset / total_size * 100)

            log.info(
                "[CHUNK] Reading chunk %d: offset=0x%X, size=%d (%d%%)",
                chunk_num, offset, this_chunk, pct,
            )
            if on_progress:
                on_progress(f"Chunk {chunk_num}: {pct}% complete...")

            # Step 1: Read NAND chunk into device RAM
            offset_hex = f"0x{offset:X}"
            size_hex = f"0x{this_chunk:X}"
            read_cmd = (
                f"store read.part {partition_label} {addr_str} {offset_hex} {size_hex}"
            )
            result = await self.bulkcmd(
                read_cmd, timeout=60, sudo_password=sudo_password,
            )
            if not result.ok:
                # Try without offset
                read_cmd2 = (
                    f"store read.part {partition_label} {addr_str} {size_hex}"
                )
                result = await self.bulkcmd(
                    read_cmd2, timeout=60, sudo_password=sudo_password,
                )
            if not result.ok:
                raise UpdateExeError(
                    f"Chunked read failed at offset 0x{offset:X}: {result.stderr}",
                    returncode=result.returncode,
                )

            # Step 2: Transfer chunk from device RAM to host temp file
            with tempfile.NamedTemporaryFile(suffix=".chunk", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                cmd = [
                    str(self._exe), "mread", "mem", addr_str,
                    "normal", str(this_chunk), str(tmp_path),
                ]
                mresult = await self._run_dump(
                    cmd, timeout=120, on_progress=None,
                    sudo_password=sudo_password,
                )
                if not mresult.ok:
                    raise UpdateExeError(
                        f"Chunked mread failed at offset 0x{offset:X}: {mresult.stderr}",
                        returncode=mresult.returncode,
                    )

                # Step 3: Append chunk to output
                with open(tmp_path, 'rb') as src, open(output_path, 'ab') as dst:
                    dst.write(src.read())
            finally:
                tmp_path.unlink(missing_ok=True)

            offset += this_chunk

        log.info(
            "[CHUNK] Completed: %d chunks, %d bytes total",
            chunk_num, output_path.stat().st_size if output_path.exists() else 0,
        )

    async def _post_validate(self, output_path: Path, label: str) -> None:
        """Validate dump file after successful transfer."""
        log = logging.getLogger(__name__)
        if not output_path.exists():
            log.error("[VALIDATE] Output file missing: %s", output_path)
            return

        actual_size = output_path.stat().st_size
        log.info("[VALIDATE] Dump file size: %d bytes", actual_size)

        # Magic byte validation
        magic_status = validate_dump_magic(output_path, label)
        if magic_status in ("squashfs_le", "squashfs_be"):
            log.info("[VALIDATE] ✓ Valid squashfs for '%s'", label)
        elif magic_status == "empty":
            log.warning("[VALIDATE] Empty/zero partition '%s'", label)
        elif magic_status == "missing":
            log.error("[VALIDATE] Dump file disappeared for '%s'", label)
        else:
            log.warning("[VALIDATE] Unexpected format '%s' for partition '%s'",
                        magic_status, label)

        # Also run `file` command for extra identification
        try:
            file_result = await run(
                ["file", str(output_path)], timeout=5,
            )
            if file_result and file_result.stdout:
                log.info("[VALIDATE] file: %s", file_result.stdout.strip())
        except Exception:
            pass  # Non-critical

    # ─── High-Level mread Wrapper ───────────────────────────────────────

    async def mread(
        self,
        partition: str,
        output_path: Path,
        *,
        size: int = 0,
        timeout: int = 0,
        on_progress: callable | None = None,    # type: ignore[type-arg]
        sudo_password: str = "",
    ) -> Path:
        """
        Dump a partition to a host file.

        High-level wrapper that resolves the partition label and size,
        then delegates to dump_partition() for direct NAND→host transfer.

        Parameters
        ----------
        partition : str
            Partition label (e.g. "bootloader", "system0") or mtd name ("mtd0").
        output_path : Path
            Host file for the dump.
        size : int
            Expected size in bytes.  If 0, looked up from PARTITION_MAP.
        timeout : int
            Timeout in seconds.  0 = use per-partition default.
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

        # Resolve timeout from per-partition constants
        if timeout <= 0:
            timeout = PARTITION_TIMEOUTS.get(label, DEFAULT_PARTITION_TIMEOUT)

        log.info("[MREAD] Resolved: partition='%s' label='%s' size=%d (0x%X) timeout=%ds",
                 partition, label, size, size, timeout)
        return await self.dump_partition(
            partition_label=label,
            output_path=output_path,
            size=size,
            timeout=timeout,
            on_progress=on_progress,
            sudo_password=sudo_password,
        )

    # ─── Diagnostics ─────────────────────────────────────────────────

    async def dump_diagnostic(
        self,
        *,
        sudo_password: str = "",
    ) -> dict[str, str]:
        """Run diagnostic checks to verify dump connectivity.

        Tests the direct NAND→host transfer path with a small read.

        Returns dict with diagnostic results.
        """
        import tempfile
        log = logging.getLogger(__name__)
        results: dict[str, str] = {}

        # Test 1: Basic bulkcmd responsiveness
        log.info("[DIAG] Test 1: Basic bulkcmd responsiveness")
        try:
            result = await self.bulkcmd("echo test123", timeout=10, sudo_password=sudo_password)
            results["bulkcmd_echo"] = f"RC={result.returncode} out={result.stdout[:100]}"
            log.info("[DIAG] Echo test: %s", results["bulkcmd_echo"])
        except Exception as exc:
            results["bulkcmd_echo"] = f"FAIL: {exc}"
            log.error("[DIAG] Echo test failed: %s", exc)

        # Test 2: Query partition table
        log.info("[DIAG] Test 2: Query partition table")
        try:
            table = await self.query_partition_table(sudo_password=sudo_password)
            results["partition_table"] = str(table)
            log.info("[DIAG] Partition table: %s", table)
        except Exception as exc:
            results["partition_table"] = f"FAIL: {exc}"
            log.error("[DIAG] Partition table query failed: %s", exc)

        # Test 3: Try store list_part
        log.info("[DIAG] Test 3: store list_part")
        try:
            result = await self.bulkcmd("store list_part", timeout=10, sudo_password=sudo_password)
            results["store_list_part"] = f"RC={result.returncode} out={result.stdout[:200]}"
            log.info("[DIAG] store list_part: %s", results["store_list_part"])
        except Exception as exc:
            results["store_list_part"] = f"FAIL: {exc}"

        # Test 4: printenv partitions
        log.info("[DIAG] Test 4: printenv partitions")
        try:
            result = await self.bulkcmd("printenv partitions", timeout=10, sudo_password=sudo_password)
            results["printenv_partitions"] = f"RC={result.returncode} out={result.stdout[:200]}"
            log.info("[DIAG] printenv partitions: %s", results["printenv_partitions"])
        except Exception as exc:
            results["printenv_partitions"] = f"FAIL: {exc}"

        # Test 5: Direct NAND→host read test (bootloader = smallest partition)
        log.info("[DIAG] Test 5: Direct mread store bootloader")
        try:
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                # Try direct NAND→host read of bootloader
                result_path = await self.dump_partition(
                    partition_label="bootloader",
                    output_path=tmp_path,
                    size=0x100000,  # 1 MB
                    timeout=60,
                    sudo_password=sudo_password,
                )
                if tmp_path.exists():
                    data = tmp_path.read_bytes()[:64]
                    magic_status = validate_dump_magic(tmp_path, "bootloader")
                    results["direct_read"] = (
                        f"size={tmp_path.stat().st_size} bytes, "
                        f"magic={magic_status}, "
                        f"first_16_hex={data[:16].hex()}"
                    )
                    log.info("[DIAG] Direct read: %s", results["direct_read"])
                else:
                    results["direct_read"] = "FAIL: temp file not created"
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            results["direct_read"] = f"FAIL: {exc}"
            log.error("[DIAG] Direct read test failed: %s", exc)

        log.info("[DIAG] All diagnostics complete: %s", list(results.keys()))
        return results

    # ─── Partition Listing ─────────────────────────────────────────────

    async def list_partitions(
        self,
        timeout: int = 10,
        *,
        sudo_password: str = "",
    ) -> str:
        """
        Query the device for partition information.

        Tries:
          - ``bulkcmd "printenv partitions"``
          - ``bulkcmd "printenv"`` (full env for parsing)
          - ``bulkcmd "store list"`` (may work on some builds)
        """
        log = logging.getLogger(__name__)
        outputs: list[str] = []

        # Try printenv partitions
        try:
            result = await self.bulkcmd("printenv partitions", timeout=timeout, sudo_password=sudo_password)
            if result.returncode == 0:
                outputs.append(f"[printenv partitions]\n{result.stdout}")
        except Exception as exc:
            log.debug("printenv partitions failed: %s", exc)

        # Try full printenv for partition-related variables
        try:
            result = await self.bulkcmd("printenv", timeout=timeout, sudo_password=sudo_password)
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
            result = await self.bulkcmd("store list", timeout=timeout, sudo_password=sudo_password)
            if result.returncode == 0:
                outputs.append(f"[store list]\n{result.stdout}")
        except Exception as exc:
            log.debug("store list failed: %s", exc)

        return "\n\n".join(outputs) if outputs else "(no partition info available)"

    async def query_partition_table(
        self,
        timeout: int = 15,
        *,
        sudo_password: str = "",
    ) -> dict[str, dict[str, int | str]]:
        """
        Query the device for actual partition names and sizes.

        Returns:
            Dict mapping partition label -> {"size": int, "offset": int, "type": str}
        """
        log = logging.getLogger(__name__)
        partitions: dict[str, dict[str, int | str]] = {}

        # Try store list_part first
        try:
            result = await self.bulkcmd(
                "store list_part", timeout=timeout, sudo_password=sudo_password,
            )
            if result.returncode == 0:
                log.debug("store list_part output:\n%s", result.stdout)
                for line in result.stdout.splitlines():
                    line = line.strip()
                    m = re.match(
                        r'(\w+)\s*[:\s]\s*size\s*=\s*(0x[0-9a-fA-F]+)',
                        line,
                    )
                    if m:
                        label = m.group(1)
                        partitions[label] = {
                            "size": int(m.group(2), 16),
                            "offset": 0,
                            "type": "data",
                        }
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].isalnum():
                        try:
                            sz = int(parts[1], 16) if parts[1].startswith("0x") else int(parts[1])
                            off = int(parts[2], 16) if len(parts) > 2 and parts[2].startswith("0x") else 0
                            partitions[parts[0]] = {
                                "size": sz,
                                "offset": off,
                                "type": "data",
                            }
                        except (ValueError, IndexError):
                            pass
                if partitions:
                    log.debug("Parsed %d partitions from store list_part", len(partitions))
                    return partitions
        except Exception as exc:
            log.debug("store list_part failed: %s", exc)

        # Fallback: store list
        try:
            result = await self.bulkcmd(
                "store list", timeout=timeout, sudo_password=sudo_password,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    m = re.match(r'(\w+)\s*[:\s]\s*(0x[0-9a-fA-F]+)', line)
                    if m:
                        label = m.group(1)
                        try:
                            partitions[label] = {
                                "size": int(m.group(2), 16),
                                "offset": 0,
                                "type": "data",
                            }
                        except ValueError:
                            pass
                if partitions:
                    return partitions
        except Exception as exc:
            log.debug("store list failed: %s", exc)

        # Third fallback: printenv partitions
        try:
            result = await self.bulkcmd(
                "printenv partitions", timeout=timeout, sudo_password=sudo_password,
            )
            if result.returncode == 0:
                labels = re.split(r'[,\s]+', result.stdout.strip())
                for label in labels:
                    label = label.strip()
                    if label and label.isalnum():
                        partitions[label] = {
                            "size": 0,
                            "offset": 0,
                            "type": "data",
                        }
                if partitions:
                    log.debug("Parsed %d partition labels from printenv", len(partitions))
        except Exception as exc:
            log.debug("printenv partitions failed: %s", exc)

        return partitions

    async def get_partition_size(
        self,
        partition_label: str,
        fallback_size: int = 0,
        *,
        sudo_password: str = "",
    ) -> int:
        """Query actual partition size from device, with fallback."""
        try:
            table = await self.query_partition_table(sudo_password=sudo_password)
            if partition_label in table:
                size = int(table[partition_label].get("size", 0))
                if size > 0:
                    return size
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "query_partition_table failed for '%s': %s", partition_label, exc,
            )
        return fallback_size


    # ─── Partition Flash ───────────────────────────────────────────────

    async def partition(
        self,
        partition_name: str,
        image_path: Path,
        *,
        partition_type: str = "normal",
        timeout: int = 300,
        on_progress: callable | None = None,    # type: ignore[type-arg]
        sudo_password: str = "",
    ) -> RunResult:
        """
        Flash an image to a named partition.

        Real syntax (from aml-flash-tool.sh)::

            update partition <name> <image> [type]

        The ``partition_type`` is typically "normal", "sparse", or "dtb".
        """
        log = logging.getLogger(__name__)
        log.info("Flashing partition '%s' from %s (type=%s)", partition_name, image_path, partition_type)

        cmd = [self._exe, "partition", partition_name, str(image_path), partition_type]

        try:
            result = await run_streaming(
                cmd,
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
        except Exception as exc:
            if not sudo_password:
                raise

            # Retry with sudo as fallback for USB permission issues
            log.debug("Retrying partition flash with sudo...")
            try:
                from lx06_tool.utils.sudo import sudo_run
                sudo_result = await sudo_run(
                    [str(c) for c in cmd],
                    password=sudo_password,
                    timeout=timeout,
                )
                if sudo_result.ok:
                    log.debug("Partition flash succeeded via sudo fallback")
                    return RunResult(
                        cmd=[str(c) for c in cmd],
                        returncode=0,
                        stdout=sudo_result.output,
                        stderr="",
                    )
                else:
                    raise UpdateExeError(
                        f"flash of '{partition_name}' from '{image_path}' failed (sudo): {sudo_result.output}",
                        returncode=sudo_result.returncode,
                    )
            except UpdateExeError:
                raise
            except Exception as sudo_exc:
                log.debug("sudo partition flash retry failed: %s", sudo_exc)
                raise exc  # Raise original exception

    # ─── Legacy / Compatibility ────────────────────────────────────────

    async def write(
        self,
        partition: str,
        image_path: Path,
        *,
        timeout: int = 300,
        on_progress: callable | None = None,    # type: ignore[type-arg]
        sudo_password: str = "",
    ) -> RunResult:
        """
        Flash an image to a partition (convenience wrapper).

        Delegates to ``partition()`` with the default type "normal".
        """
        return await self.partition(
            partition_name=partition,
            image_path=image_path,
            partition_type="normal",
            timeout=timeout,
            on_progress=on_progress,
            sudo_password=sudo_password,
        )
