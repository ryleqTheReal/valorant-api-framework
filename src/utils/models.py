"""
Shared data models for the Valorant Stats App.

Dataclasses used across multiple modules live here.
Internal dataclasses (e.g. Listener in the EventBus)
stay in their own module.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from enum import Enum
from typing import Any, Literal, TypeVar

from urllib.parse import urlparse, ParseResult
from utils.exceptions import EndpointValidationError, StructureValidationError

T = TypeVar("T")

@dataclass
class LockfileData:
    """
    Parsed data from the Riot Client lockfile.

    The lockfile contains a single line in the format:
        name:pid:port:password:protocol
        e.g.: riot:12345:52735:abcdefg123:https
    """

    name: str
    pid: int
    port: int
    password: str
    protocol: str

    @classmethod
    def from_file(cls, path: Path) -> LockfileData:
        """Read and parse the lockfile"""

        content = path.read_text(encoding="utf-8").strip()
        parts = content.split(":")

        if len(parts) != 5:
            raise StructureValidationError(
                f"Unexpected lockfile format (expected 5 parts, received {len(parts)}): {content}"
            )

        return cls(
            name=parts[0],
            pid=int(parts[1]),
            port=int(parts[2]),
            password=parts[3],
            protocol=parts[4],
        )

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def wss_url(self) -> str:
        return f"wss://127.0.0.1:{self.port}"
    
    @property
    def auth_header(self) -> str:
        """Basic auth header for the local Riot API."""
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return f"Basic {token}"


@dataclass
class AppConfig:
    """Application config loaded from config.json."""

    server_base_url: str | None = None
    poll_interval: int = 3
    collect_interval: int = 60
    enable_data_sending: bool = True
    ratelimit_timeout: int = 60

    @classmethod
    def from_config_dict(cls, data: dict[str, object]) -> AppConfig:
        """Create an AppConfig from a config dictionary obtained by config.json

        Args:
            data (dict[str, object]): The parsed config.json dict

        Returns:
            AppConfig: Returns the AppConfig object
        """

        # The defined fields of the dataclass: server_base_url, poll_interval ...
        known_fields = cls.__dataclass_fields__
        kwargs = {k: v for k, v in data.items() if k in known_fields}
        return cls(**kwargs)  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True)
class RegionInfo:
    """Region data extracted from Valorant log files.

    Attributes:
        pd_shard:  Platform-data shard  (e.g. "eu", "na", "ap", "kr")
        glz_shard: GLZ shard            (e.g. "eu-1", "na-1")
        glz_region: GLZ region          (e.g. "eu", "na")
    """

    pd_shard: str
    glz_shard: str
    glz_region: str


@dataclass(frozen=True)
class EndpointURI:
    uri: str
    parsed_endpoint: ParseResult

    def __init__(self, endpoint: str) -> None:
        parsed = urlparse(endpoint)

        if not parsed.path.startswith("/"):
            raise EndpointValidationError(
                message=f"Endpoint must start with '/': '{endpoint}'"
            )
        if parsed.scheme or parsed.netloc:
            raise EndpointValidationError(
                message=f"Endpoint must be a path, not a full URL: '{endpoint}'"
            )

        # Not using self.uri because that would raise Error due to frozen=True immutability !
        object.__setattr__(self, "uri", endpoint)
        object.__setattr__(self, "parsed_endpoint", parsed)


# ------------ API Response dataclasses ------------


@dataclass
class ValorantApiResponse[T]:
    """Generic wrapper for valorant-api.com responses.

    Attributes:
        data: The parsed response payload, typed per endpoint.
        status: The HTTP status code of the response.
        error: Optional error message if the request failed.
    """

    data: T
    status: HTTPStatus
    error: str | None = None

@dataclass
class VersionData:
    """Response obtained by https://valorant-api.com/v1/version"""
    
    manifestId: str
    branch: str
    version: str
    buildVersion: str
    engineVersion: str
    riotClientVersion: str
    riotClientBuild: str
    buildDate: str 
                
# ------------ Riot API Responses ------------

@dataclass
class EntitlementsTokenResponse:
    """Response obtained by https://127.0.0.1:{port}/entitlements/v1/token"""
    accessToken: str
    entitlements: tuple[Any]  # pyright: ignore[reportExplicitAny]
    issuer: str
    # Player UUID
    subject: str
    # Used as the entitlement in requests
    token: str
    message: Literal["Entitlements token is not ready yet", "Invalid URI format"] | str | None = None


@dataclass
class _JWTPartialPayload:
    """Nested object inside the JWT payload (e.g. "pp", "dat", "plt")."""
    c: str = ""

@dataclass
class _JWTPlatform:
    """Platform info from the JWT payload."""
    dev: str = ""
    id: str = ""

@dataclass
class _JWTDat:
    """Session data from the JWT payload."""
    c: str = ""
    lid: str = ""

