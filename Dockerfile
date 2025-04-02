# Second stage: build the python base image
FROM python:3.11-slim AS builder

# make the machines directory
RUN mkdir /machines

# Set non-interactive frontend during docker build
ENV DEBIAN_FRONTEND=noninteractive

# Set python/poetry environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV POETRY_NO_INTERACTION 1
# Disable poetry's virtual environment creation
ENV POETRY_VENV_CREATE false

# Install system dependencies, cron, and clean up in one layer to keep image small
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    gcc \
    libpq-dev \
    && curl -sSL https://install.python-poetry.org | python3 - \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && curl -L https://fly.io/install.sh | sh

# Set up flyctl environment so we can access flyctl from the container
ENV FLYCTL_INSTALL="/root/.fly"
ENV PATH="/root/.fly/bin:$PATH"

# Ensure Poetry and any globally installed packages' bins are in PATH
ENV PATH="/root/.local/bin:$PATH"

# copy the main directory
COPY machines/ /machines

# copy over poetry and pyproject.toml related files
COPY ./pyproject.toml .
COPY ./poetry.lock .
COPY ./README.md .

# Build the Python package using poetry
RUN poetry config virtualenvs.create false && poetry install --only main

WORKDIR /machines

# Activate the virtual environment
ENV PYTHONPATH "${PYTHONPATH}:/"

# Second stage - api
FROM builder AS api
CMD ["/bin/bash", "-c", "source /root/.bashrc && python main.py"]

