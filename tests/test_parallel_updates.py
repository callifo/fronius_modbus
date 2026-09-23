"""Every platform declares how many of its actions Home Assistant may run at once.

Quality scale rule parallel-updates. The writers go one at a time: the
inverter answers one Modbus request after the other anyway, and a web write
stalls its Modbus side for seconds. The read-only platform declares 0, because
the coordinator does the polling and the entities never fetch on their own.
"""

import importlib

import pytest

from custom_components.fronius_modbus import PLATFORMS

READING_PLATFORMS = {"sensor"}


@pytest.mark.parametrize("platform", [str(platform) for platform in PLATFORMS])
def test_every_platform_declares_its_parallel_updates(platform):
    module = importlib.import_module(f"custom_components.fronius_modbus.{platform}")
    expected = 0 if platform in READING_PLATFORMS else 1
    assert getattr(module, "PARALLEL_UPDATES", None) == expected
