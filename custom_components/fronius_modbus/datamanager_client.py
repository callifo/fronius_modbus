"""The web interface of a Datamanager: its login and its export limit."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass
import hashlib
from http import HTTPStatus
import os
import threading
from typing import Any
from urllib.parse import urlparse

import requests
from requests.auth import AuthBase

from .const import SITE_POWER_CEILING_W
from .fronius_modbus_api.exceptions import ControlRefused, ControlUnavailable
from .froniuswebclient import _base_url, _digest_challenge, _http, _is_number
from .web_errors import (
    FroniusWebAuthError,
    FroniusWebResponseError,
    FroniusWebUnreachable,
)

# The one login that opens the export limit (Fronius' manual 42,0426,0191).
SERVICE_USER = "service"
EXPORT_LIMIT_PATH = "/config/exportlimit/"
SAVE_QUERY = "?method=save"
MODE_ENTIRE_SYSTEM = "entireSystem"
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
    reference_w: float | None = None

    @property
    def ceiling_w(self) -> float | None:
        """The highest soft limit: the site's power or an enabled hard limit."""
        bounds = []
        # 0 where the owner entered none.
        if self.reference_w is not None and self.reference_w > 0:
            bounds.append(self.reference_w)
        if self.hard_enabled and self.hard_w is not None:
            bounds.append(self.hard_w)
        return min(bounds, default=None)

    @property
    def is_writable(self) -> bool:
        """Whether the page has the one form a write is known for."""
        seen = (
            self.soft_enabled,
            self.soft_w,
            self.hard_enabled,
            self.hard_w,
            self.fail_safe,
        )
        return self.mode == MODE_ENTIRE_SYSTEM and None not in seen


def _part(node: Any, key: str) -> dict[str, Any]:
    part = node.get(key) if isinstance(node, dict) else None
    return part if isinstance(part, dict) else {}


def _json(response: requests.Response) -> Any:
    """The body; None where it is no JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _flag(node: dict[str, Any], key: str) -> bool | None:
    value = node.get(key)
    return value if isinstance(value, bool) else None


def _watts(node: dict[str, Any], key: str = "powerLimit") -> float | None:
    value: Any = node.get(key)
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
        reference_w=_watts(
            _part(power_limits, "visualization"), "wattPeakReferenceValue"
        ),
    )


def _reports_success(payload: Any) -> bool:
    status = _part(_part(payload, "Head"), "Status").get("Code")
    return not isinstance(status, bool) and status == 0


def _power_limits(payload: Any) -> dict[str, Any] | None:
    """The ``powerLimits`` of an answer; None where it is another one."""
    if not _reports_success(payload):
        return None
    limits = _part(_part(payload, "Body"), "Data").get("powerLimits")
    return limits if isinstance(limits, dict) else None


def _require_writable(limits: ExportLimits, watts: int) -> None:
    """Refuse a form nobody has seen, and a value the device would not take."""
    if not limits.is_writable:
        raise ControlRefused(
            "export_limit_unknown_form",
            "The export limit page has a form this integration does not know",
        )
    ceiling = limits.ceiling_w
    highest = SITE_POWER_CEILING_W if ceiling is None else ceiling
    if not 0 <= watts <= highest:
        raise ControlRefused(
            "value_out_of_range",
            f"The soft limit must be between 0 and {highest:g} W",
            minimum="0",
            maximum=f"{highest:g}",
        )


def _unconfirmed() -> ControlUnavailable:
    return ControlUnavailable(
        "export_limit_unconfirmed",
        "The new export limit was not confirmed; the device may hold another value",
    )


class DatamanagerClient:
    """Reads and writes a Datamanager's export limit with the service login."""

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
        # A write reads, writes and reads again; nothing may come in between.
        self._one_at_a_time = threading.RLock()

    def _request(
        self, method: str, query: str = "", payload: Any = None
    ) -> requests.Response:
        response = _http(
            method,
            f"{self._url}{query}",
            auth=self._digest,
            json=payload,
            timeout=TIMEOUT,
        )
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
            self._request("get")
        except FroniusWebAuthError:
            return None
        return self._digest.token

    def read_export_limits(self) -> ExportLimits:
        """The export limits, as far as the answer has a known form."""
        return export_limits(self._read())

    def _read(self) -> dict[str, Any] | None:
        with self._one_at_a_time:
            return _power_limits(_json(self._request("get")))

    def set_soft_limit(self, watts: int) -> ExportLimits:
        """Set the soft limit and switch it on; what the device shows after it.

        Refused before anything is sent unless the page has the form the write
        is known for. Nobody has seen it on a device: the page is written back
        as a whole, and read again before it counts as written.
        """
        with self._one_at_a_time:
            power_limits = self._read()
            limits = export_limits(power_limits)
            _require_writable(limits, watts)
            if limits.soft_enabled and limits.soft_w == watts:
                return limits
            changed = copy.deepcopy(power_limits) or {}
            soft = changed["exportLimits"]["activePower"]["softLimit"]
            soft["enabled"], soft["powerLimit"] = True, watts
            try:
                answer = self._request("post", SAVE_QUERY, {"powerLimits": changed})
            except (FroniusWebUnreachable, FroniusWebResponseError) as err:
                raise _unconfirmed() from err
            try:
                shown = self.read_export_limits()
            except (
                FroniusWebAuthError,
                FroniusWebUnreachable,
                FroniusWebResponseError,
            ) as err:
                raise _unconfirmed() from err
            taken = shown.soft_enabled is True and shown.soft_w == watts
            if not (_reports_success(_json(answer)) and taken):
                raise _unconfirmed()
            return shown
