"""Config flow for the Intentional integration.

The user only needs to point us at their rule directory. Everything else
(rule parsing, hot-reload, schema validation) is automatic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import selector

from .const import CONF_RULE_DIR, DEFAULT_NAME, DEFAULT_RULE_DIR, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _validate_rule_dir(rule_dir: str) -> None:
    """Best-effort validation of the rule directory path.

    We don't require the directory to exist at config time — the user
    may want to create it later. We just sanity-check the path string.
    """
    if not rule_dir or not isinstance(rule_dir, str):
        raise ValueError("Rule directory must be a non-empty string")
    if not rule_dir.startswith("/"):
        raise ValueError(
            f"Rule directory must be an absolute path (got {rule_dir!r}). "
            "On Home Assistant OS, the default is /config/intentional/rules/."
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


class IntentionalOptionsFlow(config_entries.OptionsFlow):
    """Handle the options flow (changing the rule directory)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            new_dir = user_input[CONF_RULE_DIR].strip()
            try:
                _validate_rule_dir(new_dir)
            except ValueError:
                return self.async_show_form(
                    step_id="init",
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
            # Update the entry — this triggers a reload of the integration
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_RULE_DIR: new_dir},
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_RULE_DIR, default=current): selector({"text": {}})}
            ),
        )
