"""BYD Battery Box Class"""

import asyncio
import logging
import time
from functools import wraps
from typing import Any, Optional
from .extmodbusclient import ExtModbusClient

from .froniusmodbusclient_const import (
    INVERTER_ADDRESS,
    COMMON_ADDRESS,
    NAMEPLATE_ADDRESS,
    STORAGE_ADDRESS,
    METER_ADDRESS,
    AC_LIMIT_RATE_ADDRESS,
    AC_LIMIT_ENABLE_ADDRESS,
    OUT_PF_SET_ADDRESS,
    OUT_PF_SET_ENABLE_ADDRESS,
    CONN_ADDRESS,
    SUNSPEC_ID_ADDRESS,
    SUNSPEC_FIRST_MODEL_HEADER_ADDRESS,
    SUNSPEC_ID_WORD_0,
    SUNSPEC_ID_WORD_1,
    SUNSPEC_END_MODEL_ID,
    SUNSPEC_SCAN_MAX_MODELS,
    STORAGE_CONTROL_MODE,
    CHARGE_STATUS,
    CHARGE_GRID_STATUS,
    STORAGE_EXT_CONTROL_MODE,
    FRONIUS_INVERTER_STATUS,
    INVERTER_STATUS,
    CONNECTION_STATUS_CONDENSED,
    ECP_CONNECTION_STATUS,
    INVERTER_CONTROLS,
    INVERTER_EVENTS,
    CONTROL_STATUS,
    AC_LIMIT_STATUS,
    GRID_STATUS,
)

_LOGGER = logging.getLogger(__name__)
APPLY_TOGGLE_DELAY_SECONDS = 1.0
APPLY_TOGGLE_MASK_SECONDS = APPLY_TOGGLE_DELAY_SECONDS + 0.5


def _is_power_meter_model(model: str | None) -> bool:
    if not model:
        return False
    model_l = model.lower()
    return "meter" in model_l or "wattnode" in model_l or "42,0411" in model_l


