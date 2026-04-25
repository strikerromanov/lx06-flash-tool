# LX06 Flash Tool — Architecture Document

## 1. Overview

A Python TUI application (Textual framework) that transforms the Xiaomi LX06 (Xiaoai Speaker Pro)
into a custom smart speaker with universal casting, Spotify, and LLM-powered voice intelligence.

Integrates logic from:
- **`duhow/xiaoai-patch`** — SquashFS patching, Docker firmware builds, Amlogic `update.exe` flashing
- **`yihong0618/xiaogpt`** — Soft AI hijack via Xiaoai's NLP intercept
- **`idootop/open-xiaoai`** — Hard AI hijack via Rust client (custom wake-word, full mic/speaker control)

---

## 2. Technology Stack

| Layer          | Choice                    | Rationale                                    |
|----------------|---------------------------|----------------------------------------------|
| Language       | Python 3.10+              | Cross-distro, rich ecosystem                 |
| TUI Framework  | Textual                   | Async-native, widgets, CSS styling, mature   |
| Progress       | Rich (via Textual)        | Live progress bars, trees, tables            |
| Subprocess     | `asyncio.create_subprocess_exec` | Non-blocking shell commands          |
| Config         | YAML (PyYAML) + TOML      | Human-readable settings                      |
| Checksums      | `hashlib` (stdlib)        | SHA256/MD5 verification                      |
| Docker SDK     | `docker` (Python SDK)     | Containerized firmware builds                 |
| squashfs       | `unsquashfs` / `mksquashfs` via subprocess | Firmware manipulation              |
| State Machine  | Custom enum + transition guard | Explicit, debuggable flow              |

---

## 3. Folder Structure

```
lx06-flash-tool/
├── pyproject.toml                    # Project metadata, dependencies (pip/uv)
├── README.md                         # User-facing documentation
├── ARCHITECTURE.md                   # This file
├── Makefile                          # Convenience targets (install, run, dev)
│
├── lx06_tool/                        # Main Python package
│   ├── __init__.py                   # Version, app metadata
│   ├── app.py                        # Textual Application class, screen routing
│   ├── state.py                      # State machine: enum states, transitions, guards
│   ├── config.py                     # Config loading/saving, paths, user preferences
│   ├── constants.py                  # Partition maps, USB IDs, URLs, defaults
│   ├── exceptions.py                 # Custom exception hierarchy
│   │
│   ├── modules/                      # Core business logic (one module per concern)
│   │   ├── __init__.py
│   │   ├── environment.py            # Phase 1: OS detect, pkg manager, dep install
│   │   ├── usb_scanner.py            # Phase 1: udev rules, handshake loop
│   │   ├── backup.py                 # Phase 2: Partition dump + checksum verify
│   │   ├── bootloader.py             # Phase 2: U-boot unlock (bootdelay, saveenv)
│   │   ├── firmware.py               # Phase 3: SquashFS extract/modify/repack orchestrator
│   │   ├── debloat.py                # Phase 3: Remove Xiaomi telemetry/updaters/voice
│   │   ├── media_suite.py            # Phase 3: AirPlay/DLNA/Spotify/Snapcast injection
│   │   ├── ai_brain.py               # Phase 3: xiaogpt (soft) or open-xiaoai (hard) setup
│   │   ├── flasher.py                # Phase 4: A/B detection, flash commands, progress
│   │   └── docker_builder.py         # Phase 3: Docker-based safe firmware build
│   │
│   ├── ui/                           # Textual UI layer
│   │   ├── __init__.py
│   │   ├── styles.tcss               # Textual CSS (colors, layout)
│   │   ├── screens/                  # One screen per major workflow step
│   │   │   ├── __init__.py
│   │   │   ├── welcome.py            # Welcome + device info
│   │   │   ├── environment.py        # Dependency install progress
│   │   │   ├── usb_connect.py        # "Plug in your speaker" + handshake animation
│   │   │   ├── backup.py             # Backup progress + checksum results
│   │   │   ├── customize.py          # Interactive feature selection (checkbox tree)
│   │   │   ├── build.py              # Firmware build/repack progress
│   │   │   └── flash.py              # Flash progress bar + final status
│   │   └── widgets/                  # Reusable UI components
│   │       ├── __init__.py
│   │       ├── progress_log.py       # Combined progress bar + scrolling log
│   │       ├── feature_tree.py       # Checkbox tree for customization menu
│   │       ├── partition_info.py     # Partition map display widget
│   │       └── status_bar.py         # Persistent bottom status bar
│   │
│   └── utils/                        # Shared utilities
│       ├── __init__.py
│       ├── logger.py                 # Structured logging (file + console)
│       ├── runner.py                 # Async subprocess runner with output capture
│       ├── checksum.py               # SHA256/MD5 file verification
│       ├── amlogic.py                # `update.exe` CLI wrapper class
│       ├── squashfs.py               # unsquashfs/mksquashfs wrapper
│       ├── docker_utils.py           # Docker build/run helpers
│       └── downloader.py             # Resumable file download with progress
│
├── resources/                        # Static assets bundled with the tool
│   ├── udev/
│   │   └── 70-persistent-usb-ubuntu14.rules
│   ├── docker/
│   │   └── Dockerfile.firmware-builder   # Isolated build env for squashfs mods
│   ├── configs/                      # Template configs injected into rootfs
│   │   ├── shairport-sync.conf.j2
│   │   ├── mpd.conf.j2
│   │   ├── upmpdcli.conf.j2
│   │   ├── snapcast.conf.j2
│   │   ├── xiao_config.yaml.j2
│   │   └── librespot.toml.j2
│   └── scripts/                      # Helper scripts run inside Docker or on device
│       ├── unlock_bootloader.sh
│       └── dump_partitions.sh
│
├── backups/                          # Default backup storage (gitignored)
│   └── .gitkeep
│
└── build/                            # Working directory for firmware mods (gitignored)
    └── .gitkeep
```

