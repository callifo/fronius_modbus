"""The description types: what a row of the entity table carries.

The rows themselves are in entities.py; the entity classes in entity_base.py
and the platform modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from homeassistant.components.button import ButtonEntityDescription
from homeassistant.components.event import EventEntityDescription
from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.components.switch import SwitchEntityDescription

from .coordinator import FroniusRuntimeData

type Source = Literal["modbus", "web"]
type WebClientKind = Literal["public", "customer", "technician"]
type DeviceKind = Literal["inverter", "storage", "meter"]


@dataclass(frozen=True, kw_only=True)
class FroniusDescriptionMixin:
    """Fields every Fronius entity description carries, on top of the HA one."""

    key: str
    device: DeviceKind
    source: Source = "modbus"
    # The web login a web-sourced entity needs ("public": none); unavailable without it.
    web_client: WebClientKind = "customer"
    report_name: str | None = None
    # Reads the web poll besides Modbus: follows it, and is up while either is fresh.
    also_web: bool = False
    # What the state alone cannot carry, such as the rest of the active events.
    attributes_fn: Callable[[FroniusRuntimeData], dict[str, Any] | None] | None = None
    meter_unit_id: int | None = None
    value_fn: Callable[[FroniusRuntimeData], Any]
    exists_fn: Callable[[FroniusRuntimeData], bool] = staticmethod(lambda runtime: True)
    available_fn: Callable[[FroniusRuntimeData], bool] = staticmethod(
        lambda runtime: True
    )


@dataclass(frozen=True, kw_only=True)
class FroniusSensorDescription(SensorEntityDescription, FroniusDescriptionMixin):
    """A sensor built from a value_fn."""


@dataclass(frozen=True, kw_only=True)
class FroniusEventDescription(EventEntityDescription, FroniusDescriptionMixin):
    """An event entity the web poll feeds with the inverter's new log entries."""


@dataclass(frozen=True, kw_only=True)
class FroniusNumberDescription(NumberEntityDescription, FroniusDescriptionMixin):
    """A number built from a value_fn and a set_fn."""

    set_fn: Callable[[FroniusRuntimeData, float], Awaitable[None]]
    max_fn: Callable[[FroniusRuntimeData], float | None] = staticmethod(
        lambda runtime: None
    )


@dataclass(frozen=True, kw_only=True)
class FroniusSelectDescription(SelectEntityDescription, FroniusDescriptionMixin):
    """A select built from a code<->label map and a set_fn taking the code."""

    options_map: dict[int, str]
    set_fn: Callable[[FroniusRuntimeData, int], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class FroniusSwitchDescription(SwitchEntityDescription, FroniusDescriptionMixin):
    """A switch built from a value_fn and separate turn_on/turn_off actions."""

    turn_on: Callable[[FroniusRuntimeData], Awaitable[None]]
    turn_off: Callable[[FroniusRuntimeData], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class FroniusButtonDescription(ButtonEntityDescription, FroniusDescriptionMixin):
    """A button built from a press action."""

    press: Callable[[FroniusRuntimeData], Awaitable[None]]


type FroniusDescription = (
    FroniusSensorDescription
    | FroniusNumberDescription
    | FroniusSelectDescription
    | FroniusSwitchDescription
    | FroniusButtonDescription
    | FroniusEventDescription
)
