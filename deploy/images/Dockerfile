# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

# Platform images: server, scheduler and agent. Build them with
# `docker buildx bake -f deploy/images/docker-bake.hcl` from the repository
# root. Every base is pinned by digest; VERSION is stamped into the binaries
# and SOURCE_DATE_EPOCH fixes file and image timestamps, so one commit builds
# the same bytes.

FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.27.1-trixie@sha256:3b77fc618ec235a1ab412de7737f120dd507c57e8d87de4cbb7994fb94275ed5 AS go
WORKDIR /src
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=-trimpath
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY cmd cmd
COPY internal internal
COPY migrations migrations
ARG VERSION=dev
ARG TARGETOS
ARG TARGETARCH
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    for binary in server scheduler agent supervisor; do \
      GOOS=$TARGETOS GOARCH=$TARGETARCH go build -buildvcs=false \
        -ldflags "-s -w -X main.version=${VERSION}" -o /out/$binary ./cmd/$binary || exit 1; \
    done

# GeeseFS mounts volumes on hosts; the script pins its release and digest.
FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.27.1-trixie@sha256:3b77fc618ec235a1ab412de7737f120dd507c57e8d87de4cbb7994fb94275ed5 AS geesefs
WORKDIR /src
COPY deploy/local/fetch-geesefs.sh deploy/local/fetch-geesefs.sh
RUN deploy/local/fetch-geesefs.sh

# Managed Python runtimes 3.10 to 3.14, which the agent bind-mounts into
# workload containers. Dependency versions come from uv.lock.
FROM --platform=$BUILDPLATFORM docker.io/library/python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtimes
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY python python
COPY deploy/local/build-runtime.sh deploy/local/build-runtime.sh
ARG SOURCE_DATE_EPOCH
ENV UV=/usr/local/bin/uv UV_PYTHON_DOWNLOADS=never
RUN --mount=type=cache,target=/root/.cache/uv deploy/local/build-runtime.sh /out/runtimes

FROM gcr.io/distroless/static-debian13:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3 AS server
ARG VERSION=dev
LABEL org.opencontainers.image.title="lazycloud-server" org.opencontainers.image.version="${VERSION}"
COPY --from=go /out/server /usr/local/bin/server
ENV LAZYCLOUD_HTTP_ADDR=0.0.0.0:8080 LAZYCLOUD_GRPC_ADDR=0.0.0.0:8081 LAZYCLOUD_HEALTH_ADDR=0.0.0.0:8090
EXPOSE 8080 8081 8090
ENTRYPOINT ["/usr/local/bin/server"]
CMD ["serve"]

FROM gcr.io/distroless/static-debian13:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3 AS scheduler
ARG VERSION=dev
LABEL org.opencontainers.image.title="lazycloud-scheduler" org.opencontainers.image.version="${VERSION}"
COPY --from=go /out/scheduler /usr/local/bin/scheduler
ENV LAZYCLOUD_HEALTH_ADDR=0.0.0.0:8090
EXPOSE 8090
ENTRYPOINT ["/usr/local/bin/scheduler"]

# The agent's release bundle. Hosts install /opt/lazycloud from it; the agent
# finds the supervisor and GeeseFS beside its executable and the runtimes in
# /opt/lazycloud/runtimes. It needs root and the host's Docker socket, and it
# passes these paths to Docker, so a containerized agent must see them at the
# same paths as the host.
FROM gcr.io/distroless/static-debian13@sha256:58133991db06659feaabe0f4e97a35cebf15ef4ea08f8a4c6d2ee5f75e4aa6a0 AS agent
ARG VERSION=dev
LABEL org.opencontainers.image.title="lazycloud-agent" org.opencontainers.image.version="${VERSION}"
COPY --from=go /out/agent /out/supervisor /opt/lazycloud/bin/
COPY --from=geesefs /src/bin/geesefs /opt/lazycloud/bin/geesefs
COPY --from=runtimes /out/runtimes /opt/lazycloud/runtimes
ENTRYPOINT ["/opt/lazycloud/bin/agent"]
