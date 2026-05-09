"""
Shared data models for the Valorant Stats App.

Dataclasses used across multiple modules live here.
Internal dataclasses (e.g. Listener in the EventBus)
stay in their own module.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, fields
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

    poll_interval: int = 1
    rate_limit_per_minute: int = 30

    @classmethod
    def from_config_dict(cls, data: dict[str, object]) -> AppConfig:
        """Create an AppConfig from a parsed config.json dict, ignoring unknown keys."""
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
class _UserInfoPassword:
    """Password metadata from the userInfo payload."""
    cng_at: int | None = None       # Timestamp of last password change
    reset: bool | None = None       # Whether the password was reset
    must_reset: bool | None = None  # Whether a password reset is required


@dataclass
class _UserInfoBan:
    """Ban/restriction info from the userInfo payload."""
    restrictions: list[object] | None = None  # Active account restrictions


@dataclass
class _UserInfoAccount:
    """Account details from the userInfo payload.

    Note: the JSON key ``"type"`` is mapped to ``account_type`` to avoid
    shadowing the built-in :func:`type`.
    """
    account_type: int | None = None   # Account type identifier (JSON key: "type")
    state: str | None = None          # Account state, e.g. "ENABLED"
    adm: bool | None = None           # Whether the account has admin privileges
    game_name: str | None = None      # In-game display name
    tag_line: str | None = None       # Riot ID tag (e.g. "Judge")
    created_at: int | None = None     # Account creation timestamp (epoch ms)


@dataclass
class UserInfo:
    """Parsed inner JSON from the userInfo field.

    The raw API returns userInfo as a JSON *string*; this dataclass
    represents the parsed contents.  All fields are optional as the
    schema is speculative (based on a single sample).
    """
    sub: str | None = None                                      # Player UUID
    country: str | None = None                                  # ISO country code (e.g. "deu")
    email_verified: bool | None = None                          # Whether the email is verified
    player_plocale: str | None = None                           # Player platform locale
    country_at: int | None = None                               # Timestamp when country was set (epoch ms)
    pw: _UserInfoPassword | dict[str, object] | None = None     # Password metadata
    phone_number_verified: bool | None = None                   # Whether the phone number is verified
    preferred_username: str | None = None                       # Login username
    ban: _UserInfoBan | dict[str, object] | None = None         # Ban / restriction info
    ppid: str | None = None                                     # Partner player ID
    lol_region: list[str] | None = None                         # Linked League of Legends regions
    player_locale: str | None = None                            # Player locale (e.g. "de")
    pvpnet_account_id: str | None = None                        # Legacy PvP.net account ID
    lol: str | None = None                                      # Legacy League of Legends data
    original_platform_id: str | None = None                     # Original platform identifier
    original_account_id: str | None = None                      # Original account identifier
    acct: _UserInfoAccount | dict[str, object] | None = None    # Riot account details
    jti: str | None = None                                      # JWT token identifier
    username: str | None = None                                 # Riot account username

    def __post_init__(self) -> None:
        if isinstance(self.pw, dict):
            known = {f.name for f in fields(_UserInfoPassword)}
            self.pw = _UserInfoPassword(**{k: v for k, v in self.pw.items() if k in known})  # pyright: ignore[reportArgumentType]
        if isinstance(self.ban, dict):
            known = {f.name for f in fields(_UserInfoBan)}
            self.ban = _UserInfoBan(**{k: v for k, v in self.ban.items() if k in known})  # pyright: ignore[reportArgumentType]
        if isinstance(self.acct, dict):
            # Remap "type" -> "account_type" to avoid shadowing the builtin
            remapped = {("account_type" if k == "type" else k): v for k, v in self.acct.items()}
            known = {f.name for f in fields(_UserInfoAccount)}
            self.acct = _UserInfoAccount(**{k: v for k, v in remapped.items() if k in known})  # pyright: ignore[reportArgumentType]


@dataclass
class UserInfoResponse:
    """Response from GET /rso-auth/v1/authorization/userinfo.

    The API returns ``{"userInfo": "<json-string>"}`` - the ``userInfo``
    value is a JSON-encoded string, not a nested object.  ``__post_init__``
    parses it into a :class:`UserInfo` instance.
    """
    userInfo: UserInfo | str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.userInfo, str):
            parsed: dict[str, object] = json.loads(self.userInfo)  # pyright: ignore[reportAny]
            known = {f.name for f in fields(UserInfo)}
            self.userInfo = UserInfo(**{k: v for k, v in parsed.items() if k in known})  # pyright: ignore[reportArgumentType]


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
    r: str = ""

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
            known = {f.name for f in fields(_JWTPartialPayload)}
            self.pp = _JWTPartialPayload(**{k: v for k, v in self.pp.items() if k in known})
        if isinstance(self.dat, dict):
            known = {f.name for f in fields(_JWTDat)}
            self.dat = _JWTDat(**{k: v for k, v in self.dat.items() if k in known})
        if isinstance(self.plt, dict):
            known = {f.name for f in fields(_JWTPlatform)}
            self.plt = _JWTPlatform(**{k: v for k, v in self.plt.items() if k in known})

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
    rosterId: str | None = None
    rosterName: str | None = None
    rosterTag: str | None = None
    rosterType: str | None = None
    division: int | None = None
    score: int | None = None
    plating: int | None = None
    showAura: bool | None = None
    showTag: bool | None = None
    showPlating: bool | None = None


@dataclass
class _MatchPresenceData:
    sessionLoopState: str | None = None
    provisioningFlow: str | None = None
    matchMap: str | None = None
    queueId: str | None = None


@dataclass
class _PartyPresenceData:
    partyId: str | None = None
    isPartyOwner: bool | None = None
    partyState: str | None = None
    partyAccessibility: str | None = None
    partyLFM: bool | None = None
    partyClientVersion: str | None = None
    partyVersion: int | None = None
    partySize: int | None = None
    queueEntryTime: str | None = None
    isPartyCrossPlayEnabled: bool | None = None
    isPlayerCrossPlayEnabled: bool | None = None
    partyPrecisePlatformTypes: int | None = None
    customGameName: str | None = None
    customGameTeam: str | None = None
    maxPartySize: int | None = None
    tournamentId: str | None = None
    rosterId: str | None = None
    partyOwnerSessionLoopState: str | None = None
    partyOwnerMatchMap: str | None = None
    partyOwnerProvisioningFlow: str | None = None
    partyOwnerMatchScoreAllyTeam: int | None = None
    partyOwnerMatchScoreEnemyTeam: int | None = None
    activityId: str | None = None


@dataclass
class _PlayerPresenceData:
    playerCardId: str | None = None
    playerTitleId: str | None = None
    accountLevel: int | None = None
    competitiveTier: int | None = None
    leaderboardPosition: int | None = None


@dataclass
class PresencePrivate:
    """Decoded base64 JSON from the 'private' field of a presence object."""

    isValid: bool | None = None
    isIdle: bool | None = None
    queueId: str | None = None
    provisioningFlow: str | None = None
    partyId: str | None = None
    partySize: int | None = None
    maxPartySize: int | None = None
    partyOwnerMatchScoreAllyTeam: int | None = None
    partyOwnerMatchScoreEnemyTeam: int | None = None
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
    basic: str | None = None
    details: str | None = None
    game_name: str | None = None
    game_tag: str | None = None
    location: str | None = None
    msg: str | None = None
    name: str | None = None
    packedData: object | None = None
    parties: list[object] | None = None
    patchline: str | None = None
    pid: str | None = None
    platform: str | None = None
    private: PresencePrivate | str | None = None
    privateJwt: str | None = None
    product: Literal["valorant", "league_of_legends"] | str | None = None
    puuid: str | None = None
    region: str | None = None
    resource: str | None = None
    state: str | None = None
    summary: str | None = None
    time: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.private, str) and self.private:
            if self.product != "valorant":
                self.private = None
                return
            padded = self.private + "=" * (-len(self.private) % 4)
            decoded: dict[str, object] = json.loads(base64.b64decode(padded).decode())  # pyright: ignore[reportAny]
            self.private = PresencePrivate(**decoded)  # pyright: ignore[reportArgumentType]


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


# ------------ Friend Request Models ------------

@dataclass
class FriendRequest:
    """A single pending friend request (incoming or outgoing)."""

    game_name: str | None = None
    game_tag: str | None = None
    name: str | None = None
    note: str | None = None
    pid: str | None = None
    platform: str | None = None
    puuid: str | None = None
    region: str | None = None
    subscription: str | None = None  # "pending_in" or "pending_out"


@dataclass
class FriendRequestsResponse:
    """Response from GET /chat/v4/friendrequests."""

    requests: list[FriendRequest] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.requests and isinstance(self.requests[0], dict):
            known = {f.name for f in fields(FriendRequest)}
            dicts: list[dict[str, object]] = self.requests  # pyright: ignore[reportAssignmentType]
            self.requests = [
                FriendRequest(**{k: v for k, v in r.items() if k in known})  # pyright: ignore[reportArgumentType]
                for r in dicts
            ]


@dataclass
class FriendRequestWebsocketEvent(WebsocketEventWrapper[WebsocketEventEnvelope[FriendRequestsResponse]]):
    """A fully parsed WAMP friend request event.

    Structure: WAMP wrapper -> event envelope -> FriendRequestsResponse -> list[FriendRequest]
    """

    @classmethod
    def from_raw_wamp(cls, wrapper: WebsocketEventWrapper[object]) -> FriendRequestWebsocketEvent:
        """Parse from a pre-parsed WAMP wrapper."""
        payload: dict[str, object] = wrapper.data  # pyright: ignore[reportAssignmentType]
        response = FriendRequestsResponse(**payload.get("data", {}))  # pyright: ignore[reportCallIssue]
        envelope = WebsocketEventEnvelope(
            data=response,
            eventType=str(payload.get("eventType", "")),
            uri=str(payload.get("uri", "")),
        )
        return cls(opcode=wrapper.opcode, topic=wrapper.topic, data=envelope)


# ------------ Friend Models ------------

@dataclass
class Friend:
    """A single friend entry from /chat/v4/friends."""

    activePlatform: str | None = None
    displayGroup: str | None = None
    game_name: str | None = None
    game_tag: str | None = None
    group: str | None = None
    last_online_ts: int | None = None
    name: str | None = None
    note: str | None = None
    pid: str | None = None
    puuid: str | None = None
    region: str | None = None


@dataclass
class FriendResponse:
    """Parsed payload from a /chat/v4/friends event."""

    friends: list[Friend] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.friends and isinstance(self.friends[0], dict):
            self.friends = [Friend(**f) for f in self.friends]  # pyright: ignore[reportCallIssue]


@dataclass
class FriendWebsocketEvent(WebsocketEventWrapper[WebsocketEventEnvelope[FriendResponse]]):
    """A fully parsed WAMP friend event.

    Structure: WAMP wrapper -> event envelope -> FriendResponse -> list[Friend]
    """

    @classmethod
    def from_raw_wamp(cls, wrapper: WebsocketEventWrapper[object]) -> FriendWebsocketEvent:
        """Parse from a pre-parsed WAMP wrapper."""
        payload: dict[str, object] = wrapper.data  # pyright: ignore[reportAssignmentType]
        response = FriendResponse(**payload.get("data", {}))  # pyright: ignore[reportCallIssue]
        envelope = WebsocketEventEnvelope(
            data=response,
            eventType=str(payload.get("eventType", "")),
            uri=str(payload.get("uri", "")),
        )
        return cls(opcode=wrapper.opcode, topic=wrapper.topic, data=envelope)


@dataclass
class _GunLoadoutData:
    ID: str | None = None          # Weapon UUID
    SkinID: str | None = None
    SkinLevelID: str | None = None
    ChromaID: str | None = None
    CharmInstanceID: str | None = None
    CharmID: str | None = None
    CharmLevelID: str | None = None
    Attachments: list[Any] | None = None  # pyright: ignore[reportExplicitAny]

@dataclass
class _SprayLoadoutData:
    EquipSlotID: str | None = None
    SprayID: str | None = None
    SprayLevelID: str | None = None
    
@dataclass
class _IdentityLoadoutData:
    PlayerCardID: str | None = None
    PlayerTitleID: str | None = None
    AccountLevel: int | None = None
    PreferredLevelBorderID: str | None = None
    HideAccountLevel: bool | None = None
    
@dataclass
class PlayerLoadoutResponse:
    Subject: str                    # PUUID structurally required
    Version: int | None = None      # Increments with every new change
    Guns: list[_GunLoadoutData] | list[dict[str, object]] | None = None
    Sprays: list[_SprayLoadoutData] | list[dict[str, object]] | None = None
    Identity: _IdentityLoadoutData | dict[str, object] | None = None
    Incognito: bool | None = None
    
    def __post_init__(self) -> None:
        if self.Guns and isinstance(self.Guns[0], dict):
            self.Guns = [_GunLoadoutData(**g) for g in self.Guns]  # pyright: ignore[reportCallIssue]
        if self.Sprays and isinstance(self.Sprays[0], dict):
            self.Sprays = [_SprayLoadoutData(**s) for s in self.Sprays]  # pyright: ignore[reportCallIssue]
        if isinstance(self.Identity, dict):
            self.Identity = _IdentityLoadoutData(**self.Identity)  # pyright: ignore[reportArgumentType]
    
@dataclass
class _EntitlementItem:
    """A single owned item entry."""
    TypeID: str | None = None
    ItemID: str | None = None
    InstanceID: str | None = None
    Tiers: object | None = None

@dataclass
class _EntitlementsByType:
    """A group of owned items sharing the same ItemTypeID."""
    ItemTypeID: str | None = None
    Entitlements: list[_EntitlementItem] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.Entitlements and isinstance(self.Entitlements[0], dict):
            self.Entitlements = [_EntitlementItem(**e) for e in self.Entitlements]  # pyright: ignore[reportCallIssue]

@dataclass
class OwnedItemsResponse:
    """Response from GET /store/v1/entitlements/{puuid}/{ItemTypeID?}"""
    EntitlementsByTypes: list[_EntitlementsByType] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.EntitlementsByTypes and isinstance(self.EntitlementsByTypes[0], dict):
            self.EntitlementsByTypes = [_EntitlementsByType(**e) for e in self.EntitlementsByTypes]  # pyright: ignore[reportCallIssue]

    @property
    def item_count(self) -> int:
        """Total number of owned items across all item types."""
        if not self.EntitlementsByTypes:
            return 0
        return sum(
            len(group.Entitlements) if group.Entitlements else 0
            for group in self.EntitlementsByTypes
            if isinstance(group, _EntitlementsByType)
        )


class ItemTypes(Enum):
    """Collection of the item types mapped to their ItemTypeID
       This is especially useful for GET PD /store/v1/entitlements/{puuid}/{ItemTypeID}"""
    
    AGENTS = "01bb38e1-da47-4e6a-9b3d-945fe4655707"
    SPRAYS = "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475"
    GUN_BUDDIES = "dd3bf334-87f3-40bd-b043-682a57a8dc3a"
    CARDS = "3f296c07-64c3-494c-923b-fe692a4fa1bd"
    SKINS = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"
    SKIN_VARIANTS = "3ad1b2b2-acdb-4524-852f-954a76ddae0a"
    TITLES = "de7caa6b-adf7-4588-bbd1-143831e786c6"
    
@dataclass
class _XPProgress:
    """Level and XP progress snapshot."""
    Level: int | None = None              # Account level
    XP: int | None = None                 # XP within the current level

@dataclass
class _XPSource:
    """A single XP source entry (e.g. time-played, match-win)."""
    ID: str | None = None                 # Source type: "time-played", "match-win", "first-win-of-the-day"
    Amount: int | None = None             # XP awarded from this source

@dataclass
class _XPMultiplier:
    """A single XP multiplier entry."""
    ID: str | None = None                 # Multiplier type: e.g. "penalty-modifier"
    Value: float | None = None            # Multiplier value (0 = no XP granted)

@dataclass
class _XPHistoryEntry:
    """A single match XP history entry."""
    ID: str | None = None                                                       # Match UUID
    MatchStart: str | None = None                                               # ISO 8601 match start time
    StartProgress: _XPProgress | dict[str, object] | None = None                # Level/XP before the match
    EndProgress: _XPProgress | dict[str, object] | None = None                  # Level/XP after the match
    XPDelta: int | None = None                                                  # Total XP gained from the match
    XPSources: list[_XPSource] | list[dict[str, object]] | None = None          # Breakdown of XP sources
    XPMultipliers: list[_XPMultiplier] | list[dict[str, object]] | None = None  # Active XP multipliers

    def __post_init__(self) -> None:
        if isinstance(self.StartProgress, dict):
            self.StartProgress = _XPProgress(**self.StartProgress)  # pyright: ignore[reportArgumentType]
        if isinstance(self.EndProgress, dict):
            self.EndProgress = _XPProgress(**self.EndProgress)  # pyright: ignore[reportArgumentType]
        if self.XPSources and isinstance(self.XPSources[0], dict):
            self.XPSources = [_XPSource(**s) for s in self.XPSources]  # pyright: ignore[reportCallIssue]
        if self.XPMultipliers and isinstance(self.XPMultipliers[0], dict):
            self.XPMultipliers = [_XPMultiplier(**m) for m in self.XPMultipliers]  # pyright: ignore[reportCallIssue]

@dataclass
class _XPMultiplierEffect:
    """XP multiplier penalty effect."""
    XPMultiplier: float | None = None         # Multiplier applied (0 = no XP)

@dataclass
class _PenaltyEntry:
    """A single penalty entry from the penalties endpoint."""
    ID: str | None = None                                                           # Penalty UUID
    IssuingGameStartUnixMillis: int | None = None                                   # Match start time (Unix ms)
    IssuingMatchID: str | None = None                                               # Match UUID that triggered the penalty
    Expiry: str | None = None                                                       # ISO 8601 penalty expiry time
    GamesRemaining: int | None = None                                               # Games left under penalty
    ApplyToAllPlatforms: bool | None = None                                         # Whether penalty applies cross-platform
    ApplyToPlatforms: list[str] | None = None                                       # Platforms: "pc", "console"
    ApplyToPlatformGroups: list[str] | None = None                                  # Platform groups: "pc", "console"
    InfractionID: str | None = None                                                 # Links to the infraction that caused this
    Origin: str | None = None                                                       # Platform where infraction occurred
    ForgivenessIneligible: bool | None = None                                       # Whether forgiveness can apply
    IsAutomatedDetection: bool | None = None                                        # Auto-detected vs manual report
    PenaltyInfo: dict[str, object] | None = None                                    # Additional penalty metadata
    DelayedPenaltyEffect: dict[str, object] | None = None                           # Delayed penalty details
    GameBanEffect: dict[str, object] | None = None                                  # Game ban details
    QueueDelayEffect: dict[str, object] | None = None                               # Queue delay details
    QueueRestrictionEffect: dict[str, object] | None = None                         # Queue restriction details
    RankedRatingPenaltyEffect: dict[str, object] | None = None                      # RR penalty details
    RiotRestrictionEffect: dict[str, object] | None = None                          # Riot restriction details
    RMSNotifyEffect: dict[str, object] | None = None                                # RMS notification details
    WarningEffect: dict[str, object] | None = None                                  # Warning details
    XPMultiplierEffect: _XPMultiplierEffect | dict[str, object] | None = None       # XP multiplier penalty
    PremierRestrictionEffect: dict[str, object] | None = None                       # Premier restriction details

    def __post_init__(self) -> None:
        if isinstance(self.XPMultiplierEffect, dict):
            self.XPMultiplierEffect = _XPMultiplierEffect(**self.XPMultiplierEffect)  # pyright: ignore[reportArgumentType]

@dataclass
class _InfractionEntry:
    """A single infraction entry describing the rule violation."""
    ID: str | None = None                     # Infraction UUID (referenced by PenaltyEntry.InfractionID)
    Name: str | None = None                   # Infraction type: "AFK_SEVERE_NONDISRUPTIVE", "NONPARTICIPATION_COMBAT_NONDISRUPTIVE"
    RatingName: str | None = None             # Rating category: "afk", "damageParticipationOutgoingNonDisruptive"

@dataclass
class PenaltiesResponse:
    """Response from GET /restrictions/v3/penalties"""
    Subject: str | None = None                                                              # Player UUID (PUUID)
    Penalties: list[_PenaltyEntry] | list[dict[str, object]] | None = None                  # Active penalties
    Infractions: list[_InfractionEntry] | list[dict[str, object]] | None = None             # Infractions that triggered the penalties
    Version: int | None = None                                                              # Schema version

    def __post_init__(self) -> None:
        if self.Penalties and isinstance(self.Penalties[0], dict):
            self.Penalties = [_PenaltyEntry(**p) for p in self.Penalties]  # pyright: ignore[reportCallIssue]
        if self.Infractions and isinstance(self.Infractions[0], dict):
            self.Infractions = [_InfractionEntry(**i) for i in self.Infractions]  # pyright: ignore[reportCallIssue]

@dataclass
class AccountXPResponse:
    """Response from GET /account-xp/v1/players/{puuid}"""
    Version: int | None = None                                                      # Schema version
    Subject: str | None = None                                                       # Player UUID (PUUID)
    Progress: _XPProgress | dict[str, object] | None = None                          # Current level/XP progress
    History: list[_XPHistoryEntry] | list[dict[str, object]] | None = None            # Recent match XP history
    LastTimeGrantedFirstWin: str | None = None                                       # ISO 8601 last first-win-of-the-day grant
    NextTimeFirstWinAvailable: str | None = None                                     # ISO 8601 next first-win-of-the-day availability

    def __post_init__(self) -> None:
        if isinstance(self.Progress, dict):
            self.Progress = _XPProgress(**self.Progress)  # pyright: ignore[reportArgumentType]
        if self.History and isinstance(self.History[0], dict):
            self.History = [_XPHistoryEntry(**h) for h in self.History]  # pyright: ignore[reportCallIssue]

@dataclass
class _LatestCompetitiveUpdate:
    """Most recent competitive match update."""
    MatchID: str | None = None                              # Match UUID
    MapID: str | None = None                                # Map path: "/Game/Maps/Juliett/Juliett"
    SeasonID: str | None = None                             # Season UUID (act)
    MatchStartTime: int | None = None                       # Match start time (Unix ms)
    MatchLength: int | None = None                          # Match duration (ms)
    TierAfterUpdate: int | None = None                      # Competitive tier after the match
    TierBeforeUpdate: int | None = None                     # Competitive tier before the match
    RankedRatingAfterUpdate: int | None = None              # RR after the match
    RankedRatingBeforeUpdate: int | None = None             # RR before the match
    RankedRatingEarned: int | None = None                   # Net RR gained/lost
    RankedRatingPerformanceBonus: int | None = None         # Bonus RR from performance
    RankedRatingRefundApplied: int | None = None            # RR refunded (e.g. AFK teammate)
    NewMapIncentiveRRForgiven: int | None = None            # RR forgiven for new map incentive
    CompetitiveMovement: str | None = None                  # "MOVEMENT_UNKNOWN", "PROMOTED", "DEMOTED", etc.
    AFKPenalty: int | None = None                           # RR penalty for AFK
    WasDerankProtected: bool | None = None                  # Whether derank protection triggered
    WasDerankProtectionReplenished: bool | None = None      # Whether derank protection was restored

@dataclass
class _SeasonalInfo:
    """Per-season competitive stats for a queue."""
    SeasonID: str | None = None                             # Season UUID (act)
    NumberOfWins: int | None = None                         # Wins (excluding placements)
    NumberOfWinsWithPlacements: int | None = None           # Wins including placement matches
    NumberOfGames: int | None = None                        # Total games played
    Rank: int | None = None                                 # Peak rank achieved
    CapstoneWins: int | None = None                         # Capstone (promotion) wins
    LeaderboardRank: int | None = None                      # Leaderboard position (0 = unranked)
    CompetitiveTier: int | None = None                      # Current competitive tier
    RankedRating: int | None = None                         # Current RR within tier
    WinsByTier: dict[str, int] | None = None                # Wins per tier: {"21": 3, "22": 5}
    GamesNeededForRating: int | None = None                 # Placement games remaining
    TotalWinsNeededForRank: int | None = None               # Wins needed before ranked rating shows

@dataclass
class _QueueSkillData:
    """Skill/ranking data for a single queue (competitive, deathmatch, etc.)."""
    TotalGamesNeededForRating: int | None = None                                                    # Total placement games required
    TotalGamesNeededForLeaderboard: int | None = None                                               # Games needed for leaderboard eligibility
    CurrentSeasonGamesNeededForRating: int | None = None                                            # Placement games remaining this season
    SeasonalInfoBySeasonID: dict[str, _SeasonalInfo] | dict[str, dict[str, object]] | None = None   # Per-season stats keyed by season UUID

    def __post_init__(self) -> None:
        if self.SeasonalInfoBySeasonID:
            first_val = next(iter(self.SeasonalInfoBySeasonID.values()))
            if isinstance(first_val, dict):
                self.SeasonalInfoBySeasonID = {
                    k: _SeasonalInfo(**v) for k, v in self.SeasonalInfoBySeasonID.items()  # pyright: ignore[reportCallIssue]
                }

@dataclass
class PlayerMMRResponse:
    """Response from GET /mmr/v1/players/{puuid}"""
    Version: int | None = None                                                                      # Schema version
    Subject: str | None = None                                                                      # Player UUID (PUUID)
    LatestCompetitiveUpdate: _LatestCompetitiveUpdate | dict[str, object] | None = None             # Most recent ranked match result
    NewPlayerExperienceFinished: bool | None = None                                                 # Whether new player onboarding is done
    IsActRankBadgeHidden: bool | None = None                                                        # Whether act rank badge is hidden
    IsLeaderboardAnonymized: bool | None = None                                                     # Whether leaderboard name is hidden
    OnboardingFlowV2Enabled: bool | None = None                                                     # V2 onboarding flow flag
    OnboardingStatus: str | None = None                                                             # "OnboardingComplete", etc.
    IsAtDerankProtectedTier: bool | None = None                                                     # Whether at derank-protected tier
    DerankProtectedGamesRemaining: int | None = None                                                # Derank protection games left
    DerankProtectedStatus: str | None = None                                                        # "Empty", "Active", etc.
    QueueSkills: dict[str, _QueueSkillData] | dict[str, dict[str, object]] | None = None            # Per-queue skill data keyed by queue ID

    def __post_init__(self) -> None:
        if isinstance(self.LatestCompetitiveUpdate, dict):
            self.LatestCompetitiveUpdate = _LatestCompetitiveUpdate(**self.LatestCompetitiveUpdate)  # pyright: ignore[reportArgumentType]
        if self.QueueSkills:
            first_val = next(iter(self.QueueSkills.values()))
            if isinstance(first_val, dict):
                self.QueueSkills = {
                    k: _QueueSkillData(**v) for k, v in self.QueueSkills.items()  # pyright: ignore[reportCallIssue]
                }
                
                
# ------------ Match Collection Models ------------

@dataclass
class MatchHistoryEntry:
    """A single entry from the match history endpoint."""
    MatchID: str = ""
    GameStartTime: int = 0          # Unix milliseconds
    QueueID: str | None = None


@dataclass
class MatchHistoryResponse:
    """Response from GET /match-history/v1/history/{puuid}."""
    Subject: str = ""
    BeginIndex: int = 0
    EndIndex: int = 0
    Total: int = 0
    History: list[MatchHistoryEntry] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.History and isinstance(self.History[0], dict):
            self.History = [MatchHistoryEntry(**h) for h in self.History]  # pyright: ignore[reportCallIssue]


@dataclass
class LeaderboardPlayer:
    """Single player entry in the leaderboard."""
    PlayerCardID: str | None = None
    TitleID: str | None = None
    IsBanned: bool | None = None
    IsAnonymized: bool | None = None
    puuid: str | None = None
    gameName: str | None = None
    tagLine: str | None = None
    leaderboardRank: int | None = None
    rankedRating: int | None = None
    numberOfWins: int | None = None
    competitiveTier: int | None = None


@dataclass
class TierDetail:
    """Tier threshold and pagination info."""
    rankedRatingThreshold: int | None = None
    startingPage: int | None = None
    startingIndex: int | None = None


@dataclass
class LeaderboardResponse:
    """Response from GET /mmr/v1/leaderboards/affinity/{region}/queue/competitive/season/{seasonId}."""
    Deployment: str | None = None
    QueueID: str | None = None
    SeasonID: str | None = None
    Players: list[LeaderboardPlayer] | list[dict[str, object]] | None = None
    totalPlayers: int | None = None
    immortalStartingPage: int | None = None
    immortalStartingIndex: int | None = None
    topTierRRThreshold: int | None = None
    tierDetails: dict[str, TierDetail] | dict[str, dict[str, object]] | None = None
    startIndex: int | None = None
    query: str | None = None

    def __post_init__(self) -> None:
        if self.Players and isinstance(self.Players[0], dict):
            known_player = {f.name for f in fields(LeaderboardPlayer)}
            self.Players = [LeaderboardPlayer(**{k: v for k, v in p.items() if k in known_player}) for p in self.Players]    # pyright: ignore[reportArgumentType, reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]
        if self.tierDetails and isinstance(next(iter(self.tierDetails.values())), dict):
            known_tier = {f.name for f in fields(TierDetail)}
            self.tierDetails = {k: TierDetail(**{tk: tv for tk, tv in v.items() if tk in known_tier}) for k, v in self.tierDetails.items()}  # pyright: ignore[reportArgumentType, reportUnknownVariableType, reportUnknownMemberType, reportAttributeAccessIssue]


# ------------ Shared GLZ Models (used by both Pregame and Ingame) ------------


@dataclass
class _GLZPlayerIdentity:
    Subject: str | None = None
    PlayerCardID: str | None = None
    PlayerTitleID: str | None = None
    AccountLevel: int | None = None
    PreferredLevelBorderID: str | None = None
    Incognito: bool | None = None
    HideAccountLevel: bool | None = None


@dataclass
class _GLZSeasonalBadge:
    SeasonID: str | None = None
    NumberOfWins: int | None = None
    WinsByTier: dict[str, int] | None = None
    Rank: int | None = None
    LeaderboardRank: int | None = None


# ------------ Pregame Models ------------


@dataclass
class _PregamePlayer:
    Subject: str | None = None
    CharacterID: str | None = None
    CharacterSelectionState: str | None = None
    PregamePlayerState: str | None = None
    CompetitiveTier: int | None = None
    PlayerIdentity: _GLZPlayerIdentity | dict[str, object] | None = None
    SeasonalBadgeInfo: _GLZSeasonalBadge | dict[str, object] | None = None
    IsCaptain: bool | None = None
    PlatformType: str | None = None
    PremierPrestige: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.PlayerIdentity, dict):
            self.PlayerIdentity = _GLZPlayerIdentity(**self.PlayerIdentity)  # pyright: ignore[reportArgumentType]
        if isinstance(self.SeasonalBadgeInfo, dict):
            self.SeasonalBadgeInfo = _GLZSeasonalBadge(**self.SeasonalBadgeInfo)  # pyright: ignore[reportArgumentType]


@dataclass
class _PregameTeam:
    TeamID: str | None = None
    Players: list[_PregamePlayer] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.Players and isinstance(self.Players[0], dict):
            known = {f.name for f in fields(_PregamePlayer)}
            self.Players = [_PregamePlayer(**{k: v for k, v in p.items() if k in known}) for p in self.Players]  # pyright: ignore[reportArgumentType, reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]


@dataclass
class PregameMatchResponse:
    """Response from GET /pregame/v1/matches/{matchId}."""
    ID: str | None = None
    Version: int | None = None
    Teams: list[_PregameTeam] | list[dict[str, object]] | None = None
    AllyTeam: _PregameTeam | dict[str, object] | None = None
    EnemyTeam: _PregameTeam | dict[str, object] | None = None
    ObserverSubjects: list[str] | None = None
    MatchCoaches: list[str] | None = None
    EnemyTeamSize: int | None = None
    EnemyTeamLockCount: int | None = None
    PregameState: str | None = None
    LastUpdated: str | None = None
    MapID: str | None = None
    MapSelectPool: list[object] | None = None
    BannedMapIDs: list[str] | None = None
    CastedVotes: dict[str, object] | None = None
    MapSelectSteps: list[object] | None = None
    MapSelectStep: int | None = None
    Team1: str | None = None
    GamePodID: str | None = None
    Mode: str | None = None
    VoiceSessionID: str | None = None
    MUCName: str | None = None
    TeamMatchToken: str | None = None
    QueueID: str | None = None
    ProvisioningFlowID: str | None = None
    IsRanked: bool | None = None
    PhaseTimeRemainingNS: int | None = None
    StepTimeRemainingNS: int | None = None
    altModesFlagADA: bool | None = None
    TournamentMetadata: dict[str, object] | None = None
    RosterMetadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.Teams and isinstance(self.Teams[0], dict):
            self.Teams = [_PregameTeam(**t) for t in self.Teams]  # pyright: ignore[reportCallIssue]
        if isinstance(self.AllyTeam, dict):
            self.AllyTeam = _PregameTeam(**self.AllyTeam)  # pyright: ignore[reportArgumentType]
        if isinstance(self.EnemyTeam, dict):
            self.EnemyTeam = _PregameTeam(**self.EnemyTeam)  # pyright: ignore[reportArgumentType]


# ------------ Ingame Models ------------


@dataclass
class _IngameConnectionDetails:
    GameServerHosts: list[str] | None = None
    GameServerHost: str | None = None
    GameServerPort: int | None = None
    GameServerObfuscatedIP: int | None = None
    GameClientHash: int | None = None
    PlayerKey: str | None = None


@dataclass
class _IngamePostGamePlayer:
    Subject: str | None = None


@dataclass
class _IngamePostGameDetails:
    Start: str | None = None
    Players: list[_IngamePostGamePlayer] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.Players and isinstance(self.Players[0], dict):
            known = {f.name for f in fields(_IngamePostGamePlayer)}
            self.Players = [_IngamePostGamePlayer(**{k: v for k, v in p.items() if k in known}) for p in self.Players]  # pyright: ignore[reportArgumentType, reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]


@dataclass
class _IngamePlayer:
    Subject: str | None = None
    TeamID: str | None = None
    CharacterID: str | None = None
    PlayerIdentity: _GLZPlayerIdentity | dict[str, object] | None = None
    SeasonalBadgeInfo: _GLZSeasonalBadge | dict[str, object] | None = None
    IsCoach: bool | None = None
    IsAssociated: bool | None = None
    PlatformType: str | None = None
    PremierPrestige: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.PlayerIdentity, dict):
            self.PlayerIdentity = _GLZPlayerIdentity(**self.PlayerIdentity)  # pyright: ignore[reportArgumentType]
        if isinstance(self.SeasonalBadgeInfo, dict):
            self.SeasonalBadgeInfo = _GLZSeasonalBadge(**self.SeasonalBadgeInfo)  # pyright: ignore[reportArgumentType]


@dataclass
class IngameMatchResponse:
    """Response from GET /core-game/v1/matches/{matchId}."""
    MatchID: str | None = None
    Version: int | None = None
    State: str | None = None
    MapID: str | None = None
    ModeID: str | None = None
    ProvisioningFlow: str | None = None
    GamePodID: str | None = None
    AllMUCName: str | None = None
    TeamMUCName: str | None = None
    TeamVoiceID: str | None = None
    TeamMatchToken: str | None = None
    IsReconnectable: bool | None = None
    ConnectionDetails: _IngameConnectionDetails | dict[str, object] | None = None
    PostGameDetails: _IngamePostGameDetails | dict[str, object] | None = None
    Players: list[_IngamePlayer] | list[dict[str, object]] | None = None
    MatchmakingData: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ConnectionDetails, dict):
            known = {f.name for f in fields(_IngameConnectionDetails)}
            self.ConnectionDetails = _IngameConnectionDetails(**{k: v for k, v in self.ConnectionDetails.items() if k in known})  # pyright: ignore[reportArgumentType]
        if isinstance(self.PostGameDetails, dict):
            known = {f.name for f in fields(_IngamePostGameDetails)}
            self.PostGameDetails = _IngamePostGameDetails(**{k: v for k, v in self.PostGameDetails.items() if k in known})  # pyright: ignore[reportArgumentType]
        if self.Players and isinstance(self.Players[0], dict):
            known = {f.name for f in fields(_IngamePlayer)}
            self.Players = [_IngamePlayer(**{k: v for k, v in p.items() if k in known}) for p in self.Players]  # pyright: ignore[reportArgumentType, reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]


# ------------ Ingame Loadout Models ------------


@dataclass
class _LoadoutSocketItem:
    """Inner item inside a socket slot."""
    ID: str | None = None
    TypeID: str | None = None


@dataclass
class _LoadoutSocket:
    """A single socket on a weapon/item."""
    ID: str | None = None
    Item: _LoadoutSocketItem | dict[str, object] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.Item, dict):
            self.Item = _LoadoutSocketItem(**self.Item)  # pyright: ignore[reportArgumentType]


@dataclass
class _LoadoutItem:
    """A weapon or equipment entry in the loadout."""
    ID: str | None = None
    TypeID: str | None = None
    Sockets: dict[str, _LoadoutSocket] | dict[str, dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.Sockets:
            first_val = next(iter(self.Sockets.values()))
            if isinstance(first_val, dict):
                self.Sockets = {
                    k: _LoadoutSocket(**v) for k, v in self.Sockets.items()  # pyright: ignore[reportCallIssue]
                }


@dataclass
class _LoadoutSpraySelection:
    """A single spray selection."""
    SocketID: str | None = None
    SprayID: str | None = None
    LevelID: str | None = None


@dataclass
class _LoadoutSprays:
    """Spray selections container."""
    SpraySelections: list[_LoadoutSpraySelection] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.SpraySelections and isinstance(self.SpraySelections[0], dict):
            self.SpraySelections = [_LoadoutSpraySelection(**s) for s in self.SpraySelections]  # pyright: ignore[reportCallIssue]


@dataclass
class _LoadoutAESSelection:
    """A single AES (expression) selection."""
    SocketID: str | None = None
    AssetID: str | None = None
    TypeID: str | None = None


@dataclass
class _LoadoutExpressions:
    """Expression selections container."""
    AESSelections: list[_LoadoutAESSelection] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.AESSelections and isinstance(self.AESSelections[0], dict):
            self.AESSelections = [_LoadoutAESSelection(**s) for s in self.AESSelections]  # pyright: ignore[reportCallIssue]


@dataclass
class _LoadoutData:
    """A player's full loadout for a match."""
    Subject: str | None = None
    Sprays: _LoadoutSprays | dict[str, object] | None = None
    Expressions: _LoadoutExpressions | dict[str, object] | None = None
    DynamicOptions: dict[str, object] | None = None
    Items: dict[str, _LoadoutItem] | dict[str, dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.Sprays, dict):
            self.Sprays = _LoadoutSprays(**self.Sprays)  # pyright: ignore[reportArgumentType]
        if isinstance(self.Expressions, dict):
            self.Expressions = _LoadoutExpressions(**self.Expressions)  # pyright: ignore[reportArgumentType]
        if self.Items:
            first_val = next(iter(self.Items.values()))
            if isinstance(first_val, dict):
                self.Items = {
                    k: _LoadoutItem(**v) for k, v in self.Items.items()  # pyright: ignore[reportCallIssue]
                }


