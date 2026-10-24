#!/bin/sh
set -eu

expected_revision="${1:?usage: verify-deployment.sh <full-git-sha>}"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose_file="${script_directory}/compose.yaml"

compose_image() {
  service_name="$1"
  awk -v service="$service_name" '
    $0 == "  " service ":" { in_service = 1; next }
    in_service && /^  [A-Za-z0-9_-]+:$/ { exit }
    in_service && /^    image: / { sub(/^    image: /, ""); print; exit }
  ' "$compose_file"
}

expected_backend="$(compose_image backend)"
expected_frontend="$(compose_image frontend)"

case "$expected_backend" in
  *":${expected_revision}@sha256:"*) ;;
  *) echo "backend compose image is not pinned to revision $expected_revision" >&2; exit 1 ;;
esac

case "$expected_frontend" in
  *":${expected_revision}@sha256:"*) ;;
  *) echo "frontend compose image is not pinned to revision $expected_revision" >&2; exit 1 ;;
esac

actual_backend="$(docker inspect --format '{{.Config.Image}}' vova-medcenter-backend)"
actual_frontend="$(docker inspect --format '{{.Config.Image}}' vova-medcenter-frontend)"

if [ "$actual_backend" != "$expected_backend" ]; then
  echo "backend image mismatch: expected $expected_backend, got $actual_backend" >&2
  exit 1
fi

if [ "$actual_frontend" != "$expected_frontend" ]; then
  echo "frontend image mismatch: expected $expected_frontend, got $actual_frontend" >&2
  exit 1
fi

backend_status="$(curl -fsS https://vova-medcenter.ravil.space/api/v1/health)"
frontend_status="$(curl -fsS https://vova-medcenter.ravil.space/build.json)"

printf '%s' "$backend_status" | grep -Fq "\"build_revision\":\"${expected_revision}\"" || {
  echo "backend revision mismatch: expected $expected_revision" >&2
  exit 1
}

printf '%s' "$frontend_status" | grep -Fq "\"build_revision\":\"${expected_revision}\"" || {
  echo "frontend revision mismatch: expected $expected_revision" >&2
  exit 1
}

echo "vova-medcenter deployment verified at ${expected_revision}"
