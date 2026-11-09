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

FROM golang:1.27.3-alpine@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS build
WORKDIR /src
COPY keeper/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/keeper . \
    && /out/keeper version

FROM gcr.io/distroless/static-debian13:nonroot@sha256:25ac8f0c3b2334a9eb05452266bd4697864a8c8ef0ef71e5e6480d7f92868329
COPY --from=build /out/keeper /keeper
# nonroot (65532), numeric so runAsNonRoot can verify it without a passwd lookup.
USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/keeper"]
