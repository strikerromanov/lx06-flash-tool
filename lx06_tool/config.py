"""
lx06_tool/config.py
-------------------
Configuration data models and persistence.

Uses `platformdirs` for XDG-compliant paths so the tool works correctly
regardless of which directory the user launches it from.

XDG locations (Linux):
  Config  → ~/.config/lx06-tool/config.yaml
  Data    → ~/.local/share/lx06-tool/  (backups, tools)
  Cache   → ~/.cache/lx06-tool/        (build workspace)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from platformdirs import user_cache_path, user_config_path, user_data_path

from lx06_tool.constants import (
    APP_NAME,
    BACKUP_SUBDIR,
    BUILD_SUBDIR,
    DOCKER_BUILD_IMAGE,
    TOOLS_SUBDIR,
)

# ─── XDG Paths ────────────────────────────────────────────────────────────────

def _xdg_config_dir() -> Path:
    return user_config_path(APP_NAME)


def _xdg_data_dir() -> Path:
    return user_data_path(APP_NAME)


def _xdg_cache_dir() -> Path:
    return user_cache_path(APP_NAME)


def default_config_path() -> Path:
    return _xdg_config_dir() / "config.yaml"


def default_backup_dir() -> Path:
    return _xdg_data_dir() / BACKUP_SUBDIR


def default_build_dir() -> Path:
    # Build workspace lives in cache — it's throwaway data.
    return _xdg_cache_dir() / BUILD_SUBDIR


def default_tools_dir() -> Path:
    return _xdg_data_dir() / TOOLS_SUBDIR


# ─── Device Model ─────────────────────────────────────────────────────────────

@dataclass
class LX06Device:
    """Runtime state for the connected LX06 device."""
    connected: bool = False
    serial: str = ""
    chip_id: str = ""
    firmware_version: str = ""

    # A/B partition tracking — populated after DEVICE_IDENTIFIED
    active_boot: str = ""       # "boot0" or "boot1"
    inactive_boot: str = ""
    active_system: str = ""     # "system0" or "system1"
    inactive_system: str = ""

    bootloader_unlocked: bool = False

    def is_ab_detected(self) -> bool:
        return bool(self.active_system and self.inactive_system)


# ─── Backup Model ─────────────────────────────────────────────────────────────

@dataclass
class PartitionBackup:
    """Tracks a single dumped partition."""
    name: str                        # "mtd0", "mtd4", …
    label: str                       # "bootloader", "system0", …
    path: Optional[Path] = None
    size_bytes: int = 0
    expected_size: int = 0
    sha256: str = ""
    md5: str = ""
    verified: bool = False

    @property
    def size_ok(self) -> bool:
        if self.expected_size == 0:
            return self.size_bytes > 0
        # Allow ≥ 50 % of expected (NAND may have bad blocks)
        return self.size_bytes >= self.expected_size * 0.5


@dataclass
class BackupSet:
    """Complete set of partition backups for one session."""
    partitions: dict[str, PartitionBackup] = field(default_factory=dict)
    timestamp: str = ""            # ISO-8601 timestamp of backup run
    backup_dir: Optional[Path] = None
    all_verified: bool = False

    @property
    def is_complete(self) -> bool:
        return bool(self.partitions) and all(
            p.verified for p in self.partitions.values()
        )


# ─── Customization Choices ────────────────────────────────────────────────────

@dataclass
class CustomizationChoices:
    """User's a-la-carte firmware modification selections."""

    # ── Debloat
    remove_telemetry: bool = True
    remove_auto_updater: bool = True
    remove_xiaoai_voice: bool = False   # Must be False for soft-patch AI mode

    # ── Media Players
    install_airplay: bool = False       # shairport-sync
    install_dlna: bool = False          # upmpdcli + mpd
    install_snapcast: bool = False      # squeezelite + snapclient
    install_spotify: bool = False       # librespot

    # ── AI Brain
    ai_mode: str = "none"              # "none" | "soft" (xiaogpt) | "hard" (open-xiaoai)
    llm_provider: str = ""             # "openai" | "gemini" | "kimi"
    llm_api_key: str = ""
    llm_model: str = ""
    custom_wake_word: str = ""          # Hard-patch only
    ai_server_url: str = ""            # Hard-patch only

    def validate(self) -> list[str]:
        """Return a list of validation error strings (empty = valid)."""
        errors: list[str] = []
        if self.ai_mode == "soft" and self.remove_xiaoai_voice:
            errors.append(
                "Soft AI mode (xiaogpt) requires the Xiaoai voice engine. "
                "Uncheck 'Remove Xiaoai voice' or switch to hard AI mode."
            )
        if self.ai_mode in ("soft", "hard") and not self.llm_api_key:
            errors.append(
                f"AI mode '{self.ai_mode}' requires an LLM API key."
            )
        if self.ai_mode == "hard" and not self.ai_server_url:
            errors.append(
                "Hard AI mode (open-xiaoai) requires an AI server URL."
            )
        return errors


