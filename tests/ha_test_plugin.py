"""Home Assistant's test helpers, from whichever test plugin is installed.

Home Assistant 2026.11 ships its own plugin, pytest-homeassistant, and
pytest-homeassistant-custom-component publishes nothing for 2026.11 or later.
The new one starts at 2026.11, so the job for the oldest supported Home
Assistant still needs the old one. The two must not be installed together.
"""

import importlib.util

LEGACY_PLUGIN = "pytest_homeassistant_custom_component"
LEGACY_PLUGIN_INSTALLED = importlib.util.find_spec(LEGACY_PLUGIN) is not None

if LEGACY_PLUGIN_INSTALLED:
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_capture_events,
        async_fire_time_changed,
        mock_restore_cache_with_extra_data,
    )
else:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        async_capture_events,
        async_fire_time_changed,
        mock_restore_cache_with_extra_data,
    )

__all__ = [
    "MockConfigEntry",
    "async_capture_events",
    "async_fire_time_changed",
    "mock_restore_cache_with_extra_data",
]
