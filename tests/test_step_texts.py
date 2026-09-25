"""Every flow step explains itself, not the step next door.

The single-role rewrite on 2026-09-06 replaced step descriptions in bulk, and
the Solar API repair has shown the reconfigure text ever since: its owner was
asked to review host and intervals instead of being told about the firmware.
A repair exists to say why the owner is there, so none of its steps may show
the text of the settings form. The settings form is shown without placeholders, so
the steps that render it must not ask for any.
"""

import json
import pathlib

import pytest

TRANSLATIONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "fronius_modbus"
    / "translations"
)
LANGUAGES = ("en", "de")
# Steps rendered by the shared settings form, which passes no placeholders.
SETTINGS_FORM_STEPS = (
    ("config", "user"),
    ("config", "reconfigure"),
    ("options", "init"),
)


def _step_descriptions(language: str) -> dict[tuple[str, ...], str]:
    data = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
    steps: dict[tuple[str, ...], str] = {}
    for section in ("config", "options"):
        for step, body in data[section]["step"].items():
            steps[(section, step)] = body.get("description", "")
    for issue, body in data.get("issues", {}).items():
        for step, step_body in body.get("fix_flow", {}).get("step", {}).items():
            steps[("issues", issue, step)] = step_body.get("description", "")
    return steps


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_repair_step_shows_the_settings_form_text(language):
    """A shared password prompt is fine; the settings text says nothing about the repair."""
    steps = _step_descriptions(language)
    settings_texts = {
        steps[path] for path in SETTINGS_FORM_STEPS if path[0] != "issues"
    }
    borrowed = [
        path
        for path, text in steps.items()
        if path[0] == "issues" and text in settings_texts
    ]
    assert borrowed == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_settings_form_asks_for_no_placeholder(language):
    steps = _step_descriptions(language)
    with_placeholder = [path for path in SETTINGS_FORM_STEPS if "{" in steps[path]]
    assert with_placeholder == []
