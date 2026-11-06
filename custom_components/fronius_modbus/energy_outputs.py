"""The relays the inverter switches by its energy management rules, as it reports them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import UNKNOWN_STATE

ENERGY_OUTPUTS_PATH = "/api/status/emrs"
# The operating instructions allow up to four pins for load management.
OUTPUT_PINS = (1, 2, 3, 4)
OUTPUT_ON, OUTPUT_OFF = "on", "off"
_REASONS = {
    "PSTR_EMRS_REASON_0": "not_configured",
    "PSTR_EMRS_REASON_1": "power_invalid",
    "PSTR_EMRS_REASON_2": "maximum_runtime_exceeded",
    "PSTR_EMRS_REASON_3": "minimum_runtime_running",
    "PSTR_EMRS_REASON_4": "power_below_threshold",
    "PSTR_EMRS_REASON_5": "power_above_threshold",
    "PSTR_EMRS_REASON_6": "target_runtime_not_reached",
}
OUTPUT_STATES = [OUTPUT_ON, OUTPUT_OFF]
OUTPUT_REASONS = [*_REASONS.values(), UNKNOWN_STATE]


@dataclass(frozen=True)
class EnergyOutput:
    """One output as the last poll saw it; a state of None was not a yes or no."""

    state: str | None
    reason: str


def _parse_output(answer: dict[str, Any]) -> EnergyOutput:
    state = answer.get("state")
    on_or_off = None
    if isinstance(state, bool):
        on_or_off = OUTPUT_ON if state else OUTPUT_OFF
    return EnergyOutput(
        on_or_off, _REASONS.get(str(answer.get("reason")), UNKNOWN_STATE)
    )


def parse_energy_outputs(payload: Any) -> dict[int, EnergyOutput] | None:
    """The outputs an answer reports by pin, or None for an answer of another shape."""
    if not isinstance(payload, dict):
        return None
    answers = {pin: payload.get(f"pin{pin}") for pin in OUTPUT_PINS}
    return {
        pin: _parse_output(answer)
        for pin, answer in answers.items()
        if isinstance(answer, dict)
    }
