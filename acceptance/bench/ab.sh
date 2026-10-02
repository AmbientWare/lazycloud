#!/bin/sh
# Compares two commits: each gets its own worktree and a fresh stack, one
# after the other, runs the same phases with this checkout's harness, and is
# torn down before the next starts. Prints the comparison at the end.
# Usage: ab.sh <commit-a> <commit-b> [phase...]
set -eu
bench=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$bench/../.." && pwd)
a=$1 b=$2
shift 2
root=${LCBENCH_ROOT:-/tmp/lcbench}
export LCBENCH_ROOT="$root"
uv="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
mkdir -p "$root"

measure() {
  label=$1 commit=$2
  shift 2
  tree=$root/tree-$label
  export LCBENCH_TREE="$tree" LCBENCH_STATE="$root/$label" LCBENCH_RESULTS="$root/$label.jsonl"
  rm -rf "$LCBENCH_STATE" "$LCBENCH_RESULTS"
  [ ! -d "$tree" ] || git -C "$repo" worktree remove --force "$tree"
  git -C "$repo" worktree add --detach "$tree" "$commit" >/dev/null
  (cd "$tree" && "$uv" sync --frozen --group dev >/dev/null)
  trap '"$bench/stack.sh" down >/dev/null 2>&1 || true' EXIT
  "$bench/stack.sh" start >"$root/$label-start.log" 2>&1
  "$bench/run-suite.sh" "$@" >"$root/$label-suite.log" 2>&1
  "$bench/stack.sh" down >/dev/null 2>&1
  trap - EXIT
  git -C "$repo" worktree remove --force "$tree"
}

measure a "$a" "$@"
measure b "$b" "$@"
python3 "$bench/compare.py" "$root/a.jsonl" "$root/b.jsonl"