---

## 4. State Machine

### 4.1 States

```python
from enum import Enum, auto

class AppState(Enum):
    # Initialization
    WELCOME              = auto()  # Show welcome screen, device info
    CHECK_ENV            = auto()  # Detect OS, package manager
    INSTALL_DEPS         = auto()  # Install required host dependencies
    SETUP_UDEV           = auto()  # Inject udev rules, reload
    DOWNLOAD_TOOLS       = auto()  # Download aml-flash-tool from Radxa

    # USB Connection
    WAIT_USB             = auto()  # Instruct user to plug in, start handshake loop
    DEVICE_IDENTIFIED    = auto()  # Handshake succeeded, device info captured

    # Backup & Safety
    UNLOCK_BOOTLOADER    = auto()  # Set bootdelay=15, saveenv
    DUMP_PARTITIONS      = auto()  # mread mtd0..mtd6
    VERIFY_BACKUP        = auto()  # Checksum verification of all dumps

    # Firmware Customization
    EXTRACT_FIRMWARE     = auto()  # unsquashfs the inactive system partition
    CUSTOMIZE_MENU       = auto()  # Interactive feature selection screen
    APPLY_CUSTOMIZATIONS = auto()  # Execute selected modifications
    BUILD_FIRMWARE       = auto()  # mksquashfs (or Docker build) modified rootfs

    # Flashing
    DETECT_AB_PARTITION  = auto()  # Identify active vs inactive partition
    FLASH_BOOTLOADER     = auto()  # Flash boot.img to inactive boot partition
    FLASH_SYSTEM         = auto()  # Flash root.squashfs to inactive system partition

    # Completion
    VERIFY_FLASH         = auto()  # Post-flash verification
    COMPLETE             = auto()  # Success screen, next steps
    ERROR                = auto()  # Error handler (with recovery suggestions)
```

### 4.2 State Transition Diagram

