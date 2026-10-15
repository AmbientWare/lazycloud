#!/usr/bin/env bash
# Builds volume-cache.so and proves which copy each open reaches: the cache
# copy only for a read-only open of a mounted file whose cache copy matches
# in size and mtime, the mount for everything else. The copies that may be
# served from the cache hold different bytes of equal length so the bytes
# read name the copy.
#
# Needs bash, gcc, python3; no root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

gcc -shared -fPIC -O2 -Wall -Wextra -Werror -fvisibility=hidden \
  -o "$work/volume-cache.so" "$here/volume_cache.c" -ldl

# A fortified caller: a two-argument open with a flag word the compiler
# cannot see reaches __open_2.
cat >"$work/helper.c" <<'EOF'
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	if (argc != 4)
		return 2;
	char buf[64];
	ssize_t n;
	if (strcmp(argv[1], "fopen") == 0) {
		FILE *f = fopen(argv[2], argv[3]);
		if (f == NULL) {
			perror("fopen");
			return 1;
		}
		n = (ssize_t)fread(buf, 1, sizeof buf, f);
		fclose(f);
	} else {
		int flags = strcmp(argv[3], "rdwr") == 0 ? O_RDWR : O_RDONLY;
		int fd = open(argv[2], flags);
		if (fd < 0) {
			perror("open");
			return 1;
		}
		n = read(fd, buf, sizeof buf);
		close(fd);
	}
	if (n < 0)
		return 1;
	fwrite(buf, 1, (size_t)n, stdout);
	return 0;
}
EOF
gcc -O2 -Wall -Wextra -Werror -o "$work/helper" "$work/helper.c"

mount="$work/mnt/models"
data="$work/mnt/data"
cache="$work/cache"
mkdir -p "$mount/sub" "$data" "$cache$mount/sub" "$cache$data" "$work/outside"

# proof: equal size and mtime but different bytes, so the bytes read name
# the copy. same: identical bytes, for the append that ends the match.
printf 'same-bytes' >"$mount/same.bin"
cp -p "$mount/same.bin" "$cache$mount/same.bin"
printf 'mount' >"$mount/proof.bin"
printf 'cache' >"$cache$mount/proof.bin"
touch -r "$mount/proof.bin" "$cache$mount/proof.bin"
printf 'mount' >"$mount/sub/deep.bin"
printf 'cache' >"$cache$mount/sub/deep.bin"
touch -r "$mount/sub/deep.bin" "$cache$mount/sub/deep.bin"
printf 'mount' >"$data/proof.bin"
printf 'cache' >"$cache$data/proof.bin"
touch -r "$data/proof.bin" "$cache$data/proof.bin"
# mtime: same bytes, older cache copy. size: shorter cache copy.
printf 'mount' >"$mount/mtime.bin"
printf 'cache' >"$cache$mount/mtime.bin"
touch -d '2020-01-01 00:00:00' "$cache$mount/mtime.bin"
printf 'mount' >"$mount/size.bin"
printf 'cach' >"$cache$mount/size.bin"
touch -r "$mount/size.bin" "$cache$mount/size.bin"
printf 'mount' >"$mount/missing.bin"
printf 'mount' >"$mount/unreadable.bin"
printf 'cache' >"$cache$mount/unreadable.bin"
touch -r "$mount/unreadable.bin" "$cache$mount/unreadable.bin"
chmod 000 "$cache$mount/unreadable.bin"
printf 'outside' >"$work/outside/file.bin"
mkdir -p "$cache$work/outside"
printf 'cacheXX' >"$cache$work/outside/file.bin"
touch -r "$work/outside/file.bin" "$cache$work/outside/file.bin"

# Only the commands under test load the shim.
preload="$work/volume-cache.so"
export LAZYCLOUD_VOLUME_CACHE="$cache"
export LAZYCLOUD_VOLUME_PATHS="$mount:$data"

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

# run <name> <expected stdout> <command...>
run() {
  local name="$1" want="$2"
  shift 2
  local out
  out="$(LD_PRELOAD="$preload" "$@" 2>"$work/err")" || { fail "$name: command failed: $(cat "$work/err")"; return; }
  if [[ "$out" != "$want" ]]; then
    fail "$name: read '$out', want '$want'"
  fi
}

