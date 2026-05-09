"""
Authentication handler for the local Riot/Valorant API.

Reacts to VALORANT process events and builds an authenticated session
that can be used to query match data, stats, etc.
"""

from __future__ import annotations

import asyncio
import logging
from multiprocessing import AuthenticationError
import time
from http import HTTPStatus
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Literal
from pathlib import Path
from urllib.parse import urlencode

import httpx
from jwt import decode as jwt_decode, DecodeError  # pyright: ignore[reportUnknownVariableType] # PyJWT
from requests import Response

from services.event_bus import EventBus, Event
from utils.models import AccessTokenJWT, EntitlementsTokenResponse, LockfileData, EndpointURI, RegionInfo, ValorantApiResponse, VersionData
from utils.file_utils import get_recent_log_path
from utils.exceptions import RegionNotFoundError, FallbackApiError, VersionNotFoundError

logger: logging.Logger = logging.getLogger(__name__)

#NOTE: Do not move this into models.py, this is the main instance
@dataclass
class RiotSession:
    """Authenticated session to the local Riot API."""

    lockfile: LockfileData
    ratelimit_timeout: int = 60
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
                    type: Literal["pd", "glz", "local", "custom"],
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
        is_riot_endpoint: bool = type in ("pd", "glz")

        # Rate-limit: wait out any active cooldown for riot endpoints
        if is_riot_endpoint:
            await self._wait_for_rate_limit()

        url: str = self._create_url(type=type, endpoint=endpoint, params=params)

        response = await self.client.request(
            method=method,
            url=url,
            json=payload,
        )

        # Rate-limit: if 429, set cooldown and retry once after waiting
        if is_riot_endpoint and response.status_code == 429:
            self._set_rate_limit_cooldown()
            logger.warning(f"Rate-limited on {method} {endpoint.uri}. Waiting 65s before retry.")
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
        """Set the rate-limit cooldown to 65 seconds from now (60s + 5s safety margin)."""
        self._cooldown_until = time.monotonic() + 65.0

    async def _wait_for_rate_limit(self) -> None:
        """If a rate-limit cooldown is active, sleep until it expires."""
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            logger.info(f"Rate-limit active, waiting {remaining}s remaining")
            await self._cancellable_sleep(remaining)

    async def _refresh_entitlements(self) -> None:
        """Re-fetch entitlements and update session headers.

        Uses a lock so that concurrent 401 responses only trigger one refresh.
        """
        async with self._refresh_lock:
            logger.info("Refreshing entitlements...")
            try:
                response = await self.client.get(
                    f"{self.lockfile.base_url}/entitlements/v1/token"
                )
                data: EntitlementsTokenResponse = EntitlementsTokenResponse(**response.json())  # pyright: ignore[reportAny]

                if data.message is not None:
                    logger.error(f"Entitlements refresh failed: {data.message}")
                    return

                access_token: str = data.accessToken
                entitlements_token: str = data.token
                self.puuid = data.subject

                decoded = self._decode_access_token(access_token)
                self.expires_at = float(decoded.exp)

                self.headers.update({
                    "Authorization": f"Bearer {access_token}",
                    "X-Riot-Entitlements-JWT": entitlements_token,
                })
                self.client.headers.update(self.headers)

                logger.info("Entitlements refreshed successfully")

                # Restart the proactive refresh timer
                self._start_proactive_refresh()

            except Exception as e:
                logger.error(f"Failed to refresh entitlements: {e}")

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
                break
            except RegionNotFoundError:
                logger.debug("Region not in logs yet, retrying in 2s")
                await self._cancellable_sleep(2.0)

        if self._cancelled.is_set():
            from utils.exceptions import AuthenticationError
            raise AuthenticationError(message="Authentication cancelled: Valorant was closed")

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
            from utils.exceptions import AuthenticationError
            raise AuthenticationError(message="Authentication cancelled: Valorant was closed")

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
        logger.info(f"Authentication complete. PUUID: {self.puuid}")

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
            decoded = AccessTokenJWT(**decoded_raw)  # pyright: ignore[reportAny]
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
        type: Literal["pd", "glz", "local", "custom"],
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

class AuthHandler:
    """
    Manages the auth lifecycle based on event bus events.

    Registers itself with the bus and reacts to:
    - VALORANT_OPENED   -> build session
    - VALORANT_CLOSED -> tear down session
    - SHUTDOWN            -> cleanup
    """

    def __init__(self, bus: EventBus, ratelimit_timeout: int = 60) -> None:
        self.bus: EventBus = bus
        self.ratelimit_timeout: int = ratelimit_timeout
        self.session: RiotSession | None = None
        self._register()

    def _register(self) -> None:
        """Subscribe to relevant events."""
        _ = self.bus.on(Event.VALORANT_OPENED, self._on_valorant_open, priority=10)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=10)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers: ------------------- 
    # IMPORTANT: Make sure that each handdler funtion allows for a data parameter even if it's not used IMPORTANT

    async def _on_valorant_open(self, data: LockfileData) -> None:
        """Valorant started -> authenticate"""

        logger.info(f"Authenticating against {data.base_url} ...")

        try:
            self.session = RiotSession(lockfile=data, ratelimit_timeout=self.ratelimit_timeout)
            await self.session.authenticate()

            logger.info("Auth successful")
            _ = await self.bus.emit(Event.AUTH_SUCCESS, {"session": self.session})

        except Exception as e:
            logger.error(f"Auth failed: {e}")
            await self._cleanup_session()
            _ = await self.bus.emit(Event.AUTH_FAILED, AuthenticationError(str(e)))

    async def _on_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        """Valorant closed -> tear down session"""
        logger.info("Valorant closed - cleaning up session")
        await self._cleanup_session()

    async def _on_shutdown(self, data: Any = None) -> None: # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        """App is shutting down """
        await self._cleanup_session()

    async def _cleanup_session(self, data: Any = None) -> None: # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        """Cleans up the session"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("Session closed")
    
if __name__ == "__main__":
    print(RiotSession._get_version())  # pyright: ignore[reportPrivateUsage]