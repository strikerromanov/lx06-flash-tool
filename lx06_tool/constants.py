"""
lx06_tool/constants.py
----------------------
Hardware constants, partition maps, USB identifiers, download URLs,
and per-distro package name tables.

CachyOS is Arch-based: uses pacman, optionally paru/yay for AUR.
"""

from __future__ import annotations

from typing import Final

# ─── USB / Amlogic ────────────────────────────────────────────────────────────

# Amlogic SoC USB burning mode identifiers
AMLOGIC_USB_VENDOR_ID:  Final[str] = "1b8e"
AMLOGIC_USB_PRODUCT_ID: Final[str] = "c003"

# The LX06 uses the Amlogic AXG (A113X) SoC
LX06_CHIP: Final[str] = "AXG"

# udev rule line — identifies the Amlogic bootloader USB mode.
# Uses MODE="0666" for universal access + TAG+="uaccess" for systemd-udevd
# (Arch/CachyOS use systemd-udevd which honours uaccess tags).
# No GROUP="plugdev" — that group only exists on Debian/Ubuntu.
UDEV_RULE_LINE: Final[str] = (
    'SUBSYSTEM=="usb", '
    f'ATTR{{idVendor}}=="{AMLOGIC_USB_VENDOR_ID}", '
    f'ATTR{{idProduct}}=="{AMLOGIC_USB_PRODUCT_ID}", '
    'MODE="0666", TAG+="uaccess"'
)

# Handshake polling parameters
FAST_POLL_INTERVAL_S: Final[float] = 0.05    # 50 ms — sysfs/lsusb fast poll
HANDSHAKE_POLL_INTERVAL_S: Final[float] = 0.1   # 100 ms — identify poll
HANDSHAKE_DEFAULT_TIMEOUT_S: Final[int] = 120    # 2 minutes

# ─── Partition Map ────────────────────────────────────────────────────────────

# LX06 NAND partition layout (Amlogic AXG platform).
# mtd device → label, expected size in bytes.
#
# NOTE: Official xiaoai-patch guide shows smaller system partitions (26.5 MB),
# but actual LX06 devices have 40 MB system partitions. Using actual device sizes.
# Verified from successful backups.
PARTITION_MAP: Final[dict[str, dict[str, object]]] = {
    "mtd0": {"label": "bootloader", "size": 0x200000},   #  2 MB
    "mtd1": {"label": "tpl",        "size": 0x800000},   #  8 MB
    "mtd2": {"label": "boot0",      "size": 0x600000},   #  6 MB
    "mtd3": {"label": "boot1",      "size": 0x600000},   #  6 MB
    "mtd4": {"label": "system0",    "size": 0x2800000},  # 40 MB — SquashFS rootfs A
    "mtd5": {"label": "system1",    "size": 0x2800000},  # 40 MB — SquashFS rootfs B
    "mtd6": {"label": "data",       "size": 0x1400000},  # 20 MB
}

# Per-partition dump timeouts (seconds) — USB 2.0 transfer is slow.
# Large squashfs partitions (~26.5 MB) and data partition (20 MB) can take
# 10-15 minutes each over USB bulk due to protocol overhead.
PARTITION_TIMEOUTS: Final[dict[str, int]] = {
    "bootloader": 180,   # 3 min — small (2 MB)
    "tpl":        240,   # 4 min — medium (8 MB)
    "boot0":      180,   # 3 min — medium (6 MB)
    "boot1":      180,   # 3 min — medium (6 MB)
    "system0":    900,   # 15 min — large squashfs (26.5 MB)
    "system1":    900,   # 15 min — large squashfs (26.5 MB)
    "data":       900,   # 15 min — data partition (20 MB, may be slow)
}
DEFAULT_PARTITION_TIMEOUT: Final[int] = 300  # 5 min fallback

# Per-partition flash timeouts (seconds) — writes are slower than reads.
FLASH_PARTITION_TIMEOUTS: Final[dict[str, int]] = {
    "bootloader": 180,   # 3 min
    "tpl":        240,   # 4 min
    "boot0":      180,   # 3 min
    "boot1":      180,   # 3 min
    "system0":    600,   # 10 min — large squashfs writes are slow
    "system1":    600,   # 10 min — large squashfs writes are slow
    "data":       600,   # 10 min
}
DEFAULT_FLASH_TIMEOUT: Final[int] = 300  # 5 min fallback for flash

# Which partitions form the A/B slot pairs
AB_BOOT_SLOTS:   Final[tuple[str, str]] = ("boot0",   "boot1")
AB_SYSTEM_SLOTS: Final[tuple[str, str]] = ("system0", "system1")

# Partitions that are read-only references (bootloader / tpl) — never flash these
READ_ONLY_PARTITIONS: Final[frozenset[str]] = frozenset({"bootloader", "tpl"})

# ─── aml-flash-tool / update.exe ─────────────────────────────────────────────

AML_FLASH_TOOL_REPO: Final[str] = "https://github.com/radxa/aml-flash-tool.git"
AML_FLASH_TOOL_DIR:  Final[str] = "aml-flash-tool"

