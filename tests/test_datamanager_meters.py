"""Which meters a Datamanager has, from its Modbus units and its Solar API."""

import json
from typing import Any

import pytest
import requests

from custom_components.fronius_modbus import datamanager
from custom_components.fronius_modbus.datamanager import (
    Site,
    SolarApiMeter,
    meter_topology,
    read_site,
)
from custom_components.fronius_modbus.web_errors import FroniusWebUnreachable

HOST = "192.0.2.10"
METERS_URL = f"http://{HOST}/solar_api/v1/GetMeterRealtimeData.cgi?Scope=System"
INVERTERS_URL = f"http://{HOST}/solar_api/v1/GetInverterInfo.cgi"
ONE_INVERTER = {"1": {"DT": 123}}
GRID = 0
LOAD = 1


def api_meter(serial: Any = "A1", location: Any = GRID, **more: Any) -> dict[str, Any]:
    """A meter as GetMeterRealtimeData lists it, as far as it is read here."""
    details = {"Manufacturer": "Fronius", "Model": "Smart Meter 63A", "Serial": serial}
    return {"Details": details, "Meter_Location_Current": location, **more}


def serve(monkeypatch, meters: Any, inverters: Any, status: int = 200) -> list[str]:
    """Answer the two Solar API requests with these `Body.Data`; returns the URLs."""
    asked: list[str] = []
    answers = {METERS_URL: meters, INVERTERS_URL: inverters}

    def _answer(method: str, url: str, **options: Any) -> requests.Response:
        del options
        assert method == "get"
        asked.append(url)
        response = requests.Response()
        response.status_code = status
        response.encoding = "utf-8"
        body = {"Body": {"Data": answers[url]}, "Head": {"Status": {"Code": 0}}}
        response._content = json.dumps(body).encode("utf-8")
        return response

    monkeypatch.setattr(datamanager, "_http", _answer)
    return asked


def test_the_solar_api_lists_the_meters_and_the_inverters(monkeypatch):
    meters = {"0": api_meter("A1", 0), "1": api_meter("B2", 256.0)}
    asked = serve(monkeypatch, meters, {"1": {}, "2": {}})

    assert read_site(HOST) == Site(
        meters=(SolarApiMeter("A1", 0), SolarApiMeter("B2", 256)), inverters=2
    )
    assert asked == [METERS_URL, INVERTERS_URL]


@pytest.mark.parametrize(
    "meter",
    [
        # A counter on an inverter's input: it is on no Modbus unit.
        api_meter("n.a.", 1),
        api_meter("", 0),
        api_meter(None, 0),
        api_meter(17, 0),
        api_meter("A1", 0, Enable=0),
        {"Meter_Location_Current": 0},
        {"Details": "Smart Meter", "Meter_Location_Current": 0},
        "meter",
    ],
)
def test_a_meter_without_a_serial_number_or_switched_off_is_left_out(
    monkeypatch, meter
):
    serve(monkeypatch, {"0": meter}, ONE_INVERTER)

    assert read_site(HOST) == Site(meters=(), inverters=1)


@pytest.mark.parametrize("location", ["0", True, None, -1, 0.5, float("nan"), []])
def test_a_location_that_is_no_number_is_unknown(monkeypatch, location):
    serve(monkeypatch, {"0": api_meter("A1", location)}, ONE_INVERTER)

    assert read_site(HOST) == Site(meters=(SolarApiMeter("A1", None),), inverters=1)


@pytest.mark.parametrize(
    ("meters", "inverters"),
    [([], ONE_INVERTER), (None, ONE_INVERTER), ("none", ONE_INVERTER)],
)
def test_an_answer_of_another_shape_has_no_meters(monkeypatch, meters, inverters):
    serve(monkeypatch, meters, inverters)

    assert read_site(HOST) == Site(meters=(), inverters=1)


@pytest.mark.parametrize("inverters", [[], None, {}, "1"])
def test_unread_inverters_are_not_counted_as_one(monkeypatch, inverters):
    serve(monkeypatch, {"0": api_meter()}, inverters)

    assert read_site(HOST).inverters == 0


def test_no_answer_is_no_site(monkeypatch):
    serve(monkeypatch, {"0": api_meter()}, ONE_INVERTER, status=503)

    assert read_site(HOST) is None


def test_an_unreachable_device_is_no_site(monkeypatch):
    def _unreachable(method: str, url: str, **options: Any) -> requests.Response:
        raise FroniusWebUnreachable("ConnectTimeout")

    monkeypatch.setattr(datamanager, "_http", _unreachable)

    assert read_site(HOST) is None


def site(*meters: tuple[str, int | None], inverters: int = 1) -> Site:
    """What the Solar API says, shortened."""
    listed = tuple(SolarApiMeter(serial, location) for serial, location in meters)
    return Site(meters=listed, inverters=inverters)


@pytest.mark.parametrize(
    ("found", "known", "unit_ids", "primary", "locations"),
    [
        # No meter found: the first unit is asked again with every poll.
        ({}, site(), [240], 240, {}),
        ({}, None, [240], 240, {}),
        ({240: "A1"}, site(("A1", GRID)), [240], 240, {240: GRID}),
        # One meter on each side is the same one, however it writes its number.
        ({240: ""}, site(("A1", GRID)), [240], 240, {240: GRID}),
        ({240: "0A1"}, site(("A1", LOAD)), [240], 240, {240: LOAD}),
        ({240: "A1"}, site(("A1", None)), [240], 240, {}),
        ({240: "A1"}, None, [240], 240, {}),
        ({240: "A1"}, site(), [240], 240, {}),
        # The grid meter leads, whatever its address.
        (
            {240: "A1", 241: "B2"},
            site(("B2", GRID), ("A1", 256)),
            [240, 241],
            241,
            {240: 256, 241: GRID},
        ),
        # Two meters and no matching number: nothing is guessed.
        ({240: "", 241: ""}, site(("A1", GRID), ("B2", 256)), [240, 241], 240, {}),
        ({240: "A1", 241: ""}, site(("A1", 256)), [240, 241], 240, {240: 256}),
        ({240: "A1"}, site(("B2", GRID), ("C3", 256)), [240], 240, {}),
        # The first unit stays listed when its meter is silent.
        ({241: "B2"}, site(("B2", GRID)), [240, 241], 241, {241: GRID}),
        ({241: "B2", 243: "D4"}, None, [240, 241, 243], 241, {}),
        ({}, site(("A1", GRID)), [240], 240, {}),
        # A sub-meter is no load while the grid meter is silent.
        ({241: "B2"}, site(("A1", GRID), ("B2", 256)), [240, 241], 241, {}),
        ({240: "B2"}, site(("A1", GRID), ("B2", LOAD)), [240], 240, {}),
        ({240: "B2"}, site(("B2", 256), ("C3", 257)), [240], 240, {240: 256}),
    ],
)
def test_the_meters_and_where_they_are(found, known, unit_ids, primary, locations):
    topology = meter_topology(found, known)

    assert topology.unit_ids == unit_ids
    assert topology.primary_unit_id == primary
    assert topology.locations == locations
    # Exception 11 does not prove that a meter is gone.
    assert topology.confirmed is False
    assert topology.reported_unit_ids == ()


@pytest.mark.parametrize("inverters", [0, 2])
def test_the_grid_meter_s_place_is_left_out_with_other_inverters(inverters):
    """The load adds one inverter's power to the grid meter's."""
    known = site(("A1", GRID), ("B2", 256), inverters=inverters)
    topology = meter_topology({240: "A1", 241: "B2"}, known)

    assert topology.primary_unit_id == 240
    assert topology.locations == {241: 256}
