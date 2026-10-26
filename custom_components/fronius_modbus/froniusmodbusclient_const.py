SUPPORTED_MANUFACTURERS = ['Fronius']
SUPPORTED_MODELS = ['Primo GEN24', 'Symo GEN24', 'Verto']

SUNSPEC_ID_ADDRESS = 40000
SUNSPEC_FIRST_MODEL_HEADER_ADDRESS = 40002
SUNSPEC_ID_WORD_0 = 0x5375
SUNSPEC_ID_WORD_1 = 0x6E53
SUNSPEC_END_MODEL_ID = 0xFFFF
SUNSPEC_SCAN_MAX_MODELS = 128

COMMON_ADDRESS = 40004
INVERTER_ADDRESS = 40071
NAMEPLATE_ADDRESS = 40123
METER_ADDRESS = 40071
STORAGE_ADDRESS = 40345
STORAGE_CONTROL_MODE_ADDRESS = 40348
MINIMUM_RESERVE_ADDRESS = 40350
DISCHARGE_RATE_ADDRESS = 40355
CHARGE_RATE_ADDRESS = 40356
AC_LIMIT_RATE_ADDRESS = 40232
AC_LIMIT_ENABLE_ADDRESS = 40236
OUT_PF_SET_ADDRESS = 40237
OUT_PF_SET_ENABLE_ADDRESS = 40241
CONN_ADDRESS = 40231

STORAGE_CONTROL_MODE = {
    0: 'auto',
    1: 'charge',
    2: 'discharge',
    3: 'charge_and_discharge',
}

CHARGE_STATUS = {
    1: 'off',
    2: 'empty',
    3: 'discharging',
    4: 'charging',
    5: 'full',
    6: 'holding',
    7: 'testing',
}

INVERTER_STATUS = {
    1: 'off',
    2: 'sleeping',
    3: 'starting',
    4: 'normal',
    5: 'throttled',
    6: 'shutdown',
    7: 'fault',
    8: 'standby',
}

INVERTER_CONTROLS = [
    'power_reduction',
    'constant_reactive_power',
    'constant_power_factor',
]

INVERTER_EVENTS = [
    'Error',
    'Warning',
    'Info',
]

FRONIUS_INVERTER_STATUS = {
    1: 'off',
    2: 'sleeping',
    3: 'starting',
    4: 'normal',
    5: 'throttled',
    6: 'shutdown',
    7: 'fault',
    8: 'standby',
    9: 'no_solarnet',
    10: 'no_inverter_communication',
    11: 'overcurrent_solarnet',
    12: 'firmware_updating',
    13: 'acfi_event',
}

CHARGE_GRID_STATUS = {
    0: 'disabled',
    1: 'enabled',
}

GRID_STATUS = {
    0: 'off_grid',
    1: 'off_grid_operating',
    2: 'on_grid',
    3: 'on_grid_operating',
}

CONNECTION_STATUS = [ 
    'connected',
    'available',
    'operating',
]

CONNECTION_STATUS_CONDENSED = {
    0: 'disconnected',
    1: 'connected', 
    3: 'available', 
    7: 'operating', 
}

ECP_CONNECTION_STATUS = {
    0: 'disconnected',
    1: 'connected',
}

CONTROL_STATUS = {
    0: 'disabled',
    1: 'enabled',
}

AC_LIMIT_STATUS = {
    0: 'disabled',
    1: 'enabled',
}

STORAGE_EXT_CONTROL_MODE = {
    0: 'auto',
    1: 'pv_charge_limit',
    2: 'discharge_limit',
    3: 'pv_charge_and_discharge_limit',
    4: 'charge_from_grid',
    5: 'discharge_to_grid',
    6: 'block_discharging',
    7: 'block_charging',
}
