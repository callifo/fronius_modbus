import logging

from .const import (
    STORAGE_API_NUMBER_TYPES,
    STORAGE_MODBUS_NUMBER_TYPES,
    INVERTER_NUMBER_TYPES,
    INVERTER_WEB_NUMBER_TYPES,
)

from homeassistant.components.number import (
    NumberEntity,
)

from .hub import Hub
from .base import FroniusModbusBaseEntity, async_ensure_translation_cache

_LOGGER = logging.getLogger(__name__)

_STORAGE_PERCENT_NUMBER_KEYS = {
    'grid_discharge_power',
    'discharge_limit',
    'grid_charge_power',
    'charge_limit',
}

async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    await async_ensure_translation_cache(hass)
    hub: Hub = config_entry.runtime_data
    coordinator = hub.coordinator

    entities = []

    if hub.storage_configured:
        for number_info in STORAGE_MODBUS_NUMBER_TYPES:
            max_val = None
            max_key = number_info[2].get('max_key')
            if max_key is not None:
                max_val = hub.data.get(max_key)
            if max_val is None:
                max_val = number_info[2]['max']

            number = FroniusModbusNumber(
                coordinator=coordinator,
                device_info=hub.device_info_storage,
                name=number_info[0],
                key=number_info[1],
                translation_key=number_info[0],
                min_val=number_info[2]['min'],
                max_val=max_val,
                unit=number_info[2]['unit'],
                mode=number_info[2]['mode'],
                native_step=number_info[2]['step'],
                hub=hub,  # Pass hub for control methods
            )
            entities.append(number)

        if hub.web_api_configured:
            for number_info in STORAGE_API_NUMBER_TYPES:
                number = FroniusModbusNumber(
                    coordinator=coordinator,
                    device_info=hub.device_info_storage,
                    name=number_info[0],
                    key=number_info[1],
                    translation_key=number_info[0],
                    min_val=number_info[2]['min'],
                    max_val=number_info[2]['max'],
                    unit=number_info[2]['unit'],
                    mode=number_info[2]['mode'],
                    native_step=number_info[2]['step'],
                    hub=hub,
                )
                entities.append(number)

    # Add inverter number entities.
    for number_info in INVERTER_NUMBER_TYPES:
        max_val = number_info[2]['max']
        max_key = number_info[2].get('max_key')
        if max_key is not None:
            dynamic_max = hub.data.get(max_key)
            if isinstance(dynamic_max, (int, float)) and dynamic_max > 0:
                max_val = dynamic_max

        number = FroniusModbusNumber(
            coordinator=coordinator,
            device_info=hub.device_info_inverter,
            name=number_info[0],
            key=number_info[1],
            translation_key=number_info[0],
            min_val=number_info[2]['min'],
            max_val=max_val,
            unit=number_info[2]['unit'],
            mode=number_info[2]['mode'],
            native_step=number_info[2]['step'],
            hub=hub,  # Pass hub for control methods
        )
        entities.append(number)

    if hub.tech_configured:
        for number_info in INVERTER_WEB_NUMBER_TYPES:
            max_val = number_info[2]['max']
            max_key = number_info[2].get('max_key')
            if max_key is not None:
                dynamic_max = hub.data.get(max_key)
                if isinstance(dynamic_max, (int, float)) and dynamic_max > 0:
                    max_val = dynamic_max

            number = FroniusModbusNumber(
                coordinator=coordinator,
                device_info=hub.device_info_inverter,
                name=number_info[0],
                key=number_info[1],
                translation_key=number_info[0],
                min_val=number_info[2]['min'],
                max_val=max_val,
                unit=number_info[2]['unit'],
                mode=number_info[2]['mode'],
                native_step=number_info[2]['step'],
                hub=hub,
            )
            entities.append(number)

    async_add_entities(entities)
    return True

class FroniusModbusNumber(FroniusModbusBaseEntity, NumberEntity):
    """Representation of a Battery Storage Modbus number."""
    _translation_platform = "number"

    def __init__(
        self,
        coordinator,
        device_info,
        name,
        key,
        min_val,
        max_val,
        unit,
        mode,
        native_step,
        hub,
        translation_key=None,
    ):
        """Initialize the number entity."""
        super().__init__(
            coordinator=coordinator,
            device_info=device_info,
            name=name,
            key=key,
            translation_key=translation_key,
            min=min_val,
            max=max_val,
            unit=unit,
            mode=mode,
            native_step=native_step,
        )
        self._hub = hub  # Store hub reference for control methods

    @property
    def native_value(self):
        """Return the native number value."""
        if self.coordinator.data and self._key in self.coordinator.data:
            value = self.coordinator.data[self._key]
            if value is None:
                return None
            if self._key in ['grid_discharge_power', 'discharge_limit']:
                return round(value / 100.0 * self._hub.max_discharge_rate_w, 0)
            elif self._key in ['grid_charge_power', 'charge_limit']:
                return round(value / 100.0 * self._hub.max_charge_rate_w, 0)
            else:
                return value
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Change the selected value."""

        if self._key == 'soc_minimum':
            await self._hub.set_soc_minimum(value)
        elif self._key == 'charge_limit':
            await self._hub.set_charge_limit(value)
        elif self._key == 'discharge_limit':
            await self._hub.set_discharge_limit(value)
        elif self._key == 'grid_charge_power':
            await self._hub.set_grid_charge_power(value)
        elif self._key == 'grid_discharge_power':
            await self._hub.set_grid_discharge_power(value)
        elif self._key == 'ac_limit_rate':
            await self._hub.set_ac_limit_rate(value)
        elif self._key == 'power_factor':
            await self._hub.set_power_factor(value)
        elif self._key == 'api_battery_power':
            await self._hub.set_api_battery_power(value)
        elif self._key == 'soc_maximum':
            await self._hub.set_api_soc_values(soc_max=int(round(value)))
        elif self._key == 'export_soft_limit':
            await self._hub.set_export_soft_limit(value)

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return depending on mode."""
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        if not super().available:
            return False
        if self._key in _STORAGE_PERCENT_NUMBER_KEYS and data.get(self._key) is None:
            return False
        if self._key == 'soc_minimum':
            return True
        if self._key == 'charge_limit' and self._hub.storage_extended_control_mode in [1,3,6]:
            return True
        if self._key == 'discharge_limit' and self._hub.storage_extended_control_mode in [2,3,7]:
            return True
        if self._key == 'grid_charge_power' and self._hub.storage_extended_control_mode in [4]:
            return True
        if self._key == 'grid_discharge_power' and self._hub.storage_extended_control_mode in [5]:
            return True
        if self._key == 'ac_limit_rate':
            return True
        if self._key == 'power_factor':
            return data.get('power_factor') is not None
        if self._key == 'api_battery_power':
            return (
                self._hub.web_api_configured
                and data.get('api_battery_mode_effective_raw') == 1
            )
        if self._key == 'soc_maximum':
            return (
                self._hub.web_api_configured
                and data.get('api_battery_mode_effective_raw') == 1
            )
        if self._key == 'export_soft_limit':
            return self._hub.tech_configured
        return False
