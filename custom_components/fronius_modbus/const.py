from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from .froniusmodbusclient_const import (
    AC_LIMIT_STATUS,
    CHARGE_GRID_STATUS,
    CHARGE_STATUS,
    CONNECTION_STATUS_CONDENSED,
    CONTROL_STATUS,
    ECP_CONNECTION_STATUS,
    FRONIUS_INVERTER_STATUS,
    GRID_STATUS,
    INVERTER_STATUS,
    STORAGE_CONTROL_MODE,
)

DOMAIN = 'fronius_modbus'
CONNECTION_MODBUS = 'modbus'
DEFAULT_NAME = 'Fronius'
ENTITY_PREFIX = 'fm'
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_PORT = 502
DEFAULT_INVERTER_UNIT_ID = 1
DEFAULT_METER_UNIT_ID = 200
DEFAULT_METER_UNIT_IDS = [DEFAULT_METER_UNIT_ID]
DEFAULT_AUTO_ENABLE_MODBUS = True
DEFAULT_RESTRICT_MODBUS_TO_THIS_IP = False
API_USERNAME = "customer"
TECHNICIAN_USERNAME = "technician"
API_USERNAMES = (API_USERNAME, TECHNICIAN_USERNAME)
CONF_RECONFIGURE_REQUIRED = "_reconfigure_required"
MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX = "legacy_modbus_only_reconfigure_"
SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX = "solar_api_low_firmware_"
CONF_INVERTER_UNIT_ID = 'inverter_modbus_unit_id'
CONF_METER_UNIT_ID = 'meter_modbus_unit_id'
CONF_METER_UNIT_IDS = 'meter_modbus_unit_ids'
CONF_API_USERNAME = 'api_username'
CONF_API_PASSWORD = 'api_password'
CONF_AUTO_ENABLE_MODBUS = 'auto_enable_modbus'
CONF_RESTRICT_MODBUS_TO_THIS_IP = 'restrict_modbus_to_this_ip'
ATTR_MANUFACTURER = 'Fronius'
SUPPORTED_MANUFACTURERS = ['Fronius']
SUPPORTED_MODELS = ['Primo GEN24', 'Symo GEN24', 'Verto']

API_BATTERY_MODE = {
    0: 'Auto',
    1: 'Manual',
}

API_SOC_MODE = {
    'auto': 'Automatic',
    'manual': 'Manual',
}

STORAGE_EXT_CONTROL_MODE = {
    0: 'Auto',
    1: 'PV Charge Limit',
    2: 'Discharge Limit',
    3: 'PV Charge and Discharge Limit',
    4: 'Charge from Grid',
    5: 'Discharge to Grid',
    6: 'Block Discharging',
    7: 'Block Charging',
}

STORAGE_MODBUS_SELECT_TYPES = [
    ['ext_control_mode', 'ext_control_mode', STORAGE_EXT_CONTROL_MODE],
]

STORAGE_API_SELECT_TYPES = [
    ['api_battery_mode', 'api_battery_mode', API_BATTERY_MODE],
]

STORAGE_API_SWITCH_TYPES = [
    ['charge_from_ac', 'api_charge_from_ac', 'mdi:power-plug-battery'],
    ['charge_from_grid', 'api_charge_from_grid', 'mdi:transmission-tower-export'],
]

