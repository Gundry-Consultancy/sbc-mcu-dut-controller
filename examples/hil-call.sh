#!/usr/bin/env bash
# Reference CI step: submit a HIL job and long-poll until terminal.
# Copy verbatim into a downstream repo's workflow, or invoke as-is via
# `bash <(curl ...) examples/hil-call.sh` from this repo.
#
# Required env (all set by the calling workflow):
#   HIL_API_BASE        Controller base URL, e.g. https://hil.example.lan
#   HIL_API_TOKEN       Per-repo bearer token, OR
#   HIL_OIDC_TOKEN      GitHub Actions OIDC token (preferred)
#   HIL_JOB_JSON        Path to a job submission body (see docs/ARCHITECTURE.md §7.1)
#
# Optional env:
#   HIL_WAIT_TIMEOUT    Per-poll timeout in seconds, default 300 (server caps at 600)
#   HIL_TOTAL_BUDGET    Wall-clock cap in seconds, default 1800
#
# Exit codes:
#   0   job finished, result=pass
#   1   job finished, result=fail
#   2   job ended in error
#   3   job timed out
#   4   job was cancelled
#   10+ local errors (auth, network, malformed response)

set -euo pipefail

: "${HIL_API_BASE:?HIL_API_BASE is required}"
: "${HIL_JOB_JSON:?HIL_JOB_JSON is required}"

WAIT_TIMEOUT="${HIL_WAIT_TIMEOUT:-300}"
TOTAL_BUDGET="${HIL_TOTAL_BUDGET:-1800}"

if [[ -n "${HIL_OIDC_TOKEN:-}" ]]; then
    auth_header="Authorization: Bearer ${HIL_OIDC_TOKEN}"
elif [[ -n "${HIL_API_TOKEN:-}" ]]; then
    auth_header="Authorization: Bearer ${HIL_API_TOKEN}"
else
    echo "Neither HIL_OIDC_TOKEN nor HIL_API_TOKEN is set" >&2
    exit 10
fi

submit() {
    curl --fail-with-body --silent --show-error \
        -X POST "${HIL_API_BASE}/v1/jobs" \
        -H "${auth_header}" \
        -H "Content-Type: application/json" \
        --data-binary @"${HIL_JOB_JSON}"
}

wait_once() {
    local job_id="$1" since="$2"
    curl --fail-with-body --silent --show-error \
        "${HIL_API_BASE}/v1/jobs/${job_id}/wait?since=${since}&timeout=${WAIT_TIMEOUT}" \
        -H "${auth_header}"
}

response=$(submit)
job_id=$(printf '%s' "$response" | jq -er '.id')
echo "Submitted job ${job_id}"

since=0
deadline=$(( $(date +%s) + TOTAL_BUDGET ))

while (( $(date +%s) < deadline )); do
    chunk=$(wait_once "${job_id}" "${since}")
    since=$(printf '%s' "$chunk" | jq -er '.next_since')
    state=$(printf '%s' "$chunk" | jq -er '.state')

    # Stream any new events to the workflow log.
    printf '%s' "$chunk" | jq -r '.events[]? | "\(.at)\t\(.kind)\t\(.payload | tostring)"'

    case "$state" in
        finished)
            result=$(printf '%s' "$chunk" | jq -er '.result')
            echo "Job ${job_id} finished: ${result}"
            case "$result" in
                pass)      exit 0 ;;
                fail)      exit 1 ;;
                *)         exit 2 ;;
            esac
            ;;
        error)      echo "Job ${job_id} entered error state"; exit 2 ;;
        timeout)    echo "Job ${job_id} reported timeout";    exit 3 ;;
        cancelled)  echo "Job ${job_id} was cancelled";       exit 4 ;;
    esac
done

echo "Local wall-clock budget (${TOTAL_BUDGET}s) exhausted before job ${job_id} reached a terminal state" >&2
exit 3