# Relative path inside the cloned repo to the Linux x86-64 binary
UPDATE_EXE_RELPATH: Final[str] = "tools/linux-x86/update"

# udev rules file from the Radxa repo (Ubuntu 14 era, but the rule content is fine)
AML_UDEV_RULES_RELPATH: Final[str] = "tools/_install_/70-persistent-usb-ubuntu14.rules"
# Where we install our own udev rule
UDEV_RULES_DEST: Final[str] = "/etc/udev/rules.d/70-lx06-amlogic.rules"

# Old/conflicting udev rules that should be removed before installing ours.
# These come from older versions of aml-flash-tool, Radxa scripts,
# or Ubuntu-specific packages that use GROUP="plugdev" (doesn't exist on Arch).
OLD_UDEV_RULES: Final[list[str]] = [
    "/etc/udev/rules.d/70-persistent-usb-ubuntu14.rules",
    "/etc/udev/rules.d/99-amlogic.rules",
    "/lib/udev/rules.d/70-persistent-usb-ubuntu14.rules",
    "/usr/lib/udev/rules.d/70-persistent-usb-ubuntu14.rules",
    "/etc/udev/rules.d/99-amlogic-usb.rules",
    "/lib/udev/rules.d/99-amlogic-usb.rules",
]

# Directories to search for old Amlogic udev rules via glob
UDEV_GLOB_DIRS: Final[list[str]] = [
    "/etc/udev/rules.d",
    "/lib/udev/rules.d",
    "/usr/lib/udev/rules.d",
    "/run/udev/rules.d",
]

# Filename patterns that indicate old/conflicting Amlogic udev rules
UDEV_GLOB_PATTERNS: Final[list[str]] = [
    "*amlogic*",
    "*1b8e*",
    "*aml*usb*",
    "*persistent-usb*",
]

# ─── Paths & Directories ─────────────────────────────────────────────────────

# Application identifier used for config/data/cache dirs (XDG)
APP_NAME: Final[str] = "lx06-tool"

# Default subdirectory names (resolved at runtime relative to XDG dirs)
BACKUP_SUBDIR:  Final[str] = "backups"
BUILD_SUBDIR:   Final[str] = "build"
TOOLS_SUBDIR:   Final[str] = "tools"

# Docker image used for isolated squashfs builds
DOCKER_BUILD_IMAGE: Final[str] = "lx06-firmware-builder:latest"

# ─── Per-Distro Package Tables ────────────────────────────────────────────────
#
# Structure:
#   DISTRO_PACKAGES[family][logical_name] = "exact-package-name"
#
# Families:
#   "debian"  → apt  (Debian, Ubuntu, Raspberry Pi OS, …)
#   "fedora"  → dnf  (Fedora, RHEL, CentOS Stream, …)
#   "arch"    → pacman (Arch Linux, CachyOS, Manjaro, EndeavourOS, …)
#
# Notes for Arch/CachyOS:
#   - squashfs-tools      → pacman (core)
#   - libusb-compat       → pacman (extra) — provides libusb-0.1 ABI
#   - docker              → pacman (extra) — requires manual daemon start
#   - docker-buildx       → pacman (extra) — needed for multi-arch builds
#   - base-devel          → needed to build AUR packages / compile tools
#
DISTRO_PACKAGES: Final[dict[str, dict[str, str | None]]] = {
    "debian": {
        "git":            "git",
        "libusb":         "libusb-0.1-4",
        "squashfs_tools": "squashfs-tools",
        "docker":         "docker.io",
        "docker_compose": "docker-compose-plugin",
        "base_devel":     "build-essential",
        "python3":        "python3",
        "python3_venv":   "python3-venv",
    },
    "fedora": {
        "git":            "git",
        "libusb":         "libusb-compat-0.1",
        "squashfs_tools": "squashfs-tools",
        "docker":         "docker",       # via moby-engine on newer Fedora
        "docker_compose": "docker-compose-plugin",
        "base_devel":     "gcc make",
        "python3":        "python3",
        "python3_venv":   "python3-venv",
    },
    "arch": {
        # CachyOS, Arch Linux, Manjaro, EndeavourOS all use this family.
        "git":            "git",
        "libusb":         "libusb-compat",  # provides libusb-0.1 ABI for update.exe
        "squashfs_tools": "squashfs-tools",
        "docker":         "docker",
        "docker_compose": "docker-compose", # or docker-buildx for newer setups
        "base_devel":     "base-devel",     # group: gcc, make, pkg-config, …
        "python3":        "python",         # Arch uses 'python', not 'python3'
        "python3_venv":   None,             # venv is built into python on Arch
    },
}

# AUR packages — only needed on Arch family if official repos don't have them
# Install via: paru -S <pkg> or yay -S <pkg>
AUR_PACKAGES_ARCH: Final[dict[str, str]] = {
    # Currently no hard AUR requirements; listed for future reference.
    # "librespot": "librespot",  # Example: Spotify Connect daemon
}

# ─── OS Detection Markers ─────────────────────────────────────────────────────

