"""
Custom exception hierarchy for LX06 Flash Tool.

All tool-specific exceptions inherit from LX06Error, enabling
both broad catch-all and fine-grained error handling.
"""


class LX06Error(Exception):
    """Base exception for all LX06 Flash Tool errors."""

    def __init__(self, message: str = "", *, details: str = "", recoverable: bool = True):
        self.details = details
        self.recoverable = recoverable
        super().__init__(message)


# ── Environment Errors ──────────────────────────────────────────────────────


class EnvironmentError(LX06Error):
    """Host environment issues: unsupported OS, missing dependencies, wrong versions."""


class UnsupportedOSError(EnvironmentError):
    """The host operating system is not supported."""


class PackageNotFoundError(EnvironmentError):
    """A required system package could not be found or installed."""


class DockerNotAvailableError(EnvironmentError):
    """Docker daemon is not running or user lacks permissions."""


class PermissionDeniedError(EnvironmentError):
    """Insufficient permissions for a system-level operation."""

    def __init__(self, message: str = "", *, details: str = ""):
        super().__init__(message, details=details, recoverable=True)


# ── USB & Communication Errors ──────────────────────────────────────────────


class USBError(LX06Error):
    """USB communication failures with the LX06 device."""


class HandshakeTimeoutError(USBError):
    """Device not detected within the AmlUsbIdentifyHost bootloader window."""

    def __init__(self, message: str = "", *, details: str = ""):
        super().__init__(
            message or "Device not detected within the bootloader handshake window",
            details=details or "Ensure the speaker is plugged in via USB and powered on",
            recoverable=True,
        )


class DeviceDisconnectedError(USBError):
    """Device was disconnected during an operation."""


class AmlogicToolError(USBError):
    """update.exe / aml-flash-tool returned a non-zero exit code."""


# ── Backup Errors ───────────────────────────────────────────────────────────


class BackupError(LX06Error):
    """Backup dump or verification failures."""


class ChecksumMismatchError(BackupError):
    """Dump file checksum does not match the expected value."""

    def __init__(
        self,
        partition: str = "",
        expected: str = "",
        actual: str = "",
        *,
        algorithm: str = "sha256",
    ):
        self.partition = partition
        self.expected = expected
        self.actual = actual
        self.algorithm = algorithm
        msg = (
            f"Checksum mismatch for {partition} ({algorithm}): "
            f"expected {expected[:16]}... got {actual[:16]}..."
        )
        super().__init__(msg, recoverable=False)


class SizeMismatchError(BackupError):
    """Dump file size does not match the expected partition size."""

    def __init__(self, partition: str = "", expected: int = 0, actual: int = 0):
        self.expected_size = expected
        self.actual_size = actual
        msg = f"Size mismatch for {partition}: expected {expected} bytes, got {actual} bytes"
        super().__init__(msg, recoverable=False)


class BackupIncompleteError(BackupError):
    """One or more partitions failed to dump."""


# ── Firmware Errors ─────────────────────────────────────────────────────────


class FirmwareError(LX06Error):
    """SquashFS manipulation or firmware build failures."""


class SquashFSError(FirmwareError):
    """General SquashFS operation error (alias for firmware errors during squashfs ops)."""

class SquashFSExtractError(FirmwareError):
    """unsquashfs failed to extract the firmware image."""


class SquashFSRepackError(FirmwareError):
    """mksquashfs failed to repack the modified rootfs."""


class InvalidFirmwareError(FirmwareError):
    """The firmware image is corrupted or not a valid squashfs."""


class ModificationError(FirmwareError):
    """A firmware modification (debloat/media/AI) failed."""


# ── Flash Errors ────────────────────────────────────────────────────────────


class FlashError(LX06Error):
    """Flashing operation failures."""


class PartitionDetectionError(FlashError):
    """Could not determine the active/inactive A/B partition slot."""


class FlashVerifyError(FlashError):
    """Post-flash verification failed."""


class ActivePartitionError(FlashError):
    """Attempted to flash to the active partition (dangerous)."""

    def __init__(self, partition: str = ""):
        msg = f"Refusing to flash to active partition '{partition}'. This would brick the device."
        super().__init__(msg, recoverable=False)


# ── Docker Build Errors ────────────────────────────────────────────────────


class DockerBuildError(LX06Error):
    """Docker-based firmware build failures."""


class DockerImageNotFoundError(DockerBuildError):
    """The firmware builder Docker image has not been built."""


class DockerContainerError(DockerBuildError):
    """A Docker container operation failed during the build."""


# ── Configuration Errors ────────────────────────────────────────────────────


class ConfigError(LX06Error):
    """Configuration loading, validation, or generation errors."""


class TemplateRenderError(ConfigError):
    """A Jinja2 template failed to render."""


class APIKeyValidationError(ConfigError):
    """An LLM API key failed validation."""
