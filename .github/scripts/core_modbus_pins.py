"""The Modbus libraries Home Assistant's own modbus integration pins.

This integration shares core's Modbus connection, so an installation runs the
modbus-connection, tmodbus and pymodbus releases core pins, whatever newer one
PyPI offers. A test run against a newer one tests a library no user has: the
minimum job ran modbus-connection 4.12 where Home Assistant 2026.9 pins 4.10
(audit F29).

    python core_modbus_pins.py          prints the pins, for pip install
    python core_modbus_pins.py --check  fails unless they are what is installed
"""

import importlib.metadata
import json
import pathlib
import sys

from packaging.requirements import Requirement

import homeassistant.components.modbus as core_modbus


def pins() -> list[str]:
    """The requirements of core's modbus manifest, as it writes them."""
    manifest = pathlib.Path(core_modbus.__file__).with_name("manifest.json")
    return json.loads(manifest.read_text(encoding="utf-8"))["requirements"]


def mismatches(requirements: list[str]) -> list[str]:
    """Every pinned library whose installed release the pin does not allow."""
    wrong = []
    for text in requirements:
        requirement = Requirement(text)
        installed = importlib.metadata.version(requirement.name)
        if not requirement.specifier.contains(installed, prereleases=True):
            wrong.append(f"{requirement.name} {installed} installed, core pins {text}")
    return wrong


def main(argv: list[str]) -> int:
    requirements = pins()
    if "--check" not in argv:
        print(" ".join(requirements))
        return 0
    wrong = mismatches(requirements)
    for line in wrong:
        print(line, file=sys.stderr)
    if wrong:
        return 1
    print("the Modbus libraries are the ones core pins: " + ", ".join(requirements))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
