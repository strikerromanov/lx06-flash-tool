"""
Configuration management and data models for LX06 Flash Tool.

Provides:
- Data models: LX06Device, PartitionBackup, BackupSet, CustomizationChoices, AppConfig
- YAML-based config loading/saving
- Runtime state persistence between sessions
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from lx06_tool.constants import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILE,
    DEFAULT_LOG_DIR,
    PARTITION_MAP,
)

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class LX06Device:
    """Represents the connected LX06 device and its partition state.

    Populated at runtime after USB handshake succeeds.
    """

    connected: bool = False
    serial: str = ""
    chip_id: str = ""
    active_boot: str = ""       # "boot0" or "boot1"
    inactive_boot: str = ""     # Complement of active_boot
    active_system: str = ""     # "system0" or "system1"
    inactive_system: str = ""   # Complement of active_system
    bootloader_unlocked: bool = False

    def set_active_partition(self, boot_slot: int, system_slot: int) -> None:
        """Set the active/inactive partition pair from slot indices (0 or 1).

        Args:
            boot_slot: Active boot partition index (0 or 1).
            system_slot: Active system partition index (0 or 1).
        """
        self.active_boot = f"boot{boot_slot}"
        self.inactive_boot = f"boot{1 - boot_slot}"
        self.active_system = f"system{system_slot}"
        self.inactive_system = f"system{1 - system_slot}"


@dataclass
class PartitionBackup:
    """Tracks a single partition backup file."""

    name: str                  # e.g. "mtd0"
    label: str                 # e.g. "bootloader"
    size_bytes: int = 0
    expected_size: int = 0     # From PARTITION_MAP
    sha256: str = ""
    md5: str = ""
    path: Optional[str] = None  # Serialized as string for YAML compat
    verified: bool = False

    def __post_init__(self) -> None:
        if self.expected_size == 0 and self.name in PARTITION_MAP:
            self.expected_size = PARTITION_MAP[self.name]["size"]


@dataclass
class BackupSet:
    """Complete set of partition backups for a device."""

    partitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    timestamp: str = ""
    all_verified: bool = False

    def add_partition(self, backup: PartitionBackup) -> None:
        """Add or update a partition backup entry."""
        self.partitions[backup.name] = asdict(backup)

    def get_partition(self, mtd_name: str) -> PartitionBackup | None:
        """Retrieve a partition backup by MTD name."""
        if mtd_name in self.partitions:
            return PartitionBackup(**self.partitions[mtd_name])
        return None

    def mark_timestamp(self) -> None:
        """Set the timestamp to the current UTC time."""
        self.timestamp = datetime.utcnow().isoformat() + "Z"


@dataclass
class CustomizationChoices:
    """User's feature selections for firmware modification.

    Populated by the interactive customization menu (Phase 3).
    """

    # Debloat
    debloat_enabled: bool = True           # Master switch for debloat
    remove_telemetry: bool = True
    remove_ota: bool = True                # Remove OTA auto-updater
    remove_xiaoai: bool = False            # Remove stock voice engine (aggressive)
    remove_auto_updater: bool = True       # Alias for remove_ota
    remove_xiaoai_voice: bool = False      # Alias for remove_xiaoai

    # Media Players
    media_enabled: bool = False            # Master switch for media suite
    install_airplay: bool = False          # shairport-sync
    install_dlna: bool = False             # upmpdcli + mpd
    install_snapcast: bool = False         # snapcast client
    install_squeezelite: bool = False      # squeezelite
    install_spotify: bool = False          # librespot

    # Media Settings
    media_device_name: str = "LX06-Speaker"  # Cast receiver name
    spotify_username: str = ""
    spotify_password: str = ""
    audio_output: str = "default"           # ALSA output device

    # AI Brain
    ai_enabled: bool = False               # Master switch for AI
    ai_mode: str = "none"                  # "none", "soft" (xiaogpt), "hard" (open-xiaoai)
    llm_provider: str = ""                 # "openai", "gemini", "kimi"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_api_base: str = ""                 # Custom API base URL
    wake_word: str = ""                    # For hard-patch mode
    custom_wake_word: str = ""             # Alias for wake_word
    ai_server_url: str = ""               # For hard-patch mode (open-xiaoai server)

    def __post_init__(self) -> None:
        """Synchronize alias fields."""
        if self.remove_ota:
            self.remove_auto_updater = True
        if self.remove_xiaoai:
            self.remove_xiaoai_voice = True
        if self.wake_word and not self.custom_wake_word:
            self.custom_wake_word = self.wake_word
        elif self.custom_wake_word and not self.wake_word:
            self.wake_word = self.custom_wake_word

    @property
    def has_media_selected(self) -> bool:
        """Check if any media player option was selected."""
        return any([
            self.install_airplay,
            self.install_dlna,
            self.install_snapcast,
            self.install_spotify,
            self.install_squeezelite,
        ])

    @property
    def has_ai_selected(self) -> bool:
        """Check if any AI mode was selected."""
        return self.ai_mode in ("soft", "hard")

    @property
    def needs_api_key(self) -> bool:
        """Check if an API key is required for the selected AI mode."""
        return self.ai_mode == "soft"

@dataclass
class AppConfig:
    """Persistent application configuration.

    Stored in ~/.config/lx06-tool/config.yaml.
    Device, backup, and choices are runtime-only (not persisted to disk).
    """

    # Paths
    backup_dir: str = "./backups"
    build_dir: str = "./build"
    tools_dir: str = "./tools"

    # Tools (populated at runtime after download)
    aml_flash_tool_path: str = ""
    update_exe_path: str = ""

    # Docker
    use_docker_build: bool = True
    docker_build_image: str = "lx06-firmware-builder:latest"

    # Network
    proxy: str = ""
    github_mirror: str = ""

    # ── Runtime state (not persisted) ──
    device: dict[str, Any] = field(default_factory=dict, repr=False)
    backup: dict[str, Any] = field(default_factory=dict, repr=False)
    choices: dict[str, Any] = field(default_factory=dict, repr=False)

    # ── Persistence ─────────────────────────────────────────────────────────

    @classmethod
    def _persistent_fields(cls) -> set[str]:
        """Fields that should be saved to/loaded from disk."""
        return {
            "backup_dir", "build_dir", "tools_dir",
            "aml_flash_tool_path", "update_exe_path",
            "use_docker_build", "docker_build_image",
            "proxy", "github_mirror",
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize persistent fields to a dict for YAML storage."""
        return {k: getattr(self, k) for k in self._persistent_fields()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """Deserialize from a dict, ignoring unknown keys."""
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    def save(self, path: Path | None = None) -> None:
        """Save configuration to a YAML file."""
        target = path or DEFAULT_CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        with open(target, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=True)
        logger.debug("Config saved to %s", target)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        """Load configuration from a YAML file.

        Returns a default AppConfig if the file doesn't exist.
        """
        source = path or DEFAULT_CONFIG_FILE
        if not source.exists():
            logger.debug("No config file at %s, using defaults", source)
            return cls()
        with open(source) as f:
            data = yaml.safe_load(f) or {}
        logger.debug("Config loaded from %s", source)
        return cls.from_dict(data)

    # ── Path Helpers ─────────────────────────────────────────────────────────

    def get_backup_dir(self) -> Path:
        """Resolve the backup directory path."""
        return Path(self.backup_dir).resolve()

    def get_build_dir(self) -> Path:
        """Resolve the build directory path."""
        return Path(self.build_dir).resolve()

    def get_tools_dir(self) -> Path:
        """Resolve the tools directory path."""
        return Path(self.tools_dir).resolve()

    def get_update_exe(self) -> Path | None:
        """Resolve the update.exe path, or None if not set."""
        if not self.update_exe_path:
            return None
        p = Path(self.update_exe_path)
        return p if p.exists() else None

    def ensure_dirs(self) -> None:
        """Create all working directories if they don't exist."""
        for dir_path in [self.backup_dir, self.build_dir, self.tools_dir]:
            Path(dir_path).resolve().mkdir(parents=True, exist_ok=True)


# ── Module-level convenience functions ──────────────────────────────────────


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from disk (convenience wrapper)."""
    config = AppConfig.load(path)
    config.ensure_dirs()
    return config


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Save configuration to disk (convenience wrapper)."""
    config.save(path)
