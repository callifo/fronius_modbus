"""The Fronius web API client, against a fake inverter stubbed at the HTTP adapter.

The adapter is the lowest boundary `requests` offers that still runs the real
session: the response hooks fire, so the digest retry in `XHeaderDigestAuth`
is exercised rather than mocked away.
"""

import json
import logging
import socket
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from custom_components.fronius_modbus import froniuswebclient
from custom_components.fronius_modbus.const import ModbusRestriction
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlRefused,
)
from custom_components.fronius_modbus.froniuswebclient import (
    ClientIpResolutionError,
    FroniusWebAuthError,
    FroniusWebClient,
    FroniusWebResponseError,
    FroniusWebUnreachable,
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
        # Every request that carried a login, answered or refused.
        self.login_attempts: list[str] = []
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
        if authorization is not None:
            self.login_attempts.append(path)
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
    froniuswebclient._forget_hash_modes()
    yield
    froniuswebclient._forget_hash_modes()


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


def test_a_client_without_credentials_never_answers_a_challenge(inverter):
    """Audit E2-01: a no-login entry sent an empty-password login on every poll."""
    client = FroniusWebClient(host=HOST, password="")

    with pytest.raises(FroniusWebAuthError):
        client.get_inverter_info()

    assert inverter.login_attempts == []


def test_a_rejected_login_is_not_tried_again(inverter):
    """Audit E2-02: after a lost login the client kept retrying the rejected token."""
    inverter.always_401 = True
    client = FroniusWebClient(host=HOST, token={"realm": REALM, "token": "stale"})

    for _ in range(2):
        with pytest.raises(FroniusWebAuthError):
            client.get_inverter_info()

    assert inverter.login_attempts == ["/api/components/inverter/readable"]


def test_the_hash_mode_follows_the_version_the_inverter_reports(inverter):
    inverter.hashing_version = 1
    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "md5"

    froniuswebclient._forget_hash_modes()
    inverter.hashing_version = 2
    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "sha256"


def test_an_unreadable_status_page_falls_back_to_sha256(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise requests.ConnectionError

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)

    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "sha256"


def test_a_timed_out_status_page_is_not_remembered_as_the_hash_mode(
    inverter, monkeypatch
):
    """Audit A07: the sha256 fallback was cached, so md5 devices never logged in again."""
    inverter.hashing_version = 1
    answered = requests.adapters.HTTPAdapter.send
    attempts = []

    def refuse_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ConnectionError
        return answered(*args, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse_once)

    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "sha256"
    assert froniuswebclient._hash_mode(f"http://{HOST}", "customer", 4.0) == "md5"


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
        "missing": False,
        "readings": {"BAT_TEMPERATURE_CELL_F64": 22.5},
    }