```
WELCOME
  │
  ▼
CHECK_ENV ──────► INSTALL_DEPS ──────► SETUP_UDEV ──────► DOWNLOAD_TOOLS
  │                   │                     │                    │
  │ (deps ok)         │                     │                    │
  └───────────────────┴─────────────────────┴────────────────────┘
                                              │
                                              ▼
                                        WAIT_USB ◄──────────────────┐
                                              │                       │
                                              ▼                       │
                                    DEVICE_IDENTIFIED                  │
                                              │                       │
                                              ▼                       │
                                     UNLOCK_BOOTLOADER                 │
                                              │                       │
                                              ▼                       │
                                      DUMP_PARTITIONS                  │
                                              │                       │
                                              ▼                       │
                                      VERIFY_BACKUP                    │
                                              │                       │
                                              ▼                       │
                                     EXTRACT_FIRMWARE                  │
                                              │                       │
                                              ▼                       │
                                      CUSTOMIZE_MENU                   │
                                              │                       │
                                              ▼                       │
                                   APPLY_CUSTOMIZATIONS                │
                                              │                       │
                                              ▼                       │
                                      BUILD_FIRMWARE                   │
                                              │                       │
                                              ▼                       │
                                   DETECT_AB_PARTITION                 │
                                              │                       │
                                    ┌─────────┴──────────┐            │
                                    ▼                    ▼            │
                              FLASH_BOOTLOADER   FLASH_SYSTEM          │
                                    │                    │            │
                                    └─────────┬──────────┘            │
                                              ▼                       │
                                        VERIFY_FLASH                   │
                                              │                       │
                                              ▼                       │
                                           COMPLETE                    │

Any state ──► ERROR ──► (recover to last safe state)
```

### 4.3 Transition Guards

Each transition has a guard function that validates preconditions:

| Transition | Guard |
|---|---|
| `INSTALL_DEPS → SETUP_UDEV` | All packages installed successfully |
| `SETUP_UDEV → DOWNLOAD_TOOLS` | udev rules file exists, reload succeeded |
| `WAIT_USB → DEVICE_IDENTIFIED` | `update.exe identify` returned valid device info |
| `VERIFY_BACKUP → EXTRACT_FIRMWARE` | All partition dumps exist + checksums match |
| `BUILD_FIRMWARE → DETECT_AB_PARTITION` | Output `.squashfs` file exists + size > 0 |
| `FLASH_SYSTEM → VERIFY_FLASH` | `update.exe` returned success code |

---

## 5. Data Models

### 5.1 Device State

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class LX06Device:
    """Represents the connected LX06 device and its partition state."""
    connected: bool = False
    serial: str = ""
    chip_id: str = ""
    active_boot: str = ""       # "boot0" or "boot1"
    inactive_boot: str = ""     # Complement of active_boot
    active_system: str = ""     # "system0" or "system1"
    inactive_system: str = ""   # Complement of active_system
    bootloader_unlocked: bool = False

@dataclass
class PartitionBackup:
    """Tracks a single partition backup."""
    name: str                  # e.g. "mtd0" (bootloader)
    label: str                 # e.g. "bootloader"
    size_bytes: int = 0
    expected_size: int = 0     # From partition table
    sha256: str = ""
    md5: str = ""
    path: Optional[Path] = None
    verified: bool = False

@dataclass
class BackupSet:
    """Complete set of partition backups."""
    partitions: dict[str, PartitionBackup] = field(default_factory=dict)
    timestamp: str = ""
    all_verified: bool = False

# Partition map for LX06 (Amlogic AXG platform)
PARTITION_MAP = {
    "mtd0": {"label": "bootloader", "size": 0x100000},   # 1MB
    "mtd1": {"label": "tpl",        "size": 0x200000},   # 2MB
    "mtd2": {"label": "boot0",      "size": 0x800000},   # 8MB
    "mtd3": {"label": "boot1",      "size": 0x800000},   # 8MB
    "mtd4": {"label": "system0",    "size": 0x2000000},  # 32MB
    "mtd5": {"label": "system1",    "size": 0x2000000},  # 32MB
    "mtd6": {"label": "data",       "size": 0x800000},   # 8MB
}
```

### 5.2 Customization Selection

```python
@dataclass
class CustomizationChoices:
    """User's feature selections for firmware modification."""
    # Debloat
    remove_telemetry: bool = True
    remove_auto_updater: bool = True
    remove_xiaoai_voice: bool = False  # Default keep, required for soft-patch AI

    # Media Players
    install_airplay: bool = False       # shairport-sync
    install_dlna: bool = False          # upmpdcli + mpd
    install_snapcast: bool = False      # squeezelite + snapcast
    install_spotify: bool = False       # librespot/raspotify

    # AI Brain
    ai_mode: str = "none"               # "none", "soft" (xiaogpt), "hard" (open-xiaoai)
    llm_provider: str = ""              # "openai", "gemini", "kimi"
    llm_api_key: str = ""
    llm_model: str = ""
    custom_wake_word: str = ""           # For hard-patch mode
    ai_server_url: str = ""             # For hard-patch mode
