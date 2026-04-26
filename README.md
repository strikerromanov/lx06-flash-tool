# LX06 Flash Tool

All-in-one TUI tool to flash custom firmware (xiaoai-patch) onto the Xiaomi LX06 smart speaker.

## What It Does

1. **Environment Setup** — Detects your Linux distro, installs required packages, downloads aml-flash-tool, configures udev rules
2. **USB Connection** — Guides you through entering USB burning mode, detects the device
3. **Backup** — Dumps all 7 NAND partitions for safety
4. **Download** — Fetches the latest pre-built firmware from [xiaoai-patch releases](https://github.com/duhow/xiaoai-patch/releases)
5. **Flash** — Writes boot + system images to both A/B slots

## Supported Distros

| Family | Distros | Package Manager |
|--------|---------|---------------|
| Debian | Ubuntu, Debian, Linux Mint, Pop!_OS, Kali | apt |
| Fedora | Fedora, RHEL, Rocky, Alma | dnf |
| Arch | CachyOS, Arch Linux, Manjaro, EndeavourOS | pacman |

## Prerequisites

- **Linux** (amd64)
- **Python 3.10+**
- **USB port** + micro USB cable
- **sudo** access
- **git**

## Installation

```bash
git clone https://github.com/your-repo/lx06-flash-tool.git
cd lx06-flash-tool
python -m venv venv
source venv/bin/activate
pip install -e .
lx06-tool
```

## Usage

```bash
lx06-tool
```

The TUI guides you through 5 screens:

1. **Welcome** — Prerequisite checks
2. **Environment Setup** — Install packages, download tools
3. **USB Connection** — Connect speaker, enter burning mode
4. **Backup & Flash** — Backup partitions, download firmware, flash
5. **Complete** — Success summary

## Partition Table (LX06)

| MTD | Label | Size | Description |
|-----|-------|------|-------------|
| mtd0 | bootloader | 2 MB | U-Boot bootloader |
| mtd1 | tpl | 8 MB | U-Boot second stage |
| mtd2 | boot0 | 6 MB | Kernel A |
| mtd3 | boot1 | 6 MB | Kernel B |
| mtd4 | system0 | 40.2 MB | Rootfs A (squashfs) |
| mtd5 | system1 | 40 MB | Rootfs B (squashfs) |
| mtd6 | data | 20.8 MB | Persistent data |

## Flash Commands (Reference)

```bash
# Identify device
update identify

# Set boot delay (safety recovery)
update bulkcmd "setenv bootdelay 15"
update bulkcmd "saveenv"

# Backup all partitions
update mread store bootloader normal 0x200000 mtd0.img
update mread store tpl normal 0x800000 mtd1.img
update mread store boot0 normal 0x600000 mtd2.img
update mread store boot1 normal 0x600000 mtd3.img
update mread store system0 normal 0x2820000 mtd4.img
update mread store system1 normal 0x2800000 mtd5.img
update mread store data normal 0x13e0000 mtd6.img

# Flash boot (both A/B slots)
update partition boot0 boot.img
update partition boot1 boot.img

# Flash system (both A/B slots)
update partition system0 root.squashfs
update partition system1 root.squashfs
```

## Project Structure

```
lx06-flash-tool/
├── pyproject.toml
├── README.md
└── lx06_tool/
    ├── app.py              # TUI app entry point
    ├── constants.py        # Partition table, URLs, paths
    ├── config.py           # AppConfig data model (XDG paths)
    ├── state.py            # State machine
    ├── exceptions.py       # Exception hierarchy
    ├── utils/
    │   ├── runner.py       # Async subprocess engine
    │   ├── logger.py       # Rich logging
    │   ├── sudo.py         # 3-tier PTY sudo
    │   ├── checksum.py     # SHA256/MD5 verification
    │   ├── downloader.py   # HTTP downloader
    │   ├── amlogic.py      # Amlogic update binary wrapper
    │   ├── debug_log.py    # Global debug log
    │   └── compat.py       # Legacy import compat
    └── ui/
        ├── widgets.py      # Shared TUI widgets
        └── screens/
            ├── welcome.py
            ├── environment.py
            ├── usb_connect.py
            ├── backup_flash.py
            └── complete.py
```

## Recovery

If flashing fails, the device can be recovered via:
1. **Serial TTL** (115200 baud) — Interrupt U-Boot within 15 seconds (bootdelay)
2. **Backup restore** — Flash backed-up partition images
3. **A/B failover** — U-Boot automatically tries the other slot

## References

- [xiaoai-patch](https://github.com/duhow/xiaoai-patch) — Custom firmware project
- [aml-flash-tool](https://github.com/radxa/aml-flash-tool) — Amlogic flash utility

## License

MIT
