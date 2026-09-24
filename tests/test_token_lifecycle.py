"""A stored web token is a password equivalent: private on disk, gone once unused.

The Digest token alone authenticates to the inverter, so it may exist only for
a host and role some entry still logs in with, and only readable by Home
Assistant itself (audit A24-03, A24-04).
"""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus import config_flow, token_store
from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.token_store import (
    FroniusTokenStore,
    async_get_token_store,
)

HOST = "inverter.example"
SETTINGS = {"scan_interval": 10, "web_scan_interval": 60}


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


def make_entry(hass, host: str = HOST, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": host, **SETTINGS, **data},
        unique_id=host,
        version=1,
        minor_version=11 if "api_username" in data else 10,
    )
    entry.add_to_hass(hass)
    return entry


async def stored_roles(hass, host: str = HOST) -> set[str]:
    store = async_get_token_store(hass)
    return {
        role
        for role in ("customer", "technician")
        if await store.async_load_token(host, role) is not None
    }


async def save_both_roles(hass, host: str = HOST) -> None:
    store = async_get_token_store(hass)
    await store.async_save_token(host, realm="r", token="c", user="customer")
    await store.async_save_token(host, realm="r", token="t", user="technician")


def test_the_token_file_is_private(hass, monkeypatch):
    """Audit A24-03: the default Store wrote the token world-readable (0644)."""
    created: list[dict] = []

    class RecordingStore[T](token_store.Store[T]):
        def __init__(self, *args, **kwargs) -> None:
            created.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(token_store, "Store", RecordingStore)
    FroniusTokenStore(hass)

    assert created == [{"private": True}]


async def test_a_token_file_written_before_is_rewritten_privately(hass, hass_storage):
    """Only a write applies the private mode, so a file from an older version is rewritten."""
    hass_storage[token_store._TOKEN_STORE_KEY] = {
        "version": token_store._TOKEN_STORE_VERSION,
        "key": token_store._TOKEN_STORE_KEY,
        "data": {f"{HOST}:customer": {"realm": "r", "token": "t"}},
    }
    store = FroniusTokenStore(hass)
    store._store.async_save = AsyncMock(wraps=store._store.async_save)

    assert await store.async_load_token(HOST) == {"realm": "r", "token": "t"}
    await store.async_load_token(HOST)

    store._store.async_save.assert_awaited_once()


async def test_removing_the_entry_deletes_its_token(hass):
    entry = make_entry(hass, api_username="technician")
    await save_both_roles(hass)

    await hass.config_entries.async_remove(entry.entry_id)

    assert await stored_roles(hass) == set()


async def test_switching_the_role_deletes_the_old_roles_token(hass, monkeypatch):
    entry = make_entry(hass, api_username="customer")
    await save_both_roles(hass)
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    settings = config_flow.entry_defaults(entry) | {"api_username": "technician"}

    await config_flow.async_update_entry_from_input(
        hass, entry, settings, previous_host=HOST
    )

    assert await stored_roles(hass) == {"technician"}


async def test_a_token_another_entry_logs_in_with_is_kept(hass):
    removed = make_entry(hass, api_username="customer")
    make_entry(hass, host=f"http://{HOST.upper()}", api_username="customer")
    await save_both_roles(hass)

    await hass.config_entries.async_remove(removed.entry_id)

    assert await stored_roles(hass) == {"customer"}


async def test_an_entry_not_yet_migrated_keeps_both_roles(hass):
    """Below minor 11 the role is read from the stored tokens, so neither may go."""
    removed = make_entry(hass, api_username="customer")
    make_entry(hass, host=f"http://{HOST}")
    await save_both_roles(hass)

    await hass.config_entries.async_remove(removed.entry_id)

    assert await stored_roles(hass) == {"customer", "technician"}
