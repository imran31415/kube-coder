# syntax=docker/dockerfile:1.28@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# keeper/Dockerfile
#
# The always-on keeper (#728, docs/always-on-keeper.md). One static Go binary,
# standard library only, on distroless: no shell, no package manager, nothing
# to patch but the binary itself. It runs one per workspace for the whole life
# of the workspace, so it has to stay small — #728's budget is ≤ 20 MiB idle.
#
# Published, signed and attested by .github/workflows/release.yml alongside
# devlaptop and provisioner. Build context is the repo root.

FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
WORKDIR /src
COPY keeper/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/keeper . \
    && /out/keeper version

FROM gcr.io/distroless/static-debian13:nonroot@sha256:8ce6dd87e9823c5b48dcfb2c8604714ccb351ce59607327bcc4957448da279b5
COPY --from=build /out/keeper /keeper
# nonroot (65532), numeric so runAsNonRoot can verify it without a passwd lookup.
USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/keeper"]
