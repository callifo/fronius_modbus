"""Everything the manifest asks for is installed for the tests as well.

Home Assistant installs an integration's dependencies from `manifest.json`
and from nothing else. `requirements.txt` repeats those lines so the modules
import during test collection. On adoption the two disagreed in four of six
entries (a lower pymodbus bound, a matplotlib pin the code no longer uses,
no httpx, no pygal), which is what an unheld copy looks like after a year.

A subset, not an equal set: the test environment also installs what Home
Assistant's own modbus integration brings along (tmodbus, pymodbus), pinned
to the versions core 2026.9 ships. Those pins must not be in the manifest -
a custom component that pins them differently from core cannot be installed
beside it.

Dependabot is what makes that drift expensive. It understands requirements
files and not Home Assistant manifests, so a security update raises the bound
in the txt, opens a green pull request, and leaves the version people actually
install exactly where it was.
"""

import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = REPO / "requirements.txt"


def _requirements_of(text: str) -> set[str]:
    """The requirement lines of a pip requirements file.

    Comments, blank lines and `-r` includes are not requirements. Versions
    stay verbatim: the bound is the entire subject here.
    """
    lines = (line.strip() for line in text.splitlines())
    return {line for line in lines if line and not line.startswith(("#", "-"))}


def _manifest() -> dict:
    """The integration's manifest, found rather than named, so a renamed
    domain cannot make this go blind."""
    manifests = sorted((REPO / "custom_components").glob("*/manifest.json"))
    assert len(manifests) == 1, (
        f"expected exactly one integration manifest, found {manifests} - "
        "this guard compares against the manifest and cannot pick."
    )
    return json.loads(manifests[0].read_text(encoding="utf-8"))


def test_every_manifest_requirement_is_installed_for_the_tests():
    """A bound raised in only one of them is a fix nobody receives."""
    installed_by_home_assistant = set(_manifest()["requirements"])
    installed_for_the_tests = _requirements_of(
        RUNTIME_REQUIREMENTS.read_text(encoding="utf-8")
    )

    assert installed_by_home_assistant <= installed_for_the_tests, (
        f"requirements.txt is missing "
        f"{installed_by_home_assistant - installed_for_the_tests}. Home "
        "Assistant installs from the manifest and reads nothing else, so a "
        "version changed in only one place is never tested. Change both."
    )


def test_the_manifest_pins_nothing_the_core_modbus_integration_owns():
    """`modbus-connection`, `tmodbus` and `pymodbus` belong to the modbus
    integration this one depends on. An `==` pin on any of them in a custom
    component collides with core's own pin and the integration cannot be
    installed at all."""
    core_owned = ("modbus-connection", "tmodbus", "pymodbus")
    pinned = [
        requirement
        for requirement in _manifest()["requirements"]
        if "==" in requirement and requirement.split("[")[0].split("=")[0] in core_owned
    ]

    assert not pinned, (
        f"{pinned} pinned in the manifest; core pins the same packages and "
        "pip cannot satisfy both. Use a lower bound."
    )


def test_the_reader_keeps_the_bound_a_security_bump_would_raise():
    """The shape it exists for: the same requirement before and after the
    digits Dependabot would raise in one file and not the other."""
    listed = _requirements_of(
        "# a note about why\n\npymodbus>=3.10.0\n-r requirements.txt\n"
    )

    assert listed == {"pymodbus>=3.10.0"}
    assert _requirements_of("pymodbus>=3.10.3\n") != listed
