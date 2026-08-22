#!/usr/bin/env bash
# Buduje obraz i wypycha go do GitHub Container Registry (ghcr.io).
#
# Wymagania:
#   1. Ustaw GHCR_USER w pliku .env (nazwa użytkownika GitHub).
#   2. Zaloguj się do GHCR:
#        echo <TOKEN> | docker login ghcr.io -u <GHCR_USER> --password-stdin
#      gdzie <TOKEN> to PAT GitHub z zakresem write:packages.
#
# Użycie:
#   ./push.sh
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a

IMAGE="ghcr.io/${GHCR_USER}/betterposters:latest"

if [ -z "${GHCR_USER:-}" ]; then
  echo "Błąd: ustaw GHCR_USER w pliku .env (nazwa użytkownika GitHub)." >&2
  exit 1
fi

echo "Buduję ${IMAGE}..."
docker build -t "${IMAGE}" .

echo "Wypycham ${IMAGE}..."
docker push "${IMAGE}"

echo "Gotowe: ${IMAGE}"
