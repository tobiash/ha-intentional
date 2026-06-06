#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="${VENV_DIR:-.venv-ha}"
PYTHON="${VENV_DIR}/bin/python"
PYTEST_TIMEOUT="${PYTEST_TIMEOUT:-180}"

if [ ! -x "${PYTHON}" ]; then
  cat >&2 <<EOF
${PYTHON} was not found.

Create the Home Assistant test environment first:
  scripts/bootstrap-ha-test-venv.sh
EOF
  exit 127
fi

run_pytest() {
  "${PYTHON}" -m pytest "$@" --timeout="${PYTEST_TIMEOUT}"
}

run_pytest tests/test_api.py tests/test_integration.py -v --tb=short
run_pytest -m e2e_config_flow tests/test_e2e_config_flow.py -v --tb=short