cat >"$work/read.py" <<'EOF'
import sys
data = open(sys.argv[1], sys.argv[2]).read()
sys.stdout.buffer.write(data if isinstance(data, bytes) else data.encode())
EOF
py_read=(python3 "$work/read.py")

# Reads through openat (python) and fopen / __open_2 (the helper).
run "python proof" cache "${py_read[@]}" "$mount/proof.bin" r
run "python subdir" cache "${py_read[@]}" "$mount/sub/deep.bin" r
run "python second volume" cache "${py_read[@]}" "$data/proof.bin" r
run "python mtime" mount "${py_read[@]}" "$mount/mtime.bin" r
run "python size" mount "${py_read[@]}" "$mount/size.bin" r
run "python missing" mount "${py_read[@]}" "$mount/missing.bin" r
run "python outside" outside "${py_read[@]}" "$work/outside/file.bin" r
run "python dotdot" mount "${py_read[@]}" "$mount/sub/../proof.bin" r
run "fopen r" cache "$work/helper" fopen "$mount/proof.bin" r
run "fopen r+" mount "$work/helper" fopen "$mount/proof.bin" r+
run "open rdonly" cache "$work/helper" open "$mount/proof.bin" rdonly
run "open rdwr" mount "$work/helper" open "$mount/proof.bin" rdwr
if [[ "$(id -u)" != 0 ]]; then
  run "python unreadable cache" mount "${py_read[@]}" "$mount/unreadable.bin" r
fi

# Writes reach the mount and leave the cache copy alone.
LD_PRELOAD="$preload" python3 -c 'import sys; f = open(sys.argv[1], "r+"); f.write("MOUNT"); f.close()' "$mount/proof.bin" \
  2>"$work/err" || fail "r+ write failed: $(cat "$work/err")"
[[ "$(cat "$mount/proof.bin")" == MOUNT ]] || fail "r+ write did not reach the mount copy"
[[ "$(cat "$cache$mount/proof.bin")" == cache ]] || fail "r+ write changed the cache copy"
LD_PRELOAD="$preload" python3 -c 'import sys; open(sys.argv[1], "w").write("new")' "$mount/written.bin" 2>"$work/err" \
  || fail "w write failed: $(cat "$work/err")"
[[ "$(cat "$mount/written.bin")" == new ]] || fail "w write did not create the mount file"
[[ ! -e "$cache$mount/written.bin" ]] || fail "w write created a cache file"
LD_PRELOAD="$preload" python3 -c 'import sys; open(sys.argv[1], "a").write("+")' "$mount/same.bin" 2>"$work/err" \
  || fail "a write failed: $(cat "$work/err")"
[[ "$(cat "$mount/same.bin")" == same-bytes+ ]] || fail "a write did not reach the mount copy"
[[ "$(cat "$cache$mount/same.bin")" == same-bytes ]] || fail "a write changed the cache copy"
# The appended mount copy no longer matches its cache copy.
run "python same after write" same-bytes+ "${py_read[@]}" "$mount/same.bin" r

# A missing mount file fails as it would without the shim, with its errno.
out="$(LD_PRELOAD="$preload" python3 -c 'import sys
try:
    open(sys.argv[1])
except FileNotFoundError as e:
    print("ENOENT", e.errno)' "$mount/absent.bin" 2>"$work/err")" || fail "absent open: $(cat "$work/err")"
[[ "$out" == "ENOENT 2" ]] || fail "absent mount file: got '$out'"

# Without the settings every open passes through.
run "unset cache dir" MOUNT env -u LAZYCLOUD_VOLUME_CACHE "${py_read[@]}" "$mount/proof.bin" r
run "empty volume paths" MOUNT env LAZYCLOUD_VOLUME_PATHS= "${py_read[@]}" "$mount/proof.bin" r

if [[ "$failures" != 0 ]]; then
  echo "$failures failure(s)" >&2
  exit 1
fi
echo "volume-cache: all checks passed"
