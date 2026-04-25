"""
Constants for LX06 Flash Tool.

Partition maps, USB identifiers, download URLs, and default configuration values.
All magic numbers and device-specific knowledge live here.
"""

from pathlib import Path
from typing import Final

# ── Application ─────────────────────────────────────────────────────────────

APP_NAME: Final = "LX06 Flash Tool"
APP_VERSION: Final = "0.1.0"
DEFAULT_CONFIG_DIR: Final = Path.home() / ".config" / "lx06-tool"
DEFAULT_CONFIG_FILE: Final = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_LOG_DIR: Final = Path.home() / ".local" / "share" / "lx06-tool" / "logs"

# ── LX06 Device Info ────────────────────────────────────────────────────────

DEVICE_MODEL: Final = "LX06"
DEVICE_NAME: Final = "Xiaoai Speaker Pro"
DEVICE_MANUFACTURER: Final = "Xiaomi"
DEVICE_SOC: Final = "Amlogic AXG"  # A113X

# ── Partition Map ───────────────────────────────────────────────────────────
# LX06 uses an MTD-based flash layout with A/B partitioning for OTA updates.
# System partitions (system0/system1) are squashfs; boot partitions are raw images.

PARTITION_MAP: Final[dict[str, dict[str, str | int]]] = {
    "mtd0": {"label": "bootloader", "size": 0x100000,   "desc": "U-Boot bootloader"},
    "mtd1": {"label": "tpl",        "size": 0x200000,   "desc": "TPL (Secondary Program Loader)"},
    "mtd2": {"label": "boot0",      "size": 0x800000,   "desc": "Boot partition A (kernel + dtb)"},
    "mtd3": {"label": "boot1",      "size": 0x800000,   "desc": "Boot partition B (kernel + dtb)"},
    "mtd4": {"label": "system0",    "size": 0x2000000,  "desc": "Root filesystem A (squashfs)"},
    "mtd5": {"label": "system1",    "size": 0x2000000,  "desc": "Root filesystem B (squashfs)"},
    "mtd6": {"label": "data",       "size": 0x800000,   "desc": "User data / overlay"},
}

# Total partitions to dump for full backup
PARTITION_ORDER: Final[list[str]] = ["mtd0", "mtd1", "mtd2", "mtd3", "mtd4", "mtd5", "mtd6"]

# A/B partition pairing
AB_PARTITION_PAIRS: Final[dict[str, str]] = {
    "boot0": "boot1",
    "boot1": "boot0",
    "system0": "system1",
    "system1": "system0",
}

# MTD index to label mapping
MTD_TO_LABEL: Final[dict[str, str]] = {
    mtd: info["label"] for mtd, info in PARTITION_MAP.items()
}

LABEL_TO_MTD: Final[dict[str, str]] = {
    info["label"]: mtd for mtd, info in PARTITION_MAP.items()
}

# ── USB Identifiers ─────────────────────────────────────────────────────────
# Amlogic USB vendor/product IDs for burning mode

AML_USB_VENDOR_ID: Final = "1b8e"
AML_USB_PRODUCT_ID: Final = "c003"

# udev rules content for automatic USB detection
UDEV_RULES_FILENAME: Final = "70-persistent-usb-ubuntu14.rules"
UDEV_RULES_CONTENT: Final = """# Amlogic USB Burning Mode - LX06 Flash Tool
SUBSYSTEM==\"usb\", ATTR{idVendor}==\"1b8e\", ATTR{idProduct}==\"c003\", MODE=\"0666\"
"""

# ── Handshake Timing ────────────────────────────────────────────────────────
# The LX06 bootloader enters USB burning mode for ~2 seconds after power-on
# when the appropriate test pads are shorted. We poll at this interval.

HANDSHAKE_POLL_INTERVAL_SEC: Final = 0.1    # 100ms
HANDSHAKE_TIMEOUT_SEC: Final = 120          # 2 minutes total timeout
HANDSHAKE_BOOTLOADER_WINDOW_SEC: Final = 2  # The bootloader's USB window duration

