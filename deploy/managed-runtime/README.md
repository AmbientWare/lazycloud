# Managed runtime locks

These locks are generated from the runtime dependency and build-system metadata
owned by `shared`, `foundation`, `sdk`, and `runner`:

```sh
uv run --no-project python deploy/managed-runtime/locks.py generate
```

Each Python minor and Linux architecture is resolved independently with binary
packages required. The lock files can be byte-identical when the selected
release set and published hashes match, but they remain separate target
contracts so a future ABI or architecture wheel gap fails during generation and
the corresponding Docker build.

Every lock includes a digest of the managed package metadata. Docker verifies
that digest before installation and uses the hashed `build-tools.txt` with
build isolation disabled, so neither runtime dependencies nor the local wheel
build toolchain can float during an image build.
