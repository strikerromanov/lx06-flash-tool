"""
lx06_tool/exceptions.py
-----------------------
Custom exception hierarchy for the LX06 Flash Tool.

IMPORTANT: Python has a built-in `EnvironmentError` (alias for OSError).
We deliberately use `HostEnvironmentError` here to avoid shadowing it.
"""

from __future__ import annotations

# ─── Base ────────────────────────────────────────────────────────────────────

class LX06Error(Exception):
    """Base exception for all LX06 Flash Tool errors."""

    def __init__(self, message: str, *, recoverable: bool = True, details: str = "") -> None:
        super().__init__(message)
        self.recoverable = recoverable  # Whether the state machine can back-track
        self.details = details          # Optional extra context


# ─── Environment / Host Setup ─────────────────────────────────────────────────

class HostEnvironmentError(LX06Error):
    """Host environment issues: missing dependencies, wrong OS, wrong Python."""


class UnsupportedDistroError(HostEnvironmentError):
    """The host Linux distribution is not supported."""


class DependencyMissingError(HostEnvironmentError):
    """A required host package is not installed."""


class DependencyInstallError(HostEnvironmentError):
    """Package installation via the host package manager failed."""


class DockerNotRunningError(HostEnvironmentError):
    """Docker daemon is not running or current user lacks permissions."""


class PythonVersionError(HostEnvironmentError):
    """Python version is too old."""


# ─── USB / Device ─────────────────────────────────────────────────────────────

class USBError(LX06Error):
    """USB communication failure."""


class HandshakeTimeoutError(USBError):
    """Device was not detected in the Amlogic USB bootloader window."""

    def __init__(self, timeout_seconds: int = 120) -> None:
        super().__init__(
            f"Device not identified after {timeout_seconds}s. "
            "Power-cycle the speaker and try again."
        )
        self.timeout_seconds = timeout_seconds


class DeviceDisconnectedError(USBError):
    """Device disconnected unexpectedly during an operation."""


class UdevRulesError(USBError):
    """Failed to install or reload udev rules."""


class UpdateExeError(USBError):
    """The Amlogic update.exe tool returned an error."""

    def __init__(self, message: str, *, returncode: int = -1) -> None:
        super().__init__(message)
        self.returncode = returncode


class AmlogicToolError(USBError):
    """Amlogic update tool (update.exe) command failure."""

    def __init__(self, message: str, *, returncode: int = -1) -> None:
        super().__init__(message)
        self.returncode = returncode



# ─── Backup ───────────────────────────────────────────────────────────────────

class BackupError(LX06Error):
    """Backup-related failure."""


class PartitionDumpError(BackupError):
    """Failed to dump a partition."""

    def __init__(self, partition: str, reason: str) -> None:
        super().__init__(f"Failed to dump partition '{partition}': {reason}")
        self.partition = partition


class ChecksumMismatchError(BackupError):
    """Checksum verification failed for a backup file."""

    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(
            f"Checksum mismatch for '{path}': expected {expected[:12]}…, got {actual[:12]}…"
        )
        self.expected = expected
        self.actual = actual


class BackupIncompleteError(BackupError):
    """Not all partitions were backed up successfully."""


# ─── Bootloader ───────────────────────────────────────────────────────────────

class BootloaderError(LX06Error):
    """U-boot interaction failure."""


class BootloaderUnlockError(BootloaderError):
    """Failed to unlock the bootloader (set bootdelay)."""


# ─── Firmware / SquashFS ──────────────────────────────────────────────────────

class FirmwareError(LX06Error):
    """SquashFS manipulation failure."""


class SquashFSExtractError(FirmwareError):
    """unsquashfs failed."""


class SquashFSBuildError(FirmwareError):
    """mksquashfs failed."""


class SquashFSRepackError(SquashFSBuildError):
    """Repacking squashfs failed."""


class SquashFSError(FirmwareError):
    """General SquashFS operation failure."""


class InvalidFirmwareError(FirmwareError):
    """Firmware image is invalid or corrupted."""


class RootfsModificationError(FirmwareError):
    """Failed to apply a modification to the extracted rootfs."""


# ─── Flash ────────────────────────────────────────────────────────────────────

class FlashError(LX06Error):
    """Flashing operation failure."""

    def __init__(self, message: str, *, partition: str = "", recoverable: bool = True) -> None:
        super().__init__(message, recoverable=recoverable)
        self.partition = partition


class ActivePartitionError(FlashError):
    """Attempted to flash the currently active partition (safety guard)."""

    def __init__(self, partition: str) -> None:
        super().__init__(
            f"Refusing to flash active partition '{partition}'. "
            "Only the inactive partition may be flashed.",
            partition=partition,
            recoverable=False,
        )


class FlashVerificationError(FlashError):
    """Post-flash verification failed."""


# ─── Docker / Build ───────────────────────────────────────────────────────────

class DockerBuildError(LX06Error):
    """Docker-based firmware build failure."""


class DockerImageError(DockerBuildError):
    """Could not build or pull the firmware builder Docker image."""


class DockerImageNotFoundError(DockerBuildError):
    """Docker image not found locally."""


class DockerNotAvailableError(HostEnvironmentError):
    """Docker is not installed or not accessible."""


# ─── Config / State ───────────────────────────────────────────────────────────

class ConfigError(LX06Error):
    """Configuration file is missing, malformed, or has invalid values."""


class StateTransitionError(LX06Error):
    """An illegal state machine transition was attempted."""

    def __init__(self, from_state: str, to_state: str, reason: str) -> None:
        super().__init__(
            f"Cannot transition {from_state} → {to_state}: {reason}",
            recoverable=False,
        )


# ─── Network / Downloads ──────────────────────────────────────────────────────

class DownloadError(LX06Error):
    """File download failure."""


class APIKeyValidationError(LX06Error):
    """LLM API key validation against the provider endpoint failed."""
