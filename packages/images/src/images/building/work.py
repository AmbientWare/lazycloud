from __future__ import annotations

from shared.image_building.authoring import ImageSpec
from shared.image_building.requirements import sanitize_python_packages

from images.building.commands import _normalize_step, _step_has_content
from images.building.models import ImageBuildWorkPlan, ImageBuildWorkReason


def image_build_work_plan(image: ImageSpec) -> ImageBuildWorkPlan:
    reasons: list[ImageBuildWorkReason] = []
    if any(command.strip() for command in image.commands):
        reasons.append(ImageBuildWorkReason.Commands)
    if any(_step_has_content(_normalize_step(step)) for step in image.build_steps):
        reasons.append(ImageBuildWorkReason.BuildSteps)
    if sanitize_python_packages(image.packages):
        reasons.append(ImageBuildWorkReason.PythonPackages)
    if image.env:
        reasons.append(ImageBuildWorkReason.Environment)
    if any(secret.strip() for secret in image.secrets):
        reasons.append(ImageBuildWorkReason.Secrets)
    if image.python_version.strip() and not image.ignore_python:
        reasons.append(ImageBuildWorkReason.PythonRuntime)
    return ImageBuildWorkPlan(has_work=bool(reasons), reasons=tuple(reasons))


def image_has_build_work(image: ImageSpec) -> bool:
    return image_build_work_plan(image).has_work
