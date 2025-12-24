# FastAPI Example

A simple FastAPI application ready to deploy with LazyCloud.

## Files

- `main.py` - FastAPI application with a hello endpoint and health check
- `pyproject.toml` - Python dependencies (managed with uv)
- `Dockerfile` - Container build instructions
- `compose.yaml` - Docker Compose configuration with LazyCloud labels

## Deploy

```bash
cd examples/fastapi
lazycloud init
lazycloud deploy
```

## Local Development

```bash
uv sync
uv run uvicorn main:app --reload
```

Then visit http://localhost:8000
