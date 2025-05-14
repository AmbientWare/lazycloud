# Second stage: build the python base image
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

RUN mkdir /src

# Install system dependencies, cron, and clean up in one layer to keep image small
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    gcc \
    libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && curl -L https://fly.io/install.sh | sh

# Set up flyctl environment so we can access flyctl from the container
ENV FLYCTL_INSTALL="/root/.fly"
ENV PATH="/root/.fly/bin:$PATH"

# copy uv requirements
COPY ./pyproject.toml ./
COPY ./uv.lock ./
COPY ./README.md ./

COPY ./src/lazycloud_api /src/lazycloud_api

# Build the Python package using uv
RUN uv sync --locked

# set the working directory
WORKDIR /src/lazycloud_api

# Second stage - api
FROM builder AS api
CMD [ "uv", "run", "python", "main.py"]

# Second stage - celery worker
FROM builder AS celery-worker
CMD [ "uv", "run", "celery", "-A", "celery_app", "worker", "--loglevel=info"]

# Second stage - celery beat
FROM builder AS celery-beat
CMD [ "uv", "run", "celery", "-A", "celery_app", "beat", "--loglevel=info"]

