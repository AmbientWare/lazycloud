from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from examples.yolo_training import app as yolo_module
from examples.yolo_training.app import (
    artifact_path,
    predict_yolo,
    train_yolo,
    validate_run_name,
)


@pytest.mark.parametrize(
    "value",
    ["", "Uppercase", "nested/run", "../escape", ".hidden", "x" * 64],
)
def test_run_name_validation_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_run_name(value)


@pytest.mark.parametrize(
    "value",
    ["/absolute.pt", "../escape.pt", "a/../escape.pt", "a//file.pt", "a\\file.pt"],
)
def test_artifact_path_rejects_non_relative_or_ambiguous_paths(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yolo_module, "ARTIFACT_ROOT", tmp_path)

    with pytest.raises(ValueError):
        artifact_path(value)


def test_artifact_path_rejects_a_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    (artifact_root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(yolo_module, "ARTIFACT_ROOT", artifact_root)

    with pytest.raises(ValueError, match="escapes"):
        artifact_path("linked/checkpoint.pt")


def test_training_reports_persisted_checkpoint_and_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setattr(yolo_module, "ARTIFACT_ROOT", artifact_root)

    def run(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        run_path = artifact_root / "training" / "coco8-smoke"
        (run_path / "weights").mkdir(parents=True)
        (run_path / "weights" / "best.pt").write_bytes(b"checkpoint")
        (run_path / "results.csv").write_text("epoch,mAP50\n1,0.5\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(yolo_module.subprocess, "run", run)

    result = train_yolo.local()

    checkpoint = result["checkpoint"]
    metrics = result["metrics"]
    assert (artifact_root / checkpoint["path"]).read_bytes() == b"checkpoint"
    assert checkpoint["bytes"] == 10
    assert (artifact_root / metrics["path"]).read_text(encoding="utf-8") == "epoch,mAP50\n1,0.5\n"
    assert metrics["bytes"] == 18


def test_prediction_lists_persisted_outputs_for_the_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    checkpoint = artifact_root / "training" / "coco8-smoke" / "weights" / "best.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint")
    monkeypatch.setattr(yolo_module, "ARTIFACT_ROOT", artifact_root)

    def run(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        prediction = artifact_root / "predictions" / "bus-prediction"
        (prediction / "labels").mkdir(parents=True)
        (prediction / "bus.jpg").write_bytes(b"image")
        (prediction / "labels" / "bus.txt").write_text("0 0.5 0.5 0.2 0.2 0.9\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(yolo_module.subprocess, "run", run)

    result = predict_yolo.local()

    assert result["checkpoint"] == "training/coco8-smoke/weights/best.pt"
    assert [item["path"] for item in result["outputs"]] == [
        "predictions/bus-prediction/bus.jpg",
        "predictions/bus-prediction/labels/bus.txt",
    ]