def test_a_storage_payload_without_data_falls_back_to_the_generic_identity():
    assert _parse_storage_readable(None) == {
        "manufacturer": None,
        "model": "Battery Storage",
        "serial": None,
        "missing": False,
        "readings": None,
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

    assert _parse_inverter_readable(payload)["readings"] == {
        "DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 41.5
    }


def test_an_inverter_payload_without_channels_has_no_temperature():
    assert _parse_inverter_readable({"Body": {"Data": {"0": {}}}})["readings"] == {}
    assert _parse_inverter_readable(None) == {"readings": None, "missing": False}


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
    assert client.get_inverter_info()["readings"] == {
        "DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 40.0
    }


def test_a_failing_readable_endpoint_falls_back_to_the_empty_identity(client, inverter):
    inverter.statuses["/api/components/BatteryManagementSystem/readable"] = 500
    inverter.statuses["/api/components/inverter/readable"] = 500

    assert client.get_storage_info()["model"] == "Battery Storage"
    assert client.get_inverter_info()["readings"] is None


READABLE_PATHS = (
    ("get_inverter_info", "/api/components/inverter/readable"),
    ("get_storage_info", "/api/components/BatteryManagementSystem/readable"),
)


def warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize(("read", "path"), READABLE_PATHS)
def test_a_failing_readable_endpoint_is_warned_about_once(
    client, inverter, caplog, read, path
):
    """Its values stay unknown, but not silently: that hid a server error (audit A24-05)."""
    inverter.statuses[path] = 500

    with caplog.at_level(logging.DEBUG):
        getattr(client, read)()
        getattr(client, read)()
        assert len(warnings(caplog)) == 1
        assert path in warnings(caplog)[0]

        caplog.clear()
        inverter.statuses[path] = 200
        getattr(client, read)()
    assert [r.levelno for r in caplog.records if path in r.getMessage()] == [
        logging.INFO
    ]


@pytest.mark.parametrize(("read", "path"), READABLE_PATHS)
def test_firmware_without_a_readable_endpoint_is_not_a_warning(
    client, inverter, caplog, read, path
):
    inverter.statuses[path] = 404

    getattr(client, read)()

    assert warnings(caplog) == []


@pytest.mark.parametrize(("read", "path"), READABLE_PATHS)
@pytest.mark.parametrize(("status", "missing"), [(404, True), (500, False)])
def test_only_a_404_marks_a_component_endpoint_missing(
    client, inverter, read, path, status, missing
):
    """A 404 is firmware without the endpoint; a 500 is a read that failed this time."""
    inverter.statuses[path] = status

    info = getattr(client, read)()

    assert (info["readings"], info["missing"]) == (None, missing)


@pytest.mark.parametrize(("read", "path"), READABLE_PATHS)
def test_a_switched_off_inverter_is_left_to_the_refresh(
    client, monkeypatch, read, path
):
    """The coordinator logs an outage once; the readable read must not add its own."""

    def refuse(adapter, request, **_kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    with pytest.raises(FroniusWebUnreachable):
        getattr(client, read)()


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


def test_a_failing_export_limit_endpoint_is_not_an_empty_config(client, inverter):
    """Audit A24-05: an HTTP 500 read as "no export limit" and the refresh succeeded."""
    inverter.statuses["/api/config/limit_settings/powerLimits"] = 500

    with pytest.raises(FroniusWebResponseError):
        client.get_export_limit_config()


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

    assert (
        client.ensure_modbus_enabled(
            502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
        )
        is True
    )

    write = next(call for call in inverter.calls if call[0] == "post")
    assert write[2]["slave"]["ctr"]["restriction"] == {
        "on": True,
        "ip": "192.0.2.99",
    }


PRE_RESTRICTED = {"on": True, "restriction": {"on": True, "ip": "192.0.2.50"}}


def test_an_existing_restriction_is_not_lifted(client, inverter):
    """Audit A24-02: setup without the checkbox wrote restriction.on=false.

    A restricted Modbus server then answered every host on the network.
    """
    inverter.bodies["/api/config/modbus"] = modbus_config(ctr=PRE_RESTRICTED)

    assert client.ensure_modbus_enabled(502, 200, 1) is False
    assert [call for call in inverter.calls if call[0] == "post"] == []


def test_a_rewrite_for_another_reason_keeps_the_restriction(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config(port=1502, ctr=PRE_RESTRICTED)

    assert client.ensure_modbus_enabled(502, 200, 1) is True

    write = next(call for call in inverter.calls if call[0] == "post")
    assert write[2]["slave"]["ctr"]["restriction"] == PRE_RESTRICTED["restriction"]


def test_lifting_the_restriction_is_a_choice_of_its_own(client, inverter):
    """Keeping is the default; "off" is written only when the owner picks it."""
    inverter.bodies["/api/config/modbus"] = modbus_config(ctr=PRE_RESTRICTED)

    assert client.ensure_modbus_enabled(502, 200, 1, restriction=ModbusRestriction.OFF)

    write = next(call for call in inverter.calls if call[0] == "post")
    assert write[2]["slave"]["ctr"]["restriction"]["on"] is False


def test_a_lifted_restriction_is_not_lifted_again(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config()

    assert (
        client.ensure_modbus_enabled(502, 200, 1, restriction=ModbusRestriction.OFF)
        is False
    )


def modbus_config_as_read(**slave) -> dict:
    """The shape the inverter answers: RTU roles as lists, a _meta beside every field."""
    config = modbus_config(**slave)
    config["slave"] |= {"baud": 19200, "parity": "e", "demo": False}
    config["slave"].setdefault("rtuif", [{"if": "rtu1"}])
    config["slave"]["_mode_meta"] = {"valueType": "String"}
    config["_slave_meta"] = {"valueType": "Object"}
    config["master"] = {"rtuif": [{"if": "rtu0"}], "_rtuif_meta": {}}
    return config


def modbus_write(inverter) -> dict:
    return next(call for call in inverter.calls if call[0] == "post")[2]


def test_enabling_tcp_keeps_an_rtu_port_in_slave_role(client, inverter):
    """The fixed payload moved every RS485 port to master and cut off an RTU client."""
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(mode="rtu")

    assert client.ensure_modbus_enabled(502, 200, 1) is True

    write = modbus_write(inverter)
    assert write["master"] == {"rtuif": [{"if": "rtu0"}]}
    assert write["slave"]["rtuif"] == [{"if": "rtu1"}]
    assert (write["slave"]["baud"], write["slave"]["parity"]) == (19200, "e")


def test_tcp_and_rtu_together_already_serve_modbus_tcp(client, inverter):
    """Mode "both" read as wrong and was rewritten to "tcp", ending the RTU side."""
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(mode="both")

    assert client.ensure_modbus_enabled(502, 200, 1) is False


def test_a_rewrite_for_another_reason_keeps_mode_both(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(
        mode="both", port=1502
    )

    assert client.ensure_modbus_enabled(502, 200, 1) is True

    assert modbus_write(inverter)["slave"]["mode"] == "both"


def test_the_field_descriptions_are_not_written_back(client, inverter):
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(mode="rtu")

    client.ensure_modbus_enabled(502, 200, 1)

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        if isinstance(node, list):
            for value in node:
                yield from keys(value)

    assert [key for key in keys(modbus_write(inverter)) if key.startswith("_")] == []


@pytest.mark.parametrize(
    ("allowed", "written"),
    [
        ("192.0.2.50", "192.0.2.50,192.0.2.99"),
        ("192.0.2.50,198.51.100.0/24", "192.0.2.50,198.51.100.0/24,192.0.2.99"),
    ],
)
def test_home_assistant_joins_the_allowed_hosts(
    client, inverter, monkeypatch, allowed, written
):
    """Replacing the list locked out every other host on it, an energy manager say."""
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(
        ctr={"on": True, "restriction": {"on": True, "ip": allowed}}
    )
    monkeypatch.setattr(
        FroniusWebClient, "_resolve_client_ip", lambda self: "192.0.2.99"
    )

    assert client.ensure_modbus_enabled(
        502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
    )

    assert modbus_write(inverter)["slave"]["ctr"]["restriction"] == {
        "on": True,
        "ip": written,
    }


def test_a_network_that_covers_home_assistant_is_left_alone(
    client, inverter, monkeypatch
):
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(
        ctr={"on": True, "restriction": {"on": True, "ip": "192.0.2.0/24"}}
    )
    monkeypatch.setattr(
        FroniusWebClient, "_resolve_client_ip", lambda self: "192.0.2.99"
    )

    assert (
        client.ensure_modbus_enabled(
            502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
        )
        is False
    )


def test_an_inactive_list_is_not_switched_on_with_home_assistant(
    client, inverter, monkeypatch
):
    """Hosts the owner entered and switched off stay off; only Home Assistant is let in."""
    inverter.bodies["/api/config/modbus"] = modbus_config_as_read(
        ctr={"on": True, "restriction": {"on": False, "ip": "192.0.2.50"}}
    )
    monkeypatch.setattr(
        FroniusWebClient, "_resolve_client_ip", lambda self: "192.0.2.99"
    )

    client.ensure_modbus_enabled(
        502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
    )

    assert modbus_write(inverter)["slave"]["ctr"]["restriction"] == {
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
        client.ensure_modbus_enabled(
            502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
        )
        is False
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
        client.ensure_modbus_enabled(
            502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
        )


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
        client.ensure_modbus_enabled(
            502, 200, 1, restriction=ModbusRestriction.HOME_ASSISTANT
        )


# -- the write endpoints -----------------------------------------------------------


def posted(inverter: FakeInverter, path: str) -> dict | None:
    return next(
        call[2] for call in inverter.calls if call[0] == "post" and call[1] == path
    )


def test_the_mode_and_the_target_are_written_apart(client, inverter):
    """HYB_EM_MODE is self-consumption optimisation; the SoC window is a separate switch."""
    assert client.set_battery_config(1) is True
    assert client.set_battery_power(-2000) is True

    assert posts(inverter) == [
        ("/api/config/batteries", {"HYB_EM_MODE": 1}),
        ("/api/config/batteries", {"HYB_EM_POWER": -2000}),
    ]


def test_leaving_manual_battery_mode_leaves_the_soc_window_alone(client, inverter):
    assert client.set_battery_config(0) is True

    assert posted(inverter, "/api/config/batteries") == {"HYB_EM_MODE": 0}


def test_the_soc_window_write_sends_the_limits_given(client, inverter):
    assert client.set_soc_limits(soc_min=10, soc_max=95)

    assert posted(inverter, "/api/config/batteries") == {
        "BAT_M0_SOC_MIN": 10,
        "BAT_M0_SOC_MAX": 95,
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


def test_enabling_the_solar_api_keeps_the_discovery_switch(client, inverter):
    """The switch is the owner's own setting in the web interface; enabling must not clear it."""
    inverter.bodies["/api/config/solar_api"] = {
        "SolarAPIv1Enabled": False,
        "_SolarAPIv1Enabled_meta": {"valueType": "Boolean"},
        "activeOnExternalDevicesDiscovered": True,
    }

    assert client.set_solar_api_enabled(True) is True

    assert posted(inverter, "/api/config/solar_api") == {
        "SolarAPIv1Enabled": True,
        "activeOnExternalDevicesDiscovered": True,
    }


BATTERIES = "/api/config/batteries"
BATTERY_CONFIG = {
    "HYB_EM_MODE": 1,
    "_HYB_EM_MODE_meta": {"valueType": "Integer"},
    "HYB_EM_POWER": -2000,
    "BAT_M0_SOC_MODE": "manual",
    "BAT_M0_SOC_MIN": 10,
    "BAT_M0_SOC_MAX": 95,
    "HYB_BACKUP_RESERVED": 7,
    "HYB_EVU_CHARGEFROMGRID": True,
    "HYB_BM_CHARGEFROMAC": True,
}


def posts(inverter: FakeInverter) -> list[tuple[str, dict | None]]:
    return [(call[1], call[2]) for call in inverter.calls if call[0] == "post"]


@pytest.mark.parametrize(
    ("write", "arguments"),
    [
        ("set_battery_config", (1,)),
        ("set_battery_power", (-2000,)),
        ("set_soc_limits", (10, 95)),
        ("set_soc_mode", ("manual",)),
        ("set_backup_reserve", (7,)),
        ("set_battery_charge_sources", (True, True)),
    ],
)
def test_a_battery_setting_already_in_place_is_not_written(
    client, inverter, write, arguments
):
    """Every battery write reconfigures the inverter; the same value again did so for nothing."""
    inverter.bodies[BATTERIES] = dict(BATTERY_CONFIG)

    assert getattr(client, write)(*arguments) is False

    assert posts(inverter) == []


def test_the_inverters_own_spelling_counts_as_the_same_value(client, inverter):
    """It sends flags as words or numbers and modes capitalised; that is no change."""
    inverter.bodies[BATTERIES] = BATTERY_CONFIG | {
        "HYB_EVU_CHARGEFROMGRID": "true",
        "HYB_BM_CHARGEFROMAC": 1,
        "BAT_M0_SOC_MODE": "Manual",
    }

    assert client.set_battery_charge_sources(True, True) is False
    assert client.set_soc_mode("manual") is False

    assert posts(inverter) == []


def test_a_config_read_that_is_no_object_still_writes_everything(client, inverter):
    """Audit R24-01: a list instead of an object raised AttributeError, an unknown error."""
    inverter.bodies[BATTERIES] = ["unexpected"]

    client.set_backup_reserve(7)

    assert posts(inverter) == [(BATTERIES, {"HYB_BACKUP_RESERVED": 7})]


@pytest.mark.parametrize(
    ("path", "write"),
    [
        ("/api/config/solar_api", lambda client: client.set_solar_api_enabled(True)),
        (
            "/api/config/modbus",
            lambda client: client.ensure_modbus_enabled(502, 200, 1),
        ),
    ],
)
def test_a_config_that_must_be_written_back_but_is_no_object_is_an_error(
    client, inverter, path, write
):
    """Without the object there is nothing to write back; that is a failed write, not a crash."""
    inverter.bodies[path] = ["unexpected"]

    with pytest.raises(FroniusWebResponseError, match="no object"):
        write(client)

    assert posts(inverter) == []


DEVICE_CHANGED = BATTERY_CONFIG | {
    # What the inverter UI or a second controller set since the last poll.
    "BAT_M0_SOC_MIN": 20,
    "HYB_EVU_CHARGEFROMGRID": True,
    "HYB_BM_CHARGEFROMAC": False,
    "HYB_EM_POWER": -500,
}


@pytest.mark.parametrize(
    ("write", "sent"),
    [
        (lambda c: c.set_soc_limits(soc_max=90), {"BAT_M0_SOC_MAX": 90}),
        (
            lambda c: c.set_battery_charge_sources(charge_from_ac=True),
            {"HYB_BM_CHARGEFROMAC": True},
        ),
        (lambda c: c.set_battery_config(1), None),
        (lambda c: c.set_battery_power(-300), {"HYB_EM_POWER": -300}),
    ],
)
def test_a_battery_write_sends_only_what_was_asked(client, inverter, write, sent):
    """Audit F24-01: cached companions undid a change made since the last poll.

    Changing the maximum re-sent a stale minimum, switching AC charging re-sent
    a stale grid flag, and selecting manual mode re-sent a stale target.
    """
    inverter.bodies[BATTERIES] = dict(DEVICE_CHANGED)

    write(client)

    assert posts(inverter) == ([] if sent is None else [(BATTERIES, sent)])


def test_the_soc_window_is_checked_against_the_fresh_read(client, inverter):
    """A maximum below the minimum set on the inverter since the last poll is refused."""
    inverter.bodies[BATTERIES] = dict(DEVICE_CHANGED)

    with pytest.raises(ControlRefused) as refused:
        client.set_soc_limits(soc_max=15)

    assert refused.value.key == "soc_minimum_above_maximum"
    assert posts(inverter) == []


def test_only_the_battery_fields_that_differ_are_written(client, inverter):
    inverter.bodies[BATTERIES] = dict(BATTERY_CONFIG)

    assert client.set_soc_limits(10, 90) is True

    assert posts(inverter) == [(BATTERIES, {"BAT_M0_SOC_MAX": 90})]


def test_an_unreadable_battery_config_still_writes_everything(client, inverter):
    """Not knowing the current value is no reason to drop the owner's input."""
    original_handle = inverter.handle

    def fail_the_read(method, path, payload, headers):
        status, body, extra = original_handle(method, path, payload, headers)
        if method == "get" and path == BATTERIES:
            status = 500
        return status, body, extra

    inverter.handle = fail_the_read

    client.set_backup_reserve(7)

    assert posts(inverter) == [(BATTERIES, {"HYB_BACKUP_RESERVED": 7})]


@pytest.mark.parametrize(
    "refusal", ["writeFailure", "permissionFailure", "validationErrors", "unknownNodes"]
)
def test_a_field_the_inverter_refuses_is_an_error(client, inverter, refusal):
    """The inverter reports each field in the body (seen on a GEN24); HTTP 200 alone is not success."""
    inverter.bodies[BATTERIES] = {"HYB_BACKUP_RESERVED": 5}
    original_handle = inverter.handle

    def answer_with_a_refusal(method, path, payload, headers):
        status, body, extra = original_handle(method, path, payload, headers)
        if method == "post":
            body = {"writeSuccess": [], refusal: ["HYB_BACKUP_RESERVED"]}
        return status, body, extra

    inverter.handle = answer_with_a_refusal

    with pytest.raises(FroniusWebResponseError, match="HYB_BACKUP_RESERVED"):
        client.set_backup_reserve(7)


def test_a_solar_api_already_in_that_state_is_not_written(client, inverter):
    inverter.bodies["/api/config/solar_api"] = {
        "SolarAPIv1Enabled": False,
        "activeOnExternalDevicesDiscovered": False,
    }

    assert client.set_solar_api_enabled(False) is False

    assert posts(inverter) == []


def test_an_export_limit_already_in_place_is_not_written(client, inverter):
    inverter.bodies["/api/config/limit_settings/powerLimits"] = {
        "exportLimits": {
            "activePower": {"softLimit": {"enabled": True, "powerLimit": 7000}}
        }
    }

    assert client.set_export_soft_limit(7000) is False

    assert posts(inverter) == []


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
    client._post = lambda path, payload=None: sent.update(payload=payload)
    assert client.ensure_modbus_enabled(502, 200, 1) is True
    assert sent["payload"]["slave"]["sunspecMode"] == "int"


def test_the_soc_mode_is_written_alone(client, inverter):
    """Switching the window to manual must not rewrite the limits the inverter holds."""
    assert client.set_soc_mode("manual") is True

    assert posted(inverter, "/api/config/batteries") == {"BAT_M0_SOC_MODE": "manual"}


def test_the_backup_reserve_is_written_alone(client, inverter):
    """The reserve is independent of the SoC window: the write must not touch the SoC mode."""
    assert client.set_backup_reserve(30) is True

    assert posted(inverter, "/api/config/batteries") == {"HYB_BACKUP_RESERVED": 30}


# -- the transport boundary (audit E01/E04) -----------------------------------------


def test_a_transport_error_leaves_the_host_behind(client, monkeypatch):
    """Requests puts the URL into its errors; the host must not travel into the logs."""

    def refuse(adapter, request, **_kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    with pytest.raises(FroniusWebUnreachable) as caught:
        client.get_battery_config()
    assert HOST not in str(caught.value)
    assert isinstance(caught.value, OSError)


def test_the_public_meter_path_is_behind_the_same_boundary(client, monkeypatch, caplog):
    """That path swallows the error into a debug line; the line must not name the host either."""

    def refuse(adapter, request, **_kwargs):
        raise requests.ReadTimeout(f"Read timed out: {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    with caplog.at_level(logging.DEBUG):
        assert client.get_power_meter_info() is None
    assert caplog.records
    assert all(HOST not in record.getMessage() for record in caplog.records)


def test_a_server_error_is_an_answer_not_an_outage(client, inverter):
    """HTTP 500 is a responding device; it must not read as switched off."""
    inverter.statuses["/api/config/batteries"] = 500
    with pytest.raises(FroniusWebResponseError) as caught:
        client.get_battery_config()
    assert not isinstance(caught.value, OSError)
    assert "500" in str(caught.value)
    assert HOST not in str(caught.value)


def test_the_login_is_behind_the_same_boundary(monkeypatch):
    """Reaudit R02: the login called requests itself, and the config flow logged its text."""

    def refuse(adapter, request, **_kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    with pytest.raises(FroniusWebUnreachable) as caught:
        froniuswebclient.mint_token(HOST, "customer", "secret")
    assert HOST not in str(caught.value)


@pytest.mark.parametrize(
    "config",
    [
        {"slave": ["unexpected"]},
        {"slave": {"ctr": "unexpected"}},
        {"slave": {"ctr": {"restriction": ["unexpected"]}}},
    ],
)
def test_a_modbus_config_of_the_wrong_shape_is_a_response_error(config):
    """Audit F24-11: an AttributeError reached the config flow as an unknown error."""
    client = FroniusWebClient("192.0.2.10")
    client.get_modbus_config = lambda: config

    with pytest.raises(FroniusWebResponseError, match="no object"):
        client.ensure_modbus_enabled(502, 200, 1)


@pytest.mark.parametrize(
    "config",
    [
        {"slave": []},
        {"slave": {"ctr": 0}},
        {"slave": {"ctr": {"restriction": ""}}},
    ],
)
def test_an_empty_wrong_type_in_the_modbus_config_is_not_written_back_with_defaults(
    config,
):
    """Reaudit RE26-01: `[]`, `0` and `""` became `{}` and a POST with defaults followed."""
    client = FroniusWebClient("192.0.2.10")
    client.get_modbus_config = lambda: config
    client._post = lambda *_args, **_kwargs: pytest.fail(
        "posted a malformed config back"
    )

    with pytest.raises(FroniusWebResponseError, match="no object"):
        client.ensure_modbus_enabled(502, 200, 1)


def test_an_unreadable_soc_window_is_not_written(client, inverter):
    """Audit FA0FB-04: an HTTP 500 read passed the window check as an empty config."""
    inverter.statuses[BATTERIES] = 500

    with pytest.raises(FroniusWebResponseError):
        client.check_soc_window(soc_min=50)
    with pytest.raises(FroniusWebResponseError):
        client.set_soc_limits(soc_min=50)
    # Both limits given need no companion from the read, and still no write.
    with pytest.raises(FroniusWebResponseError):
        client.set_soc_limits(soc_min=10, soc_max=90)

    assert posts(inverter) == []


@pytest.mark.parametrize("master", [[], "rtu", 0])
def test_a_malformed_master_is_not_written_back(master):
    """Audit FA0FB-05: `master` went into the Modbus setup POST unchecked."""
    client = FroniusWebClient("192.0.2.10")
    client.get_modbus_config = lambda: {"master": master, "slave": {"mode": "rtu"}}
    client._post = lambda *_args, **_kwargs: pytest.fail("posted a malformed master")

    with pytest.raises(FroniusWebResponseError, match="no object"):
        client.ensure_modbus_enabled(502, 200, 1)


@pytest.mark.parametrize(
    "battery_config",
    [
        {"BAT_M0_SOC_MIN": 20, "BAT_M0_SOC_MAX": "30"},
        {"BAT_M0_SOC_MIN": 20},
        {"BAT_M0_SOC_MIN": 20, "BAT_M0_SOC_MAX": True},
        # Audit R730-04: the JSON decoder turns NaN and Infinity into floats.
        {"BAT_M0_SOC_MIN": 20, "BAT_M0_SOC_MAX": float("nan")},
        {"BAT_M0_SOC_MIN": 20, "BAT_M0_SOC_MAX": float("inf")},
    ],
)
def test_a_soc_window_without_a_numeric_limit_is_unreadable(
    client, inverter, battery_config
):
    """Audit RR770-04: a text or missing maximum passed the check before Modbus."""
    inverter.bodies[BATTERIES] = battery_config

    with pytest.raises(FroniusWebResponseError, match="SoC window"):
        client.check_soc_window(soc_min=50)


@pytest.mark.parametrize("held", [[1], "garbage", 2])
def test_a_flag_the_inverter_holds_in_no_known_form_is_written(client, inverter, held):
    """Own reaudit E-01: an unreadable flag compared as already set, nothing was sent."""
    inverter.bodies[BATTERIES] = {"HYB_BM_CHARGEFROMAC": held}

    assert client.set_battery_charge_sources(charge_from_ac=False) is True

    assert posts(inverter) == [(BATTERIES, {"HYB_BM_CHARGEFROMAC": False})]


def test_modbus_control_in_no_known_form_is_switched_on():
    """Own reaudit E-01: `on: [1]` read as on, and Modbus control stayed as it was."""
    client = FroniusWebClient("192.0.2.10")
    client.get_modbus_config = lambda: {
        "slave": {
            "mode": "tcp",
            "sunspecMode": "int",
            "port": 502,
            "meterAddress": 200,
            "rtu_inverter_slave_id": 1,
            "ctr": {"on": [1], "restriction": {"on": False}},
        }
    }
    sent = []
    client._post = lambda path, payload: sent.append(payload)

    assert client.ensure_modbus_enabled(502, 200, 1) is True

    assert sent[0]["slave"]["ctr"]["on"] is True


def test_an_export_limit_switched_on_in_no_known_form_is_written(client, inverter):
    """Own reaudit E-01: `enabled: [1]` read as on, the limit counted as in place."""
    inverter.bodies["/api/config/limit_settings/powerLimits"] = {
        "exportLimits": {
            "activePower": {"softLimit": {"enabled": [1], "powerLimit": 7000}}
        }
    }

    assert client.set_export_soft_limit(7000) is True