```

### 5.3 Application Config

```python
@dataclass
class AppConfig:
    """Persistent application configuration."""
    # Paths
    backup_dir: Path = Path("./backups")
    build_dir: Path = Path("./build")
    tools_dir: Path = Path("./tools")

    # Tools
    aml_flash_tool_path: Optional[Path] = None
    update_exe_path: Optional[Path] = None

    # Docker
    use_docker_build: bool = True       # Use Docker for squashfs operations

    # Network (for downloads)
    proxy: str = ""

    # Device (populated at runtime)
    device: LX06Device = field(default_factory=LX06Device)
    backup: BackupSet = field(default_factory=BackupSet)
    choices: CustomizationChoices = field(default_factory=CustomizationChoices)
```

---

## 6. Module Responsibilities

### 6.1 `modules/environment.py` — Host Environment Setup

```
Responsibilities:
  - detect_os()          → Returns OS name, version, package manager (apt/dnf/pacman)
  - detect_pkg_manager() → Returns PM name + install command template
  - check_dependencies() → Checks if git, squashfs-tools, docker, libusb are installed
  - install_dependencies(pkg_mgr) → Runs appropriate install commands
  - verify_docker()      → Checks Docker daemon is running + user has permissions
```

### 6.2 `modules/usb_scanner.py` — USB Detection & Handshake

```
Responsibilities:
  - install_udev_rules() → Copy rules file, reload udev
  - handshake_loop()     → Async loop running 'update.exe identify' every 100ms
  - parse_device_info()  → Parse identify output for serial, chip info
  - wait_for_disconnect() → Detect USB removal for phase transitions
