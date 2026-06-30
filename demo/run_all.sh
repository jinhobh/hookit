#!/usr/bin/env bash
# run_all.sh — Run all four reliability demo scenarios in sequence.
#
# Prerequisites (run from the project root):
#   docker compose up -d postgres
#   alembic upgrade head
#   uvicorn app.main:app --reload   # in a separate terminal
#
# Then:
#   bash demo/run_all.sh
#
# Each scenario provisions its own project + API key + endpoint, starts a
# local receiver and worker subprocess, demonstrates the scenario, tears
# everything down, and prints PASSED or FAILED.
#
# Environment variables:
#   API_BASE_URL   Override the API endpoint (default: http://localhost:8000)

set -euo pipefail

cd "$(dirname "$0")/.."

API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
export API_BASE_URL

echo "=================================================================="
echo "  Reliable Webhook Platform — Demo Suite"
echo "  API: ${API_BASE_URL}"
echo "=================================================================="

PASSED=0
FAILED=0

run_scenario() {
    local module="$1"
    echo ""
    if python -m "${module}"; then
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
        echo "  [FAILED] ${module}" >&2
    fi
}

run_scenario demo.scenario_1_failure_backoff_deadletter
run_scenario demo.scenario_2_redrive
run_scenario demo.scenario_3_idempotency
run_scenario demo.scenario_4_crash_recovery

echo ""
echo "=================================================================="
echo "  Results: ${PASSED} passed, ${FAILED} failed"
echo "=================================================================="

if [ "${FAILED}" -gt 0 ]; then
    exit 1
fi
