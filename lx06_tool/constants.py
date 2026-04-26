"""
lx06_tool/constants.py
----------------------
Hardware constants, partition maps, download URLs, and per-distro package tables.

Based on the official xiaoai-patch guide and LX06 hardware reference.
"""

from __future__ import annotations

from typing import Final

# ─── Application ───────────────────────────────────────────────────────────────

APP_NAME: Final[str] = "lx06-tool"

BACKUP_SUBDIR:  Final[str] = "backups"
BUILD_SUBDIR:   Final[str] = "build"
TOOLS_SUBDIR:   Final[str] = "tools"

# Docker image used for isolated squashfs builds (legacy, kept for config compat)
DOCKER_BUILD_IMAGE: Final[str] = "lx06-firmware-builder:latest"

# ─── Logging ──────────────────────────────────────────────────────────────────

DEFAULT_LOG_DIR: Final[str] = "logs"
LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
LOG_MAX_FILES: Final[int] = 5
LOG_MAX_SIZE_MB: Final[int] = 10

# ─── USB / Amlogic ────────────────────────────────────────────────────────────

AMLOGIC_USB_VENDOR_ID:  Final[str] = "1b8e"
AMLOGIC_USB_PRODUCT_ID: Final[str] = "c003"

UDEV_RULE_LINE: Final[str] = (
    'SUBSYSTEM=="usb", '
    f'ATTR{{idVendor}}=="{AMLOGIC_USB_VENDOR_ID}", '
    f'ATTR{{idProduct}}=="{AMLOGIC_USB_PRODUCT_ID}", '
    'MODE="0666", TAG+="uaccess"'
)

UDEV_RULES_DEST: Final[str] = "/etc/udev/rules.d/70-lx06-amlogic.rules"

OLD_UDEV_RULES: Final[list[str]] = [
    "/etc/udev/rules.d/70-persistent-usb-ubuntu14.rules",
    "/etc/udev/rules.d/99-amlogic.rules",
    "/lib/udev/rules.d/70-persistent-usb-ubuntu14.rules",
    "/usr/lib/udev/rules.d/70-persistent-usb-ubuntu14.rules",
    "/etc/udev/rules.d/99-amlogic-usb.rules",
    "/lib/udev/rules.d/99-amlogic-usb.rules",
]

UDEV_GLOB_DIRS: Final[list[str]] = [
    "/etc/udev/rules.d",
    "/lib/udev/rules.d",
    "/usr/lib/udev/rules.d",
    "/run/udev/rules.d",
]

UDEV_GLOB_PATTERNS: Final[list[str]] = [
    "*amlogic*",
    "*1b8e*",
    "*aml*usb*",
    "*persistent-usb*",
]

# Handshake polling
HANDSHAKE_POLL_INTERVAL_S: Final[float] = 0.1
HANDSHAKE_DEFAULT_TIMEOUT_S: Final[int] = 120

# ─── Partition Map (from /proc/mtd on LX06) ────────────────────────────────────
#
# mtd0: bootloader  0x200000  (2 MB)
# mtd1: tpl         0x800000  (8 MB)
# mtd2: boot0       0x600000  (6 MB)    — Kernel A
# mtd3: boot1       0x600000  (6 MB)    — Kernel B
# mtd4: system0     0x2820000 (40.2 MB) — Rootfs A (squashfs, xz, 128KB blocks)
# mtd5: system1     0x2800000 (40 MB)   — Rootfs B (squashfs, xz, 128KB blocks)
# mtd6: data        0x13e0000 (20.8 MB) — Persistent data (UBIFS)

PARTITION_MAP: Final[dict[str, dict[str, object]]] = {
    "mtd0": {"label": "bootloader", "size": 0x200000},
    "mtd1": {"label": "tpl",        "size": 0x800000},
    "mtd2": {"label": "boot0",      "size": 0x600000},
    "mtd3": {"label": "boot1",      "size": 0x600000},
    "mtd4": {"label": "system0",    "size": 0x2820000},
    "mtd5": {"label": "system1",    "size": 0x2800000},
    "mtd6": {"label": "data",       "size": 0x13e0000},
}

# Ordered list of partitions for backup (MTD order)
BACKUP_ORDER: Final[list[str]] = [
    "mtd0", "mtd1", "mtd2", "mtd3", "mtd4", "mtd5", "mtd6",
]