```

**Handshake Loop Design:**
```python
async def handshake_loop(update_exe: Path, timeout: int = 120) -> DeviceInfo:
    """
    Loop update.exe identify every 100ms to catch the 2-second
    AmlUsbIdentifyHost bootloader window.
    
    The LX06 enters USB burning mode for ~2 seconds after power-on
    with the test pads shorted. We must identify during this window.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        result = await run_command(update_exe, "identify")
        if result.returncode == 0:
            return parse_identify_output(result.stdout)
        await asyncio.sleep(0.1)  # 100ms poll interval
    raise HandshakeTimeoutError("Device not detected within timeout")
```

### 6.3 `modules/backup.py` — Partition Backup Engine

```
Responsibilities:
  - dump_partition(mtd_name) → Run 'update.exe mread mtdX' to file
  - dump_all_partitions()    → Dump mtd0 through mtd6 sequentially
  - compute_checksums()      → SHA256 + MD5 for each dump file
  - verify_backup()          → Compare checksums + verify file sizes
  - generate_backup_report() → Human-readable backup manifest
```

### 6.4 `modules/bootloader.py` — U-Boot Unlock

```
Responsibilities:
  - unlock_bootloader()     → setenv bootdelay 15 + saveenv
  - verify_bootloader()     → Check bootdelay is set
  - recovery_instructions() → Print recovery steps if bricked
```

### 6.5 `modules/firmware.py` — Firmware Orchestrator

```
Responsibilities:
  - extract_squashfs(image_path, output_dir) → unsquashfs
  - modify_rootfs(rootfs_dir, choices)        → Route to sub-modules
  - repack_squashfs(rootfs_dir, output_path)  → mksquashfs
  - build_via_docker(rootfs_dir, output_path) → Docker-based safe build
```

### 6.6 `modules/debloat.py` — Xiaomi Bloatware Removal

```
Responsibilities:
  - remove_telemetry(rootfs)   → Delete telemetry binaries + configs
  - remove_updater(rootfs)     → Disable OTA update services
  - remove_xiaoai(rootfs)      → Remove voice engine (breaks soft AI mode)
  - list_removable_items()     → Scan rootfs for known bloat patterns
```

### 6.7 `modules/media_suite.py` — Media Player Injection

```
Responsibilities:
  - install_airplay(rootfs)    → shairport-sync binary + config
  - install_dlna(rootfs)       → upmpdcli + mpd binaries + configs
  - install_snapcast(rootfs)   → squeezelite + snapclient binaries
  - install_spotify(rootfs)    → librespot binary + config
  - generate_init_scripts()    → Create init.d/systemd service files
  - cross_compile_arm64()      → Build ARM64 binaries if prebuilts unavailable
```

**Binary Strategy:** Pre-compiled ARM64 binaries bundled in `resources/binaries/`.
If unavailable, fall back to Docker-based cross-compilation using `arm64v8/ubuntu` base.

### 6.8 `modules/ai_brain.py` — AI Integration

```
Responsibilities:
  - install_soft_ai(rootfs, config)    → xiaogpt pip deps + config injection
  - install_hard_ai(rootfs, config)    → open-xiaoai Rust binary + service
  - generate_xiao_config(api_key, ...) → Write xiao_config.yaml from template
  - generate_ai_service()              → Create systemd service for AI client
  - validate_api_keys()                → Test API key validity before building
```

### 6.9 `modules/flasher.py` — Flashing Engine

```
Responsibilities:
  - detect_active_partition() → Read current boot slot from device
  - flash_partition(name, file) → Run 'update.exe partition X file'
  - flash_with_progress()      → Parse update.exe output for progress %
  - verify_flash()             → Compare flashed size to expected
```

**A/B Detection Logic:**
```python
def detect_active_partition(device: LX06Device) -> tuple[str, str]:
    """
    The LX06 uses A/B partitioning. We must flash to the INACTIVE slot.
    
    Strategy:
    1. Read boot_part from U-boot env (if accessible)
    2. Or: flash system0 first, if it fails try system1
    3. Or: use the partition that was NOT dumped as the currently booted one
    
    Returns (inactive_boot, inactive_system) partition names.
    """
```

### 6.10 `modules/docker_builder.py` — Docker Build Safety

```
Responsibilities:
  - ensure_build_image()    → Build/pull firmware builder Docker image
  - run_in_container(cmd)   → Execute squashfs operations inside container
  - copy_artifacts()        → Extract built firmware from container
  - cleanup()               → Remove build containers
```

---

## 7. Error Handling Strategy

### 7.1 Exception Hierarchy

```python
class LX06Error(Exception):
    """Base exception for all tool errors."""

class EnvironmentError(LX06Error):
    """Host environment issues (missing deps, wrong OS)."""

class USBError(LX06Error):
    """USB communication failures."""

class HandshakeTimeoutError(USBError):
    """Device not detected in bootloader window."""

class BackupError(LX06Error):
    """Backup verification or dump failures."""

class ChecksumMismatchError(BackupError):
    """Dump file checksum doesn't match expected."""

class FirmwareError(LX06Error):
    """SquashFS manipulation failures."""

class FlashError(LX06Error):
    """Flashing operation failures."""

class DockerBuildError(LX06Error):
    """Docker-based build failures."""
```

### 7.2 Recovery Points

The state machine tracks **recovery points** — safe states to roll back to on error:

| Current State | Recovery Point | Action |
|---|---|---|
| Any USB operation | `WAIT_USB` | Re-plug device, restart handshake |
| Any backup operation | `DEVICE_IDENTIFIED` | Retry backup, check USB connection |
| Firmware modification | `EXTRACT_FIRMWARE` | Re-extract clean firmware, re-modify |
| Flashing | `DETECT_AB_PARTITION` | Re-attempt flash (inactive partition unchanged) |

---

## 8. Concurrency Model

The app is **single-threaded, async** using `asyncio`:

- Textual runs its own async event loop
- All subprocess calls use `asyncio.create_subprocess_exec`
- The USB handshake loop runs as an async background task
- Progress updates are pushed to the UI via Textual's message system
- No threading, no multiprocessing (simplicity + avoids GIL issues)

---

## 9. Configuration Management

```yaml
# ~/.config/lx06-tool/config.yaml
paths:
  backup_dir: "./backups"
  build_dir: "./build"
  tools_dir: "./tools"

