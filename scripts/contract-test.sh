#!/bin/sh
# Run the hyvv /v1 contract tests. In-process by default; set HYVV_BASE_URL and
# HYVV_TOKEN_FILE (or pass --base-url/--token-file) to test a live host read-only.
set -eu
cd "$(dirname "$0")/.."
exec uv run python -m contract "$@"