# ── Download URLs ───────────────────────────────────────────────────────────
# aml-flash-tool from Radxa (contains update.exe)

AML_FLASH_TOOL_REPO: Final = "https://github.com/radxa/aml-flash-tool"
AML_FLASH_TOOL_VERSION: Final = "master"  # Branch or tag

# Reference repositories
XIAOAI_PATCH_REPO: Final = "https://github.com/duhow/xiaoai-patch"
xiaogpt_REPO: Final = "https://github.com/yihong0618/xiaogpt"
OPEN_XIAOAI_REPO: Final = "https://github.com/idootop/open-xiaoai"

# ── Supported Package Managers ──────────────────────────────────────────────

SUPPORTED_PACKAGE_MANAGERS: Final[dict[str, dict[str, str]]] = {
    "apt": {
        "detect": ["/usr/bin/apt", "/usr/bin/apt-get"],
        "install_cmd": "apt-get install -y",
        "update_cmd": "apt-get update",
        "packages": {
            "libusb": "libusb-0.1-4",
            "git": "git",
            "squashfs_tools": "squashfs-tools",
            "docker": "docker.io",
        },
        "distros": ["debian", "ubuntu", "pop", "mint", "elementary", "kali"],
    },
    "dnf": {
        "detect": ["/usr/bin/dnf"],
        "install_cmd": "dnf install -y",
        "update_cmd": "dnf check-update",
        "packages": {
            "libusb": "libusb",
            "git": "git",
            "squashfs_tools": "squashfs-tools",
            "docker": "docker",
        },
        "distros": ["fedora", "rhel", "centos", "rocky", "alma"],
    },
    "pacman": {
        "detect": ["/usr/bin/pacman"],
        "install_cmd": "pacman -S --noconfirm",
        "update_cmd": "pacman -Sy",
        "packages": {
            "libusb": "libusb-compat",
            "git": "git",
            "squashfs_tools": "squashfs-tools",
            "docker": "docker",
        },
        "distros": ["arch", "manjaro", "endeavouros", "garuda"],
    },
}

# ── Docker Build ────────────────────────────────────────────────────────────

DOCKER_BUILD_IMAGE_NAME: Final = "lx06-firmware-builder"
DOCKER_BUILD_IMAGE_TAG: Final = "latest"
FIRMWARE_BUILDER_IMAGE: Final = f"{DOCKER_BUILD_IMAGE_NAME}:{DOCKER_BUILD_IMAGE_TAG}"
DOCKERFILE_PATH: Final = str(Path(__file__).parent.parent / "resources" / "docker" / "Dockerfile.firmware-builder")

# ── SquashFS Settings ───────────────────────────────────────────────────────
# Must match the original firmware's squashfs parameters for the LX06

SQUASHFS_COMPRESSION: Final = "xz"
SQUASHFS_BLOCK_SIZE: Final = 131072  # 128KB
SQUASHFS_XATTRS: Final = True
SQUASHFS_EXCLUDE: Final[list[str]] = []  # No exclusions by default

# ── Bootloader ──────────────────────────────────────────────────────────────

BOOTLOADER_BOOTDELAY: Final = 15  # Seconds to wait in U-boot console
BOOTLOADER_UNLOCK_COMMANDS: Final[list[str]] = [
    "setenv bootdelay 15",
    "saveenv",
]

# ── Xiaomi Debloat Patterns ─────────────────────────────────────────────────
# Known paths and binaries that can be safely removed from the LX06 rootfs

BLOAT_SERVICES: Final[list[str]] = [
    "miio", "tcu", "ota", "upgrade", "xiaoai", "mico",
    "stat_point", "data_collect", "smart_home",
]

BLOAT_BINARIES: Final[list[str]] = [
    "miio_client", "miio_report", "stat_point", "tcu_control",
    "ota_update", "upgrade_util", "data_collect",
]

BLOAT_CONFIGS: Final[list[str]] = [
    "miio.conf", "tcu.conf", "ota.conf", "xiaoai.conf",
    "data_collect.conf", "stat_point.conf",
]

