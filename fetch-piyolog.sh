#!/usr/bin/env bash
# Save the current Piyolog data-feed snapshot as unmodified JSON.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
env_file="$script_dir/.env"
archive_dir="$script_dir/data/piyolog"
lock_dir="$archive_dir/.fetch.lock"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required."
command -v jq >/dev/null 2>&1 || fail "jq is required."
[[ -f "$env_file" ]] || fail ".env was not found."

# Do not source .env: read only the values this script uses.
env_value() {
  sed -n "s/^$1=//p" "$env_file" | head -n 1
}

feed_url=$(env_value PIYOLOG_FEED_URL)
feed_url=${feed_url%$'\r'}
[[ -n "$feed_url" ]] || fail "PIYOLOG_FEED_URL is not set in .env."

# Proxy settings are optional.  Passing them only to curl also makes them work
# when this script is run by a systemd user service with a minimal environment.
curl_environment=()
for proxy_name in HTTPS_PROXY HTTP_PROXY NO_PROXY; do
  proxy_value=$(env_value "$proxy_name")
  proxy_value=${proxy_value%$'\r'}
  if [[ -n "$proxy_value" ]]; then
    curl_environment+=("$proxy_name=$proxy_value")
  fi
done

mkdir -p "$archive_dir"
if ! mkdir "$lock_dir" 2>/dev/null; then
  fail "another fetch is already running."
fi

tmp_file=$(mktemp "$archive_dir/.download.XXXXXX")
cleanup() {
  rm -f "$tmp_file"
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

env "${curl_environment[@]}" curl --fail --silent --show-error --location \
  --connect-timeout 10 --max-time 30 \
  --output "$tmp_file" \
  "$feed_url"

# Keep the response byte-for-byte intact, but reject malformed or unexpected data.
jq -e '
  (.schema_version == 1) and
  (.generated_at | type == "string") and
  (.range.from | type == "string") and
  (.range.to | type == "string") and
  (.records | type == "array")
' "$tmp_file" >/dev/null || fail "the response is not a valid Piyolog schema v1 feed."

fetched_at=$(date '+%Y-%m-%dT%H-%M-%S%z')
destination="$archive_dir/$fetched_at.json"
mv "$tmp_file" "$destination"

record_count=$(jq '.records | length' "$destination")
range_from=$(jq -r '.range.from' "$destination")
range_to=$(jq -r '.range.to' "$destination")
printf 'Saved %s (%s records; UTC range %s to %s)\n' \
  "$destination" "$record_count" "$range_from" "$range_to"
