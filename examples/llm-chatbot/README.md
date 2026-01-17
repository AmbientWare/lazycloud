# LLM Chatbot

A full-stack AI chatbot with streaming responses, demonstrating LazyCloud's key features: multi-service deployment, persistent volumes, and secret management.

## Architecture

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│ Next.js  │────▶│ FastAPI  │────▶│  OpenAI  │
│ Frontend │     │   API    │     │          │
└──────────┘     └────┬─────┘     └──────────┘
                      │
              ┌───────┴───────┐
              ▼               ▼
         ┌────────┐     ┌─────────┐
         │ SQLite │     │  Redis  │
         └────────┘     └─────────┘
```

## Quick Start

```bash
# Local
export OPENAI_API_KEY=sk-...
docker compose up --build

# Deploy
lazycloud init
lazycloud deploy
```

- Frontend: http://localhost:3000
- API: http://localhost:8000

## Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key (required) | - |
| `OPENAI_MODEL` | Model for completions | `gpt-4o-mini` |

## Project Structure

```
llm-chatbot/
├── compose.yaml      # Service definitions + LazyCloud labels
├── frontend/         # Next.js chat interface
└── api/              # FastAPI backend with SQLite + Redis
```

See [LazyCloud docs](https://lazycloud.dev/docs) for more.
