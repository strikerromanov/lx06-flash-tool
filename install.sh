#!/usr/bin/env bash
# install.sh — LX06 Flash Tool installer
# Supports: CachyOS, Arch Linux, Ubuntu/Debian, Fedora
# Usage:  bash install.sh
#         bash install.sh --no-docker   (skip Docker, use native squashfs)
#         bash install.sh --dev         (install dev dependencies too)
set -euo pipefail

# ─── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${BLUE}[•]${RESET} $*"; }
ok()    { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
error() { echo -e "${RED}[✗]${RESET} $*" >&2; }
die()   { error "$*"; exit 1; }

# ─── CLI Args ─────────────────────────────────────────────────────────────────
NO_DOCKER=false
DEV_DEPS=false
for arg in "$@"; do
  case $arg in
    --no-docker) NO_DOCKER=true ;;
    --dev)       DEV_DEPS=true  ;;
    --help|-h)
      echo "Usage: $0 [--no-docker] [--dev]"
      echo "  --no-docker  Skip Docker installation (use host squashfs tools)"
      echo "  --dev        Also install development dependencies"
      exit 0 ;;
    *) warn "Unknown flag: $arg" ;;
  esac
done

echo -e "${BOLD}LX06 Flash Tool Installer${RESET}"
echo "────────────────────────────────────────"

# ─── Detect Distro ────────────────────────────────────────────────────────────
if [[ ! -f /etc/os-release ]]; then
  die "Cannot detect OS: /etc/os-release not found. Only Linux is supported."
fi
source /etc/os-release

DISTRO_ID="${ID:-unknown}"
DISTRO_LIKE="${ID_LIKE:-}"

detect_family() {
  for token in "$DISTRO_ID" $DISTRO_LIKE; do
    case "$token" in
      arch|cachyos|manjaro|endeavouros|garuda|artix) echo "arch";   return ;;
      ubuntu|debian|raspbian|linuxmint|pop|kali)     echo "debian"; return ;;
      fedora|rhel|centos|rocky|almalinux)            echo "fedora"; return ;;
    esac
  done
  echo "unknown"
}

FAMILY=$(detect_family)
info "Detected: ${PRETTY_NAME:-$DISTRO_ID} (family: $FAMILY)"

# ─── Check Python ─────────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    major=${ver%%.*}; minor=${ver##*.}
    if [[ $major -ge 3 && $minor -ge 10 ]]; then
      PYTHON="$candidate"; break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die "Python 3.10+ not found. Install it first."
ok "Python: $($PYTHON --version)"

# ─── Install System Dependencies ──────────────────────────────────────────────
install_arch() {
  local pkgs=(git libusb-compat squashfs-tools base-devel)
  $NO_DOCKER || pkgs+=(docker)

  # Prefer paru or yay for AUR support; fall back to pacman
  local pm="sudo pacman -S --noconfirm --needed"
  if command -v paru &>/dev/null; then
    pm="paru -S --noconfirm --needed"; info "Using paru"
  elif command -v yay &>/dev/null; then
    pm="yay -S --noconfirm --needed"; info "Using yay"
  fi

  info "Installing: ${pkgs[*]}"
  $pm "${pkgs[@]}"
}

install_debian() {
  local pkgs=(git libusb-0.1-4 squashfs-tools build-essential python3-venv)
  $NO_DOCKER || pkgs+=(docker.io)
  info "Updating apt cache..."
  sudo apt-get update -qq
  info "Installing: ${pkgs[*]}"
  sudo apt-get install -y "${pkgs[@]}"
}

install_fedora() {
  local pkgs=(git libusb-compat-0.1 squashfs-tools gcc make python3-venv)
  $NO_DOCKER || pkgs+=(docker moby-engine)
  info "Installing: ${pkgs[*]}"
  sudo dnf install -y "${pkgs[@]}"
}

case "$FAMILY" in
  arch)   install_arch ;;
  debian) install_debian ;;
  fedora) install_fedora ;;
  *)      warn "Unknown distro family '$FAMILY'. Skipping system package install." ;;
esac

# ─── Docker Group Setup (Arch/CachyOS) ────────────────────────────────────────
if ! $NO_DOCKER; then
  if [[ "$FAMILY" == "arch" ]]; then
    if ! systemctl is-active --quiet docker 2>/dev/null; then
      info "Enabling and starting Docker daemon..."
      sudo systemctl enable --now docker || warn "Could not start docker.service — start it manually."
    fi
    if ! groups "$USER" | grep -qw docker; then
      info "Adding $USER to docker group..."
      sudo usermod -aG docker "$USER"
      warn "You must log out and back in (or run 'newgrp docker') for the group to take effect."
    fi
  fi
fi

# ─── Create Virtual Environment ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating virtual environment at $VENV_DIR ..."
  $PYTHON -m venv "$VENV_DIR"
fi

VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"

info "Upgrading pip inside venv..."
"$VENV_PIP" install --quiet --upgrade pip

# ─── Install the Tool ─────────────────────────────────────────────────────────
info "Installing lx06-flash-tool..."
if $DEV_DEPS; then
  "$VENV_PIP" install -e "$SCRIPT_DIR[dev]"
else
  "$VENV_PIP" install -e "$SCRIPT_DIR"
fi
ok "lx06-flash-tool installed."

# ─── Wrapper Script ───────────────────────────────────────────────────────────
# Create a wrapper so the user can just run 'lx06-tool' without activating venv
WRAPPER="$SCRIPT_DIR/lx06-tool"
cat > "$WRAPPER" << EOF
#!/usr/bin/env bash
# Auto-generated wrapper — activates venv and runs lx06-tool
exec "$VENV_DIR/bin/lx06-tool" "\$@"
EOF
chmod +x "$WRAPPER"

# ─── Shell-Specific Activation Hints ──────────────────────────────────────────
SHELL_NAME=$(basename "${SHELL:-bash}")
echo ""
echo -e "${BOLD}Installation complete!${RESET}"
echo ""
echo "Run the tool with:"
echo -e "  ${GREEN}$WRAPPER${RESET}"
echo ""
echo "Or activate the venv first:"
case "$SHELL_NAME" in
  fish) echo -e "  ${YELLOW}source $VENV_DIR/bin/activate.fish${RESET}" ;;
  zsh)  echo -e "  ${YELLOW}source $VENV_DIR/bin/activate${RESET}" ;;
  *)    echo -e "  ${YELLOW}source $VENV_DIR/bin/activate${RESET}" ;;
esac
echo -e "  ${YELLOW}lx06-tool${RESET}"

if ! $NO_DOCKER && groups "$USER" | grep -qw docker 2>/dev/null; then
  : # already in group — no extra warning needed
elif ! $NO_DOCKER; then
  echo ""
  warn "Remember to log out/in (or run 'newgrp docker') so Docker group takes effect."
fi

echo ""
ok "Done. Happy flashing! 🔧"
