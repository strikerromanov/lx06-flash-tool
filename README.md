# LX06 Flash Tool

**Xiaomi Xiaoai Speaker Pro (LX06) — Custom Firmware Installer**

A complete, all-in-one Textual TUI application to transform your Xiaomi LX06 into a fully custom, bloatware-free smart speaker featuring universal cast capabilities, Spotify playback, and LLM-powered voice intelligence.

## Features

### Phase 1: Cross-Linux Environment Setup
- Auto-detects host OS package manager (`apt`, `dnf`, `pacman`)
- Automatically installs dependencies (libusb, git, squashfs-tools, Docker)
- Downloads `aml-flash-tool` from Radxa
- Injects udev rules and triggers USB scan detection (no reboot)
- **Handshake Loop**: Catches the 2-second Amlogic USB bootloader window automatically

### Phase 2: Automated Backup & Safety
- Unlocks U-boot bootloader (`bootdelay=15`) for brick recovery
- Dumps all 7 MTD partitions (bootloader, tpl, boot0/1, system0/1, data)
- Verifies backup integrity with SHA256 + MD5 checksums

### Phase 3: A-La-Carte Firmware Customizer
Interactive menu to select what to remove and install:

**Debloat:**
- Remove Xiaomi telemetry, OTA auto-updater
- Optionally remove stock Xiaoai voice engine

**Media Player Suite:**
- AirPlay (Shairport-Sync)
- DLNA/UPnP (Upmpdcli/MPD)
- Spotify Connect (librespot)
- Multi-room Audio (Snapcast/Squeezelite)

**AI Brain Selection:**
- **Soft Patch** (xiaogpt): Keep Xiaomi wake word, route thinking to OpenAI/Gemini/Kimi
- **Hard Patch** (open-xiaoai): Custom local wake word, fully bypass Xiaomi servers

### Phase 4: Flashing & Deployment
- Automatic A/B partition detection (targets inactive partition)
- Real-time progress tracking during firmware upload
- Post-flash verification
- Rollback support on failure

## Architecture

```
lx06_tool/
├── app.py                    # Main Textual application + screen routing
├── config.py                 # Data models (AppConfig, LX06Device, Choices)
├── constants.py              # Partition maps, USB IDs, URLs
├── exceptions.py             # 22-class exception hierarchy
├── state.py                  # State machine with guards + recovery
├── modules/
│   ├── environment.py        # OS detection, dependency installation
│   ├── usb_scanner.py        # udev rules, USB handshake orchestration
│   ├── bootloader.py         # U-boot unlock + verification
│   ├── backup.py             # MTD partition dump + checksum verification
│   ├── firmware.py           # SquashFS extract/modify/repack pipeline
│   ├── debloat.py            # Telemetry/OTA/Xiaoai removal engine
│   ├── media_suite.py        # AirPlay/DLNA/Spotify/Snapcast injection
│   ├── ai_brain.py           # Soft-patch (xiaogpt) + Hard-patch (open-xiaoai)
│   ├── docker_builder.py     # Containerized firmware builds
│   └── flasher.py            # A/B detection, flash, verify, rollback
├── utils/
│   ├── runner.py             # Async subprocess execution engine
│   ├── logger.py             # Rich-aware structured logging
│   ├── checksum.py           # SHA256/MD5 hashing + verification
│   ├── downloader.py         # Async HTTP downloader with resume
│   ├── amlogic.py            # update.exe CLI wrapper
│   ├── squashfs.py           # unsquashfs/mksquashfs wrapper
│   └── docker_utils.py       # Docker image build/run
└── ui/screens/
    ├── welcome.py            # Intro + safety warnings
    ├── environment.py        # Dependency check + install
    ├── usb_connect.py        # USB handshake instructions
    ├── backup.py             # Bootloader unlock + partition dump
    ├── customize.py          # Feature selection (debloat/media/AI)
    ├── build.py              # Firmware build pipeline
    ├── flash.py              # Flash with progress tracking
    └── complete.py           # Success/failure summary
```

## Installation

### Quick Install (Ubuntu/Debian/Fedora)

```bash
git clone https://github.com/strikerromanov/lx06-flash-tool.git
cd lx06-flash-tool
pip install -e .
lx06
```

### Arch Linux (and other PEP 668 systems)

Arch Linux uses externally-managed Python. Use a virtual environment:

**Bash / Zsh:**
```bash
git clone https://github.com/strikerromanov/lx06-flash-tool.git
cd lx06-flash-tool
python -m venv venv
source venv/bin/activate
pip install -e .
lx06
```

**Fish shell:**
```fish
git clone https://github.com/strikerromanov/lx06-flash-tool.git
cd lx06-flash-tool
python -m venv venv
source venv/bin/activate.fish
pip install -e .
lx06
```

### One-liners

**Bash/Zsh:**
```bash
git clone https://github.com/strikerromanov/lx06-flash-tool.git && cd lx06-flash-tool && python -m venv venv && source venv/bin/activate && pip install -e . && lx06
```

**Fish:**
```fish
git clone https://github.com/strikerromanov/lx06-flash-tool.git; and cd lx06-flash-tool; and python -m venv venv; and source venv/bin/activate.fish; and pip install -e .; and lx06
```

## Requirements

- Linux host (Ubuntu/Debian, Fedora, or Arch)
- Python 3.10+
- USB-A to USB-A cable (or USB-A to micro-USB with adapter)
- Xiaomi LX06 (Xiaoai Speaker Pro)
- sudo access for system-level operations

## Safety Features

- **A/B Partitioning**: Always flashes inactive partition; active remains intact
- **Bootloader Unlock**: Sets `bootdelay=15` for serial recovery access
- **Full Backup**: All 7 MTD partitions dumped with dual checksum verification
- **Rollback**: Restore original firmware if custom build fails
- **Active Partition Guard**: Prevents flashing to the active partition

## References

This tool integrates logic from:
- [duhow/xiaoai-patch](https://github.com/duhow/xiaoai-patch) — SquashFS patching, Docker-based firmware building
- [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt) — Xiaoai NLP interception + LLM injection
- [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai) — Rust-based microphone/speaker hijack

## License

MIT
