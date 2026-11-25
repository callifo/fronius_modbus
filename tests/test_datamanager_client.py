"""The service login of a Datamanager and what its export limit page answers."""

import copy
import threading

import pytest
import requests

from custom_components.fronius_modbus.datamanager_client import (
    EXPORT_LIMIT_PATH,
    DatamanagerClient,
    ExportLimits,
    export_limits,
)
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlRefused,
    ControlUnavailable,
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
        reference_w=18200.0,
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


# -- writing the soft limit -----------------------------------------------------------

SAVE = ("POST", f"{EXPORT_LIMIT_PATH}?method=save")


def active_power(device: FakeDatamanager) -> dict:
    return device.body["Body"]["Data"]["powerLimits"]["exportLimits"]["activePower"]


def test_a_new_soft_limit_is_the_only_change(device):
    """Its page is written as a whole; a field left out would be lost."""
    active_power(device)["seenNowhere"] = {"kept": [1, 2]}
    before = copy.deepcopy(device.body["Body"]["Data"])
    client = DatamanagerClient(HOST, token=minted())

    limits = client.set_soft_limit(9000)

    before["powerLimits"]["exportLimits"]["activePower"]["softLimit"] = {
        "enabled": True,
        "powerLimit": 9000,
    }
    assert device.written == [before]
    assert limits.soft_w == 9000.0
    assert device.calls.count(SAVE) == 2


def test_setting_the_soft_limit_switches_it_on(device):
    active_power(device)["softLimit"]["enabled"] = False
    client = DatamanagerClient(HOST, token=minted())

    assert client.set_soft_limit(14500).soft_enabled is True
    assert len(device.written) == 1


def test_the_soft_limit_held_already_is_not_written(device):
    client = DatamanagerClient(HOST, token=minted())

    assert client.set_soft_limit(14500).soft_w == 14500.0
    assert device.written == []


@pytest.mark.parametrize("watts", [0, 1, 18200])
def test_the_whole_range_is_written(device, watts):
    client = DatamanagerClient(HOST, token=minted())

    assert client.set_soft_limit(watts).soft_w == watts


@pytest.mark.parametrize(
    ("change", "watts"),
    [
        ({}, -1),
        ({}, 18201),
        ({"hardLimit": {"enabled": True, "powerLimit": 8000}}, 8001),
    ],
)
def test_a_limit_out_of_range_is_refused_before_anything_is_sent(device, change, watts):
    active_power(device).update(change)
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(ControlRefused) as refused:
        client.set_soft_limit(watts)

    assert refused.value.key == "value_out_of_range"
    assert device.written == []


@pytest.mark.parametrize("reference", [0, -5, None, "18200"])
def test_without_a_reference_the_site_ceiling_counts(device, reference):
    limits = device.body["Body"]["Data"]["powerLimits"]
    limits["visualization"]["wattPeakReferenceValue"] = reference
    client = DatamanagerClient(HOST, token=minted())

    assert client.set_soft_limit(30000).soft_w == 30000.0
    with pytest.raises(ControlRefused):
        client.set_soft_limit(1_000_001)


