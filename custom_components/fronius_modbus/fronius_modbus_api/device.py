"""The inverter, its optional sub-systems and its meters over modbus-connection units."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import logging
from typing import Any, NamedTuple, cast

from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
    ModbusUnit,
)
from modbus_connection.model.sunspec import (
    SunSpecError,
    SunSpecModel,
    SunSpecModels,
    scan,
)

from .exceptions import IncompleteChainError, NotAFroniusInverter
from .sunspec_models import (
    COMMON_MODEL_ID,
    COMMON_SERIAL_WORDS,
    CONTROLS_MODEL_ID,
    INVERTER_MODEL_IDS,
    METER_MODEL_IDS,
    MPPT_MODEL_ID,
    NAMEPLATE_MODEL_ID,
    SETTINGS_MODEL_ID,
    SINGLE_PHASE_METER_MODEL_ID,
    STATUS_MODEL_ID,
    STORAGE_MODEL_ID,
    SUNSPEC_BASE_ADDRESS,
    THREE_PHASE_INVERTER_MODEL_ID,
    AcMeter,
    Common,
    Controls,
    Inverter,
    Mppt,
    MpptModule,
    Nameplate,
    Settings,
    Status,
    Storage,
)

_LOGGER = logging.getLogger(__name__)

REPORT_DISCOVERY = "discovery"
REPORT_INVERTER = "inverter"
REPORT_NAMEPLATE = "nameplate"
REPORT_SETTINGS = "settings"
REPORT_STATUS = "status"
REPORT_CONTROLS = "controls"
REPORT_MPPT = "mppt"
REPORT_STORAGE = "storage"
OHM_PER_MEGAOHM = 1_000_000
PV_LABEL = "MPPT"
CHARGE_LABEL = "STCHA"
DISCHARGE_LABEL = "STDISCHA"


@dataclass(frozen=True)
class MpptChannels:
    """Which model-160 modules are PV strings and which are the storage paths."""

    pv: tuple[int, ...]
    charge: int | None
    discharge: int | None


EMPTY_CHANNELS = MpptChannels(pv=(), charge=None, discharge=None)


def meter_report_name(unit_id: int) -> str:
    """The report name of the meter on ``unit_id``."""
    return f"meter_{unit_id}"


class DeviceIdentity(NamedTuple):
    """What model 1 says about a unit."""

    manufacturer: str
    model: str
    serial: str
    version: str
    options: str
    # `da` is a plain uint16 register; the reader hands every numeric field
    # back as a float, so the type follows the reader rather than the spec.
    address: float | None


@dataclass
class UpdateReport:
    """What one poll managed to refresh."""

    updated: list[str] = field(default_factory=list)
    failed: dict[str, ModbusError] = field(default_factory=dict)


@dataclass
class MeterInfo:
    """A meter found on its own unit."""

    unit_id: int
    identity: DeviceIdentity
    phases: int
    meter: AcMeter


async def _read_identity(unit: ModbusUnit, chain: SunSpecModels) -> DeviceIdentity:
    # Model 1 is mandatory in every SunSpec map, and _scan() has already
    # refused a chain without it - so the lookup cannot be None here.
    common = Common(unit, cast(SunSpecModel, chain.first(COMMON_MODEL_ID)))
    await common.async_update()
    return DeviceIdentity(
        manufacturer=common.mn or "",
        model=common.md or "",
        serial=common.sn or "",
        version=common.vr or "",
        options=common.opt or "",
        address=common.da,
    )


# SunSpec header words before the first model, and the id that closes the chain.
MARKER_WORDS = 2
END_MODEL_ID = 0xFFFF
# A map longer than this is a misread chain, not a device; bounds the walk.
MAXIMUM_MODELS = 64


async def _scan(unit: ModbusUnit) -> tuple[SunSpecModels, bool]:
    """The SunSpec chain and whether it ended in the end marker rather than a refusal.

    An incomplete chain is not a device without those models: the caller has to
    keep it as undecided and look again (audit A03).
    """
    complete = True
    try:
        chain = await scan(unit, SUNSPEC_BASE_ADDRESS)
    except IllegalDataAddressError:
        # Some devices refuse a read past their map instead of answering the
        # end marker, which took the whole chain down and left setup reporting
        # "cannot connect" although Modbus was fine (upstream #105). Keep what
        # the device did serve; if that is not even a Fronius, report the
        # refusal itself, which is what an absent unit answers.
        chain = await _scan_until_refused(unit)
        if chain.first(COMMON_MODEL_ID) is None:
            raise
        complete = False
        _LOGGER.warning(
            "The SunSpec chain ends in a refused read; continuing with the models "
            "found before it: %s",
            ", ".join(str(model_id) for model_id in sorted(chain)),
        )
    except SunSpecError as err:
        raise NotAFroniusInverter(str(err)) from err
    if chain.first(COMMON_MODEL_ID) is None:
        raise NotAFroniusInverter("no SunSpec common model")
    return chain, complete


async def _scan_until_refused(unit: ModbusUnit) -> SunSpecModels:
    """Walk the model chain by hand, keeping what was read before a refusal."""
    chain = SunSpecModels()
    address = SUNSPEC_BASE_ADDRESS + MARKER_WORDS
    for _ in range(MAXIMUM_MODELS):
        try:
            model_id, length = await unit.read_holding_registers(address, MARKER_WORDS)
        except IllegalDataAddressError:
            return chain
        if model_id == END_MODEL_ID:
            return chain
        model = SunSpecModel(model_id=model_id, address=address, length=length)
        chain.setdefault(model_id, []).append(model)
        address += model.span
    return chain


class FroniusInverter:
    """A Fronius inverter: components at the addresses the SunSpec chain reports."""

    def __init__(
        self, unit: ModbusUnit, unit_id: int, meter_units: Mapping[int, ModbusUnit]
    ) -> None:
        """Bind to the inverter's unit and the units to probe for meters."""
        self._unit = unit
        self._unit_id = unit_id
        self._meter_units = dict(meter_units)
        # The configured order, kept apart from `meters`: that one holds only
        # the meters that answered, so numbering displays off it would renumber
        # the second meter as soon as the first is unplugged.
        self.meter_unit_ids: tuple[int, ...] = tuple(self._meter_units)
        self.identity: DeviceIdentity | None = None
        self.three_phase = False
        self.inverter: Inverter | None = None
        self.nameplate: Nameplate | None = None
        self.settings: Settings | None = None
        self.status: Status | None = None
        self.controls: Controls | None = None
        self.mppt: Mppt | None = None
        self.storage: Storage | None = None
        # False while the chain ended in a refused header: the models behind it
        # are undecided, not absent, so discovery is retried (audit A03).
        self.discovery_complete = False
        self.meters: dict[int, MeterInfo] = {}
        # Meter units whose probe did not answer: neither present nor absent
        # yet, probed again on every poll (audit F03).
        self._undecided_meters: dict[int, ModbusUnit] = {}
        self._polled: tuple[str, ...] = ()

    @property
    def unit(self) -> ModbusUnit:
        """The inverter's own unit."""
        return self._unit

    @staticmethod
    async def async_probe(unit: ModbusUnit) -> DeviceIdentity:
        """The identity of the inverter on ``unit``; raises NotAFroniusInverter otherwise."""
        chain, _complete = await _scan(unit)
        if chain.first(*INVERTER_MODEL_IDS) is None:
            raise NotAFroniusInverter("no SunSpec inverter model in the chain")
        return await _read_identity(unit, chain)

    @property
    def is_set_up(self) -> bool:
        """Whether discovery has run and placed the components."""
        return self.inverter is not None

    async def _async_setup(self) -> None:
        """Read what never changes and place the components. Optional models may be absent."""
        chain, self.discovery_complete = await _scan(self._unit)
        inverter_model = chain.first(*INVERTER_MODEL_IDS)
        if inverter_model is None:
            raise NotAFroniusInverter("no SunSpec inverter model in the chain")
        self.identity = await _read_identity(self._unit, chain)
        self.three_phase = inverter_model.model_id == THREE_PHASE_INVERTER_MODEL_ID
        self.inverter = Inverter(self._unit, inverter_model)
        self.nameplate = self._optional(Nameplate, chain, NAMEPLATE_MODEL_ID)
        self.settings = self._optional(Settings, chain, SETTINGS_MODEL_ID)
        self.status = self._optional(Status, chain, STATUS_MODEL_ID)
        self.controls = self._optional(Controls, chain, CONTROLS_MODEL_ID)
        self.mppt = self._optional(Mppt, chain, MPPT_MODEL_ID)
        self.storage = self._optional(Storage, chain, STORAGE_MODEL_ID)
        self.meters = {}
        self._undecided_meters = {}
        for unit_id, meter_unit in self._meter_units.items():
            await self._async_place_meter(unit_id, meter_unit)
        self._polled = tuple(
            name
            for name, component in (
                (REPORT_INVERTER, self.inverter),
                (REPORT_NAMEPLATE, self.nameplate),
                (REPORT_SETTINGS, self.settings),
                (REPORT_STATUS, self.status),
                (REPORT_CONTROLS, self.controls),
                (REPORT_MPPT, self.mppt),
                (REPORT_STORAGE, self.storage),
            )
            if component is not None
        ) + tuple(meter_report_name(unit_id) for unit_id in self.meters)

    def _optional(
        self, component_class: Any, chain: SunSpecModels, model_id: int
    ) -> Any:
        model = chain.first(model_id)
        return None if model is None else component_class(self._unit, model)

    async def _async_place_meter(self, unit_id: int, meter_unit: ModbusUnit) -> None:
        """Probe one meter unit and file it as present, absent or undecided."""
        try:
            meter = await self._async_probe_meter(unit_id, meter_unit)
        except (ModbusTimeoutError, IncompleteChainError) as err:
            # No answer is not "no meter": the inverter may still be booting
            # (audit F03). Try again on every poll, and say so in the report.
            _LOGGER.debug("Meter on unit %s did not answer yet: %s", unit_id, err)
            self._undecided_meters[unit_id] = meter_unit
            return
        self._undecided_meters.pop(unit_id, None)
        if meter is not None:
            self.meters[unit_id] = meter

    async def _async_probe_meter(
        self, unit_id: int, meter_unit: ModbusUnit
    ) -> MeterInfo | None:
        """A meter on ``unit_id``, or None when nothing SunSpec answers there.

        A unit without a device answers exception 0x0B (gateway target): that
        is a confirmed absence. A timeout propagates; the caller keeps the
        unit undecided.
        """
        try:
            chain, complete = await _scan(meter_unit)
        except ModbusConnectionError, ModbusTimeoutError:
            raise
        except ModbusError:
            _LOGGER.debug("No meter on unit %s", unit_id)
            return None
        if not complete:
            raise IncompleteChainError(f"meter {unit_id} answered an incomplete chain")
        model = chain.first(*METER_MODEL_IDS)
        if model is None:
            return None
        identity = await _read_identity(meter_unit, chain)
        phases = 1 if model.model_id == SINGLE_PHASE_METER_MODEL_ID else 3
        return MeterInfo(
            unit_id=unit_id,
            identity=identity,
            phases=phases,
            meter=AcMeter(meter_unit, model),
        )

    def _component(self, name: str) -> Any:
        if name.startswith("meter_"):
            return self.meters[int(name.removeprefix("meter_"))].meter
        return getattr(self, name)

    async def async_update(self) -> UpdateReport:
        """Refresh every sub-system on its own; a dead link propagates."""
        if not self.is_set_up:
            try:
                await self._async_setup()
            except ModbusError:
                self.inverter = None
                raise
        report = await self._async_open_report()
        for name in self._polled:
            try:
                await self._component(name).async_update()
            except ModbusConnectionError:
                raise
            except ModbusTimeoutError as err:
                if not report.updated and not report.failed:
                    raise
                report.failed[name] = err
            except ModbusError as err:
                report.failed[name] = err
            else:
                report.updated.append(name)
        await self._async_retry_undecided_meters(report)
        return report

    async def _async_open_report(self) -> UpdateReport:
        """A report for this poll; an incomplete chain is retried and named in it."""
        if self.discovery_complete:
            return UpdateReport()
        await self._async_retry_discovery()
        report = UpdateReport()
        if not self.discovery_complete:
            report.failed[REPORT_DISCOVERY] = IncompleteChainError(
                "the SunSpec chain ends in a refused read"
            )
        return report

    async def _async_retry_discovery(self) -> None:
        """Scan again; a chain that completes now brings its models with it."""
        try:
            await self._async_setup()
        except ModbusConnectionError:
            raise
        except (ModbusError, NotAFroniusInverter) as err:
            _LOGGER.debug("Discovery is still incomplete: %s", err)

    async def _async_retry_undecided_meters(self, report: UpdateReport) -> None:
        for unit_id, meter_unit in list(self._undecided_meters.items()):
            name = meter_report_name(unit_id)
            await self._async_place_meter(unit_id, meter_unit)
            if unit_id in self._undecided_meters:
                report.failed[name] = ModbusTimeoutError(
                    f"meter {unit_id} did not answer"
                )
                continue
            if unit_id in self.meters:
                self._polled = (*self._polled, name)
                await self._component(name).async_update()
                report.updated.append(name)

    async def async_read_raw(self) -> dict[int, dict[str, dict[int, int | bool]]]:
        """Every register read, undecoded, per unit id, minus the serial number words."""
        if not self.is_set_up:
            await self._async_setup()
        per_unit: dict[int, dict[str, dict[int, int | bool]]] = {}

        async def collect(
            unit_id: int, unit: ModbusUnit, components: Sequence[Any]
        ) -> None:
            chain, _complete = await _scan(unit)
            common = Common(unit, cast(SunSpecModel, chain.first(COMMON_MODEL_ID)))
            raw = per_unit.setdefault(unit_id, {})
            for component in (common, *components):
                for space, words in (await component.async_read_raw()).items():
                    raw.setdefault(space, {}).update(words)
            serial_start = common.resolved_fields["sn"].address
            for address in range(serial_start, serial_start + COMMON_SERIAL_WORDS):
                raw.get("holding", {}).pop(address, None)

        inverter_components = [
            self._component(n) for n in self._polled if not n.startswith("meter_")
        ]
        await collect(self._unit_id, self._unit, inverter_components)
        for unit_id, info in self.meters.items():
            await collect(unit_id, self._meter_units[unit_id], [info.meter])
        return per_unit

    @property
    def isolation_resistance_megaohm(self) -> float | None:
        """Model 122 reports ohms; the entity has always shown megaohms."""
        if self.status is None or self.status.ris is None:
            return None
        return round(self.status.ris / OHM_PER_MEGAOHM, 3)

    def mppt_module(self, index: int) -> MpptModule | None:
        """The module at ``index``, or None when there is no MPPT model or index."""
        if self.mppt is None or index >= len(self.mppt.module):
            return None
        return self.mppt.module[index]

    @property
    def mppt_channels(self) -> MpptChannels:
        """Classify the model-160 modules from the last poll by their label."""
        if self.mppt is None:
            return EMPTY_CHANNELS
        labels = [
            (module.id_str or "").replace(" ", "").upper()
            for module in self.mppt.module
        ]
        charge = next(
            (i for i, label in enumerate(labels) if label.startswith(CHARGE_LABEL)),
            None,
        )
        discharge = next(
            (i for i, label in enumerate(labels) if DISCHARGE_LABEL in label), None
        )
        # Older firmware ships no labels; the storage paths are then the last two modules.
        if (
            self.storage is not None
            and (charge is None or discharge is None)
            and len(labels) >= 4
        ):
            charge, discharge = len(labels) - 2, len(labels) - 1
        pv = tuple(i for i, label in enumerate(labels) if PV_LABEL in label)
        if not pv:
            pv = tuple(i for i in range(len(labels)) if i not in (charge, discharge))
        return MpptChannels(pv=pv, charge=charge, discharge=discharge)

    @property
    def pv_power_w(self) -> float | None:
        """The sum of the PV channels' dcw, or None when none of them report a value."""
        modules = [self.mppt_module(i) for i in self.mppt_channels.pv]
        values = [
            module.dcw
            for module in modules
            if module is not None and module.dcw is not None
        ]
        return round(sum(values), 2) if values else None