BLOAT_DIRECTORIES: Final[list[str]] = [
    "var/cache/miio",
    "var/cache/ota",
    "var/lib/xiaomi",
]

OTA_PACKAGES: Final[list[str]] = [
    "ota_update", "upgrade_util", "smart_home_updater",
]

XIAOMI_BLOAT_PATTERNS: Final[dict[str, dict[str, list[str]]]] = {
    "telemetry": {
        "paths": [
            "/usr/bin/miio_client",
            "/usr/bin/miio_report",
            "/usr/bin/stat_point",
            "/usr/bin/tcu_control",
            "/etc/init.d/miio",
            "/etc/init.d/tcu",
        ],
        "description": "Xiaomi telemetry and reporting services",
    },
    "updater": {
        "paths": [
            "/usr/bin/ota_update",
            "/usr/bin/upgrade_util",
            "/etc/init.d/ota",
            "/etc/init.d/upgrade",
        ],
        "description": "Over-the-air update services",
    },
    "voice_engine": {
        "paths": [
            "/usr/bin/mico_ai_proxy",
            "/usr/bin/mico_credential",
            "/usr/bin/mico_sound_capture",
            "/usr/bin/xiaoai_*",
            "/etc/init.d/mico",
            "/etc/init.d/xiaoai",
        ],
        "description": "Xiaomi Xiaoai voice assistant (removing disables stock wake-word)",
    },
}

# ── Media Suite Binaries ────────────────────────────────────────────────────
# Pre-compiled ARM64 binary names and their download sources

MEDIA_BINARIES: Final[dict[str, dict[str, str]]] = {
    "shairport-sync": {
        "binary": "shairport-sync",
        "config_template": "shairport-sync.conf.j2",
        "init_script": "S90shairport-sync",
        "description": "AirPlay audio receiver",
    },
    "upmpdcli": {
        "binary": "upmpdcli",
        "config_template": "upmpdcli.conf.j2",
        "init_script": "S91upmpdcli",
        "description": "UPnP/DLNA renderer",
    },
    "mpd": {
        "binary": "mpd",
        "config_template": "mpd.conf.j2",
        "init_script": "S89mpd",
        "description": "Music Player Daemon",
    },
    "snapcast-client": {
        "binary": "snapclient",
        "config_template": "snapcast.conf.j2",
        "init_script": "S92snapclient",
        "description": "Snapcast multi-room audio client",
    },
    "librespot": {
        "binary": "librespot",
        "config_template": "librespot.toml.j2",
        "init_script": "S93librespot",
        "description": "Spotify Connect receiver",
    },
}

# ── AI Brain Settings ───────────────────────────────────────────────────────

SUPPORTED_LLM_PROVIDERS: Final[dict[str, dict[str, str]]] = {
    "openai": {
        "name": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "gemini": {
        "name": "Google Gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.0-flash",
    },
    "kimi": {
        "name": "Moonshot Kimi",
        "api_base": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
    },
}

# ── AI Integration Paths (on-device) ───────────────────────────────────────

XIAOGPT_INSTALL_PATH: Final = "/opt/xiaogpt"
XIAOGPT_CONFIG_PATH: Final = "/opt/xiaogpt/xiao_config.yaml"
XIAOGPT_INIT_SCRIPT: Final = "S80xiaogpt"

OPEN_XIAOAI_BINARY_NAME: Final = "open-xiaoai"
OPEN_XIAOAI_INSTALL_PATH: Final = "/usr/bin/open-xiaoai"
OPEN_XIAOAI_CONFIG_PATH: Final = "/etc/open-xiaoai/config.toml"
OPEN_XIAOAI_INIT_SCRIPT: Final = "S80open-xiaoai"

# ── Checksum ────────────────────────────────────────────────────────────────

CHECKSUM_BUFFER_SIZE: Final = 65536  # 64KB read buffer for hashing

# ── Logging ─────────────────────────────────────────────────────────────────

LOG_FORMAT: Final = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
LOG_MAX_FILES: Final = 5
LOG_MAX_SIZE_MB: Final = 10
