# Archived community binaries/images are no longer publicly distributed.
# Build the pinned AGPL community releases for isolated development only.
FROM golang:1.24-bookworm AS minio-build
WORKDIR /src
RUN git clone --depth 1 --branch RELEASE.2025-09-07T16-13-09Z https://github.com/minio/minio.git .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOMAXPROCS=2 go build -p 2 -trimpath -o /out/minio .

FROM golang:1.24-bookworm AS mc-build
WORKDIR /src
RUN git clone --depth 1 --branch RELEASE.2025-08-13T08-35-41Z https://github.com/minio/mc.git .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOMAXPROCS=2 go build -p 2 -trimpath -o /out/mc .

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

FROM runtime AS minio
COPY --from=minio-build /out/minio /usr/local/bin/minio
ENTRYPOINT ["minio"]

FROM runtime AS mc
COPY --from=mc-build /out/mc /usr/local/bin/mc
ENTRYPOINT ["mc"]
