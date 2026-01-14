# Stock Dashboard

Real-time stock analytics dashboard. Built with FastAPI, HTMX, and Tailwind CSS.

## Architecture

```
┌─────────────────┐     ┌─────────────┐
│    FastAPI      │────▶│   Yahoo     │
│  + HTMX + TW    │     │   Finance   │
└─────────────────┘     └─────────────┘
```

## Quick Start

```bash
# Local
uv sync
uv run uvicorn main:app --reload

# Deploy
lazycloud init
lazycloud deploy
```

App runs at http://localhost:8000

## Features

- Search any stock ticker
- Real-time price and stats
- Interactive price charts
- No API key required (uses Yahoo Finance)

## Stack

- **FastAPI** - Python web framework
- **HTMX** - Dynamic updates without JS framework
- **Tailwind CSS** - Styling via CDN
- **Chart.js** - Price charts
- **yfinance** - Free stock data

See [LazyCloud docs](https://lazycloud.dev/docs) for more.
