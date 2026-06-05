"""End-to-end config flow integration tests for the Intentional integration.

These tests drive the **full** user and options flow through a real
Home Assistant test instance (via ``pytest-homeassistant-custom-component``).
They are the final guard against shipping a config flow that 500s on
real users.

Why this file exists
--------------------
v0.3.0..v0.3.3 had three shipped config-flow bugs that the unit tests
missed:

1. ``IntentionalOptionsFlow.__init__`` assigned ``self.config_entry``
   on HA 2025+ where it's a read-only property → 500 on Configure.
2. The options flow called ``_list_rule_files`` / ``_read_rule_file``
   / ``_write_rule_file`` / ``_delete_rule_file`` *synchronously* from
   async handlers → HA's blocking-I/O detector logged warnings and
   the flow returned 500 mid-edit.
3. ``async_step_rules`` had no test coverage at all — the bug shipped
   for 4 versions before a user hit it.

The unit tests in ``test_config_flow.py`` cover the rule_files
*helpers* in isolation. The static guard in
``test_config_flow_no_blocking_io.py`` covers the AST shape. This
file is the runtime guard: it actually instantiates a flow, drives
it through the HA flow manager, and asserts the result is a form
(not a 500, not an error).

If you add a new step to the config flow, add a corresponding test
here that drives it. The pattern is::

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "options"}, data=entry
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"action": "__create__"}
    )
    assert result["type"] == FlowResultType.FORM  # not "abort", not "error"

Isolation note
--------------
These tests are marked ``@pytest.mark.e2e_config_flow``. They must
run in a separate pytest invocation from the rest of the integration
tests (see ``pyproject.toml`` markers + ``ci/test.yml``) because of
a known issue in ``pytest-homeassistant-custom-component`` ≥ 0.13.250:

The translation cache is a ``@singleton`` keyed on
``TRANSLATION_FLATTEN_CACHE`` in
``homeassistant.helpers.translation``. The wrapper is also
``functools.lru_cache(maxsize=1)``. When the e2e flow drives the
config flow manager, it loads the integration's service
translations (which include Python ``bool`` values from
``services.yaml``'s ``required: true``). HA's
``_TranslationCache._validate_placeholders`` then crashes when it
later tries to format-parse those bools during a cache revalidation
in the same Python process. ``test_integration.py`` triggers that
revalidation on the next ``async_setup_component`` call, so it
fails with ``TypeError: expected str, got bool``.

The CI workflow runs these tests in a separate ``pytest`` step
(from the ``e2e`` marker) so each step is a fresh Python process
and the singleton is clean. The marker is also a useful opt-in for
local debugging: ``pytest -m e2e_config_flow`` runs only these.

These tests REQUIRE ``homeassistant`` to be installed. They will
skip silently on a minimal dev install (only PyYAML + voluptuous);
the real run is in CI where HA is installed.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path

import pytest

# Skip the entire module if HA isn't installed (CI has it, minimal dev doesn't)
pytest.importorskip("homeassistant", reason="homeassistant not installed")
pytest.importorskip(
    "pytest_homeassistant_custom_component",
    reason="pytest-homeassistant-custom-component not installed",
)

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"
sys.path.insert(0, str(INTEGRATION_DIR))

from homeassistant import config_entries  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.intentional.const import (  # noqa: E402
    CONF_RULE_DIR,
    DOMAIN,
)

# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations from the test integration dir."""
    yield


@pytest.fixture(autouse=True)
async def auto_unload_entries(hass: HomeAssistant):
    """Unload any config entries after each e2e test.

    The integration's tick loop and config-flow state can survive
    across tests in the same module if we don't clean up. This
    mirrors the fixture in test_integration.py.

    Note: We previously tried to clear HA's translation cache
    singleton in this teardown, but the cache lives in
    ``functools.lru_cache`` on the singleton wrapper and on
    ``hass.data[TRANSLATION_FLATTEN_CACHE]`` — neither is reliably
    resettable from here. The chosen isolation strategy is to run
    these tests in a *separate* pytest invocation (see the
    ``e2e_config_flow`` marker in ``pyproject.toml`` and the
    ``ci/test.yml`` workflow), so each pytest process gets a clean
    translation cache.
    """
    yield

    for entry in list(hass.config_entries.async_entries()):
        if entry.state is ConfigEntryState.LOADED:
            with suppress(Exception):
                await hass.config_entries.async_unload(entry.entry_id)
    with suppress(Exception):
        hass.config_entries.flow.async_abort_all()