@dataclass
class AccessTokenJWT:
    """Decoded payload of the Riot access token JWT.

    Constructed via: AccessTokenJWT(**decoded_payload)
    """
    sub: str
    iss: str
    exp: int
    iat: int
    jti: str
    cid: str
    cty: str = ""
    lcty: str = ""
    acr: str = ""
    prm: str = ""
    pp: _JWTPartialPayload | dict[str, str] | None = None
    scp: list[str] | None = None
    clm: list[str] | None = None
    amr: list[str] | None = None
    dat: _JWTDat | dict[str, str] | None = None
    plt: _JWTPlatform | dict[str, str] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.pp, dict):
            self.pp = _JWTPartialPayload(**self.pp)
        if isinstance(self.dat, dict):
            self.dat = _JWTDat(**self.dat)
        if isinstance(self.plt, dict):
            self.plt = _JWTPlatform(**self.plt)

@dataclass
class WebsocketEventWrapper[T]:
    """Wrapper for a WAMP v1.0 EVENT message: [8, topic_uri, event_payload].

    Attributes:
        opcode: WAMP message type (8 = EVENT).
        topic:  The subscription URI that triggered this event.
        data:   The event payload, typed per consumer.
    """

    opcode: int
    topic: str
    data: T

    @classmethod
    def from_raw(cls, raw: list[object]) -> WebsocketEventWrapper[T]:
        """Parse a raw WAMP EVENT array into a typed wrapper.

        Raises:
            ValueError: If the array is too short or the opcode is not 8.
        """
        if len(raw) < 3:
            raise ValueError(f"Expected at least 3 elements, got {len(raw)}")

        opcode = raw[0]
        if opcode != 8:
            raise ValueError(f"Expected WAMP EVENT opcode 8, got {opcode}")

        return cls(opcode=int(opcode), topic=str(raw[1]), data=raw[2])  # pyright: ignore[reportArgumentType]


# ------------ Presence Models ------------


@dataclass
class _PremierPresenceData:
    rosterId: str = ""
    rosterName: str = ""
    rosterTag: str = ""
    rosterType: str = ""
    division: int = 0
    score: int = 0
    plating: int = 0
    showAura: bool = False
    showTag: bool = False
    showPlating: bool = False


@dataclass
class _MatchPresenceData:
    sessionLoopState: str = ""
    provisioningFlow: str = ""
    matchMap: str = ""
    queueId: str = ""


@dataclass
class _PartyPresenceData:
    partyId: str = ""
    isPartyOwner: bool = False
    partyState: str = ""
    partyAccessibility: str = ""
    partyLFM: bool = False
    partyClientVersion: str = ""
    partyVersion: int = 0
    partySize: int = 0
    queueEntryTime: str = ""
    isPartyCrossPlayEnabled: bool = False
    isPlayerCrossPlayEnabled: bool = False
    partyPrecisePlatformTypes: int = 0
    customGameName: str = ""
    customGameTeam: str = ""
    maxPartySize: int = 5
    tournamentId: str = ""
    rosterId: str = ""
    partyOwnerSessionLoopState: str = ""
    partyOwnerMatchMap: str = ""
    partyOwnerProvisioningFlow: str = ""
    partyOwnerMatchScoreAllyTeam: int = 0
    partyOwnerMatchScoreEnemyTeam: int = 0
    activityId: str = ""


@dataclass
class _PlayerPresenceData:
    playerCardId: str = ""
    playerTitleId: str = ""
    accountLevel: int = 0
    competitiveTier: int = 0
    leaderboardPosition: int = 0


@dataclass
class PresencePrivate:
    """Decoded base64 JSON from the 'private' field of a presence object."""

    isValid: bool = False
    isIdle: bool = False
    queueId: str = ""
    provisioningFlow: str = ""
    partyId: str = ""
    partySize: int = 0
    maxPartySize: int = 5
    partyOwnerMatchScoreAllyTeam: int = 0
    partyOwnerMatchScoreEnemyTeam: int = 0
    premierPresenceData: _PremierPresenceData | dict[str, object] | None = None
    matchPresenceData: _MatchPresenceData | dict[str, object] | None = None
    partyPresenceData: _PartyPresenceData | dict[str, object] | None = None
    playerPresenceData: _PlayerPresenceData | dict[str, object] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.premierPresenceData, dict):
            self.premierPresenceData = _PremierPresenceData(**self.premierPresenceData)  # pyright: ignore[reportArgumentType]
        if isinstance(self.matchPresenceData, dict):
            self.matchPresenceData = _MatchPresenceData(**self.matchPresenceData)  # pyright: ignore[reportArgumentType]
        if isinstance(self.partyPresenceData, dict):
            self.partyPresenceData = _PartyPresenceData(**self.partyPresenceData)  # pyright: ignore[reportArgumentType]
        if isinstance(self.playerPresenceData, dict):
            self.playerPresenceData = _PlayerPresenceData(**self.playerPresenceData)  # pyright: ignore[reportArgumentType]

    @classmethod
    def from_base64(cls, encoded: str) -> PresencePrivate:
        """Decode a base64-encoded JSON string into a PresencePrivate."""
        decoded: dict[str, object] = json.loads(base64.b64decode(encoded).decode())  # pyright: ignore[reportAny]
        return cls(**decoded)  # pyright: ignore[reportArgumentType]


