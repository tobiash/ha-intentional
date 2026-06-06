# CI setup for ha-intentional

The CI workflow lives in `ci/test.yml` instead of the conventional
`.github/workflows/test.yml` because the project's GitHub token
doesn't have the `workflow` scope. Granting that scope to the
gh CLI token is a one-time setup.

## One-time setup

Grant the gh CLI token the `workflow` scope:

```bash
gh auth refresh -h github.com -s workflow
```

This will prompt you to re-authenticate and request the additional
scope. After that, you can push workflow files.

## Enabling the workflow

Copy `ci/test.yml` to its proper location and push:

```bash
mkdir -p .github/workflows
cp ci/test.yml .github/workflows/test.yml
git add .github/workflows/test.yml
git commit -m "Enable CI workflow"
git push
```

The first run will take ~5 minutes (most of which is installing
the full Home Assistant package for integration tests).

## What the workflow does

The workflow runs on every push to `main` and every pull request:

1. **Lint** — `ruff check` on the engine, tests, and integration code
2. **Bundle sync** — verifies `src/intentional/` matches
   `custom_components/intentional/_engine/` (catch drift)
3. **Unit tests + static guards** — fast tests that don't need HA installed,
   including manifest/package consistency checks
4. **HACS smoke-load** — verifies the integration imports under a HACS-like
   `sys.path`
5. **Integration/API tests** — full HA test instance, exercises the
   integration end-to-end
6. **E2E config flow** — full HA config-flow tests in a separate pytest
   process to avoid HA translation-cache leakage

If anything fails, the workflow fails and you get a red ❌ on
the PR / commit.

## Why one Python version, not a matrix

The engine is pure Python with no C extensions, but the HA-backed integration
tests follow Home Assistant's supported interpreter. We test on Python 3.14
only because HA 2026.5+ requires Python 3.14.2 or newer.
A matrix would triple CI time for no real-world signal.

If a future change introduces C extensions, add a matrix at
that point.

## Local reproduction

The unit tests run without HA:

```bash
pip install -e ".[test]"
pytest tests/ -v
```

The integration tests need HA:

```bash
scripts/bootstrap-ha-test-venv.sh
scripts/run-ha-tests.sh
```

This requires Python 3.14 by default, matching CI. Set `PYTHON_BIN` if your
Python 3.14 executable is elsewhere. It also requires ~500MB of disk space for
the HA dependency tree. Most developers only run unit tests locally and let CI
handle the integration suite.

The HA-backed tests need local networking for the Home Assistant/aiohttp test
harness. If they are run from a network-isolated sandbox, the first test can
look stuck until a timeout kills it. Run `scripts/run-ha-tests.sh` with local
networking enabled; set `PYTEST_TIMEOUT=300` if a slow machine needs a longer
per-test timeout.

## Manual trigger

You can also trigger the workflow from the Actions tab with
`workflow_dispatch`. Useful for re-running after fixing a
transient failure.
