#!/usr/bin/env bash
# Run the Hermes Agent container pointed at Nebius Token Factory.
#
# The API key is read from the repo .env and handed to the container through the
# process environment (docker run -e NAME, no value), so it never appears in a
# command line, a process list, or a log.
#
# Usage:
#   spike/hermes/hermes.sh -z "Reply with exactly: HERMES OK" -m <model> --provider custom
#   spike/hermes/hermes.sh --help
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MODEL="${HERMES_MODEL:-nvidia/nemotron-3-super-120b-a12b}"
BASE_URL="${NEMOTRON_BASE_URL:-https://api.tokenfactory.nebius.com/v1}"

# Read the key out of .env without echoing it anywhere.
KEY="$(grep -E '^NEBIUS_API_KEY=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')"
if [ -z "$KEY" ]; then
  echo "error: NEBIUS_API_KEY is empty in $ROOT/.env" >&2
  exit 2
fi

export OPENAI_API_KEY="$KEY"
export OPENAI_BASE_URL="$BASE_URL"

mkdir -p "$HERE/data"

exec docker run --rm -i \
  -v "$HERE/data:/opt/data" \
  -e OPENAI_API_KEY -e OPENAI_BASE_URL \
  nousresearch/hermes-agent:latest \
  "$@"