def _safe_read(label: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            try:
                return await func(self, *args, **kwargs)
            except Exception as err:
                _LOGGER.warning(
                    "Failed reading Fronius %s data from %s:%s: %s",
                    label,
                    self._host,
                    self._port,
                    err,
                )
                _LOGGER.debug(
                    "Detailed Fronius %s read failure",
                    label,
                    exc_info=True,
                )
                return False

        return wrapper

    return decorator

class FroniusModbusClient(ExtModbusClient):
    """Hub for BYD Battery Box Interface"""

    def __init__(self, host: str, port: int, inverter_unit_id: int, meter_unit_ids, timeout: int) -> None:
        """Init hub."""
        super(FroniusModbusClient, self).__init__(host = host, port = port, unit_id=inverter_unit_id, timeout=timeout)

        self.initialized = False

        self._inverter_unit_id = inverter_unit_id
        self._meter_address_offset = int(meter_unit_ids[0]) if meter_unit_ids else 200
        self._meter_unit_ids = [self._meter_address_offset]
        self._primary_meter_unit_id = self._meter_address_offset

        self.meter_configured = False
        self.mppt_configured = False
        self.storage_configured = False
        self.storage_extended_control_mode = 0
        # Mode-write bookkeeping for the extended-mode re-derivation in
        # read_inverter_storage_data: a storage read can overlap a mode write
        # (the coordinator poll does not take the hub's write lock), and a
        # register value read before the write must not be taken as a mode
        # change made outside HA.
        self._mode_writes_in_progress = 0
        self._mode_write_seq = 0
        # Base control mode register as last written by change_settings() or
        # seen by a storage read; None until the first read.
        self._last_base_mode = None
        # True while the extended mode is one HA chose, False while it was
        # derived from the registers (startup, or a change made outside HA).
        # The limit/power setters claim it before they write, so a read that
        # lands between the write and the bookkeeping cannot re-derive.
        self._ext_mode_set_by_ha = False
        self.max_charge_rate_w = 11000
        self.max_discharge_rate_w = 11000
        self._storage_address = STORAGE_ADDRESS
        self.mppt_module_count = 2
        self.mppt_model_length = 88
        self._sunspec_models_by_id = {}
        self._sunspec_model_headers = []
        self._grid_frequency = 50
        self._grid_frequency_lower_bound = self._grid_frequency - 0.2
        self._grid_frequency_upper_bound = self._grid_frequency + 0.2

        self._inverter_frequency_lower_bound = self._grid_frequency - 5
        self._inverter_frequency_upper_bound = self._grid_frequency + 5

        self._ac_limit_enable_mask_until = 0.0
        self._power_factor_enable_mask_until = 0.0
        self._load_inverter_sample_ts: float | None = None
        self._load_meter_sample_ts: dict[int, float] = {}
        self._last_mppt_debug_summary: tuple[Any, ...] | None = None
        self.data = {}
        # Keys whose last total-increasing reading was rejected: warned once, then quiet.
        self._lfte_rejected: set[str] = set()
        self.reset_storage_info()

    def reset_storage_info(self) -> None:
        self.data["s_manufacturer"] = None
        self.data["s_model"] = "Battery Storage"
        self.data["s_serial"] = None

    def set_storage_info(
        self,
        manufacturer: str | None = None,
        model: str | None = None,
        serial: str | None = None,
    ) -> None:
        self.reset_storage_info()
        if manufacturer:
            self.data["s_manufacturer"] = manufacturer
        if model:
            self.data["s_model"] = model
        if serial:
            self.data["s_serial"] = serial

    def _meter_prefix(self, unit_id: int) -> str:
        return f"meter_{int(unit_id)}_"

    def _decode_reg(self, regs, start: int, data_type, length: int = 1):
        return self._client.convert_from_registers(
            regs[start:start + length],
            data_type=data_type,
        )

    def _decode_registers(self, regs, specs) -> dict[str, Any]:
        return {
            name: self._decode_reg(regs, start, data_type, length)
            for name, start, length, data_type in specs
        }

    def _set_calculated(
        self,
        key: str,
        raw_value,
        scale_factor,
        precision: int | None = None,
        minimum=None,
        maximum=None,
    ):
        value = self.calculate_value(raw_value, scale_factor, precision, minimum, maximum)
        self.data[key] = value
        return value

    def _set_mapped(self, key: str, mapping: dict, raw_value, field_name: str):
        value = self._map_value(mapping, raw_value, field_name)
        self.data[key] = value
        return value

    def start_load_poll_cycle(self) -> None:
        self._load_inverter_sample_ts = None
        self._load_meter_sample_ts = {}

    def get_load_sample_timestamps(self, unit_id: int) -> tuple[float | None, float | None]:
        return (
            self._load_inverter_sample_ts,
            self._load_meter_sample_ts.get(int(unit_id)),
        )

    @property
    def primary_meter_unit_id(self) -> int:
        return self._primary_meter_unit_id

    def set_meter_unit_ids(
        self,
        unit_ids: list[int] | tuple[int, ...] | None,
        primary_unit_id: int | None = None,
    ) -> None:
        normalized: list[int] = []
        seen: set[int] = set()
        for unit_id in unit_ids or []:
            if not self.is_numeric(unit_id):
                continue
            normalized_unit_id = int(unit_id)
            if normalized_unit_id <= 0 or normalized_unit_id in seen:
                continue
            seen.add(normalized_unit_id)
            normalized.append(normalized_unit_id)

        self._meter_unit_ids = normalized
        if (
            primary_unit_id is not None
            and self.is_numeric(primary_unit_id)
            and int(primary_unit_id) in seen
        ):
            self._primary_meter_unit_id = int(primary_unit_id)
        elif normalized:
            self._primary_meter_unit_id = normalized[0]
        else:
            self._primary_meter_unit_id = self._meter_address_offset

    def _map_value(self, values: dict, key: int, field_name: str):
        value = values.get(key)
        if value is None:
            _LOGGER.debug("Unknown %s code %s", field_name, key)
            return f"Unknown ({key})"
        return value

    def _storage_register_address(self, offset: int) -> int:
        return self._storage_address + offset

    def _get_ac_limit_rate_sf(self) -> Optional[int]:
        value = self.data.get("ac_limit_rate_sf")
        if not self.is_numeric(value):
            return None
        rate_sf = round(value)
        if rate_sf < -6 or rate_sf > 6:
            _LOGGER.error("Invalid AC limit scale factor: %s", rate_sf)
            return None
        return rate_sf

    def _get_inverter_max_power_w(self) -> Optional[float]:
        value = self.data.get("max_power")
        if not self.is_numeric(value):
            return None
        max_power_w = float(value)
        if max_power_w <= 0:
            return None
        return max_power_w

    def _ac_limit_raw_to_percent(self, raw_value: int) -> Optional[float]:
        rate_sf = self._get_ac_limit_rate_sf()
        if rate_sf is None or not self.is_numeric(raw_value):
            return None

        percent = float(raw_value) * (10 ** rate_sf)
        if percent < 0 or percent > 100:
            _LOGGER.error("AC limit percent out of range: raw=%s sf=%s pct=%s", raw_value, rate_sf, percent)
            return None
        return percent

    def _ac_limit_raw_to_watts(self, raw_value: int) -> Optional[int]:
        percent = self._ac_limit_raw_to_percent(raw_value)
        max_power_w = self._get_inverter_max_power_w()
        if percent is None or max_power_w is None:
            return None
        return int(round(max_power_w * percent / 100.0))

    def _get_power_factor_sf(self) -> Optional[int]:
        value = self.data.get("power_factor_sf")
        if not self.is_numeric(value):
            return None
        power_factor_sf = int(value)
        if power_factor_sf < -6 or power_factor_sf > 6:
            _LOGGER.error("Invalid power factor scale factor: %s", power_factor_sf)
            return None
        return power_factor_sf

    def _power_factor_raw_to_value(self, raw_value: int) -> Optional[float]:
        power_factor_sf = self._get_power_factor_sf()
        if power_factor_sf is None or not self.is_numeric(raw_value):
            return None

        value = float(raw_value) * (10 ** power_factor_sf)
        if value < -1 or value > 1:
            _LOGGER.error(
                "Power factor out of range: raw=%s sf=%s value=%s",
                raw_value,
                power_factor_sf,
                value,
            )
            return None
        return round(value, max(0, -power_factor_sf))

    def _power_factor_value_to_raw(self, value: float) -> Optional[int]:
        if not self.is_numeric(value):
            return None

        numeric_value = float(value)
        if numeric_value < -1 or numeric_value > 1:
            return None

        power_factor_sf = self._get_power_factor_sf()
        if power_factor_sf is None:
            return None

        raw_value = int(round(numeric_value / (10 ** power_factor_sf)))
        if raw_value < -32768 or raw_value > 32767:
            return None
        return raw_value

    def _ac_limit_watts_to_raw(self, watts: float) -> Optional[int]:
        if not self.is_numeric(watts):
            return None

        max_power_w = self._get_inverter_max_power_w()
        rate_sf = self._get_ac_limit_rate_sf()
        if max_power_w is None or rate_sf is None:
            return None

        clamped_watts = min(max(float(watts), 0.0), max_power_w)
        percent = (clamped_watts / max_power_w) * 100.0
        raw_unclamped = percent / (10 ** rate_sf)
        raw_max = int(round(100.0 / (10 ** rate_sf)))
        raw_value = int(round(raw_unclamped, abs(rate_sf)))
        return max(0, min(raw_max, raw_value))

    async def _read_enable_raw(self, address: int) -> Optional[int]:
        regs = await self.get_registers(
            unit_id=self._inverter_unit_id,
            address=address,
            count=1,
        )
        if regs is None:
            return None

        enable_raw = self._client.convert_from_registers(
            regs[0:1],
            data_type=self._client.DATATYPE.UINT16,
        )
        if not self.is_numeric(enable_raw):
            return None
        return int(enable_raw)

    async def _read_ac_limit_enable_raw(self) -> Optional[int]:
        return await self._read_enable_raw(AC_LIMIT_ENABLE_ADDRESS)

    async def _read_power_factor_enable_raw(self) -> Optional[int]:
        return await self._read_enable_raw(OUT_PF_SET_ENABLE_ADDRESS)

    def _set_ac_limit_enable_state(self, enable_raw: int) -> None:
        self.data['ac_limit_enable'] = AC_LIMIT_STATUS.get(enable_raw, 'Unknown')

    def _set_ac_limit_control_state(self, enable_raw: int) -> None:
        self.data['WMaxLim_Ena'] = self._map_value(CONTROL_STATUS, enable_raw, 'throttle control')
        self._set_ac_limit_enable_state(enable_raw)

    def _set_power_factor_enable_state(self, enable_raw: int) -> None:
        status = self._map_value(CONTROL_STATUS, enable_raw, 'fixed power factor')
        self.data['OutPFSet_Ena'] = status
        self.data['power_factor_enable'] = status

    def _set_ac_limit_rate_values(self, raw_value: int | None) -> None:
        self.data['ac_limit_rate_raw'] = raw_value
        self.data['ac_limit_rate_pct'] = self._ac_limit_raw_to_percent(raw_value)
        self.data['ac_limit_rate'] = self._ac_limit_raw_to_watts(raw_value)

    def _rate_watts_to_percent(self, value_w: float, max_rate_w: float) -> float:
        if value_w > max_rate_w:
            return 100
        if value_w < max_rate_w * -1:
            return -100
        return value_w / max_rate_w * 100

    async def _write_signed_percent_register(self, address: int, rate: float) -> None:
        raw_rate = int(65536 + (rate * 100)) if rate < 0 else int(round(rate * 100))
        await self.write_registers(
            unit_id=self._inverter_unit_id,
            address=address,
            payload=[raw_rate],
        )

    def _set_storage_transfer_data(
        self,
        direction: str,
        module_id: int | None,
        module_current: dict[int, Any],
        module_voltage: dict[int, Any],
        module_power: dict[int, Any],
        module_lfte: dict[int, Any],
    ) -> None:
        self.data[f'storage_{direction}_module'] = module_id
        self.data[f'storage_{direction}_current'] = module_current.get(module_id) if module_id else None
        self.data[f'storage_{direction}_voltage'] = module_voltage.get(module_id) if module_id else None
        self.data[f'storage_{direction}_power'] = module_power.get(module_id) if module_id else None
        self.data[f'storage_{direction}_lfte'] = (
            self.protect_lfte(
                f'storage_{direction}_lfte',
                module_lfte.get(module_id),
            )
            if module_id
            else None
        )

    async def _set_named_mode(
        self,
        *,
        mode: int,
        charge_limit: float,
        discharge_limit: float,
        extended_mode: int,
        log_message: str,
        grid_charge_power: float = 0,
        grid_discharge_power: float = 0,
    ) -> None:
        await self.change_settings(
            mode=mode,
            charge_limit=charge_limit,
            discharge_limit=discharge_limit,
            grid_charge_power=grid_charge_power,
            grid_discharge_power=grid_discharge_power,
            extended_mode=extended_mode,
        )
        _LOGGER.info(log_message)

    async def _pulse_enable_for_apply(
        self,
        *,
        read_enable_raw,
        enable_address: int,
        mask_attr: str,
        set_enabled_state,
    ) -> tuple[Optional[int], bool]:
        enable_raw = await read_enable_raw()
        was_enabled = enable_raw == 1

        if not was_enabled:
            setattr(self, mask_attr, 0.0)
            if enable_raw is not None:
                set_enabled_state(int(enable_raw))
            return enable_raw, False

        setattr(self, mask_attr, time.monotonic() + APPLY_TOGGLE_MASK_SECONDS)
        await self.write_registers(
            unit_id=self._inverter_unit_id,
            address=enable_address,
            payload=[0],
        )
        return enable_raw, True

    def _sanitize_mppt_u16(self, value: Optional[int]) -> Optional[int]:
        if not self.is_numeric(value):
            return None
        sanitized = int(value)
        if sanitized == 0xFFFF:
            return None
        return sanitized

    def _sanitize_mppt_u32(self, value: Optional[int]) -> Optional[int]:
        if not self.is_numeric(value):
            return None
        sanitized = int(value)
        if sanitized == 0xFFFFFFFF:
            return None
        return sanitized

    def _update_storage_base_address(self, mppt_model_length: int, mppt_data_address: Optional[int] = None):
        if not self.is_numeric(mppt_model_length):
            return

        model_length = int(mppt_model_length)
        if model_length <= 0 or model_length > 4096:
            return

        if not self.is_numeric(mppt_data_address):
            return

        # Storage model data starts directly behind the next model header after model 160.
        # candidate = mppt_data_address + model_length + 2 (skip next model header ID + L).
        candidate = int(mppt_data_address) + model_length + 2
        if candidate < 40000 or candidate > 50000:
            return

        self.mppt_model_length = model_length
        self._storage_address = candidate
        self.data["storage_model_address"] = candidate

    def _get_sunspec_model(self, model_id: int):
        models = self._sunspec_models_by_id.get(model_id)
        if not models:
            return None
        return models[0]

    async def _scan_sunspec_models(self, force: bool = False) -> bool:
        if self._sunspec_model_headers and not force:
            return True

        sid_regs = await self.get_registers(
            unit_id=self._inverter_unit_id,
            address=SUNSPEC_ID_ADDRESS,
            count=2,
        )
        if sid_regs is None or len(sid_regs) != 2:
            return False
        if sid_regs[0] != SUNSPEC_ID_WORD_0 or sid_regs[1] != SUNSPEC_ID_WORD_1:
            _LOGGER.error("Invalid SunSpec SID at %s: %s", SUNSPEC_ID_ADDRESS, sid_regs)
            return False

        models_by_id = {}
        model_headers = []
        header_address = SUNSPEC_FIRST_MODEL_HEADER_ADDRESS

        for _ in range(SUNSPEC_SCAN_MAX_MODELS):
            header_regs = await self.get_registers(
                unit_id=self._inverter_unit_id,
                address=header_address,
                count=2,
            )
            if header_regs is None or len(header_regs) != 2:
                return False

            model_id = int(header_regs[0])
            model_length = int(header_regs[1])
            if model_id == SUNSPEC_END_MODEL_ID:
                break
            if model_id <= 0 or model_length <= 0 or model_length > 4096:
                _LOGGER.error(
                    "Invalid SunSpec model header at %s: id=%s length=%s",
                    header_address,
                    model_id,
                    model_length,
                )
                return False

            model_entry = {
                "id": model_id,
                "length": model_length,
                "id_address": header_address,
                "l_address": header_address + 1,
                "data_address": header_address + 2,
            }
            model_headers.append(model_entry)
            if model_id not in models_by_id:
                models_by_id[model_id] = []
            models_by_id[model_id].append(model_entry)

            header_address = model_entry["data_address"] + model_length

        if not model_headers:
            return False

        self._sunspec_models_by_id = models_by_id
        self._sunspec_model_headers = model_headers
        self.data["sunspec_model_count"] = len(model_headers)
        return True

    async def init_data(self):
        await self.connect()
        try:
            result = await self.read_device_info_data(prefix='i_', unit_id=self._inverter_unit_id)
        except Exception as e:
            _LOGGER.error(f"Error reading inverter info {self._host}:{self._port} unit id: {self._inverter_unit_id}", exc_info=True)
            raise Exception(f"Error reading inverter info unit id: {self._inverter_unit_id}")
        if result == False:
            _LOGGER.error(f"Empty inverter info {self._host}:{self._port} unit id: {self._inverter_unit_id}")
            raise Exception(f"Empty inverter info unit id: {self._inverter_unit_id}")

        try:
            if await self.read_mppt_data():
                self.mppt_configured = True
        except Exception as e:
            _LOGGER.warning(f"Error while checking mppt data {e}")

        discovered_meter_unit_ids: list[int] = []
        for unit_id in self._meter_unit_ids:
            prefix = self._meter_prefix(unit_id)
            try:
                result = await self.read_device_info_data(prefix=prefix, unit_id=unit_id)
            except Exception:
                _LOGGER.debug(
                    "Meter info probe failed for configured unit %s on %s:%s",
                    unit_id,
                    self._host,
                    self._port,
                    exc_info=True,
                )
                continue

            if not result:
                continue

            model = str(self.data.get(prefix + "model") or "").strip()
            if _is_power_meter_model(model):
                discovered_meter_unit_ids.append(unit_id)

        self._meter_unit_ids = discovered_meter_unit_ids
        self.meter_configured = bool(self._meter_unit_ids)

        if self.meter_configured:
            _LOGGER.info(
                "Configured Fronius smart meter unit ids on %s:%s: %s",
                self._host,
                self._port,
                self._meter_unit_ids,
            )

        if await self.read_inverter_nameplate_data() == False:
            _LOGGER.error(f"Error reading nameplate data", exc_info=True)
        elif self.mppt_configured:
            # Re-evaluate MPPT channels after storage detection from nameplate data.
            await self.read_mppt_data()

        _LOGGER.debug(
            "Initialized Fronius Modbus client for %s:%s: inverter_model=%s meter_unit_ids=%s storage_configured=%s storage_model_address=%s mppt_visible_modules=%s sunspec_model_count=%s",
            self._host,
            self._port,
            self.data.get("i_model"),
            self._meter_unit_ids,
            self.storage_configured,
            self.data.get("storage_model_address"),
            self.data.get("mppt_visible_module_ids"),
            self.data.get("sunspec_model_count"),
        )

        return True

    @_safe_read("device info")
    async def read_device_info_data(self, prefix, unit_id):
        regs = await self.get_registers(unit_id=unit_id, address=COMMON_ADDRESS, count=65)
        if regs is None:
            return False

        for field, start, end in (
            ('manufacturer', 0, 16),
            ('model', 16, 32),
            ('options', 32, 40),
            ('sw_version', 40, 48),
            ('serial', 48, 64),
        ):
            self.data[prefix + field] = self.get_string_from_registers(regs[start:end])
        self.data[prefix + 'unit_id'] = self._decode_reg(
            regs,
            64,
            self._client.DATATYPE.UINT16,
        )

        return True

    @_safe_read("inverter")
    async def read_inverter_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=INVERTER_ADDRESS, count=50)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('A', 0, 1, dt.UINT16), ('AphA', 1, 1, dt.UINT16), ('AphB', 2, 1, dt.UINT16),
                ('AphC', 3, 1, dt.UINT16), ('A_SF', 4, 1, dt.INT16), ('PPVphAB', 5, 1, dt.UINT16),
                ('PPVphBC', 6, 1, dt.UINT16), ('PPVphCA', 7, 1, dt.UINT16), ('PhVphA', 8, 1, dt.UINT16),
                ('PhVphB', 9, 1, dt.UINT16), ('PhVphC', 10, 1, dt.UINT16), ('V_SF', 11, 1, dt.INT16),
                ('W', 12, 1, dt.INT16), ('W_SF', 13, 1, dt.INT16), ('Hz', 14, 1, dt.UINT16),
                ('Hz_SF', 15, 1, dt.INT16), ('VAr', 18, 1, dt.INT16), ('VAr_SF', 19, 1, dt.INT16),
                ('WH', 22, 2, dt.UINT32), ('WH_SF', 24, 1, dt.INT16), ('St', 36, 1, dt.UINT16),
                ('StVnd', 37, 1, dt.UINT16), ('EvtVnd2', 44, 2, dt.UINT32),
            ),
        )

        for key in ('A', 'AphA', 'AphB', 'AphC'):
            self._set_calculated(key, raw[key], raw['A_SF'], 3, 0, 1000)
        for key in ('PPVphAB', 'PPVphBC', 'PPVphCA', 'PhVphA', 'PhVphB', 'PhVphC'):
            self._set_calculated(key, raw[key], raw['V_SF'])
        self._set_calculated("acpower", raw['W'], raw['W_SF'], 2, -50000, 50000)
        self._set_calculated("var", raw['VAr'], raw['VAr_SF'], 2, -50000, 50000)
        self._set_calculated("line_frequency", raw['Hz'], raw['Hz_SF'], 2, 0, 100)
        self._set_calculated("acenergy", raw['WH'], raw['WH_SF'])
        self._set_mapped("status", INVERTER_STATUS, raw['St'], "inverter status")
        self._set_mapped("statusvendor", FRONIUS_INVERTER_STATUS, raw['StVnd'], "inverter status")
        self.data["statusvendor_id"] = raw['StVnd']
        self.data["events2"] = self.bitmask_to_string(raw['EvtVnd2'], INVERTER_EVENTS, default='None', bits=32)
        self._load_inverter_sample_ts = time.monotonic()

        return True

    @_safe_read("inverter nameplate")
    async def read_inverter_nameplate_data(self):
        """start reading storage data"""
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=NAMEPLATE_ADDRESS, count=120)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('DERTyp', 0, 1, dt.UINT16), ('WHRtg', 17, 1, dt.UINT16), ('WHRtg_SF', 18, 1, dt.INT16),
                ('MaxChaRte', 21, 1, dt.UINT16), ('MaxChaRte_SF', 22, 1, dt.INT16),
                ('MaxDisChaRte', 23, 1, dt.UINT16), ('MaxDisChaRte_SF', 24, 1, dt.INT16),
            ),
        )

        has_storage_ratings = any(
            self.is_numeric(value) and 0 < value < 65535
            for value in [raw['WHRtg'], raw['MaxChaRte'], raw['MaxDisChaRte']]
        )
        if raw['DERTyp'] == 82 or has_storage_ratings:
            self.storage_configured = True
        self.data['DERTyp'] = raw['DERTyp']
        self._set_calculated('WHRtg', raw['WHRtg'], raw['WHRtg_SF'], 0)
        self._set_calculated('MaxChaRte', raw['MaxChaRte'], raw['MaxChaRte_SF'], 0)
        self._set_calculated('MaxDisChaRte', raw['MaxDisChaRte'], raw['MaxDisChaRte_SF'], 0)
    
        if self.is_numeric(self.data['MaxChaRte']):
            self.max_charge_rate_w = self.data['MaxChaRte']
        if self.is_numeric(self.data['MaxDisChaRte']):
            self.max_discharge_rate_w = self.data['MaxDisChaRte']

        return True

    @_safe_read("inverter status")
    async def read_inverter_status_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=40183, count=44)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('PVConn', 0, 1, dt.UINT16), ('StorConn', 1, 1, dt.UINT16),
                ('ECPConn', 2, 1, dt.UINT16), ('StActCtl', 33, 2, dt.UINT32),
                ('Ris', 42, 1, dt.UINT16), ('Ris_SF', 43, 1, dt.INT16),
            ),
        )

        self._set_mapped('pv_connection', CONNECTION_STATUS_CONDENSED, raw['PVConn'], 'pv connection')
        self._set_mapped('storage_connection', CONNECTION_STATUS_CONDENSED, raw['StorConn'], 'storage connection')
        self.storage_configured = raw['StorConn'] in {1, 3, 7}
        self._set_mapped('ecp_connection', ECP_CONNECTION_STATUS, raw['ECPConn'], 'electrical connection')
        self.data['inverter_controls'] = self.bitmask_to_string(raw['StActCtl'], INVERTER_CONTROLS, 'Normal')
        # Adjust the scaling factor because isolation resistance is provided
        # in Ohm and stored in Mega Ohm.
        self._set_calculated('isolation_resistance', raw['Ris'], raw['Ris_SF'] - 6)

        return True

    @_safe_read("inverter settings")
    async def read_inverter_model_settings_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=40151, count=30)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('WMax', 0, 1, dt.UINT16), ('VRef', 1, 1, dt.UINT16), ('VRefOfs', 2, 1, dt.INT16),
                ('WMax_SF', 20, 1, dt.INT16), ('VRef_SF', 21, 1, dt.INT16),
                ('VRefOfs_SF', 22, 1, dt.INT16),
            ),
        )

        self._set_calculated('max_power', raw['WMax'], raw['WMax_SF'], 2, 0, 50000)
        self._set_calculated('vref', raw['VRef'], raw['VRef_SF'])
        self._set_calculated('vrefofs', raw['VRefOfs'], raw['VRefOfs_SF'])

        return True

    @_safe_read("inverter controls")
    async def read_inverter_controls_data(self):
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=40229, count=24)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('Conn', 2, 1, dt.UINT16), ('WMaxLim_Ena', 7, 1, dt.UINT16),
                ('OutPFSet', 8, 1, dt.INT16), ('OutPFSet_Ena', 12, 1, dt.UINT16),
                ('VArPct_Ena', 20, 1, dt.UINT16), ('WMaxLimPct_SF', 21, 1, dt.INT16),
                ('OutPFSet_SF', 22, 1, dt.INT16),
            ),
        )

        self._set_mapped('Conn', CONTROL_STATUS, raw['Conn'], 'connection control')
        if time.monotonic() < self._ac_limit_enable_mask_until:
            self._set_ac_limit_control_state(1)
        else:
            self._set_ac_limit_control_state(raw['WMaxLim_Ena'])
        if time.monotonic() < self._power_factor_enable_mask_until:
            self._set_power_factor_enable_state(1)
        else:
            self._set_power_factor_enable_state(raw['OutPFSet_Ena'])
            if raw['OutPFSet_Ena'] == 1:
                self._power_factor_enable_mask_until = 0.0
        self._set_mapped('VArPct_Ena', CONTROL_STATUS, raw['VArPct_Ena'], 'VAr control')
        self.data['ac_limit_rate_sf'] = raw['WMaxLimPct_SF']
        self.data['power_factor_sf'] = raw['OutPFSet_SF']
        self.data['power_factor'] = self._power_factor_raw_to_value(raw['OutPFSet'])

        return True

    def protect_lfte(self, key, value):
        ''' ensure lfte values are monotonically increasing to fullfil the properties of SensorStateClass.TOTAL_INCREASING.
            Therfore this function returns the previous, last known good value, in case the modbus read was erroneus:
            * the current value from modbus is None or 0 (SunSpec: an accumulator of 0 is "not accumulated")
            * the current value from modbus is smaller than the previous value
            * the current value from modbus is much larger then the previous value
            This avoids wrong spikes in consumption / production on the energy dashboard.
            A rejected reading is logged once; the inverter serves the same bad value on every
            poll after a firmware update (#125), and a warning per poll buries the log.
        '''

        previous = self.data.get(key)
        if value is None or value == 0:
            # No reading, not a reset: SunSpec accumulators report 0 while the
            # device has nothing accumulated yet, e.g. right after a firmware
            # update. Checked before the first-value branches: a start during
            # that phase must not adopt 0 as the baseline, or the real counter
            # would then exceed the step limit and be rejected for good.
            _LOGGER.debug("No accumulated value for %s yet, keeping %s", key, previous)
            return previous
        elif previous is None:
            _LOGGER.debug("Initializing total-increasing guard for %s=%s", key, value)
            return value
        elif value < self.data[key]:
            self._reject_lfte(key, value, "below")
            return self.data[key]
        elif value > self.data[key] + 100000:
            # we allow steps of 100 kWh. Usually, at a typicall rate every 10 seconds the steps should be far below.
            # However, when data transfer is not working for minutes or even an hour it could become relevant.
            # Also, wrong values are often by orders of magnitude to large, which should still be avoided by this check.
            self._reject_lfte(key, value, "above")
            return self.data[key]
        else:
            if key in self._lfte_rejected:
                self._lfte_rejected.discard(key)
                _LOGGER.info("%s reads plausibly again: %s", key, value)
            return value

    def _reject_lfte(self, key, value, direction: str) -> None:
        log = _LOGGER.debug if key in self._lfte_rejected else _LOGGER.warning
        self._lfte_rejected.add(key)
        log("Received implausible %s=%s %s previous plausible value %s", key, value, direction, self.data[key])

    @_safe_read("mppt")
    async def read_mppt_data(self):
        if not await self._scan_sunspec_models():
            return False

        mppt_model = self._get_sunspec_model(160)
        if mppt_model is None:
            return False

        mppt_model_length = int(mppt_model["length"])
        if mppt_model_length < 20 or mppt_model_length > 200:
            return False

        mppt_read_address = int(mppt_model["l_address"])
        self._update_storage_base_address(
            mppt_model_length,
            mppt_data_address=mppt_model["data_address"],
        )
        self.data['mppt_model_length'] = mppt_model_length
        self.data['mppt_register_address'] = mppt_read_address
        self.data['mppt_model_id_address'] = int(mppt_model["id_address"])

        storage_model = self._get_sunspec_model(124)
        if storage_model is not None:
            storage_model_id = int(storage_model["id"])
            storage_model_length = int(storage_model["length"])
            self.data['storage_model_id'] = storage_model_id
            self.data['storage_model_length'] = storage_model_length
            self.data['storage_model_address'] = int(storage_model["data_address"])
            if storage_model_length == 24:
                self._storage_address = int(storage_model["data_address"])
                self.storage_configured = True

        model_register_count = mppt_model_length
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=mppt_read_address, count=model_register_count)
        if regs is None:
            return False

        model_limit = len(regs)
        if model_limit < 8:
            return False

        DCA_SF = self._client.convert_from_registers(regs[1:2], data_type=self._client.DATATYPE.INT16)
        DCV_SF = self._client.convert_from_registers(regs[2:3], data_type=self._client.DATATYPE.INT16)
        DCW_SF = self._client.convert_from_registers(regs[3:4], data_type=self._client.DATATYPE.INT16)
        DCWH_SF = self._client.convert_from_registers(regs[4:5], data_type=self._client.DATATYPE.INT16)
        reported_module_count = self._client.convert_from_registers(regs[6:7], data_type=self._client.DATATYPE.UINT16)
        if not self.is_numeric(reported_module_count) or int(reported_module_count) <= 0:
            return False

        max_modules_by_length = (model_limit - 2) // 20
        module_count = min(int(reported_module_count), int(max_modules_by_length))
        if module_count <= 0:
            return False

        self.mppt_module_count = module_count
        self.data['mppt_module_count'] = module_count

        def read_u16(index: int):
            if index + 1 > model_limit:
                return None
            return self._client.convert_from_registers(regs[index:index + 1], data_type=self._client.DATATYPE.UINT16)

        def read_u32(index: int):
            if index + 2 > model_limit:
                return None
            return self._client.convert_from_registers(regs[index:index + 2], data_type=self._client.DATATYPE.UINT32)

        module_power = {}
        module_lfte = {}
        module_tms = {}
        module_labels = {}
        module_current = {}
        module_voltage = {}

        for module_id in range(1, module_count + 1):
            label_idx = 20 * (module_id - 1) + 9
            current_idx = 20 * module_id - 2
            voltage_idx = 20 * module_id - 1
            power_idx = 20 * module_id
            lfte_idx = power_idx + 1
            tms_idx = lfte_idx + 2

            label = None
            if label_idx + 8 <= model_limit:
                try:
                    label = self.get_string_from_registers(regs[label_idx:label_idx + 8])
                except Exception:
                    label = None
            module_labels[module_id] = label

            raw_current = self._sanitize_mppt_u16(read_u16(current_idx))
            raw_voltage = self._sanitize_mppt_u16(read_u16(voltage_idx))
            raw_power = self._sanitize_mppt_u16(read_u16(power_idx))
            raw_lfte = self._sanitize_mppt_u32(read_u32(lfte_idx))
            raw_tms = self._sanitize_mppt_u32(read_u32(tms_idx))

            module_current[module_id] = self.calculate_value(raw_current, DCA_SF, 2, 0, 100) if raw_current is not None else None
            module_voltage[module_id] = self.calculate_value(raw_voltage, DCV_SF, 2, 0, 1500) if raw_voltage is not None else None
            module_power[module_id] = self.calculate_value(raw_power, DCW_SF, 2, 0, 15000) if raw_power is not None else None
            if raw_lfte is None:
                module_lfte[module_id] = None

            elif raw_lfte == 0 and raw_current is None and raw_voltage is None and raw_power is None:
                module_lfte[module_id] = None
            else:
                module_lfte[module_id] = self.calculate_value(raw_lfte, DCWH_SF)
            module_tms[module_id] = raw_tms

            self.data[f'module{module_id}_label'] = label
            self.data[f'module{module_id}_power'] = module_power[module_id]
            self.data[f'module{module_id}_lfte'] = module_lfte[module_id]
            self.data[f'module{module_id}_tms'] = module_tms[module_id]

            module_idx = module_id - 1
            self.data[f'mppt_module_{module_idx}_label'] = label
            self.data[f'mppt_module_{module_idx}_dc_current'] = module_current[module_id]
            self.data[f'mppt_module_{module_idx}_dc_voltage'] = module_voltage[module_id]
            self.data[f'mppt_module_{module_idx}_dc_power'] = module_power[module_id]
            self.data[f'mppt_module_{module_idx}_lifetime_energy'] = self.protect_lfte(
                f'mppt_module_{module_idx}_lifetime_energy',
                module_lfte[module_id],
            )
            self.data[f'mppt_module_{module_idx}_timestamp'] = module_tms[module_id]

        storage_charge_module = None
        storage_discharge_module = None
        for module_id, label in module_labels.items():
            if not isinstance(label, str):
                continue
            normalized = label.replace(" ", "").upper()
            if "STDISCHA" in normalized:
                storage_discharge_module = module_id
            elif normalized.startswith("STCHA"):
                storage_charge_module = module_id

        # If labels are unavailable, use the last two channels as storage channels.
        if self.storage_configured and not (storage_charge_module and storage_discharge_module) and module_count >= 4:
            storage_charge_module = module_count - 1
            storage_discharge_module = module_count

        if self.storage_configured and storage_charge_module and storage_discharge_module:
            self._set_storage_transfer_data(
                'charge',
                storage_charge_module,
                module_current,
                module_voltage,
                module_power,
                module_lfte,
            )
            self._set_storage_transfer_data(
                'discharge',
                storage_discharge_module,
                module_current,
                module_voltage,
                module_power,
                module_lfte,
            )
        else:
            self._set_storage_transfer_data('charge', None, module_current, module_voltage, module_power, module_lfte)
            self._set_storage_transfer_data('discharge', None, module_current, module_voltage, module_power, module_lfte)

        pv_modules = []
        for module_id, label in module_labels.items():
            if isinstance(label, str) and "MPPT" in label.upper():
                pv_modules.append(module_id)
        if not pv_modules:
            if storage_charge_module and storage_discharge_module:
                pv_modules = [module_id for module_id in range(1, module_count + 1) if module_id not in [storage_charge_module, storage_discharge_module]]
            else:
                pv_modules = list(range(1, module_count + 1))

        self.data['mppt_visible_module_ids'] = pv_modules
        pv_values = [module_power.get(module_id) for module_id in pv_modules if self.is_numeric(module_power.get(module_id))]
        self.data['pv_power'] = round(sum(pv_values), 2) if pv_values else None
        mppt_debug_summary = (
            mppt_read_address,
            mppt_model_length,
            reported_module_count,
            tuple(pv_modules),
            storage_charge_module,
            storage_discharge_module,
        )
        if mppt_debug_summary != self._last_mppt_debug_summary:
            _LOGGER.debug(
                "Parsed model 160 MPPT data: address=%s length=%s reported_count=%s visible_modules=%s storage_charge_module=%s storage_discharge_module=%s labels=%s",
                mppt_read_address,
                mppt_model_length,
                reported_module_count,
                pv_modules,
                storage_charge_module,
                storage_discharge_module,
                module_labels,
            )
            self._last_mppt_debug_summary = mppt_debug_summary

        return True

    def _rates_fit_extended_mode(self, in_rate, out_rate) -> bool:
        """Whether HA writes in the current extended mode can have produced these rates.

        Signs only: the limit and power numbers move one rate within its sign,
        the other rate stays as the mode wrote it.
        """
        mode = self.storage_extended_control_mode
        if mode == 4:
            return in_rate >= 0 and out_rate <= 0
        if mode == 5:
            return in_rate <= 0 and out_rate >= 0
        if mode == 6:
            return in_rate >= 0 and out_rate == 0
        if mode == 7:
            return in_rate == 0 and out_rate >= 0
        return in_rate >= 0 and out_rate >= 0

    @_safe_read("storage")
    async def read_inverter_storage_data(self):
        """start reading storage data"""
        mode_write_seq = self._mode_write_seq
        regs = await self.get_registers(unit_id=self._inverter_unit_id, address=self._storage_address, count=24)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('max_charge', 0, 1, dt.UINT16), ('WChaGra', 1, 1, dt.UINT16), ('WDisChaGra', 2, 1, dt.UINT16),
                ('storage_control_mode', 3, 1, dt.UINT16), ('minimum_reserve', 5, 1, dt.UINT16),
                ('charge_state', 6, 1, dt.UINT16), ('charge_status', 9, 1, dt.UINT16),
                ('discharge_power', 10, 1, dt.INT16), ('charge_power', 11, 1, dt.INT16),
                ('charge_grid_set', 15, 1, dt.UINT16),
            ),
        )
        # WChaMax_SF: Scale factor for maximum charge. 0
        # WChaDisChaGra_SF: Scale factor for maximum charge and discharge rate. 0
        # VAChaMax_SF: not supported
        # MinRsvPct_SF: Scale factor for minimum reserve percentage. -2
        # ChaState_SF: Scale factor for available energy percent. -2
        # StorAval_SF: not supported
        # InBatV_SF: not supported
        # InOutWRte_SF: Scale factor for percent charge/discharge rate. -2

        if self.is_numeric(raw['max_charge']) and raw['max_charge'] > 0:
            self.storage_configured = True

        soc_minimum_value = self.calculate_value(raw['minimum_reserve'], -2, 2, 0, 100)
        if self.is_numeric(soc_minimum_value) and float(soc_minimum_value).is_integer():
            soc_minimum_value = int(soc_minimum_value)

        self._set_mapped('grid_charging', CHARGE_GRID_STATUS, raw['charge_grid_set'], 'grid charging')
        self._set_mapped('charge_status', CHARGE_STATUS, raw['charge_status'], 'charge status')
        self.data['soc_minimum'] = soc_minimum_value
        self._set_calculated('discharging_power', raw['discharge_power'], -2, 2, -100, 100)
        self._set_calculated('charging_power', raw['charge_power'], -2, 2, -100, 100)
        self._set_calculated('soc', raw['charge_state'], -2, 2, 0, 100)
        self._set_calculated('max_charge', raw['max_charge'], 0, 0)
        self._set_calculated('WChaGra', raw['WChaGra'], 0, 0)
        self._set_calculated('WDisChaGra', raw['WDisChaGra'], 0, 0)

        mapped_control_mode = self._map_value(STORAGE_CONTROL_MODE, raw['storage_control_mode'], 'storage control mode')
        normalized_control_mode = mapped_control_mode
        if raw['discharge_power'] < 0:
            normalized_control_mode = STORAGE_CONTROL_MODE.get(1, mapped_control_mode)
        elif raw['storage_control_mode'] == 2 and raw['charge_grid_set'] == 1 and raw['discharge_power'] == 0:
            normalized_control_mode = STORAGE_CONTROL_MODE.get(1, mapped_control_mode)
        elif raw['storage_control_mode'] == 1 and self.storage_extended_control_mode == 5 and raw['charge_power'] == 0:
            normalized_control_mode = STORAGE_CONTROL_MODE.get(2, mapped_control_mode)
        elif raw['charge_power'] < 0:
            normalized_control_mode = STORAGE_CONTROL_MODE.get(2, mapped_control_mode)
        control_mode = self.data.get('control_mode')
        if control_mode is None or control_mode != normalized_control_mode:
            if raw['discharge_power'] >= 0:
                self.data['discharge_limit'] = raw['discharge_power'] / 100.0 
                self.data['grid_charge_power'] = 0
            else: 
                self.data['grid_charge_power'] = (raw['discharge_power'] * -1) / 100.0 
                self.data['discharge_limit'] = 0
            if raw['charge_power'] >= 0:
                self.data['charge_limit'] = raw['charge_power'] / 100 
                self.data['grid_discharge_power'] = 0
            else: 
                self.data['grid_discharge_power'] = (raw['charge_power'] * -1) / 100.0 
                self.data['charge_limit'] = 0

            self.data['control_mode'] = normalized_control_mode

        # Extended storage control mode. Who set the mode decides how it is
        # maintained:
        # - HA set it (change_settings, or a limit/power number written in the
        #   current mode): keep it while the base control mode register stays
        #   as written. The registers alone cannot tell it apart - Discharge
        #   to Grid at 0 W is base 1 with InWRte 0, the same as Block Charging
        #   - and a charge limit reaching 0 in mode 1 must not flip the select.
        # - Somebody else set it (Solar.web, evcc, the inverter itself, or the
        #   state found at startup): there is no HA choice to protect, so the
        #   mode follows the registers on every read. evcc "hold" and "charge"
        #   share base 2 and differ only in OutWRte.
        # The base register differing from the value HA last wrote or the
        # previous read saw means somebody else took over. So do rates that
        # no HA write in the chosen mode can produce (see
        # _rates_fit_extended_mode), which also covers a rate the inverter
        # silently did not take. Comparing the base mode against
        # that value, not against the base modes an extended mode allows,
        # matters because those overlap: Discharge to Grid and Block Charging
        # both live on base 1 and 3. Without this the stale extended mode
        # sticks until a config-entry reload, and with it the select state and
        # the availability of the mode-gated number entities. Skipped while a
        # mode write is running or finished during this read, because the
        # registers may predate that write.
        ext_control_mode = self.data.get('ext_control_mode')
        if self._mode_writes_in_progress == 0 and self._mode_write_seq == mode_write_seq:
            if self._last_base_mode is not None and raw['storage_control_mode'] != self._last_base_mode:
                self._ext_mode_set_by_ha = False
            self._last_base_mode = raw['storage_control_mode']
            if not self._rates_fit_extended_mode(raw['charge_power'], raw['discharge_power']):
                self._ext_mode_set_by_ha = False
            if not self._ext_mode_set_by_ha:
                ext_control_mode = None
        if ext_control_mode is None:
            if raw['storage_control_mode'] == 0:
                ext_control_mode = 0
            elif raw['storage_control_mode'] in [1, 3] and raw['charge_power'] == 0:
                ext_control_mode = 7
            elif raw['storage_control_mode'] == 1 and raw['charge_power'] < 0:
                # what set_grid_discharge_mode() plus a target power writes
                ext_control_mode = 5
            elif raw['storage_control_mode'] == 1:
                ext_control_mode = 1
            elif raw['storage_control_mode'] in [2, 3] and raw['discharge_power'] < 0:
                ext_control_mode = 4
            elif raw['storage_control_mode'] in [2, 3] and raw['charge_power'] < 0:
                ext_control_mode = 5
            elif raw['storage_control_mode'] in [2, 3] and raw['discharge_power'] == 0:
                ext_control_mode = 6
            elif raw['storage_control_mode'] == 2:
                ext_control_mode = 2
            elif raw['storage_control_mode'] == 3:
                ext_control_mode = 3
            if not ext_control_mode is None:
                if self.data.get('ext_control_mode') is not None and ext_control_mode != self.storage_extended_control_mode:
                    _LOGGER.info(
                        "Storage mode changed outside HA (base mode %s), extended mode %s -> %s",
                        raw['storage_control_mode'],
                        self.storage_extended_control_mode,
                        ext_control_mode,
                    )
                self.data['ext_control_mode'] = self._map_value(STORAGE_EXT_CONTROL_MODE, ext_control_mode, 'extended storage mode')
                self.storage_extended_control_mode = ext_control_mode

        return True

    @_safe_read("meter")
    async def read_meter_data(self, unit_id, is_primary=False):
        """start reading meter data"""
        meter_prefix = self._meter_prefix(unit_id)
        regs = await self.get_registers(unit_id=unit_id, address=METER_ADDRESS, count=103)
        if regs is None:
            return False

        dt = self._client.DATATYPE
        raw = self._decode_registers(
            regs,
            (
                ('A', 0, 1, dt.INT16), ('AphA', 1, 1, dt.INT16), ('AphB', 2, 1, dt.INT16),
                ('AphC', 3, 1, dt.INT16), ('A_SF', 4, 1, dt.INT16), ('PhVphA', 6, 1, dt.INT16),
                ('PhVphB', 7, 1, dt.INT16), ('PhVphC', 8, 1, dt.INT16), ('PPV', 9, 1, dt.INT16),
                ('V_SF', 13, 1, dt.INT16), ('Hz', 14, 1, dt.INT16), ('Hz_SF', 15, 1, dt.INT16),
                ('W', 16, 1, dt.INT16), ('WphA', 17, 1, dt.INT16), ('WphB', 18, 1, dt.INT16),
                ('WphC', 19, 1, dt.INT16), ('W_SF', 20, 1, dt.INT16), ('TotWhExp', 36, 2, dt.UINT32),
                ('TotWhImp', 44, 2, dt.UINT32), ('TotWh_SF', 52, 1, dt.INT16),
            ),
        )

        acpower = self.calculate_value(raw['W'], raw['W_SF'], 2, -50000, 50000)
        m_frequency = self.calculate_value(raw['Hz'], raw['Hz_SF'], 2, 0, 100)

        for key in ('A', 'AphA', 'AphB', 'AphC'):
            self._set_calculated(meter_prefix + key, raw[key], raw['A_SF'], 3, -1000, 1000)
        for key in ('PhVphA', 'PhVphB', 'PhVphC', 'PPV'):
            self._set_calculated(meter_prefix + key, raw[key], raw['V_SF'], 1, 0, 1000)
        for key in ('WphA', 'WphB', 'WphC'):
            self._set_calculated(meter_prefix + key, raw[key], raw['W_SF'], 2, -50000, 50000)
        self.data[meter_prefix + "exported"] = self.protect_lfte(
            meter_prefix + 'exported',
            self.calculate_value(raw['TotWhExp'], raw['TotWh_SF']),
        )
        self.data[meter_prefix + "imported"] = self.protect_lfte(
            meter_prefix + 'imported',
            self.calculate_value(raw['TotWhImp'], raw['TotWh_SF']),
        )
        self.data[meter_prefix + "line_frequency"] = m_frequency
        self.data[meter_prefix + "power"] = acpower
        self._load_meter_sample_ts[int(unit_id)] = time.monotonic()

        if is_primary:
            status_str = None
            i_frequency = self.data["line_frequency"]
            if not i_frequency is None and self.is_numeric(i_frequency) and not m_frequency is None and self.is_numeric(m_frequency):
                m_online = False
                if m_frequency and m_frequency > self._grid_frequency_lower_bound and m_frequency < self._grid_frequency_upper_bound:
                    m_online = True
                
                if m_online and i_frequency > self._grid_frequency_lower_bound and i_frequency < self._grid_frequency_upper_bound:
                    status_str = GRID_STATUS.get(3)
                elif not m_online and i_frequency > self._inverter_frequency_lower_bound and i_frequency < self._inverter_frequency_upper_bound:
                    status_str = GRID_STATUS.get(1)
                elif i_frequency < 1:
                    if m_online:
                        status_str = GRID_STATUS.get(2)
                    elif m_frequency < 1:
                        status_str = GRID_STATUS.get(0)
            if status_str is None:
                _LOGGER.error(f'Could not establish grid connection status m: {m_frequency} i: {i_frequency}')
                self.data["grid_status"] = None
            else:
                self.data["grid_status"] = status_str

        return True

    @_safe_read("ac limit")
    async def read_ac_limit_data(self):
        """Read AC limit control registers."""
        rate_regs = await self.get_registers(
            unit_id=self._inverter_unit_id,
            address=AC_LIMIT_RATE_ADDRESS,
            count=1,
        )
        if rate_regs is not None:
            ac_limit_rate_raw = self._decode_reg(rate_regs, 0, self._client.DATATYPE.UINT16)
            self._set_ac_limit_rate_values(ac_limit_rate_raw)
        elif (
            'ac_limit_rate_raw' not in self.data
            and 'ac_limit_rate_pct' not in self.data
            and 'ac_limit_rate' not in self.data
        ):
            self._set_ac_limit_rate_values(None)

        if time.monotonic() < self._ac_limit_enable_mask_until:
            self.data['ac_limit_enable'] = AC_LIMIT_STATUS.get(1, 'Enabled')
            return True

        ac_limit_enable_raw = await self._read_ac_limit_enable_raw()
        if ac_limit_enable_raw is not None:
            self.data['ac_limit_enable'] = AC_LIMIT_STATUS.get(ac_limit_enable_raw, 'Unknown')
            if ac_limit_enable_raw == 1:
                self._ac_limit_enable_mask_until = 0.0
        elif 'ac_limit_enable' not in self.data:
            self.data['ac_limit_enable'] = None

        return True

    async def set_storage_control_mode(self, mode: int):
        if mode not in [0, 1, 2, 3]:
            _LOGGER.error(f'Attempted to set to unsupported storage control mode. Value: {mode}')
            return
        await self.write_registers(unit_id=self._inverter_unit_id, address=self._storage_register_address(3), payload=[mode])

    async def set_power_factor(self, power_factor: float):
        raw_value = self._power_factor_value_to_raw(power_factor)
        if raw_value is None:
            raise ValueError('Power factor must be between -1 and 1')
        power_factor_enable_raw, was_enabled = await self._pulse_enable_for_apply(
            read_enable_raw=self._read_power_factor_enable_raw,
            enable_address=OUT_PF_SET_ENABLE_ADDRESS,
            mask_attr="_power_factor_enable_mask_until",
            set_enabled_state=self._set_power_factor_enable_state,
        )
        if raw_value < 0:
            raw_value = 65536 + raw_value
        await self.write_registers(
            unit_id=self._inverter_unit_id,
            address=OUT_PF_SET_ADDRESS,
            payload=[raw_value],
        )
        if was_enabled:
            await asyncio.sleep(APPLY_TOGGLE_DELAY_SECONDS)
            await self.write_registers(
                unit_id=self._inverter_unit_id,
                address=OUT_PF_SET_ENABLE_ADDRESS,
                payload=[1],
            )
            self._set_power_factor_enable_state(1)
        self.data['power_factor'] = self._power_factor_raw_to_value(
            raw_value if raw_value < 32768 else raw_value - 65536
        )
        _LOGGER.info(
            "Set power factor to %s (enable_before=%s, pulsed_enable=%s)",
            self.data['power_factor'],
            power_factor_enable_raw,
            was_enabled,
        )

    async def set_power_factor_enable(self, enable: int):
        if enable not in [0, 1]:
            raise ValueError(f'Unsupported power factor control state: {enable}')
        await self.write_registers(
            unit_id=self._inverter_unit_id,
            address=OUT_PF_SET_ENABLE_ADDRESS,
            payload=[enable],
        )
        self._power_factor_enable_mask_until = 0.0
        self._set_power_factor_enable_state(enable)

    async def set_minimum_reserve(self, minimum_reserve: float):
        if not float(minimum_reserve).is_integer():
            raise ValueError('SoC Minimum must be a whole number')
        if minimum_reserve < 5:
            _LOGGER.error(f'Attempted to set SoC Minimum below 5%. Value: {minimum_reserve}')
            return
        minimum_reserve = int(minimum_reserve) * 100
        await self.write_registers(unit_id=self._inverter_unit_id, address=self._storage_register_address(5), payload=[minimum_reserve])

    async def set_discharge_rate_w(self, discharge_rate_w):
        await self.set_discharge_rate(
            self._rate_watts_to_percent(discharge_rate_w, self.max_discharge_rate_w)
        )

    async def set_discharge_rate(self, discharge_rate):
        await self._write_signed_percent_register(
            self._storage_register_address(10),
            discharge_rate,
        )

    async def set_charge_rate_w(self, charge_rate_w):
        await self.set_charge_rate(
            self._rate_watts_to_percent(charge_rate_w, self.max_charge_rate_w)
        )

    async def set_grid_charge_power(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode != 4:
            raise ValueError("Grid charge power can only be changed in Charge from Grid mode")
        self._ext_mode_set_by_ha = True
        await self.set_discharge_rate_w(value * -1)
        percent = (value / self.max_charge_rate_w) * 100 if self.max_charge_rate_w else 0
        self.data['grid_charge_power'] = percent

    async def set_grid_discharge_power(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode != 5:
            raise ValueError("Grid discharge power can only be changed in Discharge to Grid mode")
        self._ext_mode_set_by_ha = True
        await self.set_charge_rate_w(value * -1)
        percent = (value / self.max_discharge_rate_w) * 100 if self.max_discharge_rate_w else 0
        self.data['grid_discharge_power'] = percent
        
    async def set_charge_limit(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode not in [1, 3, 6]:
            raise ValueError("Charge limit cannot be changed in the current storage mode")
        self._ext_mode_set_by_ha = True
        await self.set_charge_rate_w(value)
        percent = (value / self.max_charge_rate_w) * 100 if self.max_charge_rate_w else 0
        self.data['charge_limit'] = percent

    async def set_discharge_limit(self, value):
        """value is in W from HA, store percent internally."""
        if self.storage_extended_control_mode not in [2, 3, 7]:
            raise ValueError("Discharge limit cannot be changed in the current storage mode")
        self._ext_mode_set_by_ha = True
        await self.set_discharge_rate_w(value)
        percent = (value / self.max_discharge_rate_w) * 100 if self.max_discharge_rate_w else 0
        self.data['discharge_limit'] = percent

    async def set_charge_rate(self, charge_rate):
        await self._write_signed_percent_register(
            self._storage_register_address(11),
            charge_rate,
        )

    async def change_settings(
        self,
        mode,
        charge_limit,
        discharge_limit,
        grid_charge_power=0,
        grid_discharge_power=0,
        minimum_reserve=None,
        extended_mode: Optional[int] = None,
    ):
        effective_mode = self.storage_extended_control_mode if extended_mode is None else extended_mode
        self._mode_writes_in_progress += 1
        try:
            await self.set_storage_control_mode(mode)
            await self.set_charge_rate(charge_limit)
            await self.set_discharge_rate(discharge_limit)
            self.data['charge_limit'] = 0 if effective_mode == 5 else charge_limit
            self.data['discharge_limit'] = 0 if effective_mode == 4 else discharge_limit
            self.data['grid_charge_power'] = grid_charge_power
            self.data['grid_discharge_power'] = grid_discharge_power
            self.storage_extended_control_mode = effective_mode
            # Only after a complete write: a partly failed one leaves the old
            # values, so the next read sees the change and re-derives.
            self._last_base_mode = mode
            self._ext_mode_set_by_ha = True
        finally:
            self._mode_writes_in_progress -= 1
            self._mode_write_seq += 1
        if not minimum_reserve is None:
            await self.set_minimum_reserve(minimum_reserve)
        
    async def restore_defaults(self):
        await self.change_settings(mode=0, charge_limit=100, discharge_limit=100, minimum_reserve=7, extended_mode=0)
        _LOGGER.info(f"restored defaults")

    async def set_auto_mode(self):
        await self._set_named_mode(
            mode=0,
            charge_limit=100,
            discharge_limit=100,
            extended_mode=0,
            log_message="Auto mode",
        )

    async def set_charge_mode(self):
        await self._set_named_mode(
            mode=1,
            charge_limit=100,
            discharge_limit=100,
            extended_mode=1,
            log_message="Set charge mode",
        )
  
    async def set_discharge_mode(self):
        await self._set_named_mode(
            mode=2,
            charge_limit=100,
            discharge_limit=100,
            extended_mode=2,
            log_message="Set discharge mode",
        )

    async def set_charge_discharge_mode(self):
        await self._set_named_mode(
            mode=3,
            charge_limit=100,
            discharge_limit=100,
            extended_mode=3,
            log_message="Set charge/discharge mode.",
        )

    async def set_grid_charge_mode(self):
        grid_charge_power = self.data.get('grid_charge_power', 0)
        await self._set_named_mode(
            mode=2,
            charge_limit=100,
            discharge_limit=0,
            grid_charge_power=grid_charge_power,
            extended_mode=4,
            log_message=f"Charge from grid enabled, target {grid_charge_power}%",
        )

    async def set_grid_discharge_mode(self):
        grid_discharge_power = self.data.get('grid_discharge_power', 0)
        await self._set_named_mode(
            mode=1,
            charge_limit=0,
            discharge_limit=100,
            grid_discharge_power=grid_discharge_power,
            extended_mode=5,
            log_message=f"Discharge to grid enabled, target {grid_discharge_power}%",
        )

    async def set_block_discharge_mode(self):
        await self._set_named_mode(
            mode=3,
            charge_limit=100,
            discharge_limit=0,
            extended_mode=6,
            log_message="blocked discharging",
        )

    async def set_block_charge_mode(self):
        await self._set_named_mode(
            mode=3,
            charge_limit=0,
            discharge_limit=100,
            extended_mode=7,
            log_message="Block charging at 100",
        )

    async def set_ac_limit_rate(self, rate):
        """Set AC limit rate in watts and write WMaxLimPct raw value."""
        raw_rate = self._ac_limit_watts_to_raw(rate)
        if raw_rate is None:
            _LOGGER.error("Cannot set AC limit rate, missing max power or scale factor")
            return

        ac_limit_enable_raw, was_enabled = await self._pulse_enable_for_apply(
            read_enable_raw=self._read_ac_limit_enable_raw,
            enable_address=AC_LIMIT_ENABLE_ADDRESS,
            mask_attr="_ac_limit_enable_mask_until",
            set_enabled_state=self._set_ac_limit_control_state,
        )

        await self.write_registers(
            unit_id=self._inverter_unit_id,
            address=AC_LIMIT_RATE_ADDRESS,
            payload=[round(raw_rate)],
        )

        if was_enabled:
            await asyncio.sleep(APPLY_TOGGLE_DELAY_SECONDS)
            await self.write_registers(
                unit_id=self._inverter_unit_id,
                address=AC_LIMIT_ENABLE_ADDRESS,
                payload=[1],
            )
            self._set_ac_limit_control_state(1)

        self._set_ac_limit_rate_values(raw_rate)
        _LOGGER.info(
            "Set AC limit rate to %s W (raw=%s, enable_before=%s, pulsed_enable=%s)",
            self.data['ac_limit_rate'],
            raw_rate,
            ac_limit_enable_raw,
            was_enabled,
        )

    async def set_ac_limit_enable(self, enable):
        """Enable or disable AC limit (0=Disabled, 1=Enabled)."""
        enable_value = 1 if enable else 0
        await self.write_registers(
            unit_id=self._inverter_unit_id,
            address=AC_LIMIT_ENABLE_ADDRESS,
            payload=[enable_value],
        )
        self._ac_limit_enable_mask_until = 0.0
        self._set_ac_limit_control_state(enable_value)
        _LOGGER.info("Set AC limit enable to %s", enable_value)

    async def set_conn_status(self, enable):
        """Enable/disable inverter connection (0=Disconnected/Standby, 1=Connected/Normal)"""
        conn_value = 1 if enable else 0
        await self.write_registers(unit_id=self._inverter_unit_id, address=CONN_ADDRESS, payload=[conn_value])
        self.data['Conn'] = CONTROL_STATUS[conn_value]
        _LOGGER.info(f"Set inverter connection status to {conn_value} ({'Connected' if enable else 'Disconnected/Standby'})")
