# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

# Platform images: server, scheduler, web and agent. Build them with
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

# The agent release archive hosts install, laid out as
# deploy/agent/build-bundle.sh lays it out, with fixed owners, order and
# times so one commit archives the same bytes.
FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.27.1-trixie@sha256:3b77fc618ec235a1ab412de7737f120dd507c57e8d87de4cbb7994fb94275ed5 AS agent-archive
ARG VERSION=dev
ARG SOURCE_DATE_EPOCH=0
COPY --from=go /out/agent /stage/lazycloud-agent
COPY --from=go /out/supervisor /stage/supervisor
COPY --from=geesefs /src/bin/geesefs /stage/geesefs
COPY --from=runtimes /out/runtimes /stage/runtime
RUN mkdir -p /out/${VERSION} && \
    tar -C /stage --sort=name --mtime=@${SOURCE_DATE_EPOCH} --owner=0 --group=0 --numeric-owner \
      --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
      -cf - lazycloud-agent supervisor runtime geesefs | gzip -n -9 >/out/${VERSION}/lazycloud-agent-linux-amd64.tar.gz

# The server serves the agent archive of its own version at /install/agent;
# the chart's post-sync Job publishes it as the hosts' target release.
FROM gcr.io/distroless/static-debian13:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3 AS server
ARG VERSION=dev
LABEL org.opencontainers.image.title="lazycloud-server" org.opencontainers.image.version="${VERSION}"
COPY --from=go /out/server /usr/local/bin/server
COPY --from=agent-archive /out /opt/lazycloud/agent-dist
ENV LAZYCLOUD_HTTP_ADDR=0.0.0.0:8080 LAZYCLOUD_GRPC_ADDR=0.0.0.0:8081 LAZYCLOUD_HEALTH_ADDR=0.0.0.0:8090 \
    LAZYCLOUD_AGENT_DIST_DIR=/opt/lazycloud/agent-dist
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

# The dashboard: the web build's static files, served by unprivileged nginx
# on port 8080. Bun runs the build as in CI.
FROM --platform=$BUILDPLATFORM docker.io/oven/bun:1.3.12-debian@sha256:1b709c9dd883fc1af38c210f7ea5222c552a8d470ea73efbd4b8fcfee798a64b AS web-build
WORKDIR /src/web
COPY web/package.json web/bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache bun install --frozen-lockfile
COPY web ./
RUN bun run build

FROM docker.io/nginxinc/nginx-unprivileged:1.30-alpine@sha256:ed04ec1ff34502c339ee5c3ae3f855442398edc1d05591e2b98981dcbbd20b1e AS web
ARG VERSION=dev
LABEL org.opencontainers.image.title="lazycloud-web" org.opencontainers.image.version="${VERSION}"
COPY deploy/images/web.conf /etc/nginx/conf.d/default.conf
COPY --from=web-build /src/web/dist/client /usr/share/nginx/html
EXPOSE 8080

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
