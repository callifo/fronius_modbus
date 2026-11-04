"""The generated SunSpec components decode the captured Symo GEN24."""

from modbus_connection.model.sunspec import scan

from custom_components.fronius_modbus.fronius_modbus_api import sunspec_models as models

# Model 121 in the captured fixture, and the two sentinels SunSpec defines.
SETTINGS_W_MAX_ADDRESS = 40151
SETTINGS_V_REF_OFS_ADDRESS = 40153
UINT16_NOT_IMPLEMENTED = 0xFFFF
INT16_NOT_IMPLEMENTED = 0x8000


async def test_the_inverter_model_decodes_scaled_points(inverter_unit):
    chain = await scan(inverter_unit, 40000)
    inverter = models.Inverter(inverter_unit, chain.first(*models.INVERTER_MODEL_IDS))
    await inverter.async_update()
    assert inverter.w == 3075.1
    assert inverter.wh == 33187794.59
    assert inverter.hz == 49.99
    assert inverter.st == 4  # raw operating state, mapped to text by the entity layer
    assert inverter.evt_vnd2 == 0


async def test_unimplemented_points_read_as_none(inverter_unit):
    """The device says "not implemented" with a per-type sentinel, not with a value.

    An MPPT string in the dark sends 0xFFFF and a meter phase that does not
    exist sends 0x8000; decoding either as a number is what made the old
    integration report 65535 W and refuse the poll.
    """
    chain = await scan(inverter_unit, 40000)
    settings = models.Settings(inverter_unit, chain.first(models.SETTINGS_MODEL_ID))
    await settings.async_update()
    assert (settings.w_max, settings.v_ref_ofs) == (10000, 0)

    inverter_unit.holding[SETTINGS_W_MAX_ADDRESS] = UINT16_NOT_IMPLEMENTED
    inverter_unit.holding[SETTINGS_V_REF_OFS_ADDRESS] = INT16_NOT_IMPLEMENTED
    await settings.async_update()

    assert (settings.w_max, settings.v_ref_ofs) == (None, None)


async def test_the_mppt_modules_use_the_parent_scale_factors(inverter_unit):
    chain = await scan(inverter_unit, 40000)
    mppt = models.Mppt(inverter_unit, chain.first(models.MPPT_MODEL_ID))
    await mppt.async_update()
    assert [module.id_str for module in mppt.module] == [
        "MPPT 1",
        "MPPT 2",
        "StCha 3",
        "StDisCha 4",
    ]
    assert mppt.module[0].dcw == 2908.2
    assert mppt.module[0].dcwh == 23386544.81


async def test_the_storage_model_decodes(inverter_unit):
    chain = await scan(inverter_unit, 40000)
    storage = models.Storage(inverter_unit, chain.first(models.STORAGE_MODEL_ID))
    await storage.async_update()
    assert (storage.stor_ctl_mod, storage.cha_st, storage.cha_gri_set) == (0, 6, 1)
    assert (storage.min_rsv_pct, storage.cha_state) == (5.0, 96.0)
    assert (storage.out_w_rte, storage.in_w_rte) == (100.0, 100.0)


async def test_the_meter_model_decodes_on_its_own_unit(meter_unit):
    chain = await scan(meter_unit, 40000)
    meter = models.AcMeter(meter_unit, chain.first(*models.METER_MODEL_IDS))
    await meter.async_update()
    assert meter.model_id == 203
    assert (meter.w, meter.hz) == (30.0, 50.0)
    assert (meter.tot_wh_exp, meter.tot_wh_imp) == (16024071.0, 11710269.0)
