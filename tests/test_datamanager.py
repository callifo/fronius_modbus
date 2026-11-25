"""Telling a Datamanager from a GEN24 by an answer that needs no login."""

import json
from typing import Any

import pytest
import requests

from custom_components.fronius_modbus import datamanager
from custom_components.fronius_modbus.datamanager import DeviceFamily, identify
from custom_components.fronius_modbus.web_errors import FroniusWebUnreachable

HOST = "192.0.2.10"
HTML = "<html><title>404 Not Found</title></html>"


def solar_api(body: Any, code: int = 0) -> str:
    """A Solar API answer as a Fronius device wraps it."""
    head = {"RequestArguments": {}, "Status": {"Code": code, "Reason": ""}}
    return json.dumps({"Body": body, "Head": head})


def logger_info(product: Any) -> str:
    """A GetLoggerInfo answer, shaped like the one of issue 110."""
    return solar_api(
        {
            "LoggerInfo": {
                "HWVersion": "2.6D",
                "PlatformID": "wilma",
                "ProductID": product,
                "SWVersion": "3.34.1-5",
            }
        }
    )


@pytest.fixture
def asked() -> list[tuple[str, str, dict[str, Any]]]:
    """What was asked over HTTP."""
    return []


def answer(monkeypatch, asked: list, text: str, status: int = 200) -> None:
    """Let every request come back with this status and body."""

    def _answer(method: str, url: str, **options: Any) -> requests.Response:
        asked.append((method, url, options))
        response = requests.Response()
        response.status_code = status
        response.encoding = "utf-8"
        response._content = text.encode("utf-8")
        return response

    monkeypatch.setattr(datamanager, "_http", _answer)


@pytest.mark.parametrize(
    ("text", "status", "family"),
    [
        (logger_info("fronius-datamanager-card"), 200, DeviceFamily.DATAMANAGER),
        (logger_info("fronius-datamanager-box"), 200, DeviceFamily.DATAMANAGER),
        # A GEN24 refuses the request in the body, with HTTP 200.
        (solar_api({"Data": {}}, code=11), 200, DeviceFamily.OTHER),
        (logger_info("something-else"), 200, DeviceFamily.OTHER),
        (logger_info(7), 200, DeviceFamily.OTHER),
        (solar_api({"LoggerInfo": ["card"]}), 200, DeviceFamily.OTHER),
        (solar_api(None), 200, DeviceFamily.OTHER),
        (logger_info("fronius-datamanager-card"), 404, DeviceFamily.UNKNOWN),
        (logger_info("fronius-datamanager-card"), 503, DeviceFamily.UNKNOWN),
        (HTML, 404, DeviceFamily.UNKNOWN),
        (HTML, 200, DeviceFamily.UNKNOWN),
        ("[]", 200, DeviceFamily.UNKNOWN),
        ("{}", 200, DeviceFamily.UNKNOWN),
        ("", 200, DeviceFamily.UNKNOWN),
        (json.dumps({"Body": {}, "Head": "no"}), 200, DeviceFamily.UNKNOWN),
    ],
)
def test_the_family_an_answer_shows(monkeypatch, asked, text, status, family):
    answer(monkeypatch, asked, text, status)

    assert identify(HOST) is family


def test_a_device_that_does_not_answer_stays_unknown(monkeypatch):
    def _unreachable(method: str, url: str, **options: Any) -> requests.Response:
        raise FroniusWebUnreachable("ConnectTimeout")

    monkeypatch.setattr(datamanager, "_http", _unreachable)

    assert identify(HOST) is DeviceFamily.UNKNOWN


def test_the_question_is_one_request_without_a_login(monkeypatch, asked):
    answer(monkeypatch, asked, logger_info("fronius-datamanager-card"))

    identify(HOST, timeout=2.0)

    assert asked == [
        ("get", f"http://{HOST}/solar_api/v1/GetLoggerInfo.cgi", {"timeout": 2.0})
    ]
