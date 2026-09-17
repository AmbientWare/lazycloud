from __future__ import annotations

import shlex

from shared.image_building.authoring import (
    PROJECT_BUILD_STEP_KINDS,
    ImageBuildStep,
    ImageBuildStepKind,
    ImageSpec,
)
from shared.image_building.python import normalize_python_version, python_minor_version

from images.building.commands import _normalize_step
from images.building.constants import (
    MANAGED_PYTHON_PREFIX,
    MICROMAMBA_IMAGE_REFERENCE,
    UV_COPY_INSTRUCTION,
)
from images.building.models import (
    PythonRuntimeSetupAction,
    PythonRuntimeSetupPlan,
)
from images.building.references import (
    dockerfile_base_image,
    parse_image_source_reference,
)

PYTHON_BYTECODE_COMPILE_SCRIPT = (
    "import compileall, py_compile, sysconfig; "
    "ok = compileall.compile_dir(sysconfig.get_path('stdlib'), force=True, quiet=1, "
    "invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH); "
    "raise SystemExit(0 if ok else 1)"
)


def plan_python_runtime_setup(image: ImageSpec) -> PythonRuntimeSetupPlan:
    python_version = image.python_version.strip()
    requires_python = _image_requires_python_runtime(image)
    requires_uv = _image_requires_uv(image)
    if not requires_python:
        return PythonRuntimeSetupPlan(
            action=PythonRuntimeSetupAction.Skipped,
            requires_python=False,
            python_version=python_version,
            reason="Python setup is ignored and no package install steps require it",
        )

    if python_version.startswith("micromamba"):
        release = normalize_python_version(python_version).removeprefix("micromamba")
        environment_file = any(
            step.kind is ImageBuildStepKind.MicromambaEnvironment for step in image.build_steps
        )
        return PythonRuntimeSetupPlan(
            action=PythonRuntimeSetupAction.ConfigureMicromamba,
            requires_python=True,
            python_version=python_version,
            dockerfile_instructions=[
                f"COPY --from={MICROMAMBA_IMAGE_REFERENCE} /bin/micromamba /usr/local/bin/",
                "ENV MAMBA_ROOT_PREFIX=/opt/micromamba",
                "ENV PATH=${MAMBA_ROOT_PREFIX}/bin:${PATH}",
                *([UV_COPY_INSTRUCTION] if requires_uv else []),
            ],
            commands=[]
            if environment_file
            else [
                f"micromamba create -y -n base -c conda-forge python={release} pip"
                " && micromamba clean --all --yes",
                _bytecode_compile_command("python"),
            ],
            reason="micromamba installs the selected Python in the image environment",
        )

    if _image_base_provides_python(image):
        return PythonRuntimeSetupPlan(
            action=PythonRuntimeSetupAction.UseBasePython,
            requires_python=True,
            python_version=python_version,
            dockerfile_instructions=[UV_COPY_INSTRUCTION] if requires_uv else [],
            commands=[
                _bytecode_compile_command("python"),
            ],
            reason="base image already provides Python",
        )

    if not python_version:
        return PythonRuntimeSetupPlan(
            action=PythonRuntimeSetupAction.UseBasePython,
            requires_python=True,
            commands=[_bytecode_compile_command("python")],
            reason="no Python version configured for managed installation",
        )
    python_version = normalize_python_version(python_version)

    quoted_version = shlex.quote(python_version)
    runtime_python = f"/usr/local/bin/python{python_minor_version(python_version)}"
    return PythonRuntimeSetupPlan(
        action=PythonRuntimeSetupAction.InstallManagedPython,
        requires_python=True,
        python_version=python_version,
        python_executable=runtime_python,
        dockerfile_instructions=[UV_COPY_INSTRUCTION],
        commands=[
            _managed_python_setup_command(python_version, quoted_version=quoted_version),
            _managed_python_link_command(python_version),
            _bytecode_compile_command(runtime_python),
        ],
        reason="unknown base requires an exact compatible or managed Python runtime",
    )


def _bytecode_compile_command(python_executable: str) -> str:
    return f"{python_executable} -c {shlex.quote(PYTHON_BYTECODE_COMPILE_SCRIPT)}"