@dataclass
class Presence:
    """A single presence entry from /chat/v4/presences."""

    activePlatform: str | None = None
    actor: str | None = None
    basic: str = ""
    details: str | None = None
    game_name: str = ""
    game_tag: str = ""
    location: str | None = None
    msg: str | None = None
    name: str = ""
    packedData: object | None = None
    parties: list[object] | None = None
    patchline: str | None = None
    pid: str = ""
    platform: str | None = None
    private: PresencePrivate | str | None = None
    privateJwt: str | None = None
    product: str = ""
    puuid: str = ""
    region: str = ""
    resource: str = ""
    state: str = ""
    summary: str = ""
    time: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.private, str) and self.private:
            decoded = json.loads(base64.b64decode(self.private).decode())  # pyright: ignore[reportAny]
            self.private = PresencePrivate(**decoded)  # pyright: ignore[reportAny]


@dataclass
class PresenceResponse:
    """Parsed presence payload from a /chat/v4/presences event."""

    presences: list[Presence] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.presences and isinstance(self.presences[0], dict):
            self.presences = [Presence(**p) for p in self.presences]  # pyright: ignore[reportCallIssue]

    @classmethod
    def from_json(cls, data: dict[str, object]) -> PresenceResponse:
        """Create a PresenceResponse from an API JSON dict."""
        return cls(**data)  # pyright: ignore[reportArgumentType]


@dataclass
class WebsocketEventEnvelope[T]:
    """Intermediate envelope wrapping the actual event payload.

    The WAMP data[2] payload contains this structure:
        {"data": {...}, "eventType": "Update", "uri": "/chat/v4/presences"}
    """

    data: T
    eventType: str = ""
    uri: str = ""


@dataclass
class PresenceWebsocketEvent(WebsocketEventWrapper[WebsocketEventEnvelope[PresenceResponse]]):
    """A fully parsed WAMP presence event.

    Structure: WAMP wrapper -> event envelope -> PresenceResponse -> list[Presence]
    """

    @classmethod
    def from_raw_string(cls, raw: str) -> PresenceWebsocketEvent:
        """Parse a raw websocket message string into a fully typed presence event."""
        parsed: list[object] = json.loads(raw)  # pyright: ignore[reportAny]
        wrapper = WebsocketEventWrapper.from_raw(parsed)  # pyright: ignore[reportUnknownVariableType]
        payload: dict[str, object] = wrapper.data  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        presence_response = PresenceResponse(**payload.get("data", {}))  # pyright: ignore[reportCallIssue, reportUnknownMemberType]
        envelope = WebsocketEventEnvelope(
            data=presence_response,
            eventType=str(payload.get("eventType", "")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            uri=str(payload.get("uri", "")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        )
        return cls(opcode=wrapper.opcode, topic=wrapper.topic, data=envelope)


# ------------ Game State Models ------------


class SessionLoopState(Enum):
    """Relevant session loop states from presence data."""

    MENUS = "MENUS"
    PREGAME = "PREGAME"
    INGAME = "INGAME"


@dataclass(frozen=True)
class GameStateTransition:
    """Payload emitted with GAME_STATE_CHANGED events.

    Attributes:
        previous: The previous loop state (None on first detection).
        current:  The new loop state.
        puuid:    The player's UUID.
        presence: The full presence object that triggered the transition.
    """

    previous: SessionLoopState | None
    current: SessionLoopState
    puuid: str
    presence: Presence


# if __name__ == "__main__":
#     from dataclasses import asdict

#     with open(r"C:\Users\legit\Desktop\valorant-watcher\example_event.json", "r") as f:
#         raw = json.loads(f.read())

#     pp = lambda label, obj: print(f"\n{'=' * 40}\n{label}\n{'=' * 40}\n{json.dumps(asdict(obj), indent=2)}")

#     # 1. Full event
#     full_event = PresenceWebsocketEvent.from_raw_string(json.dumps(raw))
#     pp("PresenceWebsocketEvent", full_event)

#     # 2. WebsocketEventWrapper (raw WAMP message)
#     wrapper = WebsocketEventWrapper.from_raw(raw)
#     pp("WebsocketEventWrapper", wrapper)

#     # 3. WebsocketEventEnvelope
#     envelope_data: dict[str, object] = wrapper.data  
#     envelope = WebsocketEventEnvelope(
#         data=envelope_data.get("data", {}),
#         eventType=str(envelope_data.get("eventType", "")),
#         uri=str(envelope_data.get("uri", "")),
#     )
#     pp("WebsocketEventEnvelope", envelope)

#     # 4. PresenceResponse
#     presence_response = PresenceResponse(**envelope.data)
#     pp("PresenceResponse", presence_response)

#     # 5. Single Presence
#     presence = presence_response.presences[0]  
#     pp("Presence", presence)

#     # 6. PresencePrivate
#     pp("PresencePrivate", presence.private) 

