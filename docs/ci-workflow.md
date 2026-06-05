# CI workflow

The authoritative workflow is [`ci/test.yml`](../ci/test.yml). It lives there
instead of `.github/workflows/test.yml` until the repository token has
`workflow` scope. To enable it, follow [`docs/ci-setup.md`](ci-setup.md).

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
.venv-ha/bin/python -m pytest tests/test_api.py tests/test_integration.py -v --tb=short
.venv-ha/bin/python -m pytest -m e2e_config_flow tests/test_e2e_config_flow.py -v --tb=short
```

The bootstrap script requires Python 3.13 by default to match CI, and pins
Home Assistant plus `pytest-homeassistant-custom-component` to the same versions
as the workflow. The full loop is large because Home Assistant and its harness
pull a substantial dependency tree. CI is the normal place to run it.