docker:
  use_docker_build: true
  build_image: "lx06-firmware-builder:latest"

network:
  proxy: ""
  github_mirror: ""        # For users behind GFW

defaults:
  ai_mode: "none"
  llm_provider: "openai"
```

---

## 10. Build & Run

```bash
# Install
pip install -e .        # or: uv pip install -e .

# Run
lx06-tool               # Launches TUI

# Run without TUI (CI/automation)
lx06-tool --headless --config preset.yaml

# Specific commands
lx06-tool check-env     # Just check host environment
lx06-tool backup        # Just do backup
lx06-tool customize     # Just open customizer
lx06-tool flash         # Just flash pre-built firmware
```

---

## 11. Dependencies (`pyproject.toml`)

```toml
[project]
name = "lx06-flash-tool"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "textual>=0.40.0",
    "rich>=13.0.0",
    "pyyaml>=6.0",
    "docker>=6.0",
    "jinja2>=3.1",          # Template rendering for configs
    "httpx>=0.24",          # Downloads + API key validation
    "aiofiles>=23.0",       # Async file operations
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
    "textual-dev>=0.20",    # `textual run` dev server
    "mypy>=1.0",
    "ruff>=0.1",
]
```

---

## 12. Implementation Order (Iterative)

| Step | Module | Depends On | Estimated Size |
|------|--------|------------|----------------|
| 1 | Architecture (this doc) | — | — |
| 2 | `utils/` (runner, logger, checksum, downloader) | — | ~400 lines |
| 3 | `state.py` + `config.py` + `constants.py` | — | ~200 lines |
| 4 | `modules/environment.py` + `modules/usb_scanner.py` | utils, state | ~350 lines |
| 5 | `ui/screens/welcome.py` + `ui/screens/environment.py` + `ui/screens/usb_connect.py` | modules | ~400 lines |
| 6 | `modules/bootloader.py` + `modules/backup.py` | utils, amlogic | ~300 lines |
| 7 | `ui/screens/backup.py` | backup module | ~200 lines |
| 8 | `modules/firmware.py` + `modules/debloat.py` | utils, squashfs | ~400 lines |
| 9 | `modules/media_suite.py` + `modules/ai_brain.py` | firmware module | ~500 lines |
| 10 | `ui/screens/customize.py` + `ui/screens/build.py` | modules | ~400 lines |
| 11 | `modules/flasher.py` | amlogic, utils | ~250 lines |
| 12 | `ui/screens/flash.py` + final integration | all modules | ~300 lines |
| **Total** | | | **~3,700 lines** |

---

## 13. Key Design Decisions

1. **Textual over Rich alone**: Textual gives us full TUI app structure (screens, navigation, focus management) while Rich only provides display primitives.

2. **Docker for firmware builds**: SquashFS operations can fail with permission issues on different host kernels. Docker ensures a consistent build environment (matching the approach in `xiaoai-patch`).

3. **Async throughout**: The USB handshake loop must poll at 100ms intervals. Blocking subprocess calls would freeze the UI. `asyncio` solves both.

4. **Pre-compiled ARM64 binaries**: Cross-compiling on the host is fragile. Bundling pre-built binaries for shairport-sync, librespot, etc. is more reliable. Docker cross-compilation is the fallback.

5. **Jinja2 templates for configs**: Each media/AI service needs config files with user-specific values (API keys, server URLs). Jinja2 templates keep this clean and testable.

6. **State machine with guards**: Every state transition is validated. If something fails, the user is guided back to the last safe state — never a dead end.

7. **Separation of UI and logic**: All business logic lives in `modules/`. UI screens call modules and display results. This makes the logic testable without a TUI.

---

## 14. Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Bricked device | Mandatory backup + bootloader unlock before any flash. Recovery via bootdelay U-boot console. |
| Wrong partition flashed | A/B detection + confirmation screen showing active vs inactive. Never flash to active partition. |
| Permission errors during build | Docker isolation for squashfs operations. |
| Missing ARM64 binaries | Pre-compiled set + Docker cross-compile fallback. |
| USB handshake missed | 100ms poll loop with 120s timeout. User can retry without restart. |
| API key validation | Test keys against provider APIs before building firmware. |