def test_a_limit_at_an_enabled_hard_limit_is_written(device):
    active_power(device)["hardLimit"] = {"enabled": True, "powerLimit": 8000}
    client = DatamanagerClient(HOST, token=minted())

    assert client.set_soft_limit(8000).soft_w == 8000.0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("activePower", "mode"), "perPhase"),
        (("activePower", "mode"), None),
        (("activePower", "softLimit"), None),
        (("activePower", "softLimit", "enabled"), "true"),
        (("activePower", "softLimit", "powerLimit"), "14500"),
        (("activePower", "hardLimit"), []),
        (("activePower", "hardLimit", "enabled"), 1),
        (("activePower", "hardLimit", "powerLimit"), None),
        (("failSafeModeEnabled",), None),
        (("activePower",), None),
    ],
)
def test_a_form_nobody_has_seen_is_not_written(device, path, value):
    node = device.body["Body"]["Data"]["powerLimits"]["exportLimits"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(ControlRefused) as refused:
        client.set_soft_limit(9000)

    assert refused.value.key == "export_limit_unknown_form"
    assert device.written == []


@pytest.mark.parametrize(
    "body", ["<html></html>", {}, answer_body(POWER_LIMITS, code=12)]
)
def test_an_answer_without_the_limits_is_not_written(device, body):
    device.body = body
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(ControlRefused):
        client.set_soft_limit(9000)
    assert device.written == []


@pytest.mark.parametrize(
    "wrong",
    [
        {"takes_writes": False},
        {"write_status": 500},
        {"write_answer": "<html></html>"},
        {"write_answer": {"Head": {"Status": {"Code": 255}}}},
        {"write_answer": {"Head": {"Status": {}}}},
        {"lost_after_write": True},
    ],
)
def test_a_write_nobody_confirmed_is_an_error(device, wrong):
    """The device may hold either value; the owner has to look."""
    for name, value in wrong.items():
        setattr(device, name, value)
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(ControlUnavailable) as unconfirmed:
        client.set_soft_limit(9000)

    assert unconfirmed.value.key == "export_limit_unconfirmed"
    assert len(device.written) == 1


@pytest.mark.parametrize(("field", "shown"), [("powerLimit", 8999), ("enabled", False)])
def test_a_write_not_shown_again_is_an_error(device, field, shown):
    """Taken with an answer, and then shown otherwise."""
    taken = device._write

    def take_and_change(request):
        answer = taken(request)
        active_power(device)["softLimit"][field] = shown
        return answer

    device._write = take_and_change
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(ControlUnavailable):
        client.set_soft_limit(9000)


@pytest.mark.parametrize(("name", "value"), [("status", 500), ("password", "new")])
def test_a_read_that_fails_after_the_write_is_an_error(device, name, value):
    taken = device._write

    def take_and_fail(request):
        answer = taken(request)
        setattr(device, name, value)
        return answer

    device._write = take_and_fail
    client = DatamanagerClient(HOST, token=minted())

    with pytest.raises(ControlUnavailable):
        client.set_soft_limit(9000)


def test_a_device_off_before_the_write_is_unreachable(monkeypatch):
    def refuse(*_args, **_options):
        raise requests.ConnectionError

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)

    with pytest.raises(FroniusWebUnreachable):
        DatamanagerClient(HOST, token=minted()).set_soft_limit(9000)


def test_a_refused_login_writes_nothing(device):
    client = DatamanagerClient(HOST, token={"realm": REALM, "token": "0" * 32})

    with pytest.raises(FroniusWebAuthError):
        client.set_soft_limit(9000)
    assert device.written == []


def test_two_writes_do_not_interleave(device):
    """Each writes the page back as it read it; mixed, one would undo the other."""
    client = DatamanagerClient(HOST, token=minted())
    threads = [
        threading.Thread(target=client.set_soft_limit, args=(watts,))
        for watts in (9000, 8000, 7000)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    asked = [method for method, _path in device.calls]
    once = ["GET", "GET", "POST", "POST", "GET", "GET"]
    assert asked == once * 3


@pytest.mark.parametrize(
    ("change", "reference", "ceiling"),
    [
        ({}, 18200, 18200.0),
        ({"hardLimit": {"enabled": True, "powerLimit": 8000}}, 18200, 8000.0),
        ({"hardLimit": {"enabled": True, "powerLimit": 20000}}, 18200, 18200.0),
        ({}, 0, None),
        ({"hardLimit": {"enabled": True, "powerLimit": 8000}}, 0, 8000.0),
        ({}, None, None),
    ],
)
def test_the_highest_soft_limit(change, reference, ceiling):
    limits = copy.deepcopy(POWER_LIMITS)
    limits["exportLimits"]["activePower"].update(change)
    limits["visualization"]["wattPeakReferenceValue"] = reference

    assert export_limits(limits).ceiling_w == ceiling