def _managed_python_setup_command(python_version: str, *, quoted_version: str) -> str:
    prefix_python = f"{MANAGED_PYTHON_PREFIX}/bin/python"
    base_python = f"/usr/local/bin/python{python_minor_version(python_version)}"
    base_pip = "/usr/local/bin/pip"
    release = tuple(int(part) for part in python_version.split("."))
    version_check = shlex.quote(
        f"import sys; raise SystemExit(0 if sys.version_info[:{len(release)}] "
        f"== {release!r} else 1)"
    )
    mismatch = shlex.quote(
        f"existing {MANAGED_PYTHON_PREFIX} interpreter does not match {base_python}"
    )
    managed_mismatch = shlex.quote(
        f"managed Python {python_version} installation did not provide an exact interpreter"
    )
    return (
        f"if [ -x {base_python} ] && {base_python} -c {version_check} "
        f"&& [ -x {base_pip} ] && {base_python} -m pip --version >/dev/null 2>&1; then "
        f"mkdir -p {MANAGED_PYTHON_PREFIX}/bin && "
        f"if [ -e {prefix_python} ] || [ -L {prefix_python} ]; then "
        f"{prefix_python} -c {version_check} && [ {prefix_python} -ef {base_python} ] "
        f"|| {{ echo {mismatch} >&2; exit 1; }}; "
        f"else ln -s {base_python} {prefix_python}; fi && "
        f"{prefix_python} -m pip --version; "
        "else "
        f"/usr/local/bin/uv python install {quoted_version} --managed-python --no-bin && "
        f"managed_python=$(/usr/local/bin/uv python find {quoted_version} "
        "--managed-python --no-project) && "
        f'[ -x "$managed_python" ] && "$managed_python" -c {version_check} '
        '&& "$managed_python" -m pip --version >/dev/null 2>&1 '
        f"|| {{ echo {managed_mismatch} >&2; exit 1; }}; "
        f"mkdir -p {MANAGED_PYTHON_PREFIX}/bin && "
        f"if [ -e {prefix_python} ] || [ -L {prefix_python} ]; then "
        f'{prefix_python} -c {version_check} && [ {prefix_python} -ef "$managed_python" ] '
        f"|| {{ echo {mismatch} >&2; exit 1; }}; "
        f'else ln -s "$managed_python" {prefix_python}; fi; '
        "fi"
    )


def _managed_python_link_command(python_version: str) -> str:
    prefix_python = f"{MANAGED_PYTHON_PREFIX}/bin/python"
    minor = python_minor_version(python_version)
    return (
        'runtime_link() { source_path="$1"; target_path="$2"; '
        'if [ -e "$target_path" ] || [ -L "$target_path" ]; then '
        'if [ "$source_path" -ef "$target_path" ]; then return 0; fi; '
        'rm -f "$target_path"; fi; ln -s "$source_path" "$target_path"; }; '
        f"runtime_link {prefix_python} /usr/local/bin/python && "
        f"runtime_link {prefix_python} /usr/local/bin/python{minor} && "
        f'runtime_bin=$(dirname "$(readlink -f {prefix_python})") && '
        '[ -x "$runtime_bin/pip" ] && runtime_link "$runtime_bin/pip" /usr/local/bin/pip'
    )


def _image_requires_python_runtime(image: ImageSpec) -> bool:
    if image.packages:
        return True
    if any(_step_requires_python(_normalize_step(step)) for step in image.build_steps):
        return True
    return bool(image.python_version.strip() and not image.ignore_python)


def _image_requires_uv(image: ImageSpec) -> bool:
    return any(_normalize_step(step).kind in PROJECT_BUILD_STEP_KINDS for step in image.build_steps)


def _step_requires_python(step: ImageBuildStep) -> bool:
    return step.kind in PROJECT_BUILD_STEP_KINDS | {
        ImageBuildStepKind.Pip,
        ImageBuildStepKind.Micromamba,
    } and bool(step.args)


def _image_base_provides_python(image: ImageSpec) -> bool:
    base = dockerfile_base_image(image.dockerfile) if image.dockerfile else image.base
    if not base:
        return False
    try:
        reference = parse_image_source_reference(base)
    except ValueError:
        return False
    repository_tail = reference.repository.rsplit("/", 1)[-1].lower()
    if repository_tail != "python":
        return False
    requested = image.python_version.strip().removeprefix("python")
    if not requested:
        return True
    if reference.digest and not reference.tag:
        return True
    return reference.tag == requested or reference.tag.startswith(
        (f"{requested}-", f"{requested}.")
    )
