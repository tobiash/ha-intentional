#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3.14}"
VENV_DIR="${VENV_DIR:-.venv-ha}"
HA_VERSION="${HA_VERSION:-2026.6.0}"
HA_TEST_HARNESS_VERSION="${HA_TEST_HARNESS_VERSION:-0.13.336}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  cat >&2 <<EOF
${PYTHON_BIN} was not found.

Install Python 3.14 or set PYTHON_BIN to a CI-compatible interpreter, for example:
  PYTHON_BIN=/path/to/python3.14 scripts/bootstrap-ha-test-venv.sh

Home Assistant 2026.5+ requires Python 3.14.2 or newer.
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
  scripts/run-ha-tests.sh
EOF
