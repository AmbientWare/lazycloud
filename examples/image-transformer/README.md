# Image Transformer

Transform images into different artistic styles using AI. Built with TanStack Start and Replicate.

## Architecture

```
┌──────────────┐     ┌─────────────┐
│  TanStack    │────▶│  Replicate  │
│  Start       │     │  (Flux)     │
└──────┬───────┘     └─────────────┘
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
bun install
bun run dev

# Deploy
lazycloud init
lazycloud deploy
```

App runs at http://localhost:3000

## Styles

- **Studio Ghibli** - Soft anime aesthetic
- **Pixar 3D** - Animated character style
- **Watercolor** - Artistic brushstrokes
- **Comic Book** - Bold lines, cel shading
- **Pixel Art** - Retro 16-bit game style

## Configuration

| Environment Variable | Description |
|---------------------|-------------|
| `REPLICATE_API_TOKEN` | Replicate API token (required) |

Get your token at [replicate.com](https://replicate.com)

See [LazyCloud docs](https://lazycloud.dev/docs) for more.
