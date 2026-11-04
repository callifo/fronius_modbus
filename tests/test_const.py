from custom_components.fronius_modbus.const import (
    INVERTER_CONTROLS,
    bitmask_to_string,
    entity_prefix,
    instance_key,
    map_code,
)


def test_instance_key_normalises_the_entry_id_like_0_3_did():
    assert instance_key("01ABC-def.99") == "01abc_def_99"
    assert instance_key("---") == "fronius"
    assert entity_prefix("01ABC") == "fm_01abc"


def test_map_code_reads_an_unmapped_code_as_unknown():
    assert map_code({1: "Off"}, 1) == "Off"
    assert map_code({1: "Off"}, 9) == "unknown"
    assert map_code({1: "Off"}, None) is None


def test_bitmask_to_string():
    assert bitmask_to_string(0, INVERTER_CONTROLS, "normal") == "normal"
    assert (
        bitmask_to_string(0b101, INVERTER_CONTROLS, "normal")
        == "power_reduction_constant_power_factor"
    )
