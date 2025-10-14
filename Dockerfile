# Second stage: build the python base image
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

RUN mkdir /src

# Install system dependencies, cron, and clean up in one layer to keep image small
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

# Set up flyctl environment so we can access flyctl from the container
ENV FLYCTL_INSTALL="/root/.fly"
ENV PATH="/root/.fly/bin:$PATH"

# copy uv requirements
COPY ./pyproject.toml ./
COPY ./uv.lock ./
COPY ./README.md ./

COPY ./src/lazycloud_api /src/lazycloud_api
COPY ./src/shared /src/shared

# Build the Python package using uv
RUN uv sync --no-dev

# set the working directory
WORKDIR /src/lazycloud_api

# Second stage - api
FROM builder AS api
CMD [ "uv", "run", "python", "main.py"]