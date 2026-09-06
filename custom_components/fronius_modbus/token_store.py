from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import API_USERNAME, DOMAIN

_TOKEN_STORE_KEY = f"{DOMAIN}_web_api_tokens"
_TOKEN_STORE_VERSION = 1
_TOKEN_STORE_DATA_KEY = "web_api_token_store"


def _token_key(host: str, user: str = API_USERNAME) -> str:
    if "://" not in host:
        host = f"http://{host}"
    return f"{urlparse(host).netloc.lower()}:{user}"


class FroniusTokenStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, dict[str, str]]](
            hass, _TOKEN_STORE_VERSION, _TOKEN_STORE_KEY
        )
        self._cache: dict[str, dict[str, str]] | None = None

    async def _async_load_all(self) -> dict[str, dict[str, str]]:
        if self._cache is None:
            loaded = await self._store.async_load()
            self._cache = loaded if isinstance(loaded, dict) else {}
        return self._cache

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


def async_get_token_store(hass: HomeAssistant) -> FroniusTokenStore:
    domain_data = hass.data.setdefault(DOMAIN, {})
    token_store = domain_data.get(_TOKEN_STORE_DATA_KEY)
    if token_store is None:
        token_store = FroniusTokenStore(hass)
        domain_data[_TOKEN_STORE_DATA_KEY] = token_store
    return token_store
