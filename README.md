# LX06 Flash Tool

All-in-one TUI tool to flash custom firmware onto the **Xiaomi LX06** (Xiaoai Speaker Pro) smart speaker, transforming it into an open multi-protocol audio player with WiFi, SSH, ADB, and full media services.

## What It Does

1. **Environment Setup** — Detects your Linux distro, installs required packages, downloads aml-flash-tool, configures udev rules for Amlogic USB
2. **USB Connection** — Guides you through entering USB burning mode, detects the Amlogic device
3. **Backup** — Dumps all 7 NAND partitions for full safety backup
4. **Download** — Fetches the latest pre-built firmware from [GitHub releases](https://github.com/strikerromanov/lx06-flash-tool/releases)
5. **Flash** — Writes boot + system images to both A/B slots for redundancy

## Features (Post-Flash)

The custom firmware (based on [xiaoai-patch](https://github.com/duhow/xiaoai-patch)) provides:

- **WiFi** — Pre-configured wireless networking with DHCP hostname support
- **SSH Access** — Root shell via `ssh root@<speaker-ip>` (password: `factory`)
- **ADB Access** — Android Debug Bridge enabled via `/data/adb_enable`
- **Spotify Connect** — [librespot](https://github.com/librespot-org/librespot) with ALSA backend, 320kbps, softvol mixer
- **AirPlay** — [shairport-sync](https://github.com/mikebrady/shairport-sync) for Apple device streaming
- **UPnP/DLNA** — [upmpdcli](https://github.com/medoc92/upmpdcli) with MPD backend
- **Squeezelite** — Logitech Media Server client
- **Snapcast Client** — Synchronized multi-room audio via [Snapcast](https://github.com/badaix/snapcast)
- **MPD** — Music Player Daemon for local playback control
- **Avahi/mDNS** — Bonjour/Zeroconf service discovery for all protocols
- **Bluetooth** — Enhanced Bluetooth audio with additional codec support
- **Persistent Data** — All configs and data survive firmware updates on `/data` (UBIFS)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/strikerromanov/lx06-flash-tool.git
cd lx06-flash-tool

# Set up virtual environment
python -m venv venv
source venv/bin/activate
pip install -e .

# Run the TUI flasher
lx06-tool
```

Follow the interactive TUI screens to flash your speaker.

### Prerequisites

- **Linux** (amd64) — Ubuntu, Debian, Fedora, Arch, or derivatives
- **Python 3.10+**
- **USB 2.0/3.0 port** + micro USB cable (data cable, not charge-only)
- **sudo** access (for udev rules and flashing)
- **git**

## Post-Flash Setup

### SSH Access
```bash
ssh root@192.168.x.x  # password: factory
```

### Spotify Connect

librespot starts automatically on boot. To configure:
```bash
# Edit the init script
vi /etc/rc.d/S98librespot

# Restart the service
killall librespot
/etc/rc.d/S98librespot start
```

The speaker appears as "LX06-3555" in Spotify Connect. Change the name in the init script's `--name` parameter.

### AirPlay

Shairport-sync starts automatically. The speaker appears as "LX06" in AirPlay device list.

### UPnP/DLNA

upmpdcli starts automatically. The speaker appears as a UPnP renderer on your network.

### Snapcast

To connect to a Snapcast server:
```bash
# Edit the snapclient init script
vi /etc/rc.d/S95snapclient
# Add your Snapcast server IP
```

### Check Running Services
```bash
ps | grep -v '\[' | sort
```

## Supported Distros

| Family | Distros | Package Manager |
|--------|---------|---------------|
| Debian | Ubuntu, Debian, Linux Mint, Pop!_OS, Kali | apt |
| Fedora | Fedora, RHEL, Rocky, Alma | dnf |
| Arch | CachyOS, Arch Linux, Manjaro, EndeavourOS | pacman |

## Partition Table (LX06)

| MTD | Label | Size | Description |
|-----|-------|------|-------------|
| mtd0 | bootloader | 2 MB | U-Boot bootloader |
| mtd1 | tpl | 8 MB | U-Boot second stage |
| mtd2 | boot0 | 6 MB | Kernel A |
| mtd3 | boot1 | 6 MB | Kernel B |
| mtd4 | system0 | 40.2 MB | Rootfs A (squashfs) |
| mtd5 | system1 | 40 MB | Rootfs B (squashfs) |
| mtd6 | data | 20.8 MB | Persistent data (UBIFS) |

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
1. **Serial TTL** (115200 baud) — Interrupt U-Boot within 15 seconds (bootdelay is set to 15s)
2. **Backup restore** — Re-flash your backed-up partition images using the same tool
3. **A/B failover** — U-Boot automatically tries the other slot if one fails

## References

- [xiaoai-patch](https://github.com/duhow/xiaoai-patch) — Custom firmware project for Xiaomi speakers
- [aml-flash-tool](https://github.com/radxa/aml-flash-tool) — Amlogic USB flash utility
- [librespot](https://github.com/librespot-org/librespot) — Open Source Spotify client
- [shairport-sync](https://github.com/mikebrady/shairport-sync) — AirPlay audio player

## License

MIT
