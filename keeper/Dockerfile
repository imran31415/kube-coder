# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# keeper/Dockerfile
#
# The always-on keeper (#728, docs/always-on-keeper.md). One static Go binary,
# standard library only, on distroless: no shell, no package manager, nothing
# to patch but the binary itself. It runs one per workspace for the whole life
# of the workspace, so it has to stay small — #728's budget is ≤ 20 MiB idle.
#
# Published, signed and attested by .github/workflows/release.yml alongside
# devlaptop and provisioner. Build context is the repo root.

FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
WORKDIR /src
COPY keeper/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/keeper . \
    && /out/keeper version

FROM gcr.io/distroless/static-debian13:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3
COPY --from=build /out/keeper /keeper
# nonroot (65532), numeric so runAsNonRoot can verify it without a passwd lookup.
USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/keeper"]
