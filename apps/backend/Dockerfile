FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    gcc \
    libpq-dev \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && curl -L https://fly.io/install.sh | sh \
    && curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    && chmod 700 get_helm.sh \
    && ./get_helm.sh \
    && rm get_helm.sh \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws

ENV FLYCTL_INSTALL="/root/.fly"
ENV PATH="/root/.fly/bin:$PATH"

WORKDIR /workspace

# Copy workspace root files
COPY pyproject.toml uv.lock README.md ./

# Copy backend package
COPY apps/backend/ ./apps/backend/

# Copy shared packages
COPY packages/ ./packages/

# Install dependencies from backend directory
WORKDIR /workspace/apps/backend
RUN uv sync --no-dev

# API target
FROM builder AS api
CMD ["uv", "run", "--no-sync", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Background workers target (Prefect task workers)
FROM builder AS background-workers
CMD ["uv", "run", "--no-sync", "python", "-m", "backend.background_worker"]

# Cron workers target (Prefect flow workers)
FROM builder AS crons
CMD ["uv", "run", "--no-sync", "python", "-m", "backend.worker"]
