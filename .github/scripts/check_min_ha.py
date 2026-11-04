"""Fail unless the minimum job is running the Home Assistant hacs.json declares.

Three places say which release is the oldest supported one: hacs.json, the
plugin release the matrix pins, and the job's own name. A test can compare the
first and the last offline, and does. The pin is the one that decides what
actually gets installed - and only PyPI knows which Home Assistant a given
plugin release ships, so nothing offline can check it. A pin raised alone, or
never raised at all, therefore left the job announcing "minimum HA <x>" while
testing something else, green the whole way.

Here the version is installed and can simply be read. Run inside the minimum
matrix job, after the install step.
"""

import json
import pathlib
import sys

from homeassistant.const import __version__

REPO = pathlib.Path(__file__).resolve().parents[2]


def feature_release(version: str) -> tuple[str, str]:
    """The (year, month) part - hacs.json names a patch, any patch of the same
    feature release is the same minimum."""
    parts = version.split(".")
    return parts[0], parts[1]


def main() -> int:
    declared = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))[
        "homeassistant"
    ]
    if feature_release(__version__) != feature_release(declared):
        print(
            f"hacs.json declares {declared} as the minimum supported Home "
            f"Assistant, but this job installed {__version__}. The pinned "
            "pytest-homeassistant-custom-component release is what decides "
            "that - raise it and hacs.json together, never one alone.",
            file=sys.stderr,
        )
        return 1
    print(f"minimum job runs Home Assistant {__version__}, as hacs.json declares")
    return 0


if __name__ == "__main__":
    sys.exit(main())
