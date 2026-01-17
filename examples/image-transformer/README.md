# Image Transformer

An AI-powered image style transformer using Replicate's FLUX model, demonstrating LazyCloud's persistent storage for images and SQLite database tracking.

## Architecture

```
┌──────────────┐     ┌───────────┐
│   TanStack   │────▶│ Replicate │
│   Frontend   │     │   (FLUX)  │
└──────┬───────┘     └───────────┘
       │
       ▼
  ┌──────────┐
  │  SQLite  │
  └──────────┘
```

## Quick Start

```bash
# Local
export REPLICATE_API_TOKEN=r8_...
docker compose up --build

# Deploy
lazycloud init
lazycloud deploy
```

App: http://localhost:3000

## Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `REPLICATE_API_TOKEN` | Replicate API token (required) | - |
| `DATA_DIR` | Directory for SQLite DB and images | `/app/data` |

## Features

- **Multiple Styles** - Ghibli, Pixar, Watercolor, Comic, Pixel Art
- **Transformation History** - SQLite database tracks all transformations
- **Persistent Storage** - Original and transformed images saved to volume
- **Gallery View** - Browse and manage past transformations

## Project Structure

```
image-transformer/
├── compose.yaml      # Service definition + LazyCloud volume labels
├── Dockerfile        # Bun-based multi-stage build
└── src/
    ├── routes/       # TanStack Router pages + API
    │   ├── index.tsx     # Main transform UI
    │   ├── gallery.tsx   # Transformation history
    │   └── api/images/   # Image serving endpoint
    ├── components/   # UI components
    └── lib/
        ├── db.ts         # SQLite operations
        ├── storage.ts    # Image file storage
        └── replicate.ts  # Replicate API
```

See [LazyCloud docs](https://lazycloud.dev/docs) for more.
