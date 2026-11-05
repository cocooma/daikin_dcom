"""Config flow for Daikin DCOM-LT/MB."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CONTROL_MODE,
    CONF_FRAMING,
    CONF_SCAN_INTERVAL,
    CONF_SLAVE,
    CONTROL_LWT,
    CONTROL_ROOM,
    DEFAULT_CONTROL_MODE,
    DEFAULT_FRAMING,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE,
    DOMAIN,
    FRAMING_RTU,
    FRAMING_TCP,
    I_UNIT_ERROR,
)
from .modbus_client import DcomClient, DcomError


def _select(options: list[str], key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            translation_key=key,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class DcomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            unique = (
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}:"
                f"{user_input[CONF_SLAVE]}"
            )
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured()

            client = DcomClient(
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_SLAVE],
                user_input[CONF_FRAMING],
            )
            try:
                await client.read("i", I_UNIT_ERROR, 1)
            except DcomError:
                errors["base"] = "cannot_connect"
            finally:
                await client.close()

            if not errors:
                return self.async_create_entry(
                    title=f"Daikin Altherma 3 ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")
                ): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    int, vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_SLAVE, default=DEFAULT_SLAVE): vol.All(
                    int, vol.Range(min=1, max=247)
                ),
                vol.Required(CONF_FRAMING, default=DEFAULT_FRAMING): _select(
                    [FRAMING_TCP, FRAMING_RTU], "framing"
                ),
                vol.Required(
                    CONF_CONTROL_MODE, default=DEFAULT_CONTROL_MODE
                ): _select([CONTROL_LWT, CONTROL_ROOM], "control_mode"),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return DcomOptionsFlow()


class DcomOptionsFlow(OptionsFlow):
    """Options: control mode and polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        entry = self.config_entry
        current_mode = entry.options.get(
            CONF_CONTROL_MODE, entry.data.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE)
        )
        current_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_CONTROL_MODE, default=current_mode): _select(
                    [CONTROL_LWT, CONTROL_ROOM], "control_mode"
                ),
                vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                    int, vol.Range(min=10, max=600)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
