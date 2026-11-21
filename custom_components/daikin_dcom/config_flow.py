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
    CONF_SMART_MAX_LWT,
    CONF_SMART_MILD_TEMP,
    CONF_SMART_OFF_TEMP,
    CONF_SMART_ROOM_MAX_OFFSET,
    CONF_SMART_ROOM_MODE,
    CONF_SMART_ROOM_SENSOR,
    CONF_SMART_ROOMS_OFF_PAUSE,
    CONF_SMART_EARLY_DELAY,
    CONF_SMART_EARLY_MIN_RUN,
    CONF_SMART_EARLY_SHORTFALL,
    CONF_SMART_EARLY_START,
    CONF_SMART_REST,
    CONF_COP_SENSOR,
    ROOM_MODES,
    CONF_SMART_ROOM_TARGET,
    SMART_DEFAULTS,
)
from .modbus_client import DcomClient, DcomError


def _num(lo: float, hi: float, step: float, unit: str = "°C") -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=lo,
            max=hi,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _room_list(value) -> list[str]:
    """Room entities as a list (older options stored one id or "")."""
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


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
    """Options: control mode, polling interval and smart heating settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_SMART_OFF_TEMP] < user_input[CONF_SMART_MILD_TEMP]:
                errors[CONF_SMART_OFF_TEMP] = "off_below_mild"
            else:
                user_input.setdefault(CONF_SMART_ROOM_SENSOR, [])
                user_input.setdefault(CONF_COP_SENSOR, "")
                return self.async_create_entry(data=user_input)

        entry = self.config_entry
        opts = {**SMART_DEFAULTS, **entry.options, **(user_input or {})}
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
                # --- smart heating control ---
                vol.Required(
                    CONF_SMART_MAX_LWT, default=opts[CONF_SMART_MAX_LWT]
                ): _num(25, 55, 1),
                vol.Required(
                    CONF_SMART_MILD_TEMP, default=opts[CONF_SMART_MILD_TEMP]
                ): _num(0, 25, 0.5),
                vol.Required(
                    CONF_SMART_OFF_TEMP, default=opts[CONF_SMART_OFF_TEMP]
                ): _num(5, 30, 0.5),
                vol.Optional(
                    CONF_SMART_ROOM_SENSOR,
                    description={"suggested_value": _room_list(opts[CONF_SMART_ROOM_SENSOR])},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        multiple=True,
                        filter=[
                            selector.EntityFilterSelectorConfig(
                                domain="sensor", device_class="temperature"
                            ),
                            selector.EntityFilterSelectorConfig(domain="climate"),
                        ],
                    )
                ),
                vol.Required(
                    CONF_SMART_ROOM_MODE, default=opts[CONF_SMART_ROOM_MODE]
                ): _select(list(ROOM_MODES), "room_mode"),
                vol.Required(
                    CONF_SMART_ROOMS_OFF_PAUSE, default=opts[CONF_SMART_ROOMS_OFF_PAUSE]
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SMART_EARLY_START, default=opts[CONF_SMART_EARLY_START]
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SMART_EARLY_SHORTFALL, default=opts[CONF_SMART_EARLY_SHORTFALL]
                ): _num(0.5, 3, 0.1),
                vol.Required(
                    CONF_SMART_EARLY_DELAY, default=opts[CONF_SMART_EARLY_DELAY]
                ): _num(10, 120, 5, "min"),
                vol.Required(
                    CONF_SMART_EARLY_MIN_RUN, default=opts[CONF_SMART_EARLY_MIN_RUN]
                ): _num(30, 180, 5, "min"),
                vol.Required(
                    CONF_SMART_REST, default=opts[CONF_SMART_REST]
                ): selector.BooleanSelector(),
                # --- COP ---
                vol.Optional(
                    CONF_COP_SENSOR,
                    description={
                        "suggested_value": entry.options.get(CONF_COP_SENSOR) or None
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        filter=[
                            selector.EntityFilterSelectorConfig(
                                domain="sensor", device_class="power"
                            ),
                            selector.EntityFilterSelectorConfig(
                                domain="sensor", device_class="energy"
                            ),
                        ]
                    )
                ),
                vol.Required(
                    CONF_SMART_ROOM_TARGET, default=opts[CONF_SMART_ROOM_TARGET]
                ): _num(15, 28, 0.5),
                vol.Required(
                    CONF_SMART_ROOM_MAX_OFFSET, default=opts[CONF_SMART_ROOM_MAX_OFFSET]
                ): vol.All(int, vol.Range(min=0, max=5)),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
