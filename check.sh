#!/bin/sh
# Format and lint checks for every language in the repo, as CI runs them.
# Usage: ./check.sh [--fix]   --fix applies formatters before checking.
# Tests are separate: go test -race ./..., uv run --group dev pytest -x, and
# bun run test in web/.
set -eu
cd "$(dirname "$0")"
export GOTOOLCHAIN=go1.27.1
uv="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
bun="${BUN:-$(command -v bun || echo "$HOME/.bun/bin/bun")}"

[ -d web/node_modules ] || (cd web && "$bun" install --frozen-lockfile)

if [ "${1:-}" = --fix ]; then
  go tool golangci-lint fmt ./...
  go tool buf format -w
  go mod tidy
  "$uv" run --group dev ruff format python
  "$uv" run --group dev ruff check --fix python
  (cd web && "$bun" run format)
fi

echo "== go"
go mod tidy -diff
go vet ./...
go tool golangci-lint run ./...
go tool buf format -d --exit-code
go tool buf lint
# The protobuf bindings match their contracts.
gen=$(mktemp -d)
trap 'rm -rf "$gen"' EXIT
go tool buf generate -o "$gen"
(cd "$gen" && find . -type f) | while read -r f; do
  cmp -s "$gen/$f" "$f" || { echo "stale generated code: $f; run go generate"; exit 1; }
done
echo "== python"
"$uv" run --group dev ruff format --check python
"$uv" run --group dev ruff check python
"$uv" run --group dev basedpyright python
echo "== web"
(cd web && "$bun" run format:check && "$bun" run lint && "$bun" run typecheck)
echo "== deploy"
deploy/check.sh
echo "all checks passed"
