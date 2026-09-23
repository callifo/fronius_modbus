"""Every error a user can see has a translation in every language.

Quality scale exception-translations. The library raises ControlRefused and
ControlUnavailable with a key; the entity layer hands that key to Home
Assistant, which looks the message up. A key without a text shows the user
the raw key, so the scan runs over every raise in the package.
"""

import ast
import json
import pathlib
import re

import pytest

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "fronius_modbus"
)
TRANSLATED_ERRORS = {"ControlRefused", "ControlUnavailable"}
HOME_ASSISTANT_ARGUMENTS = {
    "translation_domain",
    "translation_key",
    "translation_placeholders",
}
LANGUAGES = ("en", "de")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _key_node(node: ast.Call) -> ast.expr | None:
    """The translation key of an error; entity descriptions carry one too and are skipped."""
    name = _call_name(node) or ""
    if name in TRANSLATED_ERRORS and node.args:
        return node.args[0]
    if not name.endswith("Error"):
        return None
    for keyword in node.keywords:
        if keyword.arg == "translation_key":
            return keyword.value
    return None


def _placeholders(node: ast.Call) -> set[str]:
    """Placeholders a raise supplies: keyword arguments, or a literal placeholder dict."""
    supplied = {
        keyword.arg
        for keyword in node.keywords
        if keyword.arg and keyword.arg not in HOME_ASSISTANT_ARGUMENTS
    }
    for keyword in node.keywords:
        if keyword.arg == "translation_placeholders" and isinstance(
            keyword.value, ast.Dict
        ):
            supplied |= {
                key.value for key in keyword.value.keys if isinstance(key, ast.Constant)
            }
    return supplied


def _raised_keys() -> dict[str, set[str]]:
    """Key -> placeholder names, for every translated error the package raises."""
    keys: dict[str, set[str]] = {}
    for source in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            key = _key_node(node)
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.setdefault(key.value, set()).update(_placeholders(node))
    return keys


def _messages(language: str) -> dict[str, str]:
    data = json.loads(
        (PACKAGE / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )
    return {key: value["message"] for key, value in data.get("exceptions", {}).items()}


def test_the_scan_finds_the_errors_it_was_written_for():
    assert {
        "charge_limit_not_in_mode",
        "web_api_not_configured",
        "write_failed",
    } <= set(_raised_keys())


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_raised_key_has_a_message(language):
    assert sorted(set(_raised_keys()) - set(_messages(language))) == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_message_is_left_without_a_raise(language):
    assert sorted(set(_messages(language)) - set(_raised_keys())) == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_message_uses_only_the_placeholders_the_raise_supplies(language):
    messages = _messages(language)
    for key, supplied in _raised_keys().items():
        used = set(PLACEHOLDER.findall(messages.get(key, "")))
        assert used <= supplied, (language, key, used - supplied)
