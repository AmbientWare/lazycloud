# Train a detector and reuse its checkpoint for prediction.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run lazycloud run app:train_yolo
uv run lazycloud run app:predict_yolo
```

This needs an L4 and account credit. The first call creates or reuses
`yolo-artifacts`. Results include checkpoint and prediction paths. Download
the prediction with:

```bash
uv run lazycloud cp lazycloud://yolo-artifacts/predictions/bus-prediction/bus.jpg ./bus-prediction.jpg
```

Run names cannot be reused. Choose new names for later trials. GPU time and
persistent storage are billed. Review Ultralytics licensing before commercial use.
Delete the volume only after saving checkpoints you need.

[Full guide](https://docs.lazycloud.dev/examples/train-yolo-object-detector)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.
