#!/bin/sh
# LazyCloud Install Script for Linux and macOS
# Usage: curl -LsSf https://lazycloud.dev/install.sh | sh

set -e

LAZYCLOUD_VERSION="${LAZYCLOUD_VERSION:-latest}"
DEPOT_VERSION="2.100.12"
INSTALL_DIR="${HOME}/.local/bin"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info() {
    printf "${CYAN}$1${NC}\n"
}

success() {
    printf "${GREEN}✓ $1${NC}\n"
}

warn() {
    printf "${YELLOW}⚠ $1${NC}\n"
}

error() {
    printf "${RED}✗ $1${NC}\n"
    exit 1
}

# Detect platform
detect_platform() {
    OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    ARCH=$(uname -m)

    case "$OS" in
        darwin) PLATFORM="darwin" ;;
        linux) PLATFORM="linux" ;;
        *) error "Unsupported operating system: $OS" ;;
    esac

    case "$ARCH" in
        x86_64|amd64) ARCH="amd64" ;;
        arm64|aarch64) ARCH="arm64" ;;
        *) error "Unsupported architecture: $ARCH" ;;
    esac

    echo "$PLATFORM-$ARCH"
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Install lazycloud binary
install_lazycloud() {
    info "Installing LazyCloud CLI..."

    PLATFORM_ARCH=$(detect_platform)
    PLATFORM=$(echo "$PLATFORM_ARCH" | cut -d'-' -f1)
    ARCH=$(echo "$PLATFORM_ARCH" | cut -d'-' -f2)

    # Determine download URL
    if [ "$LAZYCLOUD_VERSION" = "latest" ]; then
        LAZYCLOUD_URL="https://github.com/AmbientWare/lazycloud-releases/releases/latest/download/lazycloud-${PLATFORM}-${ARCH}.tar.gz"
    else
        LAZYCLOUD_URL="https://github.com/AmbientWare/lazycloud-releases/releases/download/${LAZYCLOUD_VERSION}/lazycloud-${PLATFORM}-${ARCH}.tar.gz"
    fi

    # Download and extract
    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    info "Downloading from $LAZYCLOUD_URL..."
    if command_exists curl; then
        curl -fsSL "$LAZYCLOUD_URL" -o "$TEMP_DIR/lazycloud.tar.gz" || error "Failed to download LazyCloud CLI"
    elif command_exists wget; then
        wget -q "$LAZYCLOUD_URL" -O "$TEMP_DIR/lazycloud.tar.gz" || error "Failed to download LazyCloud CLI"
    else
        error "Neither curl nor wget found. Cannot download LazyCloud CLI."
    fi

    # Extract and install
    tar -xzf "$TEMP_DIR/lazycloud.tar.gz" -C "$TEMP_DIR"
    mkdir -p "$INSTALL_DIR"
    mv "$TEMP_DIR/lazycloud" "$INSTALL_DIR/lazycloud"
    chmod +x "$INSTALL_DIR/lazycloud"

    success "LazyCloud CLI installed to $INSTALL_DIR/lazycloud"
}

# Install Depot CLI
install_depot() {
    info "Installing Depot CLI v${DEPOT_VERSION}..."

    PLATFORM_ARCH=$(detect_platform)
    PLATFORM=$(echo "$PLATFORM_ARCH" | cut -d'-' -f1)
    ARCH=$(echo "$PLATFORM_ARCH" | cut -d'-' -f2)

    DEPOT_FILENAME="depot_${DEPOT_VERSION}_${PLATFORM}_${ARCH}.tar.gz"
    DEPOT_URL="https://github.com/depot/cli/releases/download/v${DEPOT_VERSION}/${DEPOT_FILENAME}"

    # Create install directory
    mkdir -p "$INSTALL_DIR"

    # Download and extract
    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    info "Downloading from $DEPOT_URL..."
    if command_exists curl; then
        curl -fsSL "$DEPOT_URL" -o "$TEMP_DIR/$DEPOT_FILENAME"
    elif command_exists wget; then
        wget -q "$DEPOT_URL" -O "$TEMP_DIR/$DEPOT_FILENAME"
    else
        error "Neither curl nor wget found. Cannot download Depot CLI."
    fi

    # Extract
    tar -xzf "$TEMP_DIR/$DEPOT_FILENAME" -C "$TEMP_DIR"

    # Find and move the depot binary
    DEPOT_BINARY=$(find "$TEMP_DIR" -name "depot" -type f | head -1)
    if [ -z "$DEPOT_BINARY" ]; then
        error "Could not find depot binary in archive"
    fi

    mv "$DEPOT_BINARY" "$INSTALL_DIR/depot"
    chmod +x "$INSTALL_DIR/depot"

    success "Depot CLI installed to $INSTALL_DIR/depot"
}

# Add to PATH
setup_path() {
    # Check if already in PATH
    case ":$PATH:" in
        *":$INSTALL_DIR:"*) return ;;
    esac

    info "Adding $INSTALL_DIR to PATH..."

    # Detect shell and config file
    SHELL_NAME=$(basename "$SHELL")
    case "$SHELL_NAME" in
        bash)
            if [ -f "$HOME/.bashrc" ]; then
                SHELL_RC="$HOME/.bashrc"
            else
                SHELL_RC="$HOME/.bash_profile"
            fi
            ;;
        zsh)
            SHELL_RC="$HOME/.zshrc"
            ;;
        fish)
            SHELL_RC="$HOME/.config/fish/config.fish"
            ;;
        *)
            SHELL_RC="$HOME/.profile"
            ;;
    esac

    # Add to shell config if not already there
    if [ -f "$SHELL_RC" ]; then
        if ! grep -q "$INSTALL_DIR" "$SHELL_RC" 2>/dev/null; then
            echo "" >> "$SHELL_RC"
            echo "# LazyCloud" >> "$SHELL_RC"
            echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$SHELL_RC"
            success "Added to $SHELL_RC"
        fi
    fi

    # Export for current session
    export PATH="$INSTALL_DIR:$PATH"
}

# Verify installation
verify_installation() {
    info "Verifying installation..."

    if command_exists lazycloud; then
        success "lazycloud is available"
    else
        warn "lazycloud not found in PATH. You may need to restart your shell."
    fi

    if [ -x "$INSTALL_DIR/depot" ]; then
        success "depot is available at $INSTALL_DIR/depot"
    else
        warn "depot not found"
    fi
}

# Main
main() {
    echo ""
    info "╔═══════════════════════════════════════╗"
    info "║       LazyCloud Installer             ║"
    info "╚═══════════════════════════════════════╝"
    echo ""

    install_lazycloud
    install_depot
    setup_path
    verify_installation

    echo ""
    success "Installation complete!"
    echo ""
    info "To get started:"
    echo "  1. Restart your shell or run: source $SHELL_RC"
    echo "  2. Run: lazycloud login"
    echo "  3. Run: lazycloud init"
    echo ""
}

main "$@"

