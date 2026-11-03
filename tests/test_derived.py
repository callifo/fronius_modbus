"""Tests for household load and grid status derivation."""

import json
import pathlib

import pytest

from custom_components.fronius_modbus.derived import (
    LOAD_SAMPLE_WINDOW_S,
    TotalGuard,
    grid_status,
    household_load,
    throttle_reason,
)


def test_grid_status_from_both_frequencies():
    """Combine inverter and meter frequency into a grid status."""
    assert grid_status(50.0, 49.99) == "on_grid_operating"
    assert grid_status(50.0, 0.0) == "off_grid_operating"
    assert grid_status(0.0, 50.0) == "on_grid"
    assert grid_status(0.0, 0.0) == "off_grid"
    assert grid_status(None, 50.0) is None


def _load(**overrides):
    values = {
        "meter_power_w": -2000.0,
        "inverter_power_w": 3000.0,
        "meter_location": 0,
        "sample_window_s": 0.01,
    }
    values.update(overrides)
    return household_load(**values)


def test_load_at_the_feed_in_point_is_meter_plus_inverter():
    """At a feed-in meter, load is meter power plus inverter power."""
    assert _load() == 1000.0


def test_load_at_the_feed_in_point_keeps_its_sign():
    """Other generation behind the meter exporting more than the house uses."""
    assert _load(meter_power_w=-2000.0, inverter_power_w=500.0) == -1500.0


def test_load_needs_a_meter_location():
    """Without a known meter location, load cannot be derived."""
    assert _load(meter_location=None) is None


def test_load_needs_a_meter_reading():
    assert _load(meter_power_w=None) is None


def test_load_at_the_feed_in_point_needs_the_inverter():
    assert _load(inverter_power_w=None) is None


def test_a_meter_at_another_location_gives_no_load():
    """An external generator meter (location 3) measures no household demand."""
    assert _load(meter_location=3) is None


@pytest.mark.parametrize("location", [1, 256, 511])
def test_a_consumption_meter_reports_its_own_power_signed(location):
    """A consumption-path meter reports load directly, generation in that path included."""
    assert _load(meter_location=location, meter_power_w=-450.0) == 450.0
    assert _load(meter_location=location, meter_power_w=200.0) == -200.0


def test_a_feed_in_pair_read_up_to_the_window_apart_is_one_moment():
    assert _load(sample_window_s=LOAD_SAMPLE_WINDOW_S) == 1000.0
    assert _load(sample_window_s=LOAD_SAMPLE_WINDOW_S + 0.001) is None


def test_a_consumption_meter_needs_no_pair():
    """Its reading alone is the load, so the inverter's read time does not matter."""
    assert _load(meter_location=1, meter_power_w=-450.0, sample_window_s=5.0) == 450.0


def _guard(value=1000.0):
    guard = TotalGuard(max_step=100.0, confirmations=3)
    guard.seed(value)
    return guard


def test_a_plausible_increase_is_accepted_at_once():
    guard = _guard()
    assert guard.observe(1050.0) is None
    assert guard.value == 1050.0


def test_one_bad_sample_is_ignored_and_none_resets_the_streak():
    guard = _guard()
    assert guard.observe(10.0) is not None
    assert guard.observe(None) is None
    assert guard.observe(10.0) is not None
    assert guard.value == 1000.0


def test_three_lower_polls_are_a_counter_reset():
    guard = _guard()
    assert [guard.observe(10.0) is None for _ in range(3)] == [False, False, False]
    assert guard.value == 10.0


def test_three_far_higher_polls_are_a_genuine_gap():
    guard = _guard()
    for reading in (5000.0, 5001.0, 5002.0):
        guard.observe(reading)
    assert guard.value == 5002.0


