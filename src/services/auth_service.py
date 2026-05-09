"""
Authentication handler for the local Riot/Valorant API.

Reacts to RSO_LOGIN events and builds an authenticated :class:`RiotSession`
that wraps the local, GLZ, PD, and shared API surfaces. The session
handles automatic token refresh, 429 cooldowns, and 401 retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from http import HTTPStatus
import re
from collections.abc import Generator
from dataclasses import dataclass, field, fields
from typing import Any, Literal
from pathlib import Path
from urllib.parse import urlencode

import httpx
from jwt import decode as jwt_decode, DecodeError  # pyright: ignore[reportUnknownVariableType] # PyJWT
from requests import Response

from services.event_bus import EventBus, Event
from utils.models import (
    AccessTokenJWT,
    AccountAlias,
    AccountXPResponse,
    FriendRequestsResponse,
    FriendResponse,
    EntitlementsTokenResponse,
    UserInfoResponse,
    IngameLoadoutsResponse,
    LeaderboardResponse,
    LockfileData,
    EndpointURI,
    MatchHistoryResponse,
    OwnedItemsResponse,
    PenaltiesResponse,
    PlayerLoadoutResponse,
    PlayerMMRResponse,
    IngameMatchResponse,
    PregameMatchResponse,
    PresenceResponse,
    RegionInfo,
    StorefrontResponse,
    ValorantApiResponse,
    VersionData,
)

from utils.file_utils import get_recent_log_path
from utils.exceptions import (
    AuthenticationError,
    IncorrectPaginationError,
    RegionNotFoundError,
    FallbackApiError,
    VersionNotFoundError,
)


# Cooldown applied after a 429 from a rate-limited Riot endpoint.
# 60s window + 5s safety margin.
_RATE_LIMIT_COOLDOWN_SECONDS: float = 65.0

logger: logging.Logger = logging.getLogger(__name__)

# NOTE: Kept here, not in models.py, because it owns runtime state (httpx
# client, refresh task) that does not belong in a pure data layer.
@dataclass
class RiotSession:
    """Authenticated session to the local + remote Riot/Valorant APIs."""

    lockfile: LockfileData
    client: httpx.AsyncClient = field(init=False)
    region: RegionInfo = field(init=False)
    _base_urls: dict[str, str] = field(init=False)
    _cancelled: asyncio.Event = field(init=False)
    _refresh_lock: asyncio.Lock = field(init=False)
    _refresh_task: asyncio.Task[None] | None = field(init=False, default=None)
    _cooldown_until: float = 0.0

    puuid: str = field(init=False, default="")
    headers: dict[str, str] = field(init=False, default_factory=dict)
    expires_at: float = field(init=False, default=0.0)
    season_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self._cancelled = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._base_urls = {
            "local": self.lockfile.base_url,
        }
        self.client = httpx.AsyncClient(
            headers={"Authorization": self.lockfile.auth_header},
            verify=False,  # Local self-signed cert
            timeout=10.0,
        )
        
    async def fetch(self,
                    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
                    type: Literal["pd", "glz", "shared", "local", "custom"],
                    endpoint: EndpointURI,
                    params: dict[str, str] | None = None,
                    payload: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
                    ) -> httpx.Response:
        """Send an authenticated request to a Riot API endpoint.

        Handles rate-limit cooldowns for pd/glz endpoints automatically.
        If a 429 is received, all subsequent pd/glz requests are blocked
        for 65 seconds before retrying.

        Args:
            method: HTTP method to use.
            type: Target API server. Determines the base URL unless "custom".
            endpoint: The API endpoint path.
            params: Optional query string parameters.
            payload: Optional JSON body for POST/PUT/PATCH requests.

        Raises:
            httpx.HTTPStatusError: If the response status code indicates an error.

        Returns:
            The raw httpx response.
        """
        is_riot_endpoint: bool = type in ("pd", "glz", "shared")

        # Rate-limit: wait out any active cooldown for riot endpoints
        if is_riot_endpoint:
            await self._wait_for_rate_limit()

        url: str = self._create_url(type=type, endpoint=endpoint, params=params)

        # Local endpoints use Basic auth from the lockfile, not the
        # Bearer token that gets set on the client after authentication.
        request_headers = {"Authorization": self.lockfile.auth_header} if type == "local" else None

        response = await self.client.request(
            method=method,
            url=url,
            json=payload,
            headers=request_headers,
        )

        # Rate-limit: if 429, set cooldown and retry once after waiting
        if is_riot_endpoint and response.status_code == 429:
            self._set_rate_limit_cooldown()
            logger.warning(f"Rate-limited on {method} {endpoint.uri}. Waiting {_RATE_LIMIT_COOLDOWN_SECONDS:.0f}s before retry.")
            await self._wait_for_rate_limit()

            response = await self.client.request(
                method=method,
                url=url,
                json=payload,
            )

        # Auth refresh: if 401, refresh entitlements and retry once
        if is_riot_endpoint and response.status_code == 401:
            logger.warning(f"Got 401 on {method} {endpoint.uri}, refreshing entitlements.")
            await self._refresh_entitlements()

            response = await self.client.request(
                method=method,
                url=url,
                json=payload,
            )

        _ = response.raise_for_status()
        return response

    def _set_rate_limit_cooldown(self) -> None:
        """Set the rate-limit cooldown after a 429 from a Riot endpoint."""
        self._cooldown_until = time.monotonic() + _RATE_LIMIT_COOLDOWN_SECONDS

    async def _wait_for_rate_limit(self) -> None:
        """If a rate-limit cooldown is active, sleep until it expires."""
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            logger.info(f"Rate-limit cooldown active, waiting {remaining:.1f}s")
            await self._cancellable_sleep(remaining)

    async def _refresh_entitlements(self, max_retries: int = 10) -> None:
        """Re-fetch entitlements and update session headers.

        Uses a lock so that concurrent 401 responses only trigger one refresh.
        Retries up to max_retries times with increasing delay on failure.
        """
        async with self._refresh_lock:
            for attempt in range(1, max_retries + 1):
                logger.info(f"Refreshing entitlements (attempt {attempt}/{max_retries})...")
                try:
                    response = await self.client.get(
                        f"{self.lockfile.base_url}/entitlements/v1/token",
                        headers={"Authorization": self.lockfile.auth_header},
                    )

                    if response.status_code != 200:
                        logger.warning(
                            f"Entitlements endpoint returned HTTP {response.status_code} (attempt {attempt}/{max_retries}), body: {response.text!r}"
                        )
                        if attempt < max_retries:
                            delay = attempt * 2.0
                            logger.info(f"Retrying entitlements refresh in {delay}s...")
                            await self._cancellable_sleep(delay)
                        continue

                    data: EntitlementsTokenResponse = EntitlementsTokenResponse(**response.json())  # pyright: ignore[reportAny]

                    if data.message is not None:
                        logger.warning(
                            f"Entitlements refresh rejected: {data.message!r} (attempt {attempt}/{max_retries})"
                        )
                        if attempt < max_retries:
                            delay = attempt * 2.0
                            logger.info(f"Retrying entitlements refresh in {delay}s...")
                            await self._cancellable_sleep(delay)
                        continue

                    access_token: str = data.accessToken
                    entitlements_token: str = data.token
                    self.puuid = data.subject

                    decoded = self._decode_access_token(access_token)
                    self.expires_at = float(decoded.exp)

                    logger.debug(
                        f"Refreshed entitlements: subject={data.subject}, issuer={data.issuer}, expires_at={self.expires_at}"
                    )

                    self.headers.update({
                        "Authorization": f"Bearer {access_token}",
                        "X-Riot-Entitlements-JWT": entitlements_token,
                    })
                    self.client.headers.update(self.headers)

                    logger.info("Entitlements refreshed successfully")

                    # Restart the proactive refresh timer
                    self._start_proactive_refresh()
                    return

                except Exception as e:
                    logger.warning(f"Entitlements refresh error (attempt {attempt}/{max_retries}): {e}")
                    if attempt < max_retries:
                        delay = attempt * 2.0
                        logger.info(f"Retrying entitlements refresh in {delay}s...")
                        await self._cancellable_sleep(delay)

            logger.error(f"Entitlements refresh failed after {max_retries} attempts")

    def _start_proactive_refresh(self) -> None:
        """Spawn a background task that refreshes entitlements at 75% of token lifetime."""
        if self._refresh_task and not self._refresh_task.done():
            _ = self._refresh_task.cancel()

        self._refresh_task = asyncio.create_task(self._proactive_refresh_loop())

    async def _proactive_refresh_loop(self) -> None:
        """Sleep until 75% of token lifetime, then refresh. Repeats until cancelled."""
        while not self._cancelled.is_set():
            token_lifetime = self.expires_at - time.time()
            sleep_duration = max(token_lifetime * 0.75, 0)

            if sleep_duration > 0:
                logger.debug(f"Proactive refresh scheduled in {sleep_duration}s")
                await self._cancellable_sleep(sleep_duration)

            if self._cancelled.is_set():
                break

            await self._refresh_entitlements()

    async def authenticate(self) -> None:
        """Fetch entitlements from the local Riot API and build session headers.

        Retries until entitlements are obtained or the session is cancelled
        (e.g. VALORANT_CLOSED). Must be called after construction since __post_init__ is sync.

        Raises:
            AuthenticationError: If the session is cancelled before entitlements are obtained.
        """
        
        # Wait for region data to appear in the log file
        while not self._cancelled.is_set():
            try:
                self.region = self._get_region()
                self._base_urls["pd"] = f"https://pd.{self.region.pd_shard}.a.pvp.net"
                self._base_urls["glz"] = f"https://glz-{self.region.glz_shard}.{self.region.glz_region}.a.pvp.net"
                self._base_urls["shared"] = f"https://shared.{self.region.pd_shard}.a.pvp.net"
                break
            except RegionNotFoundError:
                logger.debug("Region not in logs yet, retrying in 2s")
                await self._cancellable_sleep(2.0)

        if self._cancelled.is_set():
            raise AuthenticationError(message="Authentication cancelled: session was closed")

        entitlements: EntitlementsTokenResponse | None = None
        retry_delay: float = 1.0

        while not self._cancelled.is_set():
            try:
                response = await self.fetch("GET", "local", EndpointURI("/entitlements/v1/token"))
                data: EntitlementsTokenResponse = EntitlementsTokenResponse(**response.json())  # pyright: ignore[reportAny]

                message: str | None = data.message

                if message == "Entitlements token is not ready yet":
                    logger.debug(f"Entitlements not ready yet, retrying in {retry_delay}s")
                    await self._cancellable_sleep(retry_delay)
                    continue

                if message == "Invalid URI format":
                    logger.warning("Invalid URI format from entitlements endpoint, retrying in 5s")
                    await self._cancellable_sleep(5.0)
                    continue

                entitlements = data
                break

            except httpx.ConnectError:
                logger.debug(f"Connection refused, Valorant not ready yet. Retrying in {retry_delay}s")
                await self._cancellable_sleep(retry_delay)
            except httpx.HTTPError as e:
                logger.warning(f"HTTP error fetching entitlements: {e}. Retrying in {retry_delay}s")
                await self._cancellable_sleep(retry_delay)

        if self._cancelled.is_set() or not entitlements:
            raise AuthenticationError(message="Authentication cancelled: session was closed")

        self.puuid = entitlements.subject
        access_token: str = entitlements.accessToken
        entitlements_token: str = entitlements.token

        decoded: AccessTokenJWT = self._decode_access_token(access_token)
        self.expires_at = float(decoded.exp)

        version = self._get_version()

        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Riot-Entitlements-JWT": entitlements_token,
            "X-Riot-ClientPlatform": (
                "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjog"
                "IldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5"
                "MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
            ),
            "X-Riot-ClientVersion": version,
            "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit",
        }

        # Update the httpx client to use the full auth headers for requests
        self.client.headers.update(self.headers)

        # Fetch the current competitive season immediately so it's available for the entire session
        self.season_id = await self.shared_get_season()
        logger.info(f"Authentication complete. PUUID: {self.puuid}, Season: {self.season_id}")

        # Start background token refresh
        self._start_proactive_refresh()

    async def _cancellable_sleep(self, seconds: float) -> None:
        """Sleep that wakes up early if the session is cancelled."""
        try:
            _ = await asyncio.wait_for(self._cancelled.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _decode_access_token(access_token: str) -> AccessTokenJWT:
        """Decode a Riot access token JWT into a typed model.

        Args:
            access_token: The raw JWT string from the entitlements response.

        Returns:
            The decoded and typed AccessTokenJWT instance.
        """
        try:
            decoded_raw: dict[str, Any] = jwt_decode(access_token, options={"verify_signature": False})  # pyright: ignore[reportExplicitAny]
            known_fields: set[str] = {f.name for f in fields(AccessTokenJWT)}
            decoded = AccessTokenJWT(**{k: v for k, v in decoded_raw.items() if k in known_fields})  # pyright: ignore[reportAny]
            logger.debug(f"Access token expires at {decoded.exp} (sub: {decoded.sub})")
            return decoded
        except (DecodeError, TypeError, KeyError) as e:
            logger.warning(f"Could not decode access token JWT ({e}), setting manual expiry (1h)")
            now = int(time.time())
            return AccessTokenJWT(
                sub="unknown",
                iss="unknown",
                exp=now + 3600,
                iat=now,
                jti="unknown",
                cid="unknown",
            )

    async def close(self) -> None:
        self._cancelled.set()
        if self._refresh_task and not self._refresh_task.done():
            _ = self._refresh_task.cancel()
        await self.client.aclose()

    @classmethod
    def _get_region(cls) -> RegionInfo:
        """Gets the user's shard and region

        Raises:
            RegionNotFoundError: When the region and shard is not found in the files

        Returns:
            RegionInfo: A RegionInfo instance
        """

        log_path: Path = get_recent_log_path()

        pd_shard: str | None = None
        glz_shard: str | None = None
        glz_region: str | None = None

        for line in cls._read_lines(log_path):
            if pd_shard is None:
                if match := re.search(r"https://pd\.(\w+)\.a\.pvp\.net/account-xp/v1/", line):
                    pd_shard = match.group(1)

            if glz_shard is None:
                if match := re.search(r"https://glz-(\w[\w-]*)\.(\w+)\.a\.pvp\.net", line):
                    glz_shard = match.group(1)
                    glz_region = match.group(2)

            if pd_shard is not None and glz_shard is not None:
                break

        if pd_shard is None or glz_shard is None or glz_region is None:
            raise RegionNotFoundError(message=f"Could not find region data in log file: {log_path.absolute()}")

        if pd_shard == "pbe":
            return RegionInfo(pd_shard="na", glz_shard="na-1", glz_region="na")

        logger.info(f"Got region from logs: pd={pd_shard}, glz={glz_shard}-{glz_region}")
        return RegionInfo(pd_shard=pd_shard, glz_shard=glz_shard, glz_region=glz_region)

    @staticmethod
    def _read_lines(path: Path) -> Generator[str, None, None]:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                yield line
            
    def _create_url(self,
        type: Literal["pd", "glz", "shared", "local", "custom"],
        endpoint: EndpointURI,
        params: dict[str, str] | None = None) -> str:
        """Creates the final request URL. For custom URL's set type to custom and put the URL as the endpoint"""
        
        base: str = self._base_urls.get(type, "")

        url = f"{base}{endpoint.uri}"

        if params:
            url = f"{url}?{urlencode(params)}"

        return url            

    @classmethod
    def _get_version(cls) -> str:
        """Gets the client version from the Valorant log file with a fallback to valorant-api.com.

        Raises:
            FallbackApiError: When the fallback API request fails or returns unexpected data.
            VersionNotFoundError: When the version could not be resolved from any source.

        Returns:
            The full version string (e.g. "release-12.03-shipping-20-4322591").
        """
        log_path: Path = get_recent_log_path()

        for line in cls._read_lines(log_path):
            if match := re.search(r"CI server version:\s*(.+)", line):
                parts = match.group(1).strip().split("-")
                # Older logs omit "shipping"; newer ones include it. Only insert when missing.
                if "shipping" not in parts:
                    _ = parts.insert(2, "shipping")
                version = "-".join(parts)
                logger.info(f"Got version from logs: {version}")
                return version
        
        import requests

        try:
            fallback_response: Response = requests.get("https://valorant-api.com/v1/version")
            fallback_response.raise_for_status()

            body: dict[str, Any] = fallback_response.json()  # pyright: ignore[reportAny, reportExplicitAny]

            version_data: ValorantApiResponse[VersionData] = ValorantApiResponse(
                data=VersionData(**body["data"]),  # pyright: ignore[reportAny]
                status=HTTPStatus(body["status"]),
            )

            return version_data.data.riotClientVersion

        except requests.HTTPError as e:
            raise FallbackApiError(message=f"Fallback API returned {e.response.status_code}") from e
        except (KeyError, TypeError) as e:
            raise FallbackApiError(message=f"Fallback API returned unexpected data: {e}") from e
        except requests.RequestException as e:
            raise FallbackApiError(message=f"Fallback API request failed: {e}") from e
        except Exception as e:
            raise VersionNotFoundError() from e

    # ------------------- API Wrappers -------------------
    
    # I have come up with the following naming scheme:
    # general_*  => The endpoint is available after user logs in aka RSO_LOGIN event
    # shared_*   => The endpoint hits a shared/global service (e.g. content-service) available after login
    # local_*    => The endpoint is available after VALORANT is officially loaded => VALORANT_OPENED + polling until success
    # pregame_*  => The endpoint is available during the PREGAME sessionLoopState (agent select)
    # ingame_*   => The endpoint is available during the INGAME sessionLoopState (match in progress)

    async def general_get_loadout(self) -> PlayerLoadoutResponse:
        """Fetch the player's current loadout (skins, sprays, identity)."""
        response = await self.fetch("GET", "pd", EndpointURI(f"/personalization/v2/players/{self.puuid}/playerloadout"))
        return PlayerLoadoutResponse(**response.json())  # pyright: ignore[reportAny]

    async def general_get_owned(self, itemTypeId: str | None = None) -> OwnedItemsResponse:
        """Fetch the player's currently owned items (skins, sprays, cards, titles, agents, buddies, skin variants)"""
        path = f"/store/v1/entitlements/{self.puuid}/{itemTypeId}" if itemTypeId else f"/store/v1/entitlements/{self.puuid}"
        response = await self.fetch("GET", "pd", EndpointURI(path))
        return OwnedItemsResponse(**response.json())  # pyright: ignore[reportAny]
    
    async def general_get_xp(self) -> AccountXPResponse:
        """Fetch the player's current XP history"""
        response = await self.fetch("GET", "pd", EndpointURI(f"/account-xp/v1/players/{self.puuid}"))
        return AccountXPResponse(**response.json())  # pyright: ignore[reportAny]

    async def general_get_penalties(self) -> PenaltiesResponse:
        """Fetch the player's current penalties"""
        response = await self.fetch("GET", "pd", EndpointURI("/restrictions/v3/penalties"))
        return PenaltiesResponse(**response.json())  # pyright: ignore[reportAny]
        
    async def general_get_mmr(self) -> PlayerMMRResponse:
        """Fetch the player's current MMR history"""
        response = await self.fetch("GET", "pd", EndpointURI(f"/mmr/v1/players/{self.puuid}"))
        return PlayerMMRResponse(**response.json())  # pyright: ignore[reportAny]

    async def general_get_store(self) -> StorefrontResponse:
        """Fetch the player's current storefront (daily offers, bundles, etc.)."""
        response = await self.fetch("POST", "pd", EndpointURI(f"/store/v3/storefront/{self.puuid}"), payload={})
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        known = {f.name for f in fields(StorefrontResponse)}
        return StorefrontResponse(**{k: v for k, v in data.items() if k in known})  # pyright: ignore[reportAny]

    async def general_get_history(self, puuid: str, start_index: int, end_index: int) -> MatchHistoryResponse:
        """Fetch a page of match history for any player
           Note: The maximum total amount per page is 20

        Args:
            puuid: Target player's UUID (can be self or another player)
            start_index: Inclusive start index (0 = most recent match)
            end_index: Exclusive end index
        """
        
        total: int = end_index - start_index
        if total <= 0:
            raise IncorrectPaginationError(f"The page range may not be negative, received {total}")
        
        if total > 20:
            raise IncorrectPaginationError(f"The total results per page may not exceed 20, received {total}")
        try:
            response = await self.fetch(
                "GET", "pd",
                EndpointURI(f"/match-history/v1/history/{puuid}"),
                params={"startIndex": str(start_index), "endIndex": str(end_index)},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                try:
                    body = e.response.json()  # pyright: ignore[reportAny]
                except (ValueError, httpx.DecodingError):
                    raise  # re-raise the original HTTPStatusError
                if body.get("errorCode") == "MATCH_HISTORY_INVALID_INDICES":  # pyright: ignore[reportAny]
                    raise IncorrectPaginationError("The requested pages do not exist and are out of bounds") from e
            raise
        
        return MatchHistoryResponse(**response.json())  # pyright: ignore[reportAny]

    async def general_get_details(self, match_id: str) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        """Fetch full match details. Returns raw dict (structure too complex to fully type)."""
        response = await self.fetch("GET", "pd", EndpointURI(f"/match-details/v1/matches/{match_id}"))
        return response.json()   # pyright: ignore[reportAny]
    
    async def general_get_leaderboard(self, start_index: int = 0, size: int = 510, query: str | None = None) -> LeaderboardResponse:
        """Fetch the region's competitive leaderboard

        Args:
            season_id (str): The competitive season/act UUID
            start_index (int): Index of the first entry to return. Defaults to 0
            size (int): How many entries per page, 510 is maximum. Defaults to 510
            query (str | None, optional): Optional username filter. Defaults to None."""

        params: dict[str, str] = {"startIndex": str(start_index), "size": str(size)}
        if query is not None:
            params["query"] = query

        response = await self.fetch(
            "GET", "pd",
            EndpointURI(f"/mmr/v1/leaderboards/affinity/{self.region.pd_shard}/queue/competitive/season/{self.season_id or await self.shared_get_season()}"),
            params=params,
        )
        return LeaderboardResponse(**response.json())  # pyright: ignore[reportAny]
    
    # -------------- Shared Endpoints --------------

    async def shared_get_season(self) -> str:
        """Fetch the content service and extract the current competitive season ID.

        Called once during authentication so the season is available for the entire session.

        Returns:
            The active act's season UUID, or empty string if not found.
        """
        try:
            response = await self.fetch(
                "GET", "shared",
                EndpointURI("/content-service/v3/content"),
            )
            data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
            for season in data.get("Seasons", []):  # pyright: ignore[reportAny]
                if season.get("IsActive") and season.get("Type") == "act":  # pyright: ignore[reportAny]
                    season_id: str = season.get("ID", "")  # pyright: ignore[reportAny]
                    logger.debug(f"Current competitive season: {season_id}")
                    return season_id
            logger.warning("No active competitive season found in content response")
        except httpx.HTTPStatusError as e:
            logger.warning(f"Failed to fetch current season: {e.response.status_code} {e.response.text[:200]}")
        except Exception as e:
            logger.warning(f"Failed to fetch current season: {type(e).__name__}: {e}")
        return ""

    # -------------- Local Endpoints --------------
    
    async def local_get_presences(self) -> PresenceResponse:
        """Fetch all known presences"""
        response = await self.fetch("GET", "local", EndpointURI("/chat/v4/presences"))
        return PresenceResponse(**response.json())  # pyright: ignore[reportAny]

    async def local_get_aliases(self) -> list[AccountAlias]:
        """Fetch the player's name/tag alias history."""
        response = await self.fetch("GET", "local", EndpointURI("/player-account/aliases/v1/active"))
        data = response.json()  # pyright: ignore[reportAny]
        known = {f.name for f in fields(AccountAlias)}
        items: list[dict[str, Any]] = data if isinstance(data, list) else [data]  # pyright: ignore[reportExplicitAny, reportUnknownVariableType]
        return [AccountAlias(**{k: v for k, v in alias.items() if k in known}) for alias in items]  # pyright: ignore[reportAny]

    async def local_get_friends(self) -> FriendResponse:
        """Fetch the player's full friend list."""
        response = await self.fetch("GET", "local", EndpointURI("/chat/v4/friends"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        known = {f.name for f in fields(FriendResponse)}
        return FriendResponse(**{k: v for k, v in data.items() if k in known})  # pyright: ignore[reportAny]

    async def local_get_userinfo(self) -> UserInfoResponse:
        """Fetch the authenticated user's account info (game name, tag, country, etc.)."""
        response = await self.fetch("GET", "local", EndpointURI("/rso-auth/v1/authorization/userinfo"))
        return UserInfoResponse(**response.json())  # pyright: ignore[reportAny]

    async def local_get_requests(self) -> FriendRequestsResponse:
        """Fetch the player's pending friend requests (incoming and outgoing)."""
        response = await self.fetch("GET", "local", EndpointURI("/chat/v4/friendrequests"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        known = {f.name for f in fields(FriendRequestsResponse)}
        return FriendRequestsResponse(**{k: v for k, v in data.items() if k in known})  # pyright: ignore[reportAny]

    # -------------- Pregame Endpoints --------------

    async def pregame_get_player(self) -> str:
        """Fetch the current pregame match ID for the authenticated player.

        Returns:
            The pregame match ID string.
        """
        response = await self.fetch("GET", "glz", EndpointURI(f"/pregame/v1/players/{self.puuid}"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        return data.get("MatchID", "")  # pyright: ignore[reportAny]

    async def pregame_get_match(self, match_id: str) -> PregameMatchResponse:
        """Fetch full pregame match data (agent select lobby).

        Args:
            match_id: The pregame match UUID from pregame_get_player.
        """
        response = await self.fetch("GET", "glz", EndpointURI(f"/pregame/v1/matches/{match_id}"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        known = {f.name for f in fields(PregameMatchResponse)}
        return PregameMatchResponse(**{k: v for k, v in data.items() if k in known})  # pyright: ignore [reportAny]

    # -------------- Ingame Endpoints --------------

    async def ingame_get_player(self) -> str:
        """Fetch the current ingame match ID for the authenticated player.

        Returns:
            The ingame match ID string.
        """
        response = await self.fetch("GET", "glz", EndpointURI(f"/core-game/v1/players/{self.puuid}"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        return data.get("MatchID", "")  # pyright: ignore[reportAny]

    async def ingame_get_match(self, match_id: str) -> IngameMatchResponse:
        """Fetch full ingame match data (active match).

        Args:
            match_id: The ingame match UUID from ingame_get_player.
        """
        response = await self.fetch("GET", "glz", EndpointURI(f"/core-game/v1/matches/{match_id}"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        known = {f.name for f in fields(IngameMatchResponse)}
        return IngameMatchResponse(**{k: v for k, v in data.items() if k in known})  # pyright: ignore[reportAny]

    async def ingame_get_loadouts(self, match_id: str) -> IngameLoadoutsResponse:
        """Fetch all player loadouts for an active match.

        Args:
            match_id: The ingame match UUID.
        """
        response = await self.fetch("GET", "glz", EndpointURI(f"/core-game/v1/matches/{match_id}/loadouts"))
        data: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        known = {f.name for f in fields(IngameLoadoutsResponse)}
        return IngameLoadoutsResponse(**{k: v for k, v in data.items() if k in known})  # pyright: ignore[reportAny]


class AuthHandler:
    """Manages the auth lifecycle via the event bus.

    Reacts to:
    - RSO_LOGIN    -> build a :class:`RiotSession` and emit AUTH_SUCCESS
    - RSO_LOGOUT   -> tear down the session
    - SHUTDOWN     -> tear down the session
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus: EventBus = bus
        self.session: RiotSession | None = None
        self._register()

    def _register(self) -> None:
        _ = self.bus.on(Event.RSO_LOGIN, self._on_rso_login, priority=10)
        _ = self.bus.on(Event.RSO_LOGOUT, self._on_rso_logout, priority=10)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------
    # NOTE: every handler must accept a `data` argument so the bus can
    # invoke them uniformly with any payload (or None).

    async def _on_rso_login(self, data: LockfileData) -> None:
        """Riot Client logged in -> authenticate."""
        logger.info(f"Authenticating against {data.base_url} ...")

        try:
            self.session = RiotSession(lockfile=data)
            await self.session.authenticate()
            logger.info("Auth successful")
            _ = await self.bus.emit(Event.AUTH_SUCCESS, {"session": self.session})

        except Exception as e:
            logger.error(f"Auth failed: {e}")
            await self._cleanup_session()
            _ = await self.bus.emit(Event.AUTH_FAILED, AuthenticationError(str(e)))

    async def _on_rso_logout(self, data: Any = None) -> None:  # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        logger.info("RSO logout — cleaning up session")
        await self._cleanup_session()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        await self._cleanup_session()

    async def _cleanup_session(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("Session closed")