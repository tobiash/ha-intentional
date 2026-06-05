#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3.13}"
VENV_DIR="${VENV_DIR:-.venv-ha}"
HA_VERSION="${HA_VERSION:-2026.5.1}"
HA_TEST_HARNESS_VERSION="${HA_TEST_HARNESS_VERSION:-0.13.316}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  cat >&2 <<EOF
${PYTHON_BIN} was not found.

Install Python 3.13 or set PYTHON_BIN to a CI-compatible interpreter, for example:
  PYTHON_BIN=/path/to/python3.13 scripts/bootstrap-ha-test-venv.sh

Avoid Python 3.14 for this venv unless CI has moved too; HA-backed pytest
startup can hang or diverge on unsupported interpreter versions.
EOF
  exit 127
fi

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -e ".[all-tests]"
"${VENV_DIR}/bin/python" -m pip install \
  "homeassistant==${HA_VERSION}" \
  "pytest-homeassistant-custom-component==${HA_TEST_HARNESS_VERSION}"

cat <<EOF
Created ${VENV_DIR} with Home Assistant ${HA_VERSION}.

Useful checks:
  ${VENV_DIR}/bin/python -m pytest tests/test_api.py tests/test_integration.py -v --tb=short
  ${VENV_DIR}/bin/python -m pytest -m e2e_config_flow tests/test_e2e_config_flow.py -v --tb=short
EOF