@pytest.mark.parametrize(
    "readings",
    [(10.0, 100000.0, 10.0), (100000.0, 10.0, 100000.0), (10.0, 5000.0, 20.0)],
)
def test_contradicting_samples_confirm_nothing(readings):
    """10, 100000, 10 counted as three confirmations and reset the total.

    A spike between two low readings contradicts a real reset; only samples
    that agree with each other may move the accepted range.
    """
    guard = _guard()

    for reading in readings:
        guard.observe(reading)

    assert guard.value == 1000.0


def test_a_new_range_may_keep_counting_while_it_is_confirmed():
    """A reset counter goes on counting; 10, 12, 14 is one consistent new range."""
    guard = _guard()

    for reading in (10.0, 12.0, 14.0):
        guard.observe(reading)

    assert guard.value == 14.0


def test_the_first_reading_seeds_an_unseeded_guard():
    guard = TotalGuard(max_step=100.0, confirmations=3)
    assert guard.observe(42.0) is None
    assert guard.value == 42.0


def _reason(operating_state=4, relative_reached=False, absolute_reached=False):
    return throttle_reason(
        operating_state=operating_state,
        relative_limit_reached=relative_reached,
        absolute_limit_reached=absolute_reached,
    )


def test_a_normal_inverter_is_not_throttled():
    assert _reason() == "none"


def test_the_inverter_reporting_the_state_itself_is_a_reason():
    assert _reason(operating_state=5) == "inverter_state"


def test_a_reached_percentage_limit_is_the_ac_limit():
    """Measured: the AC limit at 400 W held the output and set only the relative flag."""
    assert _reason(relative_reached=True) == "ac_limit"


def test_a_reached_limit_in_watts_is_the_feed_in_limit():
    """Measured in #26: an export limit held at 400 W set only the absolute flag."""
    assert _reason(absolute_reached=True) == "feed_in_limit"


def test_two_reasons_at_once_are_reported_as_several():
    assert _reason(relative_reached=True, absolute_reached=True) == "several"


def test_nothing_known_is_no_answer():
    assert (
        _reason(operating_state=None, relative_reached=None, absolute_reached=None)
        is None
    )


def test_an_unread_limit_flag_prevents_a_no():
    """Without the component flags a "none" would be a guess."""
    assert _reason(absolute_reached=None) is None
    assert _reason(relative_reached=None) is None


def test_a_found_reason_stands_even_while_another_source_is_silent():
    assert _reason(absolute_reached=True, relative_reached=None) == "feed_in_limit"


def test_the_limit_reason_is_named_after_the_ac_limit_it_reads():
    """Discussion #6: "Export limit" read as the grid feed-in limit, which the
    inverter reports in none of these signals; the reason is the AC limit."""
    root = pathlib.Path(__file__).parent.parent / "custom_components/fronius_modbus"
    for language in ("en", "de"):
        texts = json.loads(
            (root / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )["entity"]
        reason = texts["sensor"]["throttle_reason"]["state"]["ac_limit"]
        assert reason == texts["number"]["ac_limit_rate"]["name"].removesuffix(" rate")


def test_the_production_limit_pair_is_a_power_limit_apart_from_the_battery_s():
    """ "Aktuelle Produktionsgrenze" and "Leistungsgrenze erreicht" read as two
    limits; "Aktuelle Leistungsgrenze" is the battery's own sensor. "... erreicht"
    also read as the inverter at its own maximum, while the flag says a limit
    set on it holds the output."""
    root = pathlib.Path(__file__).parent.parent / "custom_components/fronius_modbus"
    expected = {
        "en": ("Output power limit", "Output limited"),
        "de": ("Zulässige Ausgangsleistung", "Ausgangsleistung begrenzt"),
    }
    for language, names in expected.items():
        sensors = json.loads(
            (root / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )["entity"]["sensor"]
        pair = (
            sensors["production_limit"]["name"],
            sensors["production_limit_reached"]["name"],
        )
        assert pair == names
        assert sensors["storage_power_limit"]["name"] not in pair
        # WMax is the fixed nameplate setting; this value drops with an AC limit.
        assert sensors["max_power"]["name"] not in pair
