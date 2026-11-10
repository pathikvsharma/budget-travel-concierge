#!/usr/bin/env bash
# Runs offline pytest verification and executes `adk eval` against the golden dataset
# whenever GOOGLE_API_KEY or Vertex AI ADC is configured.
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
ADK_BIN="${ADK_BIN:-adk}"

if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
  ADK_BIN=".venv/bin/adk"
fi

echo "=== 1. Running TripPilot Pytest Suite (Tools, HITL, PII, Guardrails, Memory, Telemetry) ==="
"${PYTHON_BIN}" -m pytest -v

if [[ -n "${GOOGLE_API_KEY:-}" || "${GOOGLE_GENAI_USE_VERTEXAI:-FALSE}" == "TRUE" ]]; then
  echo "=== 2. Running Live ADK Golden Evaluation Suite ==="
  if ! "${PYTHON_BIN}" -c "import google.adk.evaluation.agent_evaluator" >/dev/null 2>&1; then
    echo "adk eval needs google-adk's eval extra: ${PYTHON_BIN} -m pip install -e \".[eval]\"" >&2
    exit 1
  fi
  # `adk eval` imports the agent by file path, so the repository root must be on
  # PYTHONPATH for the package's absolute `travel_concierge.*` imports to resolve.
  PYTHONPATH="${PWD}${PYTHONPATH:+:${PYTHONPATH}}" "${ADK_BIN}" eval \
    travel_concierge \
    eval/travel_concierge.test.json \
    --config_file_path=eval/test_config.json \
    --print_detailed_results
else
  echo "=== 2. Skipping live LLM adk eval (set GOOGLE_API_KEY or GOOGLE_GENAI_USE_VERTEXAI=TRUE to run live trajectories) ==="
fi
