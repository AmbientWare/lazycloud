"""End-to-end tests for LazyCloud API.

These tests require the full stack to be running:
    docker network create lazycloud
    uv run mk-up --fresh
    docker compose up -d

Run with: pytest src/lazycloud_api/tests/e2e/ -v
"""
