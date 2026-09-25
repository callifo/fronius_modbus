from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import API_USERNAME, API_USERNAMES, CONF_API_USERNAME, DOMAIN

_TOKEN_STORE_KEY = f"{DOMAIN}_web_api_tokens"
_TOKEN_STORE_VERSION = 1
_TOKEN_STORE_DATA_KEY = "web_api_token_store"


def canonical_host(host: str) -> str:
    """The host as an identity: what the stored credential is keyed by.

    Two spellings of one host are one host. Comparing the raw strings deleted
    the live token when only the case changed (audit A06).
    """
    if "://" not in host:
        host = f"http://{host}"
    return urlparse(host).netloc.lower()


def _token_key(host: str, user: str = API_USERNAME) -> str:
    return f"{canonical_host(host)}:{user}"


class FroniusTokenStore:
    def __init__(self, hass: HomeAssistant) -> None:
        # The token alone authenticates to the inverter (audit A24-03).
        self._store = Store[dict[str, dict[str, str]]](
            hass, _TOKEN_STORE_VERSION, _TOKEN_STORE_KEY, private=True
        )
        self._cache: dict[str, dict[str, str]] | None = None

    async def _async_load_all(self) -> dict[str, dict[str, str]]:
        if self._cache is None:
            loaded = await self._store.async_load()
            self._cache = loaded if isinstance(loaded, dict) else {}
            if self._cache:
                # Only a write applies the private mode; a file from an older
                # version stays world-readable until then.
                await self._store.async_save(self._cache)
        return self._cache

    async def async_ready(self) -> None:
        """Load the tokens, so that moving them afterwards needs no await."""
        await self._async_load_all()

    @callback
    def move_tokens(self, old_host: str, new_host: str) -> None:
        """Carry every role's token of a moved host over, in one step.

        No await: the caller checks and updates the entry around this, and an
        await in between let another flow take the address, or a second move
        leave a token under a host no entry uses (audit R3B-01, R3B-02).
        """
        data = self._cache
        if data is None:
            raise RuntimeError("The token store is moved before it was loaded")
        moved = False
        for role in API_USERNAMES:
            token = data.pop(_token_key(old_host, role), None)
            if token is not None:
                data[_token_key(new_host, role)] = token
                moved = True
        if moved:
            self._store.async_delay_save(lambda: data)

    async def async_load_token(
        self, host: str, user: str = API_USERNAME
    ) -> dict[str, str] | None:
        data = await self._async_load_all()
        token = data.get(_token_key(host, user))
        if not isinstance(token, dict):
            return None
        realm = token.get("realm")
        secret = token.get("token")
        if not isinstance(realm, str) or not isinstance(secret, str):
            return None
        return {"realm": realm, "token": secret}

    async def async_save_token(
        self,
        host: str,
        realm: str,
        token: str,
        user: str = API_USERNAME,
    ) -> None:
        data = await self._async_load_all()
        data[_token_key(host, user)] = {"realm": realm, "token": token}
        await self._store.async_save(data)

    async def async_delete_token(self, host: str, user: str = API_USERNAME) -> None:
        data = await self._async_load_all()
        if data.pop(_token_key(host, user), None) is not None:
            await self._store.async_save(data)


async def async_forget_unused_tokens(hass: HomeAssistant, host: str) -> None:
    """Delete the tokens for ``host`` that no config entry logs in with any more.

    Called whenever an entry leaves a host or a role (audit A24-04). An entry
    without a role predates single roles: its migration reads the role from
    the stored tokens, so both stay.
    """
    in_use: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        values = {**entry.data, **entry.options}
        if canonical_host(str(values.get(CONF_HOST, ""))) != canonical_host(host):
            continue
        role = values.get(CONF_API_USERNAME)
        in_use |= set(API_USERNAMES) if role is None else {role}
    token_store = async_get_token_store(hass)
    for role in set(API_USERNAMES) - in_use:
        await token_store.async_delete_token(host, role)


def async_get_token_store(hass: HomeAssistant) -> FroniusTokenStore:
    domain_data = hass.data.setdefault(DOMAIN, {})
    token_store = domain_data.get(_TOKEN_STORE_DATA_KEY)
    if token_store is None:
        token_store = FroniusTokenStore(hass)
        domain_data[_TOKEN_STORE_DATA_KEY] = token_store
    return token_store
