"""The web interface of a Datamanager: its login and its export limit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from http import HTTPStatus
import os
from typing import Any
from urllib.parse import urlparse

import requests
from requests.auth import AuthBase

from .froniuswebclient import _base_url, _digest_challenge, _http, _is_number
from .web_errors import FroniusWebAuthError, FroniusWebResponseError

# The one login that opens the export limit (Fronius' manual 42,0426,0191).
SERVICE_USER = "service"
EXPORT_LIMIT_PATH = "/config/exportlimit/"
# Seconds to connect and to answer; its Solar API was logged with 3.2 s.
TIMEOUT = (4.0, 10.0)
QOP = "auth"
# Assumed without a name in the challenge (RFC 7616); seen without one.
ALGORITHM = "md5"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


REFUSED = (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)


class StandardDigest(AuthBase):
    """The digest of RFC 7616, answered from a password or the token it left.

    The token is the part of the digest made from user, realm and password, so
    the password itself is never stored.
    """

    def __init__(self, password: str, token: Mapping[str, str] | None) -> None:
        """Hold what the challenge is answered with."""
        self._password = password
        self.token = dict(token) if token else None
        # Set by a challenge this class cannot answer: no refused login.
        self.unknown_challenge = False
        self._nonce = ""
        self._nonce_count = 0

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        """Answer a challenge to this request."""
        self.unknown_challenge = False
        request.register_hook("response", self._answer)
        return request

    def _answer(self, response: requests.Response, **options: Any) -> requests.Response:
        request = response.request
        challenged = response.status_code == HTTPStatus.UNAUTHORIZED
        if not challenged or "Authorization" in request.headers:
            return response
        challenge = _digest_challenge(response.headers.get("WWW-Authenticate", ""))
        if not _is_known(challenge):
            self.unknown_challenge = True
            return response
        token = self._token_for(challenge["realm"])
        if token is None:
            return response
        # Read to its end, so that the connection serves the next request.
        _ = response.content
        response.close()
        answered = request.copy()
        answered.headers["Authorization"] = self._header(answered, challenge, token)
        second = response.connection.send(answered, **options)
        second.history.append(response)
        second.request = answered
        if second.status_code in REFUSED:
            # Not sent again: the device may lock the login after a few tries.
            self._password, self.token = "", None
        else:
            self.token = {"realm": challenge["realm"], "token": token}
        return second

    def _token_for(self, realm: str) -> str | None:
        """What the login for ``realm`` is made from; None where nothing fits it."""
        if self._password:
            return _md5(f"{SERVICE_USER}:{realm}:{self._password}")
        if self.token is None or self.token.get("realm") != realm:
            return None
        return self.token.get("token")

    def _header(
        self, request: requests.PreparedRequest, challenge: dict[str, str], token: str
    ) -> str:
        nonce = challenge["nonce"]
        self._nonce_count = self._nonce_count + 1 if nonce == self._nonce else 1
        self._nonce = nonce
        count = f"{self._nonce_count:08x}"
        client_nonce = os.urandom(8).hex()
        parsed = urlparse(str(request.url))
        uri = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        target = _md5(f"{request.method}:{uri}")
        response = _md5(f"{token}:{nonce}:{count}:{client_nonce}:{QOP}:{target}")
        parts = [
            f'username="{SERVICE_USER}"',
            f'realm="{challenge["realm"]}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{response}"',
            f"qop={QOP}",
            f"nc={count}",
            f'cnonce="{client_nonce}"',
        ]
        if challenge.get("opaque"):
            parts.append(f'opaque="{challenge["opaque"]}"')
        return "Digest " + ", ".join(parts)


def _is_known(challenge: Mapping[str, str]) -> bool:
    """Whether the challenge is one this client can answer."""
    # A key without a value parses to None: unusable like a missing one.
    if not (challenge.get("realm") and challenge.get("nonce")):
        return False
    offered = [part.strip() for part in (challenge.get("qop") or "").split(",")]
    algorithm = challenge.get("algorithm") or ALGORITHM
    return QOP in offered and algorithm.casefold() == ALGORITHM


@dataclass(frozen=True)
class ExportLimits:
    """What the export limit page holds; None where it answers another form."""

    soft_enabled: bool | None = None
    soft_w: float | None = None
    hard_enabled: bool | None = None
    hard_w: float | None = None
    mode: str | None = None
    fail_safe: bool | None = None


def _part(node: Any, key: str) -> dict[str, Any]:
    part = node.get(key) if isinstance(node, dict) else None
    return part if isinstance(part, dict) else {}


def _flag(node: dict[str, Any], key: str) -> bool | None:
    value = node.get(key)
    return value if isinstance(value, bool) else None


def _watts(node: dict[str, Any]) -> float | None:
    value: Any = node.get("powerLimit")
    return float(value) if _is_number(value) else None


def export_limits(power_limits: dict[str, Any] | None) -> ExportLimits:
    """The limits in the page's ``powerLimits``."""
    limits = _part(power_limits, "exportLimits")
    active = _part(limits, "activePower")
    soft, hard = _part(active, "softLimit"), _part(active, "hardLimit")
    mode = active.get("mode")
    return ExportLimits(
        soft_enabled=_flag(soft, "enabled"),
        soft_w=_watts(soft),
        hard_enabled=_flag(hard, "enabled"),
        hard_w=_watts(hard),
        mode=mode if isinstance(mode, str) else None,
        fail_safe=_flag(limits, "failSafeModeEnabled"),
    )


def _power_limits(payload: Any) -> dict[str, Any] | None:
    """The ``powerLimits`` of an answer; None where it is another one."""
    status = _part(_part(payload, "Head"), "Status").get("Code")
    if isinstance(status, bool) or status != 0:
        return None
    limits = _part(_part(payload, "Body"), "Data").get("powerLimits")
    return limits if isinstance(limits, dict) else None


class DatamanagerClient:
    """Reads a Datamanager's export limit with the service login."""

    def __init__(
        self,
        host: str,
        *,
        password: str = "",
        token: Mapping[str, str] | None = None,
    ) -> None:
        """Log in with the password, or with the token it left."""
        self._url = f"{_base_url(host)}{EXPORT_LIMIT_PATH}"
        self._digest = StandardDigest(password, token)

    def _read(self) -> requests.Response:
        response = _http("get", self._url, auth=self._digest, timeout=TIMEOUT)
        status = response.status_code
        # A 403 before anything was signed is the page's answer, not the login's.
        signed = "Authorization" in response.request.headers
        forbidden_login = status == HTTPStatus.FORBIDDEN and signed
        unanswered = (
            status == HTTPStatus.UNAUTHORIZED and not self._digest.unknown_challenge
        )
        if forbidden_login or unanswered:
            raise FroniusWebAuthError("the service login was refused")
        if response.status_code != HTTPStatus.OK:
            raise FroniusWebResponseError(
                f"HTTP {response.status_code} on {EXPORT_LIMIT_PATH}",
                response.status_code,
            )
        return response

    def login(self) -> dict[str, str] | None:
        """The token the login leaves; None when the device refuses it."""
        try:
            self._read()
        except FroniusWebAuthError:
            return None
        return self._digest.token

    def read_export_limits(self) -> ExportLimits:
        """The export limits, as far as the answer has a known form."""
        try:
            payload = self._read().json()
        except ValueError:
            payload = None
        return export_limits(_power_limits(payload))
