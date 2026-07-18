# CI workflow

The executable source of truth is
[`.github/workflows/test.yml`](../.github/workflows/test.yml). The copy at
[`ci/test.yml`](../ci/test.yml) is kept byte-for-byte identical for local
inspection and recovery.

## What CI Runs

CI installs the full test dependency set, including Home Assistant and
`pytest-homeassistant-custom-component`, then runs these gates:

1. `ruff check src/ tests/ custom_components/intentional/`
2. `python ci/check-bundle-sync.py`
3. Fast tests and static guards, excluding only HA-harness test files
4. HACS smoke-load test
5. HA integration/API tests
6. E2E config-flow tests in a separate pytest process

The e2e config-flow tests run separately because Home Assistant translation
state can leak between pytest modules in the HA custom-component harness.

## Local Commands

Fast local loop without Home Assistant:

```bash
ruff check src/intentional custom_components/intentional tests
python ci/check-bundle-sync.py
pytest -q
```

Full HA-backed loop:

```bash
scripts/bootstrap-ha-test-venv.sh
scripts/run-ha-tests.sh
```

The bootstrap script requires Python 3.14 by default to match CI, and pins
Home Assistant plus `pytest-homeassistant-custom-component` to the same versions
as the workflow. The full loop is large because Home Assistant and its harness
pull a substantial dependency tree. CI is the normal place to run it.

The HA-backed tests use Home Assistant's aiohttp test harness and need local
network access. If you run them from a sandboxed agent or container, allow
local networking; a network-isolated sandbox can make the first HA test appear
to hang. `scripts/run-ha-tests.sh` adds a per-test timeout so that real stalls
fail with diagnostics.
