"""The service login of a Datamanager and what its export limit page answers."""

import copy

import pytest
import requests

from custom_components.fronius_modbus.datamanager_client import (
    EXPORT_LIMIT_PATH,
    DatamanagerClient,
    ExportLimits,
    export_limits,
)
from custom_components.fronius_modbus.web_errors import (
    FroniusWebAuthError,
    FroniusWebResponseError,
    FroniusWebUnreachable,
)

from .fake_datamanager import (
    CHALLENGE,
    HOST,
    PASSWORD,
    POWER_LIMITS,
    REALM,
    FakeDatamanager,
    answer_body,
    md5,
    minted,
    serve_datamanager,
)


@pytest.fixture
def device(monkeypatch) -> FakeDatamanager:
    return serve_datamanager(monkeypatch)


# -- the login ------------------------------------------------------------------------


def test_the_right_password_leaves_a_token(device):
    client = DatamanagerClient(HOST, password=PASSWORD)

    assert client.login() == minted()
    assert device.calls == [("GET", EXPORT_LIMIT_PATH)] * 2


def test_a_wrong_password_is_tried_once(device):
    """A device that locks a login out would count every try."""
    client = DatamanagerClient(HOST, password="wrong")

    assert client.login() is None
    assert client.login() is None
    assert len(device.logins) == 1


def test_the_stored_token_logs_in_without_the_password(device):
    client = DatamanagerClient(HOST, token=minted())

    assert client.login() == minted()
    assert client.read_export_limits().soft_w == 14500


@pytest.mark.parametrize(
    "token",
    [
        {"realm": REALM, "token": "0" * 32},
        # Stored for another device or role, so it is not sent to this one.
        {"realm": "Another area", "token": md5(f"service:{REALM}:{PASSWORD}")},
    ],
)
def test_a_token_that_does_not_fit_is_a_refused_login(device, token):
    client = DatamanagerClient(HOST, token=token)

    with pytest.raises(FroniusWebAuthError):
        client.read_export_limits()
    with pytest.raises(FroniusWebAuthError):
        client.read_export_limits()
    assert len(device.logins) <= 1


@pytest.mark.parametrize("credentials", [{"token": minted()}, {"password": PASSWORD}])
def test_a_forbidden_login_is_not_sent_again(device, credentials):
    """The service login may lack the right; every try counts towards a lock."""
    device.status = 403
    client = DatamanagerClient(HOST, **credentials)

    with pytest.raises(FroniusWebAuthError):
        client.read_export_limits()
    with pytest.raises(FroniusWebAuthError):
        client.read_export_limits()
    assert len(device.logins) == 1


def test_nothing_is_sent_without_credentials(device):
    with pytest.raises(FroniusWebAuthError):
        DatamanagerClient(HOST).read_export_limits()

    assert device.logins == []


@pytest.mark.parametrize(
    "challenge",
    [
        "",
        "Basic realm='x'",
        'Digest realm="Webinterface area", nonce="{nonce}"',
        'Digest realm="Webinterface area", nonce="{nonce}", qop="auth-int"',
        'Digest nonce="{nonce}", qop="auth"',
        'Digest realm="Webinterface area", qop="auth"',
        'Digest realm="Webinterface area", nonce="{nonce}", qop="auth", algorithm=SHA-256',
        'Digest realm="Webinterface area", nonce="{nonce}", qop="auth", algorithm=MD5-sess',
    ],
)
def test_a_challenge_of_another_kind_is_no_refused_login(device, challenge):
    """The password may be right: nobody should be asked for it again."""
    device.challenge = challenge
    client = DatamanagerClient(HOST, password=PASSWORD)

    with pytest.raises(FroniusWebResponseError):
        client.login()
    assert device.logins == []


@pytest.mark.parametrize(
    "challenge",
    [
        CHALLENGE + ", algorithm=MD5",
        CHALLENGE + ", algorithm=md5",
        'Digest realm="Webinterface area", nonce="{nonce}", qop="auth-int, auth"',
        'digest realm="Webinterface area", nonce="{nonce}", qop="auth", opaque="o1"',
    ],
)
def test_the_spellings_of_a_known_challenge_are_answered(device, challenge):
    device.challenge = challenge
    client = DatamanagerClient(HOST, password=PASSWORD)

    assert client.login() == minted()
    (login,) = device.logins
    assert login["qop"] == "auth"
    assert login.get("opaque") == ("o1" if "opaque" in challenge else None)


