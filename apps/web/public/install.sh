#!/bin/sh
# LazyCloud Install Script for Linux and macOS
# Usage: curl -LsSf https://lazycloud.dev/install.sh | sh

set -e

LAZYCLOUD_VERSION="${LAZYCLOUD_VERSION:-latest}"
DEPOT_VERSION="2.100.12"
INSTALL_DIR="${HOME}/.local/bin"
VERBOSE="${VERBOSE:-false}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { printf "${CYAN}$1${NC}\n"; }
success() { printf "${GREEN}✓ $1${NC}\n"; }
error() { printf "${RED}✗ $1${NC}\n"; exit 1; }
debug() { [ "$VERBOSE" = "true" ] && printf "  $1\n" || true; }

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

command_exists() { command -v "$1" >/dev/null 2>&1; }

download() {
    url="$1"
    output="$2"
    debug "Downloading $url"
    if command_exists curl; then
        curl -fsSL "$url" -o "$output" || error "Download failed"
    elif command_exists wget; then
        wget -q "$url" -O "$output" || error "Download failed"
    else
        error "Neither curl nor wget found"
    fi
}

verify_checksum() {
    file="$1"
    expected="$2"
    if command_exists sha256sum; then
        actual=$(sha256sum "$file" | awk '{print $1}')
    elif command_exists shasum; then
        actual=$(shasum -a 256 "$file" | awk '{print $1}')
    else
        debug "No checksum tool found, skipping verification"
        return 0
    fi
    [ "$actual" = "$expected" ] || error "Checksum verification failed for $(basename "$file")"
}

install_lazycloud() {
    PLATFORM_ARCH=$(detect_platform)
    PLATFORM=$(echo "$PLATFORM_ARCH" | cut -d'-' -f1)
    ARCH=$(echo "$PLATFORM_ARCH" | cut -d'-' -f2)

    if [ "$LAZYCLOUD_VERSION" = "latest" ]; then
        BASE_URL="https://github.com/AmbientWare/lazycloud-releases/releases/latest/download"
    else
        BASE_URL="https://github.com/AmbientWare/lazycloud-releases/releases/download/${LAZYCLOUD_VERSION}"
    fi

    FILENAME="lazycloud-${PLATFORM}-${ARCH}.tar.gz"
    URL="${BASE_URL}/${FILENAME}"
    CHECKSUM_URL="${BASE_URL}/${FILENAME}.sha256"

    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    download "$URL" "$TEMP_DIR/$FILENAME"
    download "$CHECKSUM_URL" "$TEMP_DIR/${FILENAME}.sha256"

    EXPECTED_CHECKSUM=$(cat "$TEMP_DIR/${FILENAME}.sha256" | awk '{print $1}')
    verify_checksum "$TEMP_DIR/$FILENAME" "$EXPECTED_CHECKSUM"

    tar -xzf "$TEMP_DIR/$FILENAME" -C "$TEMP_DIR"
    mkdir -p "$INSTALL_DIR"
    mv "$TEMP_DIR/lazycloud" "$INSTALL_DIR/lazycloud"
    chmod +x "$INSTALL_DIR/lazycloud"
}

install_depot() {
    PLATFORM_ARCH=$(detect_platform)
    PLATFORM=$(echo "$PLATFORM_ARCH" | cut -d'-' -f1)
    ARCH=$(echo "$PLATFORM_ARCH" | cut -d'-' -f2)

    FILENAME="depot_${DEPOT_VERSION}_${PLATFORM}_${ARCH}.tar.gz"
    URL="https://github.com/depot/cli/releases/download/v${DEPOT_VERSION}/${FILENAME}"

    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    download "$URL" "$TEMP_DIR/$FILENAME"
    tar -xzf "$TEMP_DIR/$FILENAME" -C "$TEMP_DIR"

    DEPOT_BINARY=$(find "$TEMP_DIR" -name "depot" -type f | head -1)
    [ -z "$DEPOT_BINARY" ] && error "Could not find depot binary"

    mkdir -p "$INSTALL_DIR"
    mv "$DEPOT_BINARY" "$INSTALL_DIR/depot"
    chmod +x "$INSTALL_DIR/depot"
}

detect_shell_rc() {
    SHELL_NAME=$(basename "$SHELL")
    case "$SHELL_NAME" in
        bash)
            [ -f "$HOME/.bashrc" ] && echo "$HOME/.bashrc" || echo "$HOME/.bash_profile"
            ;;
        zsh) echo "$HOME/.zshrc" ;;
        fish) echo "$HOME/.config/fish/config.fish" ;;
        *) echo "$HOME/.profile" ;;
    esac
}

setup_path() {
    case ":$PATH:" in
        *":$INSTALL_DIR:"*) return ;;
    esac

    SHELL_RC=$(detect_shell_rc)

    if [ -f "$SHELL_RC" ] && ! grep -q "$INSTALL_DIR" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# LazyCloud" >> "$SHELL_RC"
        echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$SHELL_RC"
        debug "Added to $SHELL_RC"
    fi

    export PATH="$INSTALL_DIR:$PATH"
}

main() {
    echo ""
    info "Installing LazyCloud..."
    echo ""

    install_lazycloud
    install_depot
    setup_path

    SHELL_RC=$(detect_shell_rc)

    success "Installation complete!"
    echo ""
    echo "Restart your shell or run: source $SHELL_RC"
    echo ""
}

main "$@"
