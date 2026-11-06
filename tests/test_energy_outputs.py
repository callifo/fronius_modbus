"""What the inverter reports for its energy management outputs, as the sensors read it."""

from custom_components.fronius_modbus.energy_outputs import (
    OUTPUT_OFF,
    OUTPUT_ON,
    EnergyOutput,
    parse_energy_outputs,
)

# The answer of a Primo GEN24 with its first relay on.
ONE_RELAY_ON = {
    "pin1": {"reason": "PSTR_EMRS_REASON_5", "state": True},
    "pin2": {"reason": "PSTR_EMRS_REASON_4", "state": False},
    "pin3": {"reason": "PSTR_EMRS_REASON_0", "state": False},
    "pin4": {"reason": "PSTR_EMRS_REASON_0", "state": False},
}


def test_a_reported_answer_reads_as_state_and_reason():
    assert parse_energy_outputs(ONE_RELAY_ON) == {
        1: EnergyOutput(OUTPUT_ON, "power_above_threshold"),
        2: EnergyOutput(OUTPUT_OFF, "power_below_threshold"),
        3: EnergyOutput(OUTPUT_OFF, "not_configured"),
        4: EnergyOutput(OUTPUT_OFF, "not_configured"),
    }


def test_a_reason_of_a_newer_firmware_reads_as_unknown():
    answer = {"pin1": {"reason": "PSTR_EMRS_REASON_7", "state": True}}
    assert parse_energy_outputs(answer) == {1: EnergyOutput(OUTPUT_ON, "unknown")}


def test_a_state_that_is_no_boolean_stays_unknown():
    answer = {"pin1": {"reason": "PSTR_EMRS_REASON_0", "state": "maybe"}}
    assert parse_energy_outputs(answer) == {1: EnergyOutput(None, "not_configured")}


def test_a_pin_the_answer_leaves_out_is_not_reported():
    assert set(parse_energy_outputs({"pin2": ONE_RELAY_ON["pin2"]})) == {2}


def test_a_pin_outside_the_four_outputs_is_left_out():
    answer = {"pin9": ONE_RELAY_ON["pin1"], "pin1": ONE_RELAY_ON["pin1"]}
    assert set(parse_energy_outputs(answer)) == {1}


def test_an_answer_of_another_shape_is_no_answer():
    assert parse_energy_outputs(None) is None
    assert parse_energy_outputs(["pin1"]) is None
    assert parse_energy_outputs({"pin1": "on"}) == {}