def test_a_nonce_used_again_is_counted(device):
    device.same_nonce = True
    client = DatamanagerClient(HOST, password=PASSWORD)

    client.login()
    client.read_export_limits()

    assert [login["nc"] for login in device.logins] == ["00000001", "00000002"]
    first, second = (login["cnonce"] for login in device.logins)
    assert first != second


def test_every_new_nonce_starts_the_count(device):
    client = DatamanagerClient(HOST, password=PASSWORD)

    client.login()
    client.read_export_limits()

    assert [login["nc"] for login in device.logins] == ["00000001", "00000001"]


# -- what the page answers ------------------------------------------------------------


def test_the_export_limits_of_the_dump(device):
    client = DatamanagerClient(HOST, token=minted())

    assert client.read_export_limits() == ExportLimits(
        soft_enabled=True,
        soft_w=14500.0,
        hard_enabled=False,
        hard_w=0.0,
        mode="entireSystem",
        fail_safe=True,
    )


@pytest.mark.parametrize(
    "body",
    [
        "<html>not JSON</html>",
        "[]",
        "null",
        {},
        {"Body": None},
        {"Body": {"Data": []}},
        {"Body": {"Data": {"powerLimits": []}}},
        # Older firmware keeps other names.
        {"Body": {"Data": {"DPL_ON": 1, "DPL_WPEAK": 18200}}},
        answer_body(POWER_LIMITS, code=12),
        answer_body(POWER_LIMITS, code="0"),
        answer_body(POWER_LIMITS, code=None),
        {"Body": {"Data": {"powerLimits": POWER_LIMITS}}},
    ],
)
def test_a_page_of_another_form_has_no_values(device, body):
    device.body = body
    client = DatamanagerClient(HOST, token=minted())

    assert client.read_export_limits() == ExportLimits()


@pytest.mark.parametrize(
    ("path", "value", "field"),
    [
        (("activePower", "softLimit", "powerLimit"), "14500", "soft_w"),
        (("activePower", "softLimit", "powerLimit"), True, "soft_w"),
        (("activePower", "softLimit", "powerLimit"), float("nan"), "soft_w"),
        (("activePower", "softLimit", "enabled"), 1, "soft_enabled"),
        (("activePower", "softLimit"), None, "soft_w"),
        (("activePower", "hardLimit", "powerLimit"), [0], "hard_w"),
        (("activePower", "hardLimit", "enabled"), "false", "hard_enabled"),
        (("activePower", "hardLimit"), 0, "hard_enabled"),
        (("activePower", "mode"), 3, "mode"),
        (("activePower",), [], "mode"),
        (("failSafeModeEnabled",), "true", "fail_safe"),
    ],
)
def test_a_value_of_another_form_is_unknown(path, value, field):
    limits = copy.deepcopy(POWER_LIMITS)
    node = limits["exportLimits"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value

    assert getattr(export_limits(limits), field) is None


def test_no_answer_at_all_has_no_values():
    assert export_limits(None) == ExportLimits()


@pytest.mark.parametrize("status", [403, 404, 500])
def test_an_error_status_is_an_error(device, status):
    device.status = status
    client = DatamanagerClient(HOST, token=minted())

    expected = FroniusWebAuthError if status == 403 else FroniusWebResponseError
    with pytest.raises(expected):
        client.read_export_limits()


def test_a_page_forbidden_before_the_login_keeps_the_login(device):
    """Nothing was signed, so nothing says the password is wrong."""
    device.unsigned_status = 403
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(FroniusWebResponseError):
        client.read_export_limits()

    device.unsigned_status = None
    assert client.read_export_limits().soft_w == 14500


def test_a_device_that_is_off_is_unreachable(monkeypatch):
    def refuse(*_args, **_options):
        raise requests.ConnectionError(f"http://{HOST}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)

    with pytest.raises(FroniusWebUnreachable) as raised:
        DatamanagerClient(HOST, token=minted()).read_export_limits()
    assert HOST not in str(raised.value)


def test_no_error_names_the_device_or_the_login(device):
    device.status = 500
    with pytest.raises(FroniusWebResponseError) as refused:
        DatamanagerClient(HOST, token=minted()).read_export_limits()
    with pytest.raises(FroniusWebAuthError) as wrong:
        DatamanagerClient(HOST, password="wrong").read_export_limits()

    for raised in (refused, wrong):
        assert HOST not in str(raised.value)
        assert "wrong" not in str(raised.value)
        assert minted()["token"] not in str(raised.value)
