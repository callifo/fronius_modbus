"""Every message the config flow can show has a text, in every language.

A reason or error key without a text renders as the raw key in the dialog.
In the sibling integration this guard comes from, `unknown` sat in every
translation for a year without the flow ever producing it, while a new rule
arrived with none of its texts - two directions of the same drift, and
nothing looked at either.
"""

import ast
import json
import pathlib

import pytest

INTEGRATION = pathlib.Path(__file__).resolve().parents[1] / "custom_components"
FLOW = next(INTEGRATION.glob("*/config_flow.py"))
TRANSLATION_FILES = tuple(
    sorted(
        f"translations/{path.name}" for path in (FLOW.parent / "translations").iterdir()
    )
)


def _flow_messages() -> set[str]:
    """Every abort reason and error key the flow hands to the frontend.

    Read out of the source rather than listed here: a list beside the flow is
    one more place to forget. The shapes it uses are `async_abort(reason=...)`,
    an assignment into the `errors` dict, a returned key (`namespace_error`),
    a `(field, message)` pair in the table of form errors, and
    `AbortFlow("...")` raised from a step.
    """
    messages = set()
    for node in ast.walk(ast.parse(FLOW.read_text(encoding="utf-8"))):
        if (
            (isinstance(node, ast.keyword) and node.arg == "reason")
            or (
                isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Subscript) for target in node.targets)
            )
            or isinstance(node, ast.Return)
        ):
            messages.add(node.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "AbortFlow"
            and node.args
        ):
            messages.add(node.args[0])
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            field, message = node.elts
            if isinstance(field, ast.Constant) and field.value in ("base", "host"):
                messages.add(message)
    return {
        node.value
        for node in messages
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _texts_of(name: str) -> set[str]:
    """Abort and error keys of both the config and the options flow."""
    translation = json.loads((FLOW.parent / name).read_text(encoding="utf-8"))
    return {
        key
        for flow in ("config", "options")
        for kind in ("abort", "error")
        for key in translation.get(flow, {}).get(kind, {})
    }


@pytest.mark.parametrize("name", TRANSLATION_FILES)
def test_every_message_the_flow_shows_has_a_text(name):
    assert _texts_of(name) == _flow_messages(), (
        f"{name} and the flow disagree about which messages exist"
    )


def test_the_reader_finds_the_messages_it_is_pointed_at():
    """Proof-of-red for the reader above."""
    messages = _flow_messages()

    assert "cannot_connect" in messages, "an errors[...] assignment"
    assert "invalid_host" in messages, "a message the flow builds itself"
