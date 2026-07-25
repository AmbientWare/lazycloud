"""Train and use a small Ultralytics YOLO object detector on an L4 GPU.

The default training run uses the eight-image COCO8 dataset for one epoch. Both
functions persist their outputs in ``yolo-artifacts`` so checkpoints and
predictions survive container replacement and deployment deletion.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict

from lazycloud import App, GpuType, Image, Volume

ULTRALYTICS_IMAGE = "ultralytics/ultralytics:8.4.101"
MODEL = "yolo26n.pt"
DEFAULT_DATASET = "coco8.yaml"
DEFAULT_SOURCE = "https://ultralytics.com/images/bus.jpg"

ARTIFACT_ROOT = Path("/artifacts")
ARTIFACT_VOLUME_NAME = "yolo-artifacts"
artifact_volume = Volume(ARTIFACT_VOLUME_NAME, str(ARTIFACT_ROOT))

image = Image.from_registry(ULTRALYTICS_IMAGE).with_envs(
    {
        "PYTHONUNBUFFERED": "1",
        "YOLO_CONFIG_DIR": "/tmp/ultralytics-config",
    }
)
app = App("yolo_training")

_RUN_NAME = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62})\Z")


class ArtifactResult(TypedDict):
    path: str
    bytes: int


class TrainingResult(TypedDict):
    run_name: str
    model: str
    dataset: str
    epochs: int
    checkpoint: ArtifactResult
    metrics: ArtifactResult


class PredictionResult(TypedDict):
    training_run: str
    prediction_run: str
    source: str
    checkpoint: str
    outputs: list[ArtifactResult]


def validate_run_name(value: str) -> str:
    """Return a bounded run name that cannot create nested artifact paths."""
    if _RUN_NAME.fullmatch(value) is None:
        raise ValueError(
            "run names must be 1-63 lowercase letters, digits, underscores, or hyphens"
        )
    return value


def artifact_path(value: str, *, must_exist: bool = False) -> Path:
    """Resolve a strict POSIX-relative path beneath the mounted artifact root."""
    if not value or len(value) > 240 or "\\" in value or "\x00" in value:
        raise ValueError("artifact paths must be non-empty POSIX-relative paths")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("artifact paths cannot contain empty, dot, or parent segments")
    relative = PurePosixPath(value)
    if relative.is_absolute():
        raise ValueError("artifact paths must be relative")

    root = ARTIFACT_ROOT.resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("artifact path escapes the mounted volume")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    return candidate


def _new_run_path(group: str, run_name: str) -> Path:
    path = artifact_path(f"{group}/{validate_run_name(run_name)}")
    if path.exists():
        raise FileExistsError(f"artifact run already exists: {path.relative_to(ARTIFACT_ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _artifact_result(path: Path) -> ArtifactResult:
    if not path.is_file():
        raise RuntimeError(f"expected YOLO artifact was not created: {path}")
    return {
        "path": path.relative_to(ARTIFACT_ROOT).as_posix(),
        "bytes": path.stat().st_size,
    }


@app.function(
    name="train-yolo",
    image=image,
    cpu=4,
    memory="16Gi",
    gpu=GpuType.L4,
    gpu_count=1,
    timeout_seconds=3600,
    retries=0,
    volumes=[artifact_volume],
)
def train_yolo(
    run_name: str = "coco8-smoke",
    epochs: int = 1,
    dataset_artifact: str | None = None,
) -> TrainingResult:
    """Train YOLO26n and persist the best checkpoint and metrics CSV."""
    if not 1 <= epochs <= 300:
        raise ValueError("epochs must be between 1 and 300")
    run_path = _new_run_path("training", run_name)
    dataset = (
        DEFAULT_DATASET
        if dataset_artifact is None
        else str(artifact_path(dataset_artifact, must_exist=True))
    )

    subprocess.run(
        [
            "yolo",
            "detect",
            "train",
            f"model={MODEL}",
            f"data={dataset}",
            f"epochs={epochs}",
            "imgsz=640",
            "batch=8",
            "device=0",
            "workers=0",
            f"project={run_path.parent}",
            f"name={run_path.name}",
            "exist_ok=False",
            "plots=True",
        ],
        check=True,
    )

    return {
        "run_name": run_name,
        "model": MODEL,
        "dataset": DEFAULT_DATASET if dataset_artifact is None else dataset_artifact,
        "epochs": epochs,
        "checkpoint": _artifact_result(run_path / "weights" / "best.pt"),
        "metrics": _artifact_result(run_path / "results.csv"),
    }


@app.function(
    name="predict-yolo",
    image=image,
    cpu=2,
    memory="8Gi",
    gpu=GpuType.L4,
    gpu_count=1,
    timeout_seconds=900,
    retries=0,
    volumes=[artifact_volume],
)
def predict_yolo(
    training_run: str = "coco8-smoke",
    prediction_run: str = "bus-prediction",
    source_artifact: str | None = None,
) -> PredictionResult:
    """Load a persisted checkpoint and save annotated prediction artifacts."""
    training_name = validate_run_name(training_run)
    prediction_path = _new_run_path("predictions", prediction_run)
    checkpoint = artifact_path(
        f"training/{training_name}/weights/best.pt",
        must_exist=True,
    )
    source = (
        DEFAULT_SOURCE
        if source_artifact is None
        else str(artifact_path(source_artifact, must_exist=True))
    )

    subprocess.run(
        [
            "yolo",
            "detect",
            "predict",
            f"model={checkpoint}",
            f"source={source}",
            "imgsz=640",
            "conf=0.25",
            "device=0",
            f"project={prediction_path.parent}",
            f"name={prediction_path.name}",
            "exist_ok=False",
            "save=True",
            "save_txt=True",
            "save_conf=True",
        ],
        check=True,
    )

    outputs = [
        _artifact_result(path) for path in sorted(prediction_path.rglob("*")) if path.is_file()
    ]
    if not outputs:
        raise RuntimeError(f"YOLO did not create prediction artifacts in {prediction_path}")
    return {
        "training_run": training_run,
        "prediction_run": prediction_run,
        "source": DEFAULT_SOURCE if source_artifact is None else source_artifact,
        "checkpoint": checkpoint.relative_to(ARTIFACT_ROOT).as_posix(),
        "outputs": outputs,
    }


__all__ = [
    "ARTIFACT_VOLUME_NAME",
    "DEFAULT_DATASET",
    "DEFAULT_SOURCE",
    "MODEL",
    "ULTRALYTICS_IMAGE",
    "app",
    "artifact_path",
    "predict_yolo",
    "train_yolo",
    "validate_run_name",
]
