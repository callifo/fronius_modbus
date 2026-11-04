"""Pick the pytest-homeassistant-custom-component release to test against.

The test matrix wants "the newest Home Assistant our users can actually be
running". Installing the plugin unpinned does NOT give that: the plugin
publishes a normal, non-prerelease version for every Home Assistant BETA as
well, so `pip install pytest-homeassistant-custom-component` resolves to a
release pinning e.g. homeassistant==2026.8.0b3. pip's own prerelease
exclusion cannot see that - the plugin version is stable, only what it
depends on is not.

The effect is quiet and bad: the job named "current HA" stops covering the
release people run, and a regression against current stable ships green.

So the newest release whose Home Assistant pin is a final version is chosen
explicitly. A literal requirement (the pinned minimum-version entry of the
matrix) is passed through untouched.

Usage:
    python resolve_phcc.py latest-stable-ha  ->  PHCC_SPEC=...==0.13.348
    python resolve_phcc.py 'pkg==0.13.190'   ->  PHCC_SPEC=pkg==0.13.190
"""

from __future__ import annotations

import json
import sys
import urllib.request

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

PACKAGE = "pytest-homeassistant-custom-component"
SENTINEL = "latest-stable-ha"


def is_prerelease(pin: str | None) -> bool:
    """Whether a `homeassistant==...` requirement names a prerelease.

    Anything that is not an exact pin to a final version counts as one: no
    pin, a range, or a dev version - the uncertainty this script
    exists to remove.
    """
    if not pin:
        return True
    try:
        pinned = [
            spec.version for spec in Requirement(pin).specifier if spec.operator == "=="
        ]
        return len(pinned) != 1 or Version(pinned[0]).is_prerelease
    except InvalidRequirement, InvalidVersion:
        return True


def home_assistant_pin(requires_dist) -> str | None:
    """The homeassistant requirement among a release's dependencies."""
    for requirement in requires_dist or []:
        try:
            name = Requirement(requirement).name
        except InvalidRequirement:
            continue
        if name.lower() == "homeassistant":
            return requirement
    return None


def sort_key(version: str) -> Version:
    return Version(version)


def newest_stable(pins: dict[str, str | None]) -> str:
    """Return the highest version in `pins` whose Home Assistant is final."""
    for version in sorted(pins, key=sort_key, reverse=True):
        if not is_prerelease(pins[version]):
            return version
    raise SystemExit(
        "No pytest-homeassistant-custom-component release pins a final Home "
        "Assistant version. Refusing to guess - fix the matrix by hand."
    )


def _fetch(url: str):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def resolve_from_pypi(candidates: int = 20) -> str:
    index = _fetch(f"https://pypi.org/pypi/{PACKAGE}/json")
    published = [version for version, files in index["releases"].items() if files]
    recent = sorted(published, key=sort_key, reverse=True)[:candidates]
    pins = {
        version: home_assistant_pin(
            _fetch(f"https://pypi.org/pypi/{PACKAGE}/{version}/json")["info"].get(
                "requires_dist"
            )
        )
        for version in recent
    }
    return newest_stable(pins)


def main(argv: list[str]) -> int:
    requested = argv[1] if len(argv) > 1 else SENTINEL
    if requested != SENTINEL:
        spec = requested
    else:
        spec = f"{PACKAGE}=={resolve_from_pypi()}"
    # Printed as an environment assignment so the workflow can append it to
    # $GITHUB_ENV in one step. Also echoed to the log, since "which Home
    # Assistant did this run actually test?" is the first question when a
    # matrix job disagrees with a local run.
    print(f"PHCC_SPEC={spec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
