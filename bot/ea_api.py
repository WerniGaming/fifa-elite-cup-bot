"""
Inoffizieller Wrapper für die (nicht dokumentierte) EA Pro Clubs Web-API.

Nutzt curl_cffi mit Chrome-Fingerprint + Residential-Proxy (siehe .env: EA_PROXY_URL).
Ohne beides wird der Server-Request von Akamai geblockt.

Platform-Codes:
- "common-gen5" -> PS5 / Xbox Series X|S / PC
- "common-gen4" -> PS4 / Xbox One
- "nx"          -> Nintendo Switch
"""
from __future__ import annotations
import os
from typing import Any, Optional
from curl_cffi.requests import AsyncSession

BASE_URL = "https://proclubs.ea.com/api/fc"
IMPERSONATE = "chrome124"


class EAProClubsAPI:
    def __init__(self, proxy_url: Optional[str] = None):
        self.proxy_url = proxy_url or os.getenv("EA_PROXY_URL")
        self._session: Optional[AsyncSession] = None

    async def __aenter__(self):
        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        self._session = AsyncSession(impersonate=IMPERSONATE, proxies=proxies, timeout=25)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict) -> Any:
        assert self._session is not None
        url = f"{BASE_URL}{path}"
        resp = await self._session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def search_club(self, club_name: str, platform: str = "common-gen5") -> list[dict]:
        data = await self._get(
            "/allTimeLeaderboard/search",
            {"platform": platform, "clubName": club_name},
        )
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
            return []
        return data or []

    async def get_matches(
        self,
        club_id: str,
        platform: str = "common-gen5",
        match_type: str = "friendlyMatch",
        max_results: int = 10,
    ) -> list[dict]:
        data = await self._get(
            "/clubs/matches",
            {
                "platform": platform,
                "clubIds": club_id,
                "matchType": match_type,
                "maxResultCount": max_results,
            },
        )
        return data or []

    async def get_club_info(self, club_id: str, platform: str = "common-gen5") -> dict:
        data = await self._get(
            "/clubs/info",
            {"platform": platform, "clubIds": club_id},
        )
        if isinstance(data, dict):
            return data.get(str(club_id), data)
        return data
