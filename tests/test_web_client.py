"""The Fronius web API client, against a fake inverter stubbed at the HTTP adapter.

The adapter is the lowest boundary `requests` offers that still runs the real
session: the response hooks fire, so the digest retry in `XHeaderDigestAuth`
is exercised rather than mocked away.
"""

import json
import socket
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from custom_components.fronius_modbus import froniuswebclient
from custom_components.fronius_modbus.froniuswebclient import (
    ClientIpResolutionError,
    FroniusWebAuthError,
    FroniusWebClient,
    XHeaderDigestAuth,
    _parse_inverter_readable,
    _parse_power_meter_info,
    _parse_storage_readable,
    login,
    mint_token,
)

HOST = "192.0.2.10"
REALM = "Webinterface area"
CHALLENGE = {"realm": REALM, "nonce": "nonce-1", "qop": "auth"}


class FakeInverter:
    """The routes these tests need, answering like the inverter's web server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.authorized_calls: list[str] = []
        self.hashing_version = 2
        self.nonce = "nonce-1"
        self.opaque = "op"
        self.always_401 = False
        self.no_challenge = False
        self.bodies: dict[str, object] = {}
        self.statuses: dict[str, int] = {}

    def handle(self, method: str, path: str, payload: dict | None, headers) -> tuple:
        if path == "/api/status/common":
            return (
                200,
                {
                    "authenticationOptions": {
                        "digest": {"customerHashingVersion": self.hashing_version}
                    }
                },
                {},
            )

        # The inverter serves the meter list without authentication, and the
        # client relies on that: it reads this one path with no auth attached.
        public = path == "/api/components/PowerMeter/readable"

        authorization = headers.get("Authorization")
        if not public and (authorization is None or self.always_401):
            if self.no_challenge:
                return 401, {}, {}
            challenge = (
                f'Digest realm="{REALM}", nonce="{self.nonce}", qop="auth", '
                f'opaque="{self.opaque}"'
            )
            return 401, {}, {"X-WWW-Authenticate": challenge}

        if authorization is not None:
            self.authorized_calls.append(authorization)
        self.calls.append((method, path, payload))
        return self.statuses.get(path, 200), self.bodies.get(path, {}), {}


@pytest.fixture(autouse=True)
def _no_cached_hash_mode():
    """The hash mode is memoized per host; a fresh inverter must not inherit it."""
    froniuswebclient._hash_mode.cache_clear()
    yield
    froniuswebclient._hash_mode.cache_clear()


@pytest.fixture
def inverter(monkeypatch) -> FakeInverter:
    fake = FakeInverter()

    def send(adapter, request, **_kwargs):
        parsed = urlparse(request.url)
        payload = json.loads(request.body) if request.body else None
        status, body, headers = fake.handle(
            request.method.lower(), parsed.path, payload, request.headers
        )
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.connection = adapter
        response.headers.update(headers)
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return fake


@pytest.fixture
def client(inverter) -> FroniusWebClient:
    return FroniusWebClient(host=HOST, password="secret")


# -- digest authentication ---------------------------------------------------------


def test_the_digest_challenge_is_parsed_without_its_scheme_prefix():
    challenge = froniuswebclient._digest_challenge(
        'Digest realm="r", nonce="n", qop="auth"'
    )

    assert challenge == {"realm": "r", "nonce": "n", "qop": "auth"}


def test_an_empty_challenge_header_parses_to_nothing():
    assert froniuswebclient._digest_challenge("") == {}


def test_a_protected_request_is_retried_once_with_a_digest_header(client, inverter):
    inverter.bodies["/api/config/modbus"] = {"slave": {"mode": "tcp"}}

    assert client.get_modbus_config() == {"slave": {"mode": "tcp"}}

    header = inverter.authorized_calls[-1]
    assert header.startswith("Digest ")
    assert f'realm="{REALM}"' in header
    assert 'opaque="op"' in header
    assert 'uri="/api/config/modbus"' in header


def test_a_challenge_that_repeats_the_401_is_not_retried_again(client, inverter):
    inverter.always_401 = True

    with pytest.raises(FroniusWebAuthError):
        client.get_modbus_config()

    assert inverter.authorized_calls == []


def test_a_401_without_a_challenge_is_reported_as_an_auth_failure(client, inverter):
    inverter.no_challenge = True

    with pytest.raises(FroniusWebAuthError):
        client.get_modbus_config()


def test_the_hash_mode_follows_the_version_the_inverter_reports(inverter):
    inverter.hashing_version = 1
    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "md5"

    froniuswebclient._hash_mode.cache_clear()
    inverter.hashing_version = 2
    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "sha256"


def test_an_unreadable_status_page_falls_back_to_sha256(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise requests.ConnectionError

    monkeypatch.setattr(requests, "get", refuse)

    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "sha256"


def test_the_nonce_count_rises_while_the_nonce_stays_the_same():
    auth = XHeaderDigestAuth("customer", password="secret")
    auth.mode = "sha256"

    first = auth._build_header("get", "/a", CHALLENGE)
    second = auth._build_header("get", "/a", CHALLENGE)
    third = auth._build_header("get", "/a", CHALLENGE | {"nonce": "nonce-2"})

    assert "nc=00000001" in first
    assert "nc=00000002" in second
    assert "nc=00000001" in third


def test_a_stored_token_replaces_the_password_in_the_digest_secret():
    auth = XHeaderDigestAuth("customer", token={"realm": REALM, "token": "stored"})

    assert auth._secret(REALM) == "stored"


def test_the_login_uri_drops_the_query_the_other_paths_keep():
    auth = XHeaderDigestAuth("customer")

    assert auth._digest_uri("http://h/api/commands/Login?user=customer") == (
        "/api/commands/Login"
    )
    assert auth._digest_uri("http://h/api/config/modbus?a=1") == (
        "/api/config/modbus?a=1"
    )


def test_login_succeeds_and_mints_a_token_for_the_realm(inverter):
    assert login(HOST, "customer", password="secret") is True
    assert mint_token(HOST, "customer", "secret") == {
        "realm": REALM,
        "token": XHeaderDigestAuth("customer", password="secret")._secret(REALM),
    }


def test_login_fails_when_the_inverter_keeps_refusing(inverter):
    inverter.always_401 = True

    assert login(HOST, "customer", password="secret") is False
    assert mint_token(HOST, "customer", "secret") is None


def test_the_client_reports_the_token_its_login_minted(client):
    assert client.login() is True
    # The client's own auth object never saw the login exchange; login() runs a
    # separate one, so the client has nothing to hand back yet.
    assert client.issued_token() is None


# -- payload parsing ---------------------------------------------------------------


def meter_node(addr, model="Smart Meter TS 65A-3", **attributes) -> dict:
    return {"attributes": {"addr": addr, "model": model, **attributes}}


def test_the_power_meter_payload_is_read_from_the_plain_body_shape():
    payload = {
        "Body": {
            "Data": {
                "1": meter_node("1", label="<primary>", phaseCnt="3"),
                "2": meter_node("2", **{"meter-location": "1", "phaseCnt": "1"}),
            }
        }
    }

    info = _parse_power_meter_info(payload, 200)

    assert info["unit_ids"] == [200, 201]
    assert info["primary_unit_id"] == 200
    assert info["phase_counts_by_unit_id"] == {200: 3, 201: 1}
    assert info["locations_by_unit_id"] == {201: 1}
    assert info["payload_shape"] == "Body.Data"


def test_the_power_meter_payload_is_also_read_from_the_nested_meter_shape():
    payload = {"meter": {"Body": {"Data": {"1": meter_node("3")}}}}

    info = _parse_power_meter_info(payload, 200)

    assert info["unit_ids"] == [202]
    assert info["primary_unit_id"] == 202
    assert info["payload_shape"] == "meter.Body.Data"


def test_the_meter_at_location_zero_becomes_the_primary_one():
    payload = {
        "Body": {
            "Data": {
                "1": meter_node("2", **{"meter-location": "1"}),
                "2": meter_node("1", **{"meter-location": "0"}),
            }
        }
    }

    info = _parse_power_meter_info(payload, 200)

    assert info["primary_unit_id"] == 200
    assert info["locations_by_unit_id"] == {200: 0, 201: 1}


def test_nodes_that_are_not_power_meters_are_left_out():
    payload = {
        "Body": {
            "Data": {
                "1": meter_node("1", model="Inverter"),
                "2": meter_node("0"),
                "3": meter_node("x"),
                "4": {"attributes": "not a mapping"},
                "5": "not a node",
            }
        }
    }

    assert _parse_power_meter_info(payload, 200)["unit_ids"] == []


def test_an_unrecognized_power_meter_payload_has_no_shape():
    assert _parse_power_meter_info({"nothing": {}}, 200) is None


def test_the_storage_identity_prefers_the_nameplate_over_the_attributes():
    payload = {
        "Body": {
            "Data": {
                "0": {
                    "attributes": {
                        "nameplate": json.dumps(
                            {"manufacturer": "BYD", "serial": "SN-1"}
                        ),
                        "DisplayName": "HVS",
                    },
                    "channels": {"BAT_TEMPERATURE_CELL_F64": 22.5},
                }
            }
        }
    }

    assert _parse_storage_readable(payload) == {
        "manufacturer": "BYD",
        "model": "HVS",
        "serial": "SN-1",
        "cell_temperature": 22.5,
    }


def test_a_storage_payload_without_data_falls_back_to_the_generic_identity():
    assert _parse_storage_readable(None) == {
        "manufacturer": None,
        "model": "Battery Storage",
        "serial": None,
        "cell_temperature": None,
    }


def test_a_broken_nameplate_string_does_not_break_the_storage_identity():
    payload = {
        "Body": {"Data": {"0": {"attributes": {"nameplate": "{", "model": "HVM"}}}}
    }

    info = _parse_storage_readable(payload)

    assert (info["model"], info["manufacturer"]) == ("HVM", None)


def test_the_inverter_temperature_is_read_from_its_channel():
    payload = {
        "Body": {
            "Data": {"0": {"channels": {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 41.5}}}
        }
    }

    assert _parse_inverter_readable(payload) == {"temperature": 41.5}


def test_an_inverter_payload_without_channels_has_no_temperature():
    assert _parse_inverter_readable({"Body": {"Data": {"0": {}}}}) == {
        "temperature": None
    }
    assert _parse_inverter_readable(None) == {"temperature": None}


# -- the read endpoints ------------------------------------------------------------


def test_the_power_meter_endpoint_is_read_without_authentication(client, inverter):
    inverter.bodies["/api/components/PowerMeter/readable"] = {
        "Body": {"Data": {"1": meter_node("1")}}
    }

    assert client.get_power_meter_info()["unit_ids"] == [200]
    assert inverter.authorized_calls == []


def test_a_failing_power_meter_read_is_reported_as_no_information(client, inverter):
    inverter.statuses["/api/components/PowerMeter/readable"] = 500

    assert client.get_power_meter_info() is None


def test_an_unparsable_power_meter_payload_is_reported_as_no_information(
    client, inverter
):
    inverter.bodies["/api/components/PowerMeter/readable"] = {"unexpected": {}}

    assert client.get_power_meter_info() is None


def test_the_storage_and_inverter_identities_come_from_the_readable_endpoints(
    client, inverter
):
    inverter.bodies["/api/components/BatteryManagementSystem/readable"] = {
        "Body": {"Data": {"0": {"attributes": {"model": "HVS"}}}}
    }
    inverter.bodies["/api/components/inverter/readable"] = {
        "Body": {
            "Data": {"0": {"channels": {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 40.0}}}
        }
    }

    assert client.get_storage_info()["model"] == "HVS"
    assert client.get_inverter_info()["temperature"] == 40.0


def test_a_failing_readable_endpoint_falls_back_to_the_empty_identity(client, inverter):
    inverter.statuses["/api/components/BatteryManagementSystem/readable"] = 500
    inverter.statuses["/api/components/inverter/readable"] = 500

    assert client.get_storage_info()["model"] == "Battery Storage"
    assert client.get_inverter_info()["temperature"] is None


def test_an_auth_failure_on_a_readable_endpoint_is_not_swallowed(client, inverter):
    inverter.always_401 = True

    with pytest.raises(FroniusWebAuthError):
        client.get_storage_info()
    with pytest.raises(FroniusWebAuthError):
        client.get_inverter_info()


def test_the_battery_and_solar_api_configs_are_read_from_their_paths(client, inverter):
    inverter.bodies["/api/config/batteries"] = {"HYB_EM_MODE": 1}
    inverter.bodies["/api/config/solar_api"] = {"SolarAPIv1Enabled": True}

    assert client.get_battery_config() == {"HYB_EM_MODE": 1}
    assert client.get_solar_api_config() == {"SolarAPIv1Enabled": True}


def test_an_unavailable_export_limit_config_reads_as_empty(client, inverter):
    inverter.statuses["/api/config/limit_settings/powerLimits"] = 404

    assert client.get_export_limit_config() == {}


# -- enabling Modbus ---------------------------------------------------------------


def modbus_config(**slave) -> dict:
    return {
        "slave": {
            "mode": "tcp",
            "sunspecMode": "int",
            "port": 502,
            "meterAddress": 200,
            "rtu_inverter_slave_id": 1,
            "ctr": {"on": True, "restriction": {"on": False}},
            **slave,
        }
    }


def test_modbus_that_already_matches_is_not_written_again(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config()

    assert client.ensure_modbus_enabled(502, 200, 1) is False
    assert [call for call in inverter.calls if call[0] == "post"] == []


def test_disabled_modbus_is_switched_on_with_one_write(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config(
        ctr={"on": False, "restriction": {"on": False}}
    )

    assert client.ensure_modbus_enabled(502, 200, 1) is True

    writes = [call for call in inverter.calls if call[0] == "post"]
    assert len(writes) == 1
    slave = writes[0][2]["slave"]
    assert (slave["mode"], slave["port"], slave["meterAddress"]) == ("tcp", 502, 200)
    assert slave["ctr"] == {"on": True, "restriction": {"on": False}}
    assert writes[0][2]["master"] == froniuswebclient.MASTER_RTUIF["master"]


def test_a_different_port_is_rewritten(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config(port=1502)

    assert client.ensure_modbus_enabled(502, 200, 1) is True


def test_the_restriction_carries_the_resolved_client_ip(client, inverter, monkeypatch):
    inverter.bodies["/api/config/modbus"] = modbus_config()
    monkeypatch.setattr(
        FroniusWebClient, "_resolve_client_ip", lambda self: "192.0.2.99"
    )

    assert client.ensure_modbus_enabled(502, 200, 1, restrict_to_client_ip=True) is True

    write = next(call for call in inverter.calls if call[0] == "post")
    assert write[2]["slave"]["ctr"]["restriction"] == {
        "on": True,
        "ip": "192.0.2.99",
    }


def test_a_matching_restriction_is_not_rewritten(client, inverter, monkeypatch):
    inverter.bodies["/api/config/modbus"] = modbus_config(
        ctr={"on": True, "restriction": {"on": True, "ip": "192.0.2.99"}}
    )
    monkeypatch.setattr(
        FroniusWebClient, "_resolve_client_ip", lambda self: "192.0.2.99"
    )

    assert (
        client.ensure_modbus_enabled(502, 200, 1, restrict_to_client_ip=True) is False
    )


def test_an_unroutable_host_makes_the_restriction_unresolvable(client, monkeypatch):
    class RefusingSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self, _address):
            raise OSError("no route to host")

    monkeypatch.setattr(socket, "socket", lambda *_args: RefusingSocket())

    with pytest.raises(ClientIpResolutionError):
        client.ensure_modbus_enabled(502, 200, 1, restrict_to_client_ip=True)


def test_a_loopback_client_ip_is_refused_as_a_restriction(client, monkeypatch):
    class LoopbackSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self, _address):
            return None

        def getsockname(self):
            return ("127.0.0.1", 0)

    monkeypatch.setattr(socket, "socket", lambda *_args: LoopbackSocket())

    with pytest.raises(ClientIpResolutionError):
        client.ensure_modbus_enabled(502, 200, 1, restrict_to_client_ip=True)


# -- the write endpoints -----------------------------------------------------------


def posted(inverter: FakeInverter, path: str) -> dict | None:
    return next(
        call[2] for call in inverter.calls if call[0] == "post" and call[1] == path
    )


def test_manual_battery_mode_writes_the_manual_soc_mode_and_minimum(client, inverter):
    assert client.set_battery_config(1, power=-2000, soc_min=12) is True

    assert posted(inverter, "/api/config/batteries") == {
        "HYB_EM_MODE": 1,
        "BAT_M0_SOC_MODE": "manual",
        "BAT_M0_SOC_MIN": 12,
        "HYB_EM_POWER": -2000,
    }


def test_leaving_manual_battery_mode_restores_the_full_soc_window(client, inverter):
    assert client.set_battery_config(0) is True

    assert posted(inverter, "/api/config/batteries") == {
        "HYB_EM_MODE": 0,
        "BAT_M0_SOC_MODE": "auto",
        "BAT_M0_SOC_MIN": 5,
        "BAT_M0_SOC_MAX": 100,
    }


def test_the_soc_window_write_carries_the_backup_reserve(client, inverter):
    assert client.set_battery_soc_config(soc_min=10, soc_max=95, backup_reserved=7)

    assert posted(inverter, "/api/config/batteries") == {
        "BAT_M0_SOC_MIN": 10,
        "BAT_M0_SOC_MODE": "manual",
        "BAT_M0_SOC_MAX": 95,
        "HYB_BACKUP_RESERVED": 7,
    }


def test_the_charge_sources_are_written_as_booleans(client, inverter):
    assert client.set_battery_charge_sources(True, False) is True

    assert posted(inverter, "/api/config/batteries") == {
        "HYB_EVU_CHARGEFROMGRID": True,
        "HYB_BM_CHARGEFROMAC": False,
    }


def test_disabling_the_solar_api_also_clears_the_discovery_flag(client, inverter):
    assert client.set_solar_api_enabled(False) is True

    assert posted(inverter, "/api/config/solar_api") == {
        "SolarAPIv1Enabled": False,
        "activeOnExternalDevicesDiscovered": False,
    }


def test_the_modbus_control_reset_is_a_bare_command(client, inverter):
    assert client.reset_modbus_control() is True

    assert posted(inverter, "/api/commands/ModbusReset") is None


def test_the_export_soft_limit_write_leaves_the_rest_of_the_config_alone(
    client, inverter
):
    inverter.bodies["/api/config/limit_settings/powerLimits"] = {
        "exportLimits": {
            "activePower": {
                "softLimit": {"enabled": False, "powerLimit": 0},
                "hardLimit": {"enabled": True, "powerLimit": 10000},
            }
        },
        "otherSetting": "untouched",
    }

    assert client.set_export_soft_limit(7000) is True

    written = posted(inverter, "/api/config/limit_settings/powerLimits")
    assert written["exportLimits"]["activePower"]["softLimit"] == {
        "enabled": True,
        "powerLimit": 7000,
    }
    assert written["exportLimits"]["activePower"]["hardLimit"]["powerLimit"] == 10000
    assert written["otherSetting"] == "untouched"


def test_the_login_request_names_the_user_it_authenticates(inverter, monkeypatch):
    seen: list[dict] = []
    original = requests.adapters.HTTPAdapter.send

    def record(adapter, request, **kwargs):
        seen.append(parse_qs(urlparse(request.url).query))
        return original(adapter, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", record)

    login(HOST, "technician", password="secret")

    assert {"user": ["technician"]} in seen


def test_assisted_setup_converts_an_existing_float_register_map():
    """Audit F13: a TCP map that only differed in sunspecMode counted as ready, then failed the probe."""
    client = FroniusWebClient("192.0.2.10")
    client.get_modbus_config = lambda: {
        "slave": {
            "mode": "tcp",
            "port": 502,
            "meterAddress": 200,
            "rtu_inverter_slave_id": 1,
            "sunspecMode": "float",
            "ctr": {"on": True, "restriction": {"on": False}},
        }
    }
    sent = {}
    client._request = lambda method, path, payload=None: sent.update(payload=payload)
    assert client.ensure_modbus_enabled(502, 200, 1) is True
    assert sent["payload"]["slave"]["sunspecMode"] == "int"
