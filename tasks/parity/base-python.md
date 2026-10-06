# Base Python from uv

## Scope

Every Python LazyCloud provides comes from uv's managed builds on a pinned
`debian:trixie-slim`, and every package install uses uv. Owns:

- `internal/images/render.go`: the default base, Python setup and install
  commands. Today a Python base (`python:<version>-slim` from Docker Hub)
  installs with `python -m pip install`; only bases without Python get
  `uv python install` into `/opt/runtime-python`.
- The platform's Python base images, one per supported version (3.10 to
  3.14): built from `debian:trixie-slim` by digest, `uv python install`
  of the exact version into `/opt/runtime-python`, the standard library
  compiled to bytecode, uv itself pinned. Built and pushed by Ship to the
  platform registry, and named by the managed template the server converts
  at start (the image template config, `ManagedBase`).
- Install commands: `uv pip install --python <python> --compile-bytecode`
  for every pip-style step on every base, including user bases that bring
  their own Python. Keep the existing step kinds (pip, requirements files,
  projects, poetry, micromamba) and their merge rules.

Stay off the agent's build runner, upload and conversion (build-speed owns
them).

## The old app, for behavior

`git show 41f2ade1:packages/images/src/images/building/python_runtime.py`
and `.../dockerfile.py` and `shared/image_building/constants.py`
(`DEFAULT_IMAGE_BASE`, `PYTHON_BYTECODE_COMPILE_SCRIPT`): the managed
Python prefix, the exact-version check, `ensurepip`, links for `python`,
`python3`, `python3.X` and `pip`, and stdlib bytecode with unchecked
hashes. Same outcome, implemented in the current renderer.

## Evidence to record

- Renderer tests for each base kind (default, user Python base, base
  without Python, Dockerfile, micromamba).
- Build of the torch benchmark image on a local stack: package install time
  with uv against pip, and that the custom image reuses the platform base's
  converted layers (only its own layers convert).
- Python version and `import ssl, sqlite3, ctypes` working in each base.

## Progress

## Gaps and unverified boundaries