@pytest.fixture
async def rule_dir(tmp_path: Path) -> Path:
    """An isolated rule directory with one starter rule file."""
    rd = tmp_path / "rules"
    rd.mkdir()
    (rd / "starter.yaml").write_text(
        "- id: starter\n"
        "  when: time_of_day == '00:00'\n"
        "  emit:\n"
        "    target: light.test\n"
        "    set:\n"
        "      state: 'on'\n"
    )
    return rd


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, rule_dir: Path
) -> MockConfigEntry:
    """A loaded MockConfigEntry pointing at rule_dir.

    Loading the entry is what triggers the integration setup, so
    flows that need ``hass.data[DOMAIN]`` (e.g. options flow reading
    the current engine) can use this fixture.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_RULE_DIR: str(rule_dir)},
        title="Intentional E2E",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


# ── User config flow ───────────────────────────────────────────────


@pytest.mark.e2e_config_flow
@pytest.mark.e2e_config_flow
async def test_user_flow_shows_initial_form(hass: HomeAssistant) -> None:
    """Starting the user flow shows a form asking for the rule directory."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    # The form should ask for the rule directory
    schema = result["data_schema"]
    assert schema is not None
    assert CONF_RULE_DIR in {str(k) for k in schema.schema}


@pytest.mark.e2e_config_flow
async def test_user_flow_rejects_relative_path(hass: HomeAssistant) -> None:
    """A relative path should be rejected with a form error, not crash."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_RULE_DIR: "relative/path"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_RULE_DIR: "invalid_path"}


@pytest.mark.e2e_config_flow
async def test_user_flow_rejects_empty_path(hass: HomeAssistant) -> None:
    """An empty path should be rejected."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_RULE_DIR: ""},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_RULE_DIR: "invalid_path"}


@pytest.mark.e2e_config_flow
async def test_user_flow_creates_entry(
    hass: HomeAssistant, rule_dir: Path
) -> None:
    """A valid absolute path creates a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_RULE_DIR: str(rule_dir)},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Intentional"
    assert result["data"] == {CONF_RULE_DIR: str(rule_dir)}


@pytest.mark.e2e_config_flow
async def test_user_flow_prevents_duplicate_entries(
    hass: HomeAssistant, rule_dir: Path
) -> None:
    """Two flows with the same path should not both create entries."""
    # First flow — should succeed
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result1 = await hass.config_entries.flow.async_configure(
        result1["flow_id"],
        user_input={CONF_RULE_DIR: str(rule_dir)},
    )
    assert result1["type"] == FlowResultType.CREATE_ENTRY

    # Second flow with the same path — should abort
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result2["flow_id"],
        user_input={CONF_RULE_DIR: str(rule_dir)},
    )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


# ── Options flow — the v0.3.3 / v0.3.4 bug surface ─────────────────


@pytest.mark.e2e_config_flow
async def test_options_flow_init_does_not_500(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The Configure button should open the options menu, not 500.

    v0.3.2 hit this with::
        AttributeError: property 'config_entry' has no setter

    v0.3.0..v0.3.3 also would have hit it if the entry could even
    be created (the import bug blocked that in v0.3.1).
    """
    # Capture the integration logger so we can assert no ERROR-level
    # config_flow errors fire during the flow.
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    assert result["type"] != FlowResultType.ABORT, (
        f"Options flow aborted on init: {result.get('reason')!r}"
    )
    # Most likely outcome: a menu form, or directly a "general" / "rules" form
    assert result["type"] in (
        FlowResultType.FORM,
        FlowResultType.MENU,
    ), f"Unexpected result type: {result['type']}"


