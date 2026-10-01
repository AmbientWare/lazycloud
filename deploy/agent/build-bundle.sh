#!/usr/bin/env bash
# Builds the agent release archives the install script and self-update
# download: <out_dir>/<version>/lazycloud-agent-linux-<arch>.tar.gz, each
# holding lazycloud-agent, supervisor, runtime/<python> and, on amd64,
# geesefs at its root. Prints
# one "<arch> <sha256>" line per archive.
#
# Usage: deploy/agent/build-bundle.sh [--arch amd64,arm64] [--python "3.12 3.13"] <out_dir> <version>
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
arches=amd64
pythons=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) arches="$2"; shift 2 ;;
    --python) pythons="$2"; shift 2 ;;
    -*) echo "unknown flag $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
if [[ $# -ne 2 ]]; then
  echo "usage: $0 [--arch amd64,arm64] [--python \"3.12 3.13\"] <out_dir> <version>" >&2
  exit 2
fi
out="$1"
version="$2"
if [[ ! "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  echo "version $version is not a release name" >&2
  exit 2
fi

mkdir -p "$out/$version"
out="$(cd "$out" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export GOTOOLCHAIN="${GOTOOLCHAIN:-go1.27.1}"

IFS=, read -r -a arch_list <<<"$arches"
for arch in "${arch_list[@]}"; do
  case "$arch" in
    amd64) platform=x86_64-manylinux_2_28 ;;
    arm64) platform=aarch64-manylinux_2_28 ;;
    *) echo "unsupported architecture $arch" >&2; exit 2 ;;
  esac
  stage="$work/$arch"
  mkdir -p "$stage/runtime"
  (cd "$root" && CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -trimpath \
    -ldflags "-s -w -X main.version=$version" -o "$stage/lazycloud-agent" ./cmd/agent)
  (cd "$root" && CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -trimpath -ldflags "-s -w" \
    -o "$stage/supervisor" ./cmd/supervisor)
  # shellcheck disable=SC2086 # the Python versions are separate words
  LAZYCLOUD_RUNTIME_PLATFORM="$platform" "$root/deploy/local/build-runtime.sh" "$stage/runtime" $pythons
  files=(lazycloud-agent supervisor runtime)
  # GeeseFS mounts volumes; its pinned release exists for amd64.
  if [[ "$arch" == amd64 ]]; then
    "$root/deploy/local/fetch-geesefs.sh"
    cp "$root/bin/geesefs" "$stage/geesefs"
    files+=(geesefs)
  fi
  archive="$out/$version/lazycloud-agent-linux-$arch.tar.gz"
  tar -C "$stage" --owner=0 --group=0 --numeric-owner -czf "$archive.tmp" "${files[@]}"
  mv "$archive.tmp" "$archive"
  echo "$arch $(sha256sum "$archive" | cut -d' ' -f1)"
done
