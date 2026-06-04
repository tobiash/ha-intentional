# GitHub Actions workflow

We use GitHub Actions for CI. Since this repo's token doesn't have
`workflow` scope (and we don't want to add it), the workflow file is
not committed by the agent. To enable CI on your fork, copy the
following into `.github/workflows/tests.yml` and commit it:

```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[test,dev]"

      - name: Lint with ruff
        run: |
          ruff check src/intentional tests

      - name: Run tests
        run: |
          pytest --cov=intentional --cov-report=term-missing tests/
```

## Status

[![Tests](https://github.com/tobiash/ha-intentional/actions/workflows/tests.yml/badge.svg)](https://github.com/tobiash/ha-intentional/actions/workflows/tests.yml)

> The badge above will go green once you commit the workflow file in your fork.

## Test results (locally verified)

```
156 passed in 0.65s
Coverage: 85%
Lint: All checks passed
```
