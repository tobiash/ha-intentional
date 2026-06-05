"""Manifest and packaging consistency tests.

These tests guard against the kind of bug we shipped in v0.1.1: forgetting
to bump ``manifest.json``'s ``version`` field when cutting a release.
HACS strict-mode refuses to load integrations whose declared version
doesn't match the released tag, and HA's config flow handler registration
silently fails — producing the dreaded "Invalid handler specified"
error when the user tries to add the integration.

Tests in this module:

- ``test_manifest_version_matches_latest_release``:
    Cross-checks ``custom_components/intentional/manifest.json`` against
    the latest git tag. Catches release-bump omissions.
- ``test_manifest_domain_matches_integration``:
    Sanity check: the manifest's ``domain`` must equal the folder name.
- ``test_manifest_required_fields``:
    Verifies the schema HA's loader expects is present.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MANIFEST_PATH = REPO_ROOT / "custom_components" / "intentional" / "manifest.json"


def _read_manifest() -> dict:
    with MANIFEST_PATH.open() as f:
        return json.load(f)


def _latest_semver_tag() -> str | None:
    """Return the most recent semver tag (e.g. '0.1.1') or None.

    Falls back to None if git is unavailable or no tags exist, which
    lets the test skip gracefully on fresh clones.
    """
    try:
        result = subprocess.run(
            ["git", "tag", "--sort=-version:refname"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    tags = [t.strip().lstrip("v") for t in result.stdout.splitlines() if t.strip()]
    semver = [t for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t)]
    return semver[0] if semver else None


def test_manifest_version_matches_latest_release() -> None:
    """manifest.json's version field must be at or above the latest semver tag.

    Catches the v0.1.0 / v0.1.1 mismatch that caused HACS strict-mode
    to refuse loading the integration and HA's config flow to fail
    with 'Invalid handler specified'.

    Invariant: ``manifest_version >= latest_tag``.

    - If manifest < latest_tag: someone released a tag without bumping
      the manifest first (the v0.1.1 bug). Test fails.
    - If manifest == latest_tag: everything is in sync. Test passes.
    - If manifest > latest_tag: a release is in progress (manifest
      bumped, tag not yet cut). This is the normal "bump then tag"
      workflow — test passes. The tag MUST be cut before the next
      release, otherwise HACS will load the wrong version.
    """
    manifest = _read_manifest()
    latest_tag = _latest_semver_tag()

    if latest_tag is None:
        # Fresh clone, no tags yet — skip rather than fail
        import pytest

        pytest.skip("No semver git tags found — skipping version cross-check")

    manifest_v = _semver_tuple(manifest["version"])
    tag_v = _semver_tuple(latest_tag)
    assert manifest_v >= tag_v, (
        f"manifest.json version is {manifest['version']!r} but the latest "
        f"git tag is {latest_tag!r}. The manifest is BEHIND the latest "
        f"tag — this is the v0.1.1 bug. Bump manifest.json to match the "
        f"latest tag, or HACS will refuse to load the integration."
    )


def _semver_tuple(version: str) -> tuple[int, int, int]:
    """Parse a strict 'MAJOR.MINOR.PATCH' string into a comparable tuple."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        # test_manifest_version_is_semver is the canonical guard for this;
        # if we're here it's because that test is broken, so don't double-fail.
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def test_manifest_domain_matches_integration() -> None:
    manifest = _read_manifest()
    assert manifest["domain"] == MANIFEST_PATH.parent.name


def test_manifest_required_fields() -> None:
    manifest = _read_manifest()
    required = {
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
        "requirements",
        "dependencies",
        "iot_class",
        "config_flow",
        "integration_type",
    }
    missing = required - set(manifest.keys())
    assert not missing, f"manifest.json is missing required fields: {missing}"


def test_manifest_version_is_semver() -> None:
    manifest = _read_manifest()
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), (
        f"manifest.json version {manifest['version']!r} is not a valid "
        f"semver string. HA expects MAJOR.MINOR.PATCH."
    )
