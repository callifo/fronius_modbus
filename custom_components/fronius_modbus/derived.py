"""Values no single register carries: the household load and the grid status."""

from __future__ import annotations

from typing import NamedTuple

from .const import GRID_STATUS, THROTTLE_REASONS

GRID_FREQUENCY_HZ = 50.0
METER_GRID_BAND_HZ = 0.2
INVERTER_GRID_BAND_HZ = 5.0
STANDSTILL_HZ = 1.0
METER_LOCATION_FEED_IN = 0
METER_LOCATION_CONSUMPTION = 1
METER_LOCATION_CONSUMPTION_RANGE = range(256, 512)
# From the start of the inverter read to the end of the primary meter read; a
# pair further apart than this is not one moment.
LOAD_SAMPLE_WINDOW_S = 0.5


# Model 103 St: the operating state the inverter names itself.
THROTTLED_OPERATING_STATE = 5
NO_REASON, SEVERAL_REASONS = THROTTLE_REASONS[0], THROTTLE_REASONS[-1]


def throttle_reason(
    *,
    operating_state: int | None,
    relative_limit_reached: bool | None,
    absolute_limit_reached: bool | None,
) -> str | None:
    """Why the inverter is holding its output back, or None while that is unknown.

    A limit that is set is no throttling: only one the output has reached is.
    The component endpoint says so for a percentage limit and for one in watts
    apart; the Modbus signals only say a limit is switched on (#26). A GEN24
    left the operating state at normal while it throttled; other firmware
    may not.
    """
    signals = (
        (
            THROTTLE_REASONS[1],
            None
            if operating_state is None
            else operating_state == THROTTLED_OPERATING_STATE,
        ),
        (THROTTLE_REASONS[2], relative_limit_reached),
        (THROTTLE_REASONS[3], absolute_limit_reached),
    )
    reasons = [reason for reason, applies in signals if applies]
    if reasons:
        return reasons[0] if len(reasons) == 1 else SEVERAL_REASONS
    # No reason found is only an answer once every source answered: a limit we
    # could not read may be the one that is throttling.
    unknown = any(applies is None for _, applies in signals)
    return None if unknown else NO_REASON


def _within(value: float, centre: float, band: float) -> bool:
    """Return whether value lies strictly within band of centre."""
    return centre - band < value < centre + band


def grid_status(inverter_hz: float | None, meter_hz: float | None) -> str | None:
    """Derive the grid status from the inverter and meter grid frequencies."""
    if inverter_hz is None or meter_hz is None:
        return None
    meter_online = _within(meter_hz, GRID_FREQUENCY_HZ, METER_GRID_BAND_HZ)
    if meter_online and _within(inverter_hz, GRID_FREQUENCY_HZ, METER_GRID_BAND_HZ):
        return GRID_STATUS[3]
    if not meter_online and _within(
        inverter_hz, GRID_FREQUENCY_HZ, INVERTER_GRID_BAND_HZ
    ):
        return GRID_STATUS[1]
    if inverter_hz < STANDSTILL_HZ:
        if meter_online:
            return GRID_STATUS[2]
        if meter_hz < STANDSTILL_HZ:
            return GRID_STATUS[0]
    return None


def household_load(
    *,
    meter_power_w: float | None,
    inverter_power_w: float | None,
    meter_location: int | None,
    sample_window_s: float,
) -> float | None:
    """The household's net demand from the primary meter, signed.

    Generation behind the meter the inverter does not see (a balcony inverter,
    an AC-coupled battery) is not in it: the value is the consumption minus
    that generation, and negative while that generation exports.
    """
    if meter_power_w is None or meter_location is None:
        return None
    if (
        meter_location == METER_LOCATION_CONSUMPTION
        or meter_location in METER_LOCATION_CONSUMPTION_RANGE
    ):
        return round(-meter_power_w, 2)
    if meter_location != METER_LOCATION_FEED_IN or inverter_power_w is None:
        return None
    if sample_window_s > LOAD_SAMPLE_WINDOW_S:
        return None
    return round(meter_power_w + inverter_power_w, 2)


class TotalVerdict(NamedTuple):
    """Why a reading was rejected, or accepted late; adopted says which."""

    message: str
    adopted: bool


class TotalGuard:
    """Accepts readings of a monotonically increasing counter, one verdict per poll.

    A single lower or far-too-high sample is a bad reading and is ignored.
    ``confirmations`` consecutive samples that agree with each other are the
    device telling the truth - a counter reset after a hardware swap, or a
    large but genuine gap while Home Assistant was offline -
    and are accepted. Property reads never advance this state: only observe()
    does.
    """

    def __init__(self, *, max_step: float, confirmations: int) -> None:
        """Bound single-poll jumps by max_step; adopt a new range after confirmations polls."""
        self.value: float | None = None
        self._max_step = max_step
        self._confirmations = confirmations
        self._suspect_polls = 0
        # The last out-of-range reading: the next one has to continue from it.
        self._last_suspect: float | None = None

    def seed(self, value: float | None) -> None:
        """Start from a restored value without counting it as a poll."""
        self.value = value
        self._suspect_polls = 0
        self._last_suspect = None

    def observe(self, reading: float | None) -> TotalVerdict | None:
        """Record one poll; returns why a reading was rejected or accepted late, else None."""
        if reading is None:
            self._suspect_polls = 0
            self._last_suspect = None
            return None
        if self.value is None or self._plausible_after(self.value, reading):
            self.value = reading
            self._suspect_polls = 0
            self._last_suspect = None
            return None
        # Only samples that agree with each other confirm a new range: a spike
        # between two low readings is no reset.
        continues = self._last_suspect is not None and self._plausible_after(
            self._last_suspect, reading
        )
        self._suspect_polls = self._suspect_polls + 1 if continues else 1
        self._last_suspect = reading
        kind = "lower than" if reading < self.value else "far above"
        if self._suspect_polls < self._confirmations:
            return TotalVerdict(
                f"ignoring {reading}: {kind} the last value {self.value}", adopted=False
            )
        message = (
            f"accepting {reading}: {kind} the last value {self.value} for "
            f"{self._confirmations} polls, so the counter really moved"
        )
        self.value = reading
        self._suspect_polls = 0
        self._last_suspect = None
        return TotalVerdict(message, adopted=True)

    def _plausible_after(self, earlier: float, reading: float) -> bool:
        return 0 <= reading - earlier <= self._max_step
