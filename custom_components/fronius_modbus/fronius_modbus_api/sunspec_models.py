"""SunSpec components of a Fronius inverter and its meters.

Generated with ``python -m modbus_connection.model.sunspec.generate 1 101 103
120 121 122 123 124 160 201 202 203 204`` and pruned to the points this
integration reads or writes. Models 101/103 and 201-204 share one point layout
each, so one class serves every variant; the model id tells the phases apart.
Status and bitfield points stay raw integers: the state texts they map to are
part of the entity contract and live in the integration, not here.
"""

from __future__ import annotations

from modbus_connection.model import Component, repeating_group
from modbus_connection.model.sunspec import (
    SunSpecComponent,
    acc32,
    bitfield16,
    bitfield32,
    enum16,
    int16,
    string,
    uint16,
)

INVERTER_MODEL_IDS = (103, 101)
METER_MODEL_IDS = (203, 201, 202, 204)
SINGLE_PHASE_METER_MODEL_ID = 201
THREE_PHASE_INVERTER_MODEL_ID = 103
STORAGE_MODEL_ID = 124
MPPT_MODEL_ID = 160
NAMEPLATE_MODEL_ID = 120
SETTINGS_MODEL_ID = 121
STATUS_MODEL_ID = 122
CONTROLS_MODEL_ID = 123
COMMON_MODEL_ID = 1
SUNSPEC_BASE_ADDRESS = 40000
COMMON_SERIAL_WORDS = 16


class Common(SunSpecComponent):
    """SunSpec model 1: Common."""

    mn = string(2, 16)
    md = string(18, 16)
    opt = string(34, 8)
    vr = string(42, 8)
    sn = string(50, 16)
    da = uint16(66)


class Inverter(SunSpecComponent):
    """SunSpec model 101 (single phase) or 103 (three phase): the AC side."""

    a = uint16(2, scale_register=6, unit="A")
    aph_a = uint16(3, scale_register=6, unit="A")
    aph_b = uint16(4, scale_register=6, unit="A")
    aph_c = uint16(5, scale_register=6, unit="A")
    pp_vph_ab = uint16(7, scale_register=13, unit="V")
    pp_vph_bc = uint16(8, scale_register=13, unit="V")
    pp_vph_ca = uint16(9, scale_register=13, unit="V")
    ph_vph_a = uint16(10, scale_register=13, unit="V")
    ph_vph_b = uint16(11, scale_register=13, unit="V")
    ph_vph_c = uint16(12, scale_register=13, unit="V")
    w = int16(14, scale_register=15, unit="W")
    hz = uint16(16, scale_register=17, unit="Hz")
    v_ar = int16(20, scale_register=21, unit="var")
    wh = acc32(24, scale_register=26, unit="Wh")
    st = enum16(38)
    st_vnd = enum16(39)
    evt_vnd2 = bitfield32(46)


class Nameplate(SunSpecComponent):
    """SunSpec model 120: ratings."""

    wh_rtg = uint16(19, scale_register=20, unit="Wh")
    max_cha_rte = uint16(23, scale_register=24, unit="W")
    max_dis_cha_rte = uint16(25, scale_register=26, unit="W")


class Settings(SunSpecComponent):
    """SunSpec model 121: basic settings."""

    w_max = uint16(2, scale_register=22, unit="W")
    v_ref = uint16(3, scale_register=23, unit="V")
    v_ref_ofs = int16(4, scale_register=24, unit="V")


class Status(SunSpecComponent):
    """SunSpec model 122: measurements and status."""

    pv_conn = bitfield16(2)
    stor_conn = bitfield16(3)
    ecp_conn = bitfield16(4)
    st_act_ctl = bitfield32(35)
    ris = uint16(44, scale_register=45, unit="ohms")


class Controls(SunSpecComponent):
    """SunSpec model 123: immediate controls."""

    conn = enum16(4, writable=True)
    w_max_lim_pct = uint16(5, scale_register=23, writable=True, unit="% WMax")
    w_max_lim_ena = enum16(9, writable=True)
    out_pf_set = int16(10, scale_register=24, writable=True, unit="cos()")
    out_pf_set_ena = enum16(14, writable=True)
    v_ar_pct_ena = enum16(22)


class Storage(SunSpecComponent):
    """SunSpec model 124: storage."""

    w_cha_max = uint16(2, scale_register=18, unit="W")
    stor_ctl_mod = bitfield16(5, writable=True)
    min_rsv_pct = uint16(7, scale_register=21, writable=True, unit="% WChaMax")
    cha_state = uint16(8, scale_register=22, unit="% AhrRtg")
    cha_st = enum16(11)
    out_w_rte = int16(12, scale_register=25, writable=True, unit="% WDisChaMax")
    in_w_rte = int16(13, scale_register=25, writable=True, unit="% WChaMax")
    cha_gri_set = enum16(17)


class MpptModule(Component):
    """One module block of model 160; the scale factors sit in the parent's fixed block."""

    id = uint16(10)
    id_str = string(11, 8)
    dca = uint16(19, scale_register=2, unit="A")
    dcv = uint16(20, scale_register=3, unit="V")
    dcw = uint16(21, scale_register=4, unit="W")
    dcwh = acc32(22, scale_register=5, unit="Wh")


class Mppt(SunSpecComponent):
    """SunSpec model 160: multiple MPPT inverter extension."""

    n = uint16(8)
    module = repeating_group(uint16(8), MpptModule, stride=20)


class AcMeter(SunSpecComponent):
    """SunSpec model 201-204: an AC meter (integer variant, as Fronius serves it)."""

    a = int16(2, scale_register=6, unit="A")
    aph_a = int16(3, scale_register=6, unit="A")
    aph_b = int16(4, scale_register=6, unit="A")
    aph_c = int16(5, scale_register=6, unit="A")
    ph_vph_a = int16(8, scale_register=15, unit="V")
    ph_vph_b = int16(9, scale_register=15, unit="V")
    ph_vph_c = int16(10, scale_register=15, unit="V")
    ppv = int16(11, scale_register=15, unit="V")
    hz = int16(16, scale_register=17, unit="Hz")
    w = int16(18, scale_register=22, unit="W")
    wph_a = int16(19, scale_register=22, unit="W")
    wph_b = int16(20, scale_register=22, unit="W")
    wph_c = int16(21, scale_register=22, unit="W")
    tot_wh_exp = acc32(38, scale_register=54, unit="Wh")
    tot_wh_imp = acc32(46, scale_register=54, unit="Wh")