# A/B slot pairs
AB_BOOT_SLOTS:   Final[tuple[str, str]] = ("boot0", "boot1")
AB_SYSTEM_SLOTS: Final[tuple[str, str]] = ("system0", "system1")

# Max squashfs image size for LX06 (system1 size — the smaller system partition)
LX06_MAX_SYSTEM_SIZE: Final[int] = 0x2800000  # 41,943,040 bytes (40 MB)

# Per-partition dump timeouts (seconds) — USB 2.0 is slow
PARTITION_TIMEOUTS: Final[dict[str, int]] = {
    "bootloader": 180,
    "tpl":        240,
    "boot0":      180,
    "boot1":      180,
    "system0":    900,   # 15 min — large squashfs
    "system1":    900,
    "data":       900,
}
DEFAULT_PARTITION_TIMEOUT: Final[int] = 300

# Per-partition flash timeouts (seconds)
FLASH_TIMEOUTS: Final[dict[str, int]] = {
    "boot0":      180,
    "boot1":      180,
    "system0":    600,   # 10 min
    "system1":    600,
}
DEFAULT_FLASH_TIMEOUT: Final[int] = 300

# SquashFS magic bytes
SQUASHFS_MAGIC_LE: Final[bytes] = b'hsqs'

# ─── aml-flash-tool ────────────────────────────────────────────────────────────

AML_FLASH_TOOL_REPO: Final[str] = "https://github.com/radxa/aml-flash-tool.git"
AML_FLASH_TOOL_DIR:  Final[str] = "aml-flash-tool"
UPDATE_EXE_RELPATH:  Final[str] = "tools/linux-x86/update"

# ─── Firmware Download ────────────────────────────────────────────────────────

FIRMWARE_RELEASES_URL: Final[str] = "https://api.github.com/repos/duhow/xiaoai-patch/releases/latest"
FIRMWARE_REPO_URL: Final[str] = "https://github.com/duhow/xiaoai-patch"
FIRMWARE_FILE_PATTERN: Final[str] = "mico_firmware_*_lx06.tar"

# Files expected in firmware tarball
FIRMWARE_BOOT_FILE: Final[str] = "boot.img"
FIRMWARE_SYSTEM_FILE: Final[str] = "root.squashfs"

# ─── Per-Distro Package Tables ────────────────────────────────────────────────

DISTRO_PACKAGES: Final[dict[str, dict[str, str | None]]] = {
    "debian": {
        "git":            "git",
        "libusb":         "libusb-0.1-4",
        "squashfs_tools": "squashfs-tools",
        "base_devel":     "build-essential",
    },
    "fedora": {
        "git":            "git",
        "libusb":         "libusb-compat-0.1",
        "squashfs_tools": "squashfs-tools",
        "base_devel":     "gcc make",
    },
    "arch": {
        "git":            "git",
        "libusb":         "libusb-compat",
        "squashfs_tools": "squashfs-tools",
        "base_devel":     "base-devel",
        "python3":        "python",
        "python3_venv":   None,  # venv built into python on Arch
    },
}

# ─── OS Detection ─────────────────────────────────────────────────────────────

OS_FAMILY_MAP: Final[dict[str, str]] = {
    "ubuntu":      "debian",
    "debian":      "debian",
    "raspbian":    "debian",
    "linuxmint":   "debian",
    "pop":         "debian",
    "elementary":  "debian",
    "kali":        "debian",
    "fedora":      "fedora",
    "rhel":        "fedora",
    "centos":       "fedora",
    "rocky":       "fedora",
    "almalinux":   "fedora",
    "arch":        "arch",
    "cachyos":     "arch",
    "manjaro":     "arch",
    "endeavouros": "arch",
    "garuda":      "arch",
    "artix":       "arch",
}

OS_LIKE_MAP: Final[dict[str, str]] = {
    "debian": "debian",
    "ubuntu": "debian",
    "arch":   "arch",
    "fedora": "fedora",
    "rhel":   "fedora",
}

# ─── Bootloader ───────────────────────────────────────────────────────────────

BOOTLOADER_BOOTDELAY: Final[int] = 15

# ─── Validation ───────────────────────────────────────────────────────────────

MIN_SQUASHFS_SIZE_BYTES: Final[int] = 1 * 1024 * 1024   # 1 MB
MIN_PARTITION_DUMP_RATIO: Final[float] = 0.5
