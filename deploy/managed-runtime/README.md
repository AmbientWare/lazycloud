# Managed runtime locks

These locks are generated from the runtime dependency and build-system metadata
owned by `shared`, `foundation`, `lazycloud`, and `runner`.
After changing those dependencies, regenerate the locks from the repository root:

```sh
uv run --no-project python deploy/managed-runtime/locks.py generate
```

Check the generated files before committing:

```sh
uv run --no-project python deploy/managed-runtime/locks.py check
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