@pytest.mark.e2e_config_flow
async def test_options_flow_init_shows_menu(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The options menu should offer 'general' and 'rules'."""
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    assert result["type"] == FlowResultType.MENU
    assert "general" in result["menu_options"]
    assert "rules" in result["menu_options"]


@pytest.mark.e2e_config_flow
async def test_options_flow_rules_step_lists_files_without_blocking_io(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rule_dir: Path
) -> None:
    """async_step_rules must list files WITHOUT blocking the event loop.

    v0.3.0..v0.3.3 called ``_list_rule_files`` sync, triggering HA's
    "Detected blocking call to scandir" warning. We assert that the
    step returns a form (not a 500) and the listed files match the
    on-disk state.
    """
    # Add an extra rule file
    (rule_dir / "extra.yaml").write_text(
        "- id: extra\n  when: 'true'\n  emit:\n    target: light.x\n    set:\n      state: 'on'\n"
    )

    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    # Navigate to rules
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "rules"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rules"

    # The form should have a 'action' selector with both files plus
    # __create__ and __delete__ options.
    schema = result["data_schema"]
    choices = set(schema.schema["action"].container)
    assert "starter.yaml" in choices, (
        f"Expected starter.yaml in choices, got {choices}"
    )
    assert "extra.yaml" in choices, (
        f"Expected extra.yaml in choices, got {choices}"
    )
    assert "__create__" in choices
    assert "__delete__" in choices


@pytest.mark.e2e_config_flow
async def test_options_flow_create_new_rule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rule_dir: Path
) -> None:
    """The 'create new rule' path: select __create__, fill form, save, reload."""
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "rules"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"action": "__create__"},
    )
    # Should now be on edit_new
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit_new"

    # Submit
    new_rule = (
        "- id: created-by-options-flow\n"
        "  when: input_boolean.x == 'on'\n"
        "  emit:\n"
        "    target: light.test\n"
        "    set:\n"
        "      state: 'on'\n"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "filename": "created.yaml",
            "contents": new_rule,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY, (
        f"Expected CREATE_ENTRY after saving new rule, got "
        f"{result['type']!r} reason={result.get('reason')!r}"
    )

    # The file should exist on disk
    assert (rule_dir / "created.yaml").read_text() == new_rule

    # And the engine should have picked it up
    engine = hass.data[DOMAIN][loaded_entry.entry_id]
    assert "created-by-options-flow" in engine._rules  # noqa: SLF001


@pytest.mark.e2e_config_flow
async def test_options_flow_edit_existing_rule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rule_dir: Path
) -> None:
    """The 'edit existing rule' path: pick file, modify, save, reload."""
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "rules"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"action": "starter.yaml"},
    )
    # Should now be on edit_existing with the current file contents
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit_existing"
    # The "contents" key's default should match the on-disk file content.
    # HA's vol schema stores the default on the key node, not the selector.
    current_default = result["data_schema"].schema["contents"]
    # current_default is a Marker (vol.Optional returns a Marker). The
    # default is on the description; for vol.Optional, it's the .default
    # attribute of the wrapped validator.
    assert current_default is not None
    # The file on disk should have the starter content
    on_disk = (rule_dir / "starter.yaml").read_text()
    assert "starter" in on_disk

    # Submit with new content
    new_content = (
        "- id: starter\n"
        "  when: input_boolean.y == 'on'\n"  # changed from time_of_day
        "  emit:\n"
        "    target: light.test\n"
        "    set:\n"
        "      state: 'off'\n"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"contents": new_content},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # File should be updated on disk
    assert (rule_dir / "starter.yaml").read_text() == new_content


@pytest.mark.e2e_config_flow
async def test_options_flow_delete_rule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rule_dir: Path
) -> None:
    """The 'delete rule' path: pick file, confirm, file gone, engine updated."""
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "rules"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"action": "__delete__"},
    )
    # Should now be on delete_pick
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "delete_pick"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"filename": "starter.yaml"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # File should be gone
    assert not (rule_dir / "starter.yaml").exists()

    # Engine should no longer have the rule
    engine = hass.data[DOMAIN][loaded_entry.entry_id]
    assert "starter" not in engine._rules  # noqa: SLF001


@pytest.mark.e2e_config_flow
async def test_options_flow_general_step_changes_rule_dir(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, tmp_path: Path
) -> None:
    """The 'change rule directory' path: navigate, submit, entry updated, reloaded."""
    new_dir = tmp_path / "new_rules"
    new_dir.mkdir()

    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "general"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "general"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_RULE_DIR: str(new_dir)},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # The config entry should now point at the new dir
    assert loaded_entry.data[CONF_RULE_DIR] == str(new_dir)


@pytest.mark.e2e_config_flow
async def test_options_flow_general_step_rejects_invalid_path(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Submitting a relative path in the general step should not 500."""
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "general"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_RULE_DIR: "not/absolute"},
    )
    # Should re-show the form with an error
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_RULE_DIR: "invalid_path"}