# ─── Application Config ───────────────────────────────────────────────────────

@dataclass
class AppConfig:
    """
    Persistent application configuration.

    Saved to and loaded from ~/.config/lx06-tool/config.yaml.
    Runtime device/backup state is NOT persisted (too volatile).
    """

    # ── Paths (stored as strings in YAML, converted to Path on load)
    backup_dir: Path = field(default_factory=default_backup_dir)
    build_dir: Path = field(default_factory=default_build_dir)
    tools_dir: Path = field(default_factory=default_tools_dir)

    # Resolved path to the `update` binary (aml-flash-tool)
    update_exe_path: Optional[Path] = None

    # ── Docker
    use_docker_build: bool = True
    docker_build_image: str = DOCKER_BUILD_IMAGE

    # ── Network
    proxy: str = ""
    github_mirror: str = ""         # Useful for GFW users

    # ── Runtime state (not persisted)
    device: LX06Device = field(default_factory=LX06Device, repr=False)
    backup: BackupSet = field(default_factory=BackupSet, repr=False)
    choices: CustomizationChoices = field(
        default_factory=CustomizationChoices, repr=False
    )

    # ─────────────────────────────────────────────────────────────────

    def ensure_dirs(self) -> None:
        """Create all required directories (XDG + config dir)."""
        for d in (self.backup_dir, self.build_dir, self.tools_dir):
            d.mkdir(parents=True, exist_ok=True)
        default_config_path().parent.mkdir(parents=True, exist_ok=True)

    # ─── Persistence ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, object]:
        return {
            "paths": {
                "backup_dir":    str(self.backup_dir),
                "build_dir":     str(self.build_dir),
                "tools_dir":     str(self.tools_dir),
                "update_exe":    str(self.update_exe_path) if self.update_exe_path else "",
            },
            "docker": {
                "use_docker_build":  self.use_docker_build,
                "build_image":       self.docker_build_image,
            },
            "network": {
                "proxy":         self.proxy,
                "github_mirror": self.github_mirror,
            },
        }

    def save(self, path: Optional[Path] = None) -> None:
        config_path = path or default_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, default_flow_style=False)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        config_path = path or default_config_path()
        if not config_path.exists():
            return cls()

        with open(config_path, encoding="utf-8") as fh:
            raw: dict = yaml.safe_load(fh) or {}

        cfg = cls()

        paths = raw.get("paths", {})
        if paths.get("backup_dir"):
            cfg.backup_dir = Path(paths["backup_dir"]).expanduser()
        if paths.get("build_dir"):
            cfg.build_dir = Path(paths["build_dir"]).expanduser()
        if paths.get("tools_dir"):
            cfg.tools_dir = Path(paths["tools_dir"]).expanduser()
        if paths.get("update_exe"):
            p = Path(paths["update_exe"]).expanduser()
            cfg.update_exe_path = p if p != Path("") else None

        docker = raw.get("docker", {})
        cfg.use_docker_build = bool(docker.get("use_docker_build", True))
        cfg.docker_build_image = str(docker.get("build_image", DOCKER_BUILD_IMAGE))

        network = raw.get("network", {})
        cfg.proxy = str(network.get("proxy", ""))
        cfg.github_mirror = str(network.get("github_mirror", ""))

        return cfg


# ─── Environment variable overrides (for CI/headless use) ─────────────────────

def apply_env_overrides(cfg: AppConfig) -> None:
    """Override config fields from environment variables."""
    if val := os.environ.get("LX06_BACKUP_DIR"):
        cfg.backup_dir = Path(val)
    if val := os.environ.get("LX06_BUILD_DIR"):
        cfg.build_dir = Path(val)
    if val := os.environ.get("LX06_TOOLS_DIR"):
        cfg.tools_dir = Path(val)
    if val := os.environ.get("LX06_NO_DOCKER"):
        cfg.use_docker_build = val.lower() not in ("1", "true", "yes")
    if val := os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        cfg.proxy = cfg.proxy or val