# /etc/os-release ID values for each distro family
OS_FAMILY_MAP: Final[dict[str, str]] = {
    # Exact ID match → family
    "ubuntu":      "debian",
    "debian":      "debian",
    "raspbian":    "debian",
    "linuxmint":   "debian",
    "pop":         "debian",
    "elementary":  "debian",
    "kali":        "debian",
    "fedora":      "fedora",
    "rhel":        "fedora",
    "centos":      "fedora",
    "rocky":       "fedora",
    "almalinux":   "fedora",
    "arch":        "arch",
    "cachyos":     "arch",    # CachyOS reports ID=cachyos or ID=arch
    "manjaro":     "arch",
    "endeavouros": "arch",
    "garuda":      "arch",
    "artix":       "arch",
}

# ID_LIKE values — if ID doesn't match, check ID_LIKE
OS_LIKE_MAP: Final[dict[str, str]] = {
    "debian":      "debian",
    "ubuntu":      "debian",
    "arch":        "arch",    # CachyOS sets ID_LIKE=arch
    "fedora":      "fedora",
    "rhel":        "fedora",
}

# ─── Download URLs ────────────────────────────────────────────────────────────

# xiaogpt — soft AI patch
XIAOGPT_REPO: Final[str] = "https://github.com/yihong0618/xiaogpt.git"

# open-xiaoai — hard AI patch (Rust binary)
OPEN_XIAOAI_REPO: Final[str] = "https://github.com/idootop/open-xiaoai.git"

# xiaoai-patch — reference for SquashFS patching patterns
XIAOAI_PATCH_REPO: Final[str] = "https://github.com/duhow/xiaoai-patch.git"

# ─── Bootloader ──────────────────────────────────────────────────────────────

# Bootdelay value for unlocked bootloader (seconds)
BOOTLOADER_BOOTDELAY: Final[int] = 15

# ─── Minimum file size thresholds for sanity checks ───────────────────────────

MIN_SQUASHFS_SIZE_BYTES: Final[int] = 1 * 1024 * 1024   #  1 MB  — probably empty if smaller
MIN_PARTITION_DUMP_RATIO: Final[float] = 0.5              # Dump must be ≥ 50 % of expected size

# SquashFS magic bytes for format validation
SQUASHFS_MAGIC_LE: Final[bytes] = b'hsqs'   # Little-endian squashfs
SQUASHFS_MAGIC_BE: Final[bytes] = b'sqsh'   # Big-endian squashfs

# ─── SquashFS Build Settings ──────────────────────────────────────────────────

SQUASHFS_BLOCK_SIZE: Final[int] = 131072          # 128 KB blocks
SQUASHFS_COMPRESSION: Final[str] = "lz4"          # Fast compression for embedded
SQUASHFS_XATTRS: Final[bool] = True               # Preserve extended attributes
SQUASHFS_EXCLUDE: Final[list[str]] = [             # Glob patterns to exclude from repack
    "proc/*", "sys/*", "dev/*", "run/*", "tmp/*",
    "var/log/*", "var/cache/*",
]

# ─── Docker Image / Build ─────────────────────────────────────────────────────

# Legacy names used by docker_utils.py
DOCKER_BUILD_IMAGE_NAME: Final[str] = "lx06-firmware-builder"
DOCKER_BUILD_IMAGE_TAG: Final[str] = "latest"

# Firmware builder image reference (docker_builder.py)
FIRMWARE_BUILDER_IMAGE: Final[str] = f"{DOCKER_BUILD_IMAGE_NAME}:{DOCKER_BUILD_IMAGE_TAG}"

# Path to the Dockerfile for the firmware builder image
DOCKERFILE_PATH: Final[str] = "resources/docker/Dockerfile.firmware-builder"

# ─── Debloat Targets ──────────────────────────────────────────────────────────

# Xiaomi telemetry / data-collection services
BLOAT_SERVICES: Final[list[str]] = [
    "miio",
    "xiaomi-data-report",
    "xiaomi-stat-pkg",
    "naMi",
    "gateway-service",
]

# Known bloat binaries to remove
BLOAT_BINARIES: Final[list[str]] = [
    "/usr/bin/miio",
    "/usr/bin/naMi",
    "/usr/bin/xiaomi-data-report",
    "/usr/bin/xiaomi-stat-pkg",
]

# Bloat configuration files
BLOAT_CONFIGS: Final[list[str]] = [
    "/etc/miio/*.conf",
    "/etc/xiaomi/*.json",
    "/etc/init.d/*xiaomi*",
    "/etc/init.d/*miio*",
]

# Directories to clean out
BLOAT_DIRECTORIES: Final[list[str]] = [
    "/data/log",
    "/data/tmp",
    "/data/.xiaoai",
]

# OTA update packages / services
OTA_PACKAGES: Final[list[str]] = [
    "ota-update",
    "xiaoai-ota",
]

# ─── Logging ──────────────────────────────────────────────────────────────────

DEFAULT_LOG_DIR: Final[str] = "logs"
LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
LOG_MAX_FILES: Final[int] = 5
LOG_MAX_SIZE_MB: Final[int] = 10
