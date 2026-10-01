#!/bin/sh
# Installs the pinned GeeseFS release the agent mounts volumes with at
# bin/geesefs, verifying its digest.
set -eu
cd "$(dirname "$0")/../.."
version=v0.43.9
sha256=8253f0bad8070f504b3ca94a76919d9617252e7ffae04efca9ca54d5cfbaaca8
if [ -x bin/geesefs ] && echo "$sha256  bin/geesefs" | sha256sum -c - >/dev/null 2>&1; then
  exit 0
fi
mkdir -p bin
curl -fsSL -o bin/geesefs.tmp "https://github.com/yandex-cloud/geesefs/releases/download/$version/geesefs-linux-amd64"
echo "$sha256  bin/geesefs.tmp" | sha256sum -c - >/dev/null
chmod +x bin/geesefs.tmp
mv bin/geesefs.tmp bin/geesefs
