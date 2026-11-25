"""A Datamanager's web interface, as far as the dump of issue 110 shows it."""

import copy
import hashlib
import json
from typing import Any

import requests
from requests.utils import parse_dict_header

HOST = "192.0.2.10"
REALM = "Webinterface area"
PASSWORD = "secret"
CHALLENGE = 'Digest realm="Webinterface area", nonce="{nonce}", qop="auth"'

POWER_LIMITS = {
    "exportLimits": {
        "activePower": {
            "hardLimit": {"enabled": False, "powerLimit": 0},
            "mode": "entireSystem",
            "softLimit": {"enabled": True, "powerLimit": 14500},
        },
        "failSafeModeEnabled": True,
    },
    "visualization": {
        "exportLimits": {
            "activePower": {
                "displayModeHardLimit": "absolute",
                "displayModeSoftLimit": "absolute",
            }
        },
        "wattPeakReferenceValue": 18200,
    },
}


def answer_body(power_limits: Any, code: Any = 0) -> dict[str, Any]:
    """An answer of the page, as its interface reads it."""
    return {
        "Body": {"Data": {"powerLimits": power_limits}},
        "Head": {"Status": {"Code": code, "ErrorDetail": {"Nodes": []}}},
    }


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class FakeDatamanager:
    """Answers the digest as RFC 7616 describes it, for one user and password."""

    def __init__(self) -> None:
        self.challenge = CHALLENGE
        self.body: Any = answer_body(copy.deepcopy(POWER_LIMITS))
        self.status = 200
        self.calls: list[tuple[str, str]] = []
        self.logins: list[dict[str, str]] = []
        self.same_nonce = False
        self._asked = 0

    def _accepts(self, request: requests.PreparedRequest, header: str) -> bool:
        login = parse_dict_header(header.removeprefix("Digest "))
        self.logins.append(login)
        secret = md5(f"service:{REALM}:{PASSWORD}")
        target = md5(f"{request.method}:{request.path_url}")
        signed = ":".join(
            [secret, login["nonce"], login["nc"], login["cnonce"], login["qop"], target]
        )
        return (login["username"], login["uri"], login["response"]) == (
            "service",
            request.path_url,
            md5(signed),
        )

    def send(self, adapter, request, **_options) -> requests.Response:
        self.calls.append((request.method, request.path_url))
        response = requests.Response()
        response.request, response.connection = request, adapter
        response.url, response.encoding = request.url, "utf-8"
        header = request.headers.get("Authorization")
        if header is not None and self._accepts(request, header):
            response.status_code = self.status
            text = self.body if isinstance(self.body, str) else json.dumps(self.body)
            response._content = text.encode()
            return response
        self._asked += 1
        nonce = "1" if self.same_nonce else str(self._asked)
        response.status_code = 401
        response._content = b"<html>401</html>"
        if self.challenge:
            response.headers["WWW-Authenticate"] = self.challenge.format(nonce=nonce)
        return response


def serve_datamanager(monkeypatch) -> FakeDatamanager:
    """Answer every request of the test as this device."""
    fake = FakeDatamanager()

    def _send(adapter, request, **options):
        return fake.send(adapter, request, **options)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _send)
    return fake


def minted() -> dict[str, str]:
    """The token the right password leaves."""
    return {"realm": REALM, "token": md5(f"service:{REALM}:{PASSWORD}")}
