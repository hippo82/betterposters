#!/usr/bin/env bash
# Builds the image and pushes it to GitHub Container Registry (ghcr.io).
#
# Requirements:
#   1. Set GHCR_USER in .env (your GitHub username).
#   2. Log in to GHCR:
#        echo <TOKEN> | docker login ghcr.io -u <GHCR_USER> --password-stdin
#      where <TOKEN> is a GitHub PAT with the write:packages scope.
#
# Usage:
#   ./push.sh
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a

IMAGE="ghcr.io/${GHCR_USER}/betterposters:latest"

if [ -z "${GHCR_USER:-}" ]; then
  echo "Error: set GHCR_USER in .env (your GitHub username)." >&2
  exit 1
fi

echo "Building ${IMAGE}..."
docker build -t "${IMAGE}" .

echo "Pushing ${IMAGE}..."
docker push "${IMAGE}"

echo "Done: ${IMAGE}"
