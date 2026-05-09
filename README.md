# VALORANT API Framework
  This is an unofficial VALORANT API scaffolding which handles all tedious API tasks for you.
  It handles:

  - **Authentication**: finds API credentials, authenticates, and manages the token's lifetime
  - **Connection lifecycle**: knows when to connect, when to tear down, and when the user's
  account has changed
  - **Endpoint readiness**: knows which endpoints are callable in the current state and only
  lets you hit them when they're actually available *(for example `pregame` endpoints cannot be called while in `menus`)*
  - **Rate limiting**: enforces a 30 req/min *(modifyable)* ceiling for you for all rate-limited endpoints while allowing non-ratelimited endpoints
  - **Retrying**: transparently retries on 401 and 429
  - **Game-state detection**: surfaces menus, pregame, ingame, queue, and idle transitions as
  events
  - **Typed responses**: every request is parsed into an object you can navigate with type-hints instead of raw JSON

  Define the events you care about and you receive exactly the data you need the moment it's available, already parsed, already safe, already within rate limits

# Installation
  This project was written using [Python 3.14.3](https://www.python.org/downloads/release/python-3143/) and I highly recommend using it too
  
  - **Step 1**: Clone this repository `git clone https://github.com/ryleqTheReal/valorant-api-framework/`
  - **Step 2**: Change to the directory
  - **Step 3**: Set up a venv *(pay attention to use the correct python version!)*
  - **Step 4**: Install dependencies `pip install -r requirements.txt`

  Now you can run `main.py`

# Important Architecture
  ## Request Scheduler
  The request scheduler enforces that you cannot go over the safe amount of requests per minute *(default set to 30)*. 
  This excludes any endpoints which are not rate limited like local endpoints or glz endpoints.
  You can adjust the rate-limit in the [`config.json`](https://github.com/ryleqTheReal/valorant-api-framework/blob/main/config.json)
  
  ## How to implement own handlers
  Since all events flow through the same central event bus, you just have to create a new listener on the event bus like this:

  ```python
  from services.event_bus import EventBus, Event

  class MyHandler:
    def __init__(self, bus: EventBus):
      self.bus = bus
      self._register()

    def _register(self) -> None:
      """Subscribe to relevant events."""
      self.bus.on(Event.WEBSOCKET_EVENT, self._on_websocket_event, priority=5)  
  ```
  The `EventBus.on()` method takes the following parameters:
  * `Event`: An event to listen to defined here: [src/services/event_bus.py](https://github.com/ryleqTheReal/valorant-api-framework/blob/main/src/services/event_bus.py#L19)
  * `callback`: An asynchronous handler which takes the parsed data as parameter and returns `None`:
    
    ```python
    async def _on_websocket_event(self, data: PresenceWebsocketEvent) -> None:
      print(data.something)
    ```
    
  * `prioty`: A priority as numer where a higher number means that it will be requested first
  
  ## Events
  The event uses a central event bus to register and handle events in order. Here is a list of the built-in events that I have already implemented. 
  You can add your custom. 

  Event Name | Trigger
  -----------|-----------
  RSO_LOGIN               | Riot Client logged in (lockfile + RSO 200)
  RSO_LOGOUT              | Riot Client logged out (RSO non-200 / lockfile gone)
  VALORANT_OPENED         | Valorant process detected, lockfile read
  VALORANT_CLOSED         | Valorant process terminated
  AUTH_SUCCESS            | Riot auth succeeded 
  AUTH_FAILED             | Riot auth failed
  SHUTDOWN                | The application is shutting down
  WEBSOCKET_CONNECTED     | Local WAMP socket connected and subscribed
  WEBSOCKET_DISCONNECTED  | Local WAMP socket closed
  WEBSOCKET_EVENT         | Raw presence event from Riot Client websocket
  INGAME_API_AVAILABLE    | In-game endpoints reachable: first own-presence seen via poll or websocket. Ends on VALORANT_CLOSED.
  GAME_STATE_CHANGED      | sessionLoopState transition (MENUS / PREGAME / INGAME)
  USER_BECAME_IDLE        | No menu activity for the configured idle window
  USER_BECAME_ACTIVE      | Menu activity detected after being idle (or after first becoming active)
  USER_JOINED_QUEUE       | Party state transitioned to MATCHMAKING
  USER_LEFT_QUEUE         | Party state transitioned from MATCHMAKING to anything else
  LOADOUT_UPDATED         | User's equipped loadout version changed
  OWNED_ITEMS_UPDATED     | Owned-item count changed
  USER_XP_UPDATED         | Account XP version changed
  PENALTIES_UPDATED       | Penalties version changed
  MMR_HISTORY_UPDATED     | MMR history version changed
  USERINFO_FETCHED        | User account info fetched (one-shot per session)
  ACCOUNT_ALIASES_FETCHED | Name/tag alias history fetched (one-shot per session)
  STORE_OFFERS_UPDATED    | Daily skin offers rotated
  PREGAME_MATCH_UPDATED   | Pregame match data refreshed during agent select
  INGAME_MATCH_UPDATED    | Ingame match data fetched when a match starts
  INGAME_LOADOUTS_FETCHED | Player loadouts fetched for the active match
  FRIEND_ADDED            | Friend request accepted
  FRIEND_REMOVED          | Friend removed 
  FRIEND_REQUEST_RECEIVED | Incoming friend request received
  FRIEND_REQUEST_SENT     | Outgoing friend request sent
  FRIENDS_LIST_FETCHED    | Initial friend list fetched on presence baseline

  All events are defined inside the [event bus](https://github.com/ryleqTheReal/valorant-api-framework/blob/main/src/services/event_bus.py) module.

# Errors
Not much to tell about errors. All errors that happen inside event handlers are ignored and logged as `WARNING`. 
If however a critical error happens, it's logged as `AppError` and the app attempts a reboot.
All errors are inherited from `AppError` and have these three fields:
* `is_critical`: A `boolean` that suggests whether the error is critical for the app
* `message`: A human-readable message that explains what went wrong
* `internal_status`: An error code meant for possible API integrations. Here is an example: `CRITICAL_APP_ERROR`

I don't want to explain all errors to you. If you want to find out more about them, read through: [exceptions.py](https://github.com/ryleqTheReal/valorant-api-framework/blob/main/src/utils/exceptions.py)

# Disclaimer
Hello dear riot lawyer. I don't mean harm with this project so please don't sue me :) if you really don't like this, please reach out to me first I'm happy to comply.
If you however don't like it to the extent that you would like to hire me, reach out to me too :P

# Help
For help feel free to reach out to me via discord: `@ryleq`
