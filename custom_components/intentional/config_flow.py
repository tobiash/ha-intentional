"""Config flow for the Intentional integration.

The initial setup only asks for the rule directory. The options flow
provides a UI for managing rules (list/edit/save/delete) without
leaving Home Assistant.

Async-safety contract
---------------------
All filesystem operations in this flow MUST run in HA's executor
(via ``hass.async_add_executor_job``). The ``rule_files`` module
is deliberately synchronous so it can be unit-tested without HA,
but calling it directly from an async handler blocks the event
loop. HA detects this and returns 500.

The regression test ``tests/test_config_flow_no_blocking_io.py``
fails the build if a sync call from this module slips back in.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import selector

from .const import CONF_RULE_DIR, DEFAULT_NAME, DEFAULT_RULE_DIR, DOMAIN
from .rule_files import (
    _delete_rule_file,
    _is_safe_filename,
    _list_rule_files,
    _read_rule_file,
    _starter_template,
    _validate_rule_dir,
    _write_rule_file,
)

_LOGGER = logging.getLogger(__name__)


async def _list_in_executor(hass, rule_dir: str) -> list[dict[str, str]]:
    """Async wrapper: list rule files in the executor.

    Defined at module level (not as a method) so it's trivial to
    audit and mock. The instance methods on the flows below all
    route filesystem I/O through this and its siblings.
    """
    return await hass.async_add_executor_job(_list_rule_files, rule_dir)


async def _read_in_executor(hass, rule_dir: str, filename: str) -> str | None:
    """Async wrapper: read a rule file in the executor."""
    return await hass.async_add_executor_job(_read_rule_file, rule_dir, filename)


async def _write_in_executor(
    hass, rule_dir: str, filename: str, contents: str
) -> str | None:
    """Async wrapper: write a rule file (with YAML validation) in the executor."""
    return await hass.async_add_executor_job(
        _write_rule_file, rule_dir, filename, contents
    )


async def _delete_in_executor(
    hass, rule_dir: str, filename: str
) -> str | None:
    """Async wrapper: delete a rule file in the executor."""
    return await hass.async_add_executor_job(
        _delete_rule_file, rule_dir, filename
    )


class IntentionalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            rule_dir = user_input[CONF_RULE_DIR].strip()
            try:
                # _validate_rule_dir is pure-string validation,
                # no I/O — safe to call sync.
                _validate_rule_dir(rule_dir)
            except ValueError as err:
                errors[CONF_RULE_DIR] = "invalid_path"
                _LOGGER.warning("Invalid rule directory: %s", err)
            else:
                # Prevent duplicate entries with the same rule dir
                await self.async_set_unique_id(rule_dir)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=DEFAULT_NAME,
                    data={CONF_RULE_DIR: rule_dir},
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RULE_DIR, default=DEFAULT_RULE_DIR
                ): selector({"text": {}}),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return IntentionalOptionsFlow(config_entry)


class IntentionalOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    """Handle the options flow.

    Provides two steps via a menu:
    1. general  — change the rule directory
    2. rules    — list, edit, create, delete rule files

    Inherits from ``OptionsFlowWithConfigEntry`` (the documented
    convenience base for custom integrations) so ``self.config_entry``
    is available via the parent ``OptionsFlow`` property after init.

    The earlier code inherited from ``OptionsFlow`` directly and did
    ``self.config_entry = config_entry`` in ``__init__``. That worked
    on HA versions where ``OptionsFlow.config_entry`` was a writable
    attribute, but HA 2025+ made it a read-only property. Result:
    HA logged ``AttributeError: property 'config_entry' of
    'IntentionalOptionsFlow' object has no setter`` and Configure
    returned HTTP 500. See CHANGELOG v0.3.3.

    A second bug bit users in v0.3.0: every rule-files call was sync
    on the event loop, so HA logged blocking-call warnings and returned
    500 on every "list rules" / "edit" / "create" / "delete" step.
    v0.3.4 routes all such calls through ``hass.async_add_executor_job``
    via the ``_*_in_executor`` helpers above.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "rules"],
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Change the rule directory."""
        if user_input is not None:
            new_dir = user_input[CONF_RULE_DIR].strip()
            try:
                # Pure-string validation, safe to call sync.
                _validate_rule_dir(new_dir)
            except ValueError:
                return self.async_show_form(
                    step_id="general",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_RULE_DIR,
                                default=self.config_entry.data.get(
                                    CONF_RULE_DIR, DEFAULT_RULE_DIR
                                ),
                            ): selector({"text": {}}),
                        }
                    ),
                    errors={CONF_RULE_DIR: "invalid_path"},
                )
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_RULE_DIR: new_dir},
            )
            await self.hass.config_entries.async_reload(
                self.config_entry.entry_id
            )
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)
        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(
                {vol.Required(CONF_RULE_DIR, default=current): selector({"text": {}})}
            ),
        )

    async def async_step_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Rule management hub: pick a file to edit, or create/delete."""
        rule_dir = self.config_entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

        # Handle a previous submit: route to the right step
        if user_input is not None:
            action = user_input.get("action", "")
            if action == "__create__":
                return await self.async_step_edit_new()
            if action == "__delete__":
                return await self.async_step_delete_pick()
            # Otherwise it's a filename → show editor
            self._selected_file = action
            return await self.async_step_edit_existing()

        # EXECUTOR: list_rule_files does path.glob() + stat() — both
        # blocking I/O. v0.3.0..v0.3.3 called this sync, triggering
        # HA's "Detected blocking call to scandir" warning + 500.
        files = await _list_in_executor(self.hass, rule_dir)
        # Build select options: each file + create + delete
        if not files:
            return self.async_show_form(
                step_id="rules",
                data_schema=vol.Schema(
                    {
                        vol.Required("action", default="__create__"): vol.In(
                            {"__create__": "➕ Create your first rule"}
                        ),
                    }
                ),
                description_placeholders={
                    "rule_dir": rule_dir,
                    "file_count": "0",
                    "filenames": "(no rule files yet)",
                },
            )

        file_choices: dict[str, str] = {
            f["filename"]: f["filename"] for f in files
        }
        file_choices["__create__"] = "➕ Create new rule"
        file_choices["__delete__"] = "🗑️  Delete a rule"

        return self.async_show_form(
            step_id="rules",
            data_schema=vol.Schema(
                {vol.Required("action"): vol.In(file_choices)}
            ),
            description_placeholders={
                "rule_dir": rule_dir,
                "file_count": str(len(files)),
                "filenames": ", ".join(f["filename"] for f in files),
            },
        )

    async def async_step_edit_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit the currently-selected rule file."""
        rule_dir = self.config_entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

        if user_input is not None:
            contents = user_input.get("contents", "")
            # EXECUTOR: writes go through the executor (mkdir + write_text
            # + yaml validation — all blocking).
            err = await _write_in_executor(
                self.hass, rule_dir, self._selected_file, contents
            )
            if err:
                return self.async_show_form(
                    step_id="edit_existing",
                    data_schema=vol.Schema(
                        {
                            vol.Optional(
                                "contents", default=contents
                            ): selector(
                                {"text": {"multiline": True}}
                            ),
                        }
                    ),
                    errors={"base": err},
                    description_placeholders={"filename": self._selected_file},
                )
            await self.hass.services.async_call(DOMAIN, "reload", blocking=True)
            return self.async_create_entry(title="", data={})

        # EXECUTOR: read_text() on the loop. v0.3.0..v0.3.3 hit this
        # every time the user clicked "edit" on an existing rule.
        current = await _read_in_executor(
            self.hass, rule_dir, self._selected_file
        ) or ""
        return self.async_show_form(
            step_id="edit_existing",
            data_schema=vol.Schema(
                {
                    vol.Optional("contents", default=current): selector(
                        {"text": {"multiline": True}}
                    ),
                }
            ),
            description_placeholders={"filename": self._selected_file},
        )

    async def async_step_edit_new(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create a new rule file."""
        rule_dir = self.config_entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

        if user_input is not None:
            filename = user_input["filename"].strip()
            contents = user_input.get("contents") or _starter_template().replace(
                "new-rule", filename.rsplit(".", 1)[0]
            )
            # EXECUTOR: same reason as edit_existing.
            err = await _write_in_executor(
                self.hass, rule_dir, filename, contents
            )
            if err:
                return self.async_show_form(
                    step_id="edit_new",
                    data_schema=vol.Schema(
                        {
                            vol.Required("filename", default=filename): str,
                            vol.Optional("contents", default=contents): selector(
                                {"text": {"multiline": True}}
                            ),
                        }
                    ),
                    errors={"base": err},
                )
            await self.hass.services.async_call(DOMAIN, "reload", blocking=True)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="edit_new",
            data_schema=vol.Schema(
                {
                    vol.Required("filename", default="new-rule.yaml"): str,
                    vol.Optional("contents", default=_starter_template()): selector(
                        {"text": {"multiline": True}}
                    ),
                }
            ),
            description_placeholders={"rule_dir": rule_dir},
        )

    async def async_step_delete_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a file to delete, then delete it."""
        rule_dir = self.config_entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

        if user_input is not None:
            filename = user_input["filename"]
            # EXECUTOR: unlink() is blocking I/O.
            err = await _delete_in_executor(
                self.hass, rule_dir, filename
            )
            if err:
                return self.async_abort(reason=err)
            await self.hass.services.async_call(DOMAIN, "reload", blocking=True)
            return self.async_create_entry(title="", data={})

        # EXECUTOR: listing files for the picker.
        files = await _list_in_executor(self.hass, rule_dir)
        if not files:
            return self.async_create_entry(title="", data={})
        file_choices = {f["filename"]: f["filename"] for f in files}
        return self.async_show_form(
            step_id="delete_pick",
            data_schema=vol.Schema(
                {vol.Required("filename"): vol.In(file_choices)}
            ),
            description_placeholders={
                "rule_dir": rule_dir,
                "filenames": ", ".join(f["filename"] for f in files),
            },
        )
