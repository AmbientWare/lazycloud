#!/bin/sh
# Runs lazycloud-admin in the reference benchmark stack with its local
# administrator token. Usage: ref-admin.sh <lazycloud-admin args...>
set -eu
cd "${LCBENCH_REF:-/tmp/lc-ref}"
GATEWAY_TOKEN=$(grep '^LAZYCLOUD_TOKEN=' .env | cut -d= -f2-)
export GATEWAY_TOKEN
exec docker compose run --rm --no-deps -T -e GATEWAY_HTTP_URL=http://api-ingress:9000 -e GATEWAY_TOKEN cli lazycloud-admin "$@"
