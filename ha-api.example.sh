#!/bin/bash
# ha-api — tiny Home Assistant REST client used by the bridge.
# Usage: ha-api METHOD PATH [JSON-DATA]
# The token lives in a mode-600 file and is passed to curl via stdin so
# it never appears in process arguments or normal output.
set +x
set -euo pipefail
fail() { printf 'ha-api: %s\n' "$1" >&2; exit 2; }
[[ $# -ge 2 && $# -le 3 ]] || fail 'Usage: ha-api METHOD PATH [JSON-DATA]'
[[ $1 =~ ^[A-Z]+$ ]] || fail 'METHOD must contain uppercase letters only'
[[ $2 == /* && $2 != //* ]] || fail 'PATH must be a local path starting with /'
HA_BASE_URL="${HA_BASE_URL:?set HA_BASE_URL, e.g. http://homeassistant.local:8123}"
token_file="${HA_TOKEN_FILE:-$HOME/.config/ha/token}"
[[ -f $token_file && ! -L $token_file && -r $token_file ]] || fail "Create $token_file first (mode 600)"
[[ $(stat -c %a "$token_file") == 600 ]] || fail 'Token file must have mode 600'
token="$(cat "$token_file")"
args=()
if [[ $# == 3 ]]; then
    args+=(--header 'Content-Type: application/json' --data-raw "$3")
fi
printf 'Authorization: Bearer %s\n' "$token" |
    /usr/bin/curl -q --silent --show-error --fail-with-body --globoff \
        --noproxy '*' --connect-timeout 5 --max-time 60 \
        --request "$1" --header @- "${args[@]}" \
        --url "${HA_BASE_URL}$2"
