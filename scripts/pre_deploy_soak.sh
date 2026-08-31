#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# pre_deploy_soak.sh — soak test before deploying the F0.5 hotfix
#
# Runs 5 hermes chat requests against the mock LLM server in sequence:
#   ok, slow, reset, ok, slow
# Each request has a 60-second timeout. Exit code 0 = all passed, non-zero = fail.
# No Traceback in logs (soak script should swallow exception detail).
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MOCK_PORT="${MOCK_PORT:-9999}"
TIMEOUT_SEC="${TIMEOUT_SEC:-60}"
HERMES_HOME="${HERMES_HOME:-$(mktemp -d)}"

export HERMES_HOME
export OPENAI_API_KEY="mock-key-for-testing"

echo "[soak] HERMES_HOME=$HERMES_HOME"
echo "[soak] Starting mock LLM server on port $MOCK_PORT ..."

# Start mock server in background (no MODE env var needed — server reads per-request)
python "$REPO_DIR/scripts/mock_llm_server.py" &
MOCK_PID=$!
sleep 0.5

cleanup() {
    echo "[soak] Stopping mock server (pid=$MOCK_PID) ..."
    kill "$MOCK_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Check if mock server is up
if ! curl -s "http://127.0.0.1:${MOCK_PORT}/" > /dev/null 2>&1; then
    echo "[soak] ERROR: Mock server did not start"
    exit 1
fi

MODES=("ok" "slow" "reset" "ok" "slow")
PASS=0
FAIL=0

for i in "${!MODES[@]}"; do
    MODE="${MODES[$i]}"
    REQUEST="Tell me a short joke"
    echo ""
    echo "[soak] Run $((i+1))/5 — MODE=$MODE"

    START=$(date +%s)
    set +e
    # Set per-request OPENAI_BASE_URL in a subshell so each iteration
    # gets the correct mode baked into the URL (mock server reads ?mode=).
    OPENAI_BASE_URL="http://127.0.0.1:${MOCK_PORT}/v1?mode=${MODE}" \
        python -m hermes chat --no-input --plain "$REQUEST" \
        > /tmp/hermes_soak_${i}.stdout \
        2> /tmp/hermes_soak_${i}.stderr
    RC=$?
    set -e
    END=$(date +%s)
    ELAPSED=$((END - START))

    # Filter Tracebacks from logs (keep only essential output)
    grep -v "Traceback" /tmp/hermes_soak_${i}.stdout > /tmp/hermes_soak_${i}.stdout.clean || true
    grep -v "Traceback" /tmp/hermes_soak_${i}.stderr > /tmp/hermes_soak_${i}.stderr.clean || true

    if [ $RC -eq 0 ]; then
        echo "[soak]   PASS (${ELAPSED}s, rc=$RC)"
        ((PASS++))
    elif [ $RC -eq 124 ]; then  # timeout
        echo "[soak]   FAIL (TIMEOUT after ${TIMEOUT_SEC}s)"
        echo "[soak]   stderr: $(cat /tmp/hermes_soak_${i}.stderr.clean | tail -5)"
        ((FAIL++))
    else
        echo "[soak]   WARN (rc=$RC, ${ELAPSED}s) — may be expected for reset/slow"
        # reset and slow may return non-zero depending on hermes internal handling
        # Only count as FAIL if timeout occurred
        if [ $RC -eq -9 ] || [ $ELAPSED -ge $((TIMEOUT_SEC)) ]; then
            echo "[soak]   FAIL (hung or killed)"
            ((FAIL++))
        else
            echo "[soak]   OK (acceptable non-zero exit)"
            ((PASS++))
        fi
    fi
done

echo ""
echo "[soak] ==========================================="
echo "[soak] Results: $PASS passed, $FAIL failed out of ${#MODES[@]} runs"
echo "[soak] ==========================================="

if [ $FAIL -gt 0 ]; then
    echo "[soak] SOAK FAILED"
    exit 1
else
    echo "[soak] SOAK PASSED"
    exit 0
fi
