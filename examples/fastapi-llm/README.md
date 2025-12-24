# FastAPI LLM Example

A FastAPI application with OpenAI integration, ready to deploy with LazyCloud.

## Files

- `main.py` - FastAPI app with `/chat` endpoint for LLM inference
- `pyproject.toml` - Python dependencies (managed with uv)
- `Dockerfile` - Container build instructions
- `compose.yaml` - Docker Compose configuration with LazyCloud labels

## Deploy

```bash
cd examples/fastapi-llm
lazycloud init
lazycloud deploy
```

Set your OpenAI API key:
```bash
lazycloud env set OPENAI_API_KEY=sk-...
```

## Usage

```bash
curl -X POST https://your-app.lazycloud.dev/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, how are you?"}'
```

## Local Development

```bash
export OPENAI_API_KEY=sk-...
uv sync
uv run uvicorn main:app --reload
```

Then visit http://localhost:8000
