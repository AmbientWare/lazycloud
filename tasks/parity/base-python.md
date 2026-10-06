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

2026-10-06, PR #506:

- `deploy/images/python/Dockerfile` and the bake `python` target (in
  Ship's `release` group): 3.10.20, 3.11.15, 3.12.13, 3.13.14, 3.14.6,
  tagged `<minor>-<version>` in `<release registry>/python`. Two no-cache
  builds give the same digest. Each base: exact version, ssl (HTTPS to
  PyPI), sqlite3, ctypes, pip install, unchecked-hash stdlib bytecode.
  174 to 202 MB.
- The chart sets `LAZYCLOUD_IMAGE_TEMPLATE`; the server requires it.
  Terraform adds the `python` release repository, server read and build
  host pull on it; Deploy checks the five tags.
- Renderer: every pip-style step is `uv pip install --python <path>
  --compile-bytecode` without a cache; the managed base is chosen by minor
  version, a patch release installs over it unless the base already is it.
  Real builds passed for the managed base, a patch release, a user Python
  base, a base without Python, a Dockerfile, micromamba and a pyproject.
- Torch CPU 2.9.1 + numpy, install step, no cache, two runs: uv on the
  base 18.2 s / 14.7 s; pip on the base 22.6 s / 24.0 s; pip on
  `python:3.12-slim` (before) 25.3 s / 26.4 s.

## Gaps and unverified boundaries

- Not built on a running local stack: its fixed ports belong to the user's
  `lazycloud-local`, so conversion reuse of the base's layers by a custom
  image is unverified.
- Prod needs `platform-core`, `github` and `platform-deployment` applied
  before the Ship that first pushes the bases.
- `acceptance/` still names `docker.io/library/python:{version}-slim` as
  its managed template.
- uv rejects some pip-only flags (`--prefer-binary`,
  `--ignore-requires-python`); such steps now fail at build.
