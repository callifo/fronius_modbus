"""The test Home Assistant keeps its files out of the repository."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


async def test_home_assistant_writes_its_configuration_outside_the_repository(hass):
    """pytest-homeassistant uses tests/testing_config as the configuration
    folder, and a full run left blueprint folders there in the repository. The
    old plugin used a folder inside its own package."""
    config_dir = await hass.async_add_executor_job(Path(hass.config.config_dir).resolve)

    assert not config_dir.is_relative_to(REPO)