@dataclass
class _IngameLoadoutEntry:
    """A single player's loadout entry in the response."""
    CharacterID: str | None = None
    Loadout: _LoadoutData | dict[str, object] | None = None
    Subject: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.Loadout, dict):
            known = {f.name for f in fields(_LoadoutData)}
            self.Loadout = _LoadoutData(**{k: v for k, v in self.Loadout.items() if k in known})  # pyright: ignore[reportArgumentType]


@dataclass
class IngameLoadoutsResponse:
    """Response from GET /core-game/v1/matches/{matchId}/loadouts."""
    Loadouts: list[_IngameLoadoutEntry] | list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.Loadouts and isinstance(self.Loadouts[0], dict):
            known = {f.name for f in fields(_IngameLoadoutEntry)}
            self.Loadouts = [_IngameLoadoutEntry(**{k: v for k, v in entry.items() if k in known}) for entry in self.Loadouts]  # pyright: ignore[reportArgumentType, reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]


# ------------ Storefront Models ------------


@dataclass
class StorefrontResponse:
    """Response from POST /store/v3/storefront/{puuid}.

    Only the daily skin offers (SkinsPanelLayout) are modelled;
    the remaining sections are kept as raw dicts.
    """
    SkinsPanelLayout: dict[str, object] | None = None
    FeaturedBundle: dict[str, object] | None = None
    UpgradeCurrencyStore: dict[str, object] | None = None
    AccessoryStore: dict[str, object] | None = None
    PluginStores: list[dict[str, object]] | None = None

    @property
    def single_item_offers(self) -> list[str]:
        """The 4 daily skin-offer item IDs."""
        if not self.SkinsPanelLayout:
            return []
        return self.SkinsPanelLayout.get("SingleItemOffers", [])  # pyright: ignore[reportReturnType]

    @property
    def single_item_offers_remaining_seconds(self) -> int | None:
        """Seconds until the daily offers rotate."""
        if not self.SkinsPanelLayout:
            return None
        return self.SkinsPanelLayout.get("SingleItemOffersRemainingDurationInSeconds")  # pyright: ignore[reportReturnType]


@dataclass
class AccountAlias:
    """A single name/tag alias from the player's account history.

    Attributes:
        active: Whether this alias is the currently active one.
        created_datetime: Epoch milliseconds when this alias was set.
        game_name: The display name (e.g. "JudgeMainNoBrain").
        summoner: Whether this is a summoner name (League of Legends).
        tag_line: The tag (e.g. "Judge").
    """

    active: bool | None = None
    created_datetime: int | None = None
    game_name: str | None = None
    summoner: bool | None = None
    tag_line: str | None = None


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