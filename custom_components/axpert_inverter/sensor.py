from datetime import datetime
import logging
import math
from typing import Optional, Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfApparentPower,
    UnitOfEnergy,
    UnitOfTemperature,
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import AxpertDataUpdateCoordinator
from .entity import AxpertEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Axpert sensor entities."""
    coordinator: AxpertDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        AxpertGridInputSensor(coordinator, entry, "grid_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertGridInputSensor(coordinator, entry, "grid_frequency", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY),
        AxpertSensor(coordinator, entry, "ac_output_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertSensor(coordinator, entry, "ac_output_frequency", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY),
        AxpertSensor(coordinator, entry, "ac_output_active_power", UnitOfPower.WATT, SensorDeviceClass.POWER),
        AxpertSensor(coordinator, entry, "ac_output_apparent_power", UnitOfApparentPower.VOLT_AMPERE, SensorDeviceClass.APPARENT_POWER),
        AxpertSensor(coordinator, entry, "output_load_percent", PERCENTAGE, None),
        AxpertSensor(coordinator, entry, "battery_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertSensor(coordinator, entry, "battery_charging_current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
        AxpertSensor(coordinator, entry, "battery_discharge_current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
        AxpertSensor(coordinator, entry, "battery_capacity", PERCENTAGE, SensorDeviceClass.BATTERY),
        AxpertSensor(coordinator, entry, "heat_sink_temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
        AxpertSensor(coordinator, entry, "pv_input_voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
        AxpertSensor(coordinator, entry, "pv_input_current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
        AxpertPVSensor(coordinator, entry),
        AxpertOutputCurrentSensor(coordinator, entry),
        AxpertGridCurrentSensor(coordinator, entry),
        AxpertGridPowerSensor(coordinator, entry),
        AxpertInverterLossSensor(coordinator, entry),
        AxpertStatusSensor(coordinator, entry),
        AxpertMachineTypeSensor(coordinator, entry),
        AxpertReactivePowerSensor(coordinator, entry),
        AxpertPowerFactorSensor(coordinator, entry),
    ]

    entities.append(AxpertEnergySensor(coordinator, entry, "pv_energy", "pv_power"))
    entities.append(AxpertEnergySensor(coordinator, entry, "load_energy", "ac_output_active_power"))
    entities.append(AxpertEnergySensor(coordinator, entry, "grid_energy", "grid_power"))

    async_add_entities(entities)


class AxpertSensor(AxpertEntity, SensorEntity):
    """Representation of an Axpert Sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key, unit, device_class):
        super().__init__(coordinator)
        self._key = key
        self._entry = entry
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_state_class = SensorStateClass.MEASUREMENT if device_class else None

    @property
    def native_value(self):
        return self.coordinator.data.get(self._key)


class AxpertGridInputSensor(AxpertSensor):
    """Sensor for Grid/Generator Input (Voltage/Frequency)."""

    @property
    def translation_key(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        base = "grid"
        if machine_type == "01":
            base = "generator"
        if "voltage" in self._key:
            return f"{base}_voltage"
        elif "frequency" in self._key:
            return f"{base}_frequency"
        return self._key

    @property
    def icon(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "mdi:generator-portable"
        return "mdi:transmission-tower"


class AxpertPVSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for PV Power (V * A)."""

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_name = "PV Power"
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_unique_id = f"{entry.entry_id}_pv_power"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        if "pv_charging_power" in self.coordinator.data:
            return float(self.coordinator.data["pv_charging_power"])
        v = self.coordinator.data.get("pv_input_voltage", 0)
        a = self.coordinator.data.get("pv_input_current", 0)
        return round(float(v) * float(a), 1)


class AxpertEnergySensor(AxpertEntity, RestoreEntity, SensorEntity):
    """Sensor that integrates power over time to calculate energy (kWh)."""

    _MAX_INTEGRATION_INTERVAL = 300

    def __init__(self, coordinator, entry: ConfigEntry, key, source_key):
        super().__init__(coordinator, source_type="calculated")
        self._key = key
        self._source_key = source_key
        self._entry = entry
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_unique_id = f"{entry.entry_id}_{key}_total"
        self._state = 0.0
        self._last_update_time = None
        self._last_power = None

    @property
    def translation_key(self):
        if self._key == "grid_energy":
            machine_type = self.coordinator.data.get("machine_type", "00")
            if machine_type == "01":
                return "generator_energy"
        return self._attr_translation_key

    @property
    def icon(self):
        if self._key == "grid_energy":
            machine_type = self.coordinator.data.get("machine_type", "00")
            if machine_type == "01":
                return "mdi:generator-portable"
            return "mdi:transmission-tower"
        return None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state:
            try:
                self._state = float(state.state)
            except ValueError:
                self._state = 0.0
        self._last_update_time = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        now = dt_util.utcnow()
        current_power = 0.0

        if self._source_key == "pv_power":
            if "pv_charging_power" in self.coordinator.data:
                current_power = float(self.coordinator.data["pv_charging_power"])
            else:
                v = self.coordinator.data.get("pv_input_voltage", 0)
                a = self.coordinator.data.get("pv_input_current", 0)
                current_power = float(v) * float(a)
        elif self._source_key == "grid_power":
            try:
                p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
                batt_v = float(self.coordinator.data.get("battery_voltage", 0))
                batt_chg_i = float(self.coordinator.data.get("battery_charging_current", 0))
                p_charge = batt_v * batt_chg_i
                batt_dis_i = float(self.coordinator.data.get("battery_discharge_current", 0))
                p_discharge = batt_v * batt_dis_i
                pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
                pv_i = float(self.coordinator.data.get("pv_input_current", 0))
                p_pv = pv_v * pv_i
                current_power = p_load + p_charge - p_discharge - p_pv
                if current_power < 0:
                    current_power = 0.0
            except (ValueError, TypeError):
                current_power = 0.0
        else:
            current_power = float(self.coordinator.data.get(self._source_key, 0))

        if self._last_update_time is None or self._last_power is None:
            self._last_update_time = now
            self._last_power = current_power
            return

        time_diff_seconds = (now - self._last_update_time).total_seconds()

        if time_diff_seconds > self._MAX_INTEGRATION_INTERVAL:
            _LOGGER.debug(f"Time difference {time_diff_seconds}s > {self._MAX_INTEGRATION_INTERVAL}s. Skipping integration.")
            self._last_update_time = now
            self._last_power = current_power
            return

        time_diff_hours = time_diff_seconds / 3600.0
        avg_power = (self._last_power + current_power) / 2.0
        added_energy_kwh = (avg_power / 1000.0) * time_diff_hours

        if added_energy_kwh > 0:
            self._state += added_energy_kwh

        self._last_update_time = now
        self._last_power = current_power
        self.async_write_ha_state()

    @property
    def native_value(self):
        return round(self._state, 2)


class AxpertOutputCurrentSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for Output Current."""

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_name = "Output Current"
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_unique_id = f"{entry.entry_id}_output_current"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:current-ac"

    @property
    def native_value(self):
        s = self.coordinator.data.get("ac_output_apparent_power", 0)
        v = self.coordinator.data.get("ac_output_voltage", 0)
        try:
            if float(v) == 0:
                return 0.0
            return round(float(s) / float(v), 1)
        except (ValueError, TypeError):
            return 0.0


class AxpertGridCurrentSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for Grid Current (Calculated)."""

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_unique_id = f"{entry.entry_id}_grid_current"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def translation_key(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "generator_current"
        return "grid_current"

    @property
    def icon(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "mdi:generator-portable"
        return "mdi:transmission-tower"

    @property
    def native_value(self):
        try:
            p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
            s_load = float(self.coordinator.data.get("ac_output_apparent_power", 0))
            q_load = math.sqrt(max(0, s_load ** 2 - p_load ** 2))
            batt_v = float(self.coordinator.data.get("battery_voltage", 0))
            p_charge = batt_v * float(self.coordinator.data.get("battery_charging_current", 0))
            p_discharge = batt_v * float(self.coordinator.data.get("battery_discharge_current", 0))
            pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
            p_pv = pv_v * float(self.coordinator.data.get("pv_input_current", 0))
            p_grid = p_load + p_charge - p_discharge - p_pv
            s_grid = math.sqrt(p_grid ** 2 + q_load ** 2)
            v_grid = float(self.coordinator.data.get("grid_voltage", 0))
            if v_grid < 10:
                return 0.0
            i_grid = s_grid / v_grid
            if p_grid < 0:
                i_grid = 0.0
            return round(i_grid, 1)
        except (ValueError, TypeError):
            return 0.0


class AxpertGridPowerSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for Grid Power (Calculated)."""

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_unique_id = f"{entry.entry_id}_grid_power"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def translation_key(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "generator_power"
        return "grid_power"

    @property
    def icon(self):
        machine_type = self.coordinator.data.get("machine_type", "00")
        if machine_type == "01":
            return "mdi:generator-portable"
        return "mdi:transmission-tower"

    @property
    def native_value(self):
        try:
            p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
            batt_v = float(self.coordinator.data.get("battery_voltage", 0))
            p_charge = batt_v * float(self.coordinator.data.get("battery_charging_current", 0))
            p_discharge = batt_v * float(self.coordinator.data.get("battery_discharge_current", 0))
            pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
            p_pv = pv_v * float(self.coordinator.data.get("pv_input_current", 0))
            p_grid = p_load + p_charge - p_discharge - p_pv
            if p_grid < 0:
                p_grid = 0.0
            return round(p_grid, 1)
        except (ValueError, TypeError):
            return 0.0


class AxpertInverterLossSensor(AxpertEntity, SensorEntity):
    """Sensor for Inverter Consumption/Loss (Calculated)."""

    _attr_has_entity_name = True
    _attr_translation_key = "inverter_consumption"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_unique_id = f"{entry.entry_id}_inverter_consumption"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        try:
            v_grid = float(self.coordinator.data.get("grid_voltage", 0))
            if v_grid >= 10:
                return 0.0
            p_load = float(self.coordinator.data.get("ac_output_active_power", 0))
            batt_v = float(self.coordinator.data.get("battery_voltage", 0))
            p_charge = batt_v * float(self.coordinator.data.get("battery_charging_current", 0))
            p_discharge = batt_v * float(self.coordinator.data.get("battery_discharge_current", 0))
            pv_v = float(self.coordinator.data.get("pv_input_voltage", 0))
            p_pv = pv_v * float(self.coordinator.data.get("pv_input_current", 0))
            p_loss = (p_pv + p_discharge) - (p_load + p_charge)
            if p_loss < 0:
                p_loss = 0.0
            return round(p_loss, 1)
        except (ValueError, TypeError):
            return 0.0


class AxpertMachineTypeSensor(AxpertEntity, SensorEntity):
    """Sensor for Machine Type."""

    _attr_has_entity_name = True
    _attr_translation_key = "machine_type"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = ["grid_tie", "off_grid", "hybrid"]

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_machine_type"

    @property
    def native_value(self):
        m_type = self.coordinator.data.get("machine_type", "")
        if m_type == "00":
            return "grid_tie"
        elif m_type == "01":
            return "off_grid"
        elif m_type == "10":
            return "hybrid"
        return None


class AxpertStatusSensor(AxpertEntity, SensorEntity):
    """Sensor for Inverter Status."""

    _attr_has_entity_name = True
    _attr_translation_key = "inverter_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = ["power_on", "standby", "line_mode", "battery_mode", "fault", "power_saving"]

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_inverter_status"

    @property
    def native_value(self):
        mode = self.coordinator.data.get("mode", "")
        mapping = {"P": "power_on", "S": "standby", "L": "line_mode", "B": "battery_mode", "F": "fault", "H": "power_saving"}
        return mapping.get(mode)


class AxpertReactivePowerSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for Reactive Power."""

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_translation_key = "reactive_power"
        self._attr_native_unit_of_measurement = "VAR"
        self._attr_unique_id = f"{entry.entry_id}_reactive_power"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:flash-outline"

    @property
    def native_value(self):
        s = float(self.coordinator.data.get("ac_output_apparent_power", 0))
        p = float(self.coordinator.data.get("ac_output_active_power", 0))
        try:
            val = max(0, s ** 2 - p ** 2)
            return round(math.sqrt(val), 1)
        except (ValueError, TypeError):
            return 0.0


class AxpertPowerFactorSensor(AxpertEntity, SensorEntity):
    """Synthetic sensor for Power Factor."""

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, source_type="calculated")
        self._entry = entry
        self._attr_translation_key = "power_factor"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_device_class = SensorDeviceClass.POWER_FACTOR
        self._attr_unique_id = f"{entry.entry_id}_power_factor"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:angle-acute"

    @property
    def native_value(self):
        s = float(self.coordinator.data.get("ac_output_apparent_power", 0))
        p = float(self.coordinator.data.get("ac_output_active_power", 0))
        try:
            if s == 0:
                return 0.0
            return round(min((p / s) * 100, 100.0), 1)
        except (ValueError, TypeError):
            return 0.0
