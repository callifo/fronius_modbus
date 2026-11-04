"""Every platform declares how many of its actions Home Assistant may run at once.

Quality scale rule parallel-updates. The writers go one at a time: the
inverter answers one Modbus request after the other anyway, and a web write
stalls its Modbus side for seconds. The read-only platforms declare 0, because
the coordinator does the polling and the entities never fetch on their own.

The numbers declare 0 too: every write path behind them holds its own lock,
and a burst of values for one web control has to reach that lock together to
be cut to its first and last value. Home Assistant's semaphore let the next
value in only after the previous one was written, so every step became a write.
"""

import importlib

import pytest

from custom_components.fronius_modbus import PLATFORMS

# Platforms Home Assistant must not serialise: readers, and writers that
# serialise themselves.
UNLIMITED_PLATFORMS = {"sensor", "event", "number"}


@pytest.mark.parametrize("platform", [str(platform) for platform in PLATFORMS])
def test_every_platform_declares_its_parallel_updates(platform):
    module = importlib.import_module(f"custom_components.fronius_modbus.{platform}")
    expected = 0 if platform in UNLIMITED_PLATFORMS else 1
    assert getattr(module, "PARALLEL_UPDATES", None) == expected