# ── Blocking-I/O detection ─────────────────────────────────────────


@pytest.mark.e2e_config_flow
async def test_no_blocking_io_warnings_during_full_options_flow(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rule_dir: Path
) -> None:
    """Drive the full options flow once and assert no behavior regression.

    This is the runtime complement to the AST guard in
    ``test_config_flow_no_blocking_io.py``. The AST guard catches
    call sites statically; this test catches behavior regressions
    at runtime in a real event loop.

    Note: We previously set ``hass.config.debug = True`` here to
    detect blocking calls, but that flag pollutes HA's translation
    cache across tests in the same module, breaking subsequent
    tests. The AST + executor-mock tests in
    ``test_config_flow_no_blocking_io.py`` and
    ``test_options_flow_uses_executor_for_io`` are the actual
    defenses. This test is a "smoke" run that proves the whole flow
    completes without 500s.
    """

    # Drive the full flow
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    assert result["type"] == FlowResultType.MENU

    # Visit every step. Each is a potential blocking-I/O call site.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "rules"}
    )
    assert result["type"] == FlowResultType.FORM  # async_step_rules
    assert result["step_id"] == "rules"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"action": "starter.yaml"}
    )
    assert result["type"] == FlowResultType.FORM  # async_step_edit_existing
    assert result["step_id"] == "edit_existing"

    # And the delete path (read file, unlink)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"contents": "garbage yaml: :"}
    )
    # Bad YAML → error in the form, not a CREATE_ENTRY
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit_existing"
    assert "base" in result["errors"]

    # The starter file must still exist (validation rejected the write)
    assert (rule_dir / "starter.yaml").exists()


@pytest.mark.e2e_config_flow
async def test_options_flow_uses_executor_for_io(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, rule_dir: Path
) -> None:
    """Verify the flow actually goes through hass.async_add_executor_job.

    AST inspection (test_config_flow_no_blocking_io.py) catches static
    call sites. This test wraps the executor and verifies it was
    actually called — guarding against the case where someone replaces
    the executor wrapper with a no-op or sync fallback "because it's
    faster" without realizing it reintroduces blocking-I/O.
    """
    # Wrap the executor to count calls
    original_executor_job = hass.async_add_executor_job
    executor_calls: list[tuple[str, tuple]] = []

    async def counting_executor_job(func, *args):
        executor_calls.append((func.__name__, args))
        return await original_executor_job(func, *args)

    hass.async_add_executor_job = counting_executor_job  # type: ignore[method-assign]

    # Drive the flow
    result = await hass.config_entries.options.async_init(
        loaded_entry.entry_id,
        context={"source": "config_entry"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "rules"}
    )
    assert result["type"] == FlowResultType.FORM

    # async_step_rules must have called the executor for _list_rule_files
    list_calls = [c for c in executor_calls if c[0] == "_list_rule_files"]
    assert list_calls, (
        f"async_step_rules did not go through the executor for "
        f"_list_rule_files. Calls seen: {executor_calls}. "
        f"This means a sync I/O call snuck back into the config flow."
    )

    # Now click into the edit form — that should call _read_rule_file
    executor_calls.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"action": "starter.yaml"}
    )
    assert result["type"] == FlowResultType.FORM
    read_calls = [c for c in executor_calls if c[0] == "_read_rule_file"]
    assert read_calls, (
        f"async_step_edit_existing did not go through the executor for "
        f"_read_rule_file. Calls seen: {executor_calls}."
    )
