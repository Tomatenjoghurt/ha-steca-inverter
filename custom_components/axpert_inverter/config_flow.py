import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_DEVICE_PATH,
    CONF_SCAN_INTERVAL,
    DEFAULT_DEVICE_PATH,
    DEFAULT_SCAN_INTERVAL,
)
from .axpert import AxpertInverter

_LOGGER = logging.getLogger(__name__)


class AxpertConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Axpert Inverter."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            device_path = user_input.get(CONF_DEVICE_PATH)
            scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

            # Validate that the port path format looks correct before saving.
            # We don't do a live USB connection check here because it requires
            # the inverter to be responsive and adds a long timeout to setup.
            import re
            if not re.match(r"^\d+-\d+(\.\d+)*$", device_path.strip()):
                errors[CONF_DEVICE_PATH] = "invalid_port_path"
            else:
                # Use port path as unique ID so all 3 inverters can be added separately
                await self.async_set_unique_id(device_path.strip())
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Axpert Inverter ({device_path})",
                    data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                # Changed default to a port path example instead of /dev/hidrawX
                vol.Required(CONF_DEVICE_PATH, default="1-1.1"): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
            }),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return AxpertOptionsFlowHandler(config_entry)


class AxpertOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Axpert Inverter."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Manage the options."""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_path = self.config_entry.options.get(
            CONF_DEVICE_PATH,
            self.config_entry.data.get(CONF_DEVICE_PATH, DEFAULT_DEVICE_PATH)
        )
        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_PATH, default=current_path): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=current_scan_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
            }),
            errors=errors,
        )