STORAGE_MODBUS_NUMBER_TYPES = [
    ['grid_discharge_power', 'grid_discharge_power', {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxDisChaRte'}],
    ['grid_charge_power', 'grid_charge_power', {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxChaRte'}],
    ['discharge_limit', 'discharge_limit',  {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxDisChaRte'}],
    ['charge_limit', 'charge_limit', {'min': 0, 'max': 10100, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'MaxChaRte'}],
    ['soc_minimum', 'soc_minimum', {'min': 5, 'max': 100, 'step': 1, 'mode':'box', 'unit': '%'}],
]

STORAGE_API_NUMBER_TYPES = [
    ['api_battery_power', 'api_battery_power', {'min': -20000, 'max': 20000, 'step': 10, 'mode': 'box', 'unit': 'W'}],
    ['soc_maximum', 'soc_maximum', {'min': 0, 'max': 100, 'step': 1, 'mode': 'box', 'unit': '%'}],
]

INVERTER_NUMBER_TYPES = [
    ['ac_limit_rate', 'ac_limit_rate', {'min': 0, 'max': 50000, 'step': 10, 'mode':'box', 'unit': 'W', 'max_key': 'max_power'}],
    ['power_factor', 'power_factor', {'min': -1, 'max': 1, 'step': 0.001, 'mode':'box', 'unit': None}],
]

INVERTER_WEB_NUMBER_TYPES = [
    ['export_soft_limit', 'export_soft_limit', {'min': 0, 'max': 15000, 'step': 10, 'mode': 'box', 'unit': 'W'}],
]

INVERTER_SELECT_TYPES = [
    ['ac_limit_enable', 'ac_limit_enable', {0: 'Disabled', 1: 'Enabled'}],
    ['power_factor_enable', 'power_factor_enable', {0: 'Disabled', 1: 'Enabled'}],
    ['Conn', 'Conn', {0: 'Disabled', 1: 'Enabled'}],
]

INVERTER_API_SWITCH_TYPES = [
    ['api_solar_api_enabled', 'api_solar_api_enabled', 'mdi:api', EntityCategory.DIAGNOSTIC],
]

INVERTER_API_BUTTON_TYPES = [
    ['reset_modbus_control', 'reset_modbus_control', 'mdi:restart', EntityCategory.DIAGNOSTIC],
]

INVERTER_SENSOR_TYPES = {
    'A': ['A', 'A', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphA': ['AphA', 'AphA', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'acpower': ['acpower', 'acpower', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'var': ['var', 'var', None, SensorStateClass.MEASUREMENT, 'var', 'mdi:sine-wave', None],
    'acenergy': ['acenergy', 'acenergy', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:lightning-bolt', None],
    'pv_power': ['pv_power', 'pv_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:solar-power', None],
    'load': ['load', 'load', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'pv_connection': ['pv_connection', 'pv_connection', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'ecp_connection': ['ecp_connection', 'ecp_connection', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'status': ['status', 'status', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'statusvendor': ['statusvendor', 'statusvendor', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'line_frequency': ['line_frequency', 'line_frequency', SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, 'Hz', None, None],
    'inverter_controls': ['control_mode', 'inverter_controls', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'vref': ['vref', 'vref', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', EntityCategory.DIAGNOSTIC],
    'vrefofs': ['vrefofs', 'vrefofs', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', EntityCategory.DIAGNOSTIC],
    'max_power': ['max_power', 'max_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'events2': ['events2', 'events2', None, None, None, None, EntityCategory.DIAGNOSTIC],

    'grid_status': ['grid_status', 'grid_status', None, None, None, None, EntityCategory.DIAGNOSTIC],

    'Conn': ['Conn', 'Conn', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'WMaxLim_Ena': ['WMaxLim_Ena', 'WMaxLim_Ena', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'OutPFSet_Ena': ['OutPFSet_Ena', 'OutPFSet_Ena', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'VArPct_Ena': ['VArPct_Ena', 'VArPct_Ena', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'PhVphA': ['PhVphA', 'PhVphA', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'unit_id': ['unit_id', 'i_unit_id', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'ac_limit_rate': ['ac_limit_rate', 'ac_limit_rate', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:chart-line', None],
    'ac_limit_enable': ['ac_limit_enable', 'ac_limit_enable', None, None, None, 'mdi:power-plug', EntityCategory.DIAGNOSTIC],
    'isolation_resistance': ['isolation_resistance', 'isolation_resistance', None, SensorStateClass.MEASUREMENT, 'MΩ', 'mdi:omega', None],
}

INVERTER_WEB_SENSOR_TYPES = {
    'inverter_temperature': ['inverter_temperature', 'inverter_temperature', SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, '°C', 'mdi:thermometer', None],
    'export_soft_limit': ['export_soft_limit', 'export_soft_limit', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:transmission-tower-export', None],
    'api_modbus_mode': ['api_modbus_mode', 'api_modbus_mode', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_control': ['api_modbus_control', 'api_modbus_control', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_sunspec_mode': ['api_modbus_sunspec_mode', 'api_modbus_sunspec_mode', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_restriction': ['api_modbus_restriction', 'api_modbus_restriction', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'api_modbus_restriction_ip': ['api_modbus_restriction_ip', 'api_modbus_restriction_ip', None, None, None, None, EntityCategory.DIAGNOSTIC],
}

MPPT_MODULE_SENSOR_TYPES = [
    ['dc_current', 'dc_current', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-dc', None],
    ['dc_voltage', 'dc_voltage', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    ['dc_power', 'dc_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:solar-power', None],
    ['lifetime_energy', 'lifetime_energy', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:solar-panel', None],
]

INVERTER_SYMO_SENSOR_TYPES = {
    'AphB': ['AphB', 'AphB', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphC': ['AphC', 'AphC', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'PhVphB': ['PhVphB', 'PhVphB', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PhVphC': ['PhVphC', 'PhVphC', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPVphAB': ['PPVphAB', 'PPVphAB', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPVphBC': ['PPVphBC', 'PPVphBC', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPVphCA': ['PPVphCA', 'PPVphCA', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
}

INVERTER_STORAGE_SENSOR_TYPES = {
    'storage_charge_current': ['storage_charge_current', 'storage_charge_current', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-dc', None],
    'storage_charge_voltage': ['storage_charge_voltage', 'storage_charge_voltage', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'storage_charge_power': ['storage_charge_power', 'storage_charge_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:home-battery', None],
    'storage_charge_lfte': ['storage_charge_lfte', 'storage_charge_lfte', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:home-battery', None],
    'storage_discharge_current': ['storage_discharge_current', 'storage_discharge_current', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-dc', None],
    'storage_discharge_voltage': ['storage_discharge_voltage', 'storage_discharge_voltage', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'storage_discharge_power': ['storage_discharge_power', 'storage_discharge_power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:home-battery', None],
    'storage_discharge_lfte': ['storage_discharge_lfte', 'storage_discharge_lfte', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:home-battery', None],
    'storage_connection': ['storage_connection', 'storage_connection', None, None, None, None, EntityCategory.DIAGNOSTIC],
}


METER_SENSOR_TYPES = {
    'A': ['A', 'A', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphA': ['AphA', 'AphA', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphB': ['AphB', 'AphB', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'AphC': ['AphC', 'AphC', SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, 'A', 'mdi:current-ac', None],
    'power': ['power', 'power', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'WphA': ['WphA', 'WphA', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'WphB': ['WphB', 'WphB', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'WphC': ['WphC', 'WphC', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', 'mdi:lightning-bolt', None],
    'exported': ['exported', 'exported', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:lightning-bolt', None],
    'imported': ['imported', 'imported', SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 'Wh', 'mdi:lightning-bolt', None],
    'line_frequency': ['line_frequency', 'line_frequency', SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, 'Hz', None, None],
    'PhVphA': ['PhVphA', 'PhVphA', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PhVphB': ['PhVphB', 'PhVphB', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PhVphC': ['PhVphC', 'PhVphC', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'PPV': ['PPV', 'PPV', SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 'V', 'mdi:lightning-bolt', None],
    'unit_id': ['unit_id', 'unit_id', None, None, None, None, EntityCategory.DIAGNOSTIC],
}

SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS = (
    "AphB",
    "AphC",
    "WphB",
    "WphC",
    "PhVphB",
    "PhVphC",
    "PPV",
)

STORAGE_SENSOR_TYPES = {
    'storage_temperature': ['storage_temperature', 'storage_temperature', SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, '°C', 'mdi:thermometer', None],
    'control_mode': ['control_mode', 'control_mode', None, None, None, None, EntityCategory.DIAGNOSTIC],
    'charge_status': ['charge_status', 'charge_status', None, None, None, None, None, EntityCategory.DIAGNOSTIC],
    'max_charge': ['max_charge', 'max_charge', SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', None, EntityCategory.DIAGNOSTIC],
    'soc': ['soc', 'soc', SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT, '%', None, None],
    'charging_power': ['charging_power', 'charging_power',  None, None, '%', 'mdi:gauge', EntityCategory.DIAGNOSTIC],
    'discharging_power': ['discharging_power', 'discharging_power',  None, None, '%', 'mdi:gauge', EntityCategory.DIAGNOSTIC],
    'soc_minimum': ['soc_minimum', 'soc_minimum',  None, None, '%', 'mdi:gauge', None],
    'grid_charging': ['grid_charging', 'grid_charging',  None, None, None, None, EntityCategory.DIAGNOSTIC],
    'WHRtg': ['WHRtg', 'WHRtg',  SensorDeviceClass.ENERGY, SensorStateClass.MEASUREMENT, 'Wh', None, EntityCategory.DIAGNOSTIC],
    'MaxChaRte': ['MaxChaRte', 'MaxChaRte',  SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', None, EntityCategory.DIAGNOSTIC],
    'MaxDisChaRte': ['MaxDisChaRte', 'MaxDisChaRte',  SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 'W', None, EntityCategory.DIAGNOSTIC],
}


def _state_values(*mappings) -> list[str]:
    return list(dict.fromkeys(value for mapping in mappings for value in mapping.values()))


INVERTER_CONTROL_STATE_VALUES = [
    "Normal",
    "Power reduction",
    "Constant reactive power",
    "Constant power factor",
    "Power reduction,Constant reactive power",
    "Power reduction,Constant power factor",
    "Constant reactive power,Constant power factor",
    "Power reduction,Constant reactive power,Constant power factor",
]


SENSOR_STATE_OPTIONS = {
    'pv_connection': _state_values(CONNECTION_STATUS_CONDENSED),
    'storage_connection': _state_values(CONNECTION_STATUS_CONDENSED),
    'ecp_connection': _state_values(ECP_CONNECTION_STATUS),
    'status': _state_values(INVERTER_STATUS),
    'statusvendor': _state_values(FRONIUS_INVERTER_STATUS),
    'grid_status': _state_values(GRID_STATUS),
    'Conn': _state_values(CONTROL_STATUS),
    'WMaxLim_Ena': _state_values(CONTROL_STATUS),
    'OutPFSet_Ena': _state_values(CONTROL_STATUS),
    'VArPct_Ena': _state_values(CONTROL_STATUS),
    'ac_limit_enable': _state_values(AC_LIMIT_STATUS, {2: 'Unknown'}),
    'control_mode': _state_values(STORAGE_CONTROL_MODE) + INVERTER_CONTROL_STATE_VALUES,
    'charge_status': _state_values(CHARGE_STATUS),
    'grid_charging': _state_values(CHARGE_GRID_STATUS),
    'api_modbus_control': _state_values(CONTROL_STATUS),
    'api_modbus_restriction': _state_values(CONTROL_STATUS),
}
