"""Async client for the MyFantasyLeague.com API.

Endpoint signatures used here were verified against
``https://api.myfantasyleague.com/2026/api_info`` (the "Request Reference"
page). Summary of the ones that matter here:

============================  ==========================================
Request                       Arguments
============================  ==========================================
``mfl_status``                none; lives at ``/fflnetdynamic<year>/mfl_status.json``
``export?TYPE=league``        ``L``
``export?TYPE=rules``         ``L``
``export?TYPE=rosters``       ``L``
``export?TYPE=players``       ``L?``, ``DETAILS``, ``SINCE``, ``PLAYERS``
``export?TYPE=freeAgents``    ``L``
``export?TYPE=injuries``      ``W?``
``export?TYPE=projectedScores`` ``L``, ``W?``, ``PLAYERS``, ``POSITION``, ``STATUS``, ``COUNT``
``export?TYPE=whoShouldIStart`` ``L?``, ``WEEK``, ``FRANCHISE``, ``PLAYERS``
``export?TYPE=pendingWaivers`` ``L``
``export?TYPE=abilities``     ``L``
``import?TYPE=lineup``        ``L``, ``W``, ``STARTERS``, ``COMMENTS``, ``TIEBREAKERS``, ``FRANCHISE_ID``
``import?TYPE=fcfsWaiver``    ``L``, ``ADD``, ``DROP``, ``FRANCHISE_ID``
``import?TYPE=waiverRequest`` ``L``, ``ROUND``, ``PICKS``, ``REPLACE``, ``FRANCHISE_ID``
``import?TYPE=ir``            ``L``, ``IR``, ``FRANCHISE_ID``
============================  ==========================================

Note that ``fcfsWaiver`` is the *documented* add/drop import: an immediate
first-come-first-served add and/or drop. No XML ``DATA`` payload is required,
which removes the usual guesswork around hand-rolled transaction XML.

Safety: every import goes through :meth:`MFLClient._import`, which refuses to
run unless :meth:`Settings.require_writes` passes. Writes cannot be reached by
accident.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

from . import __version__
from .config import (
    API_HOST,
    CLIENT_NAME,
    DEFAULT_USER_AGENT,
    Settings,
    get_settings,
)
from .errors import MFLError, WritesDisabledError
from .models import (
    Franchise,
    LeagueSettings,
    Player,
    RosterEntry,
    RosterPlayer,
    as_float,
    as_int,
    as_list,
    as_str,
    franchise_id as norm_franchise_id,
    parse_slot_spec,
    player_id as norm_player_id,
    players_index,
)

log = logging.getLogger(__name__)

__all__ = ["MFLClient", "MFLError", "WritesDisabledError"]


class MFLClient:
    """Thin, well-behaved wrapper over the MFL export/import API.

    Rate limiting: MFL throttles aggressive clients with HTTP 429 and does not
    publish the limits. Their guidance is at most one request per second, with
    caching for data that changes slowly. We do both.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._cookie: str | None = None
        self._logged_in = False
        self._last_request = 0.0
        self._resolved_host: str | None = self.settings.host
        self._sleep = asyncio.sleep
        self._monotonic = time.monotonic

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "MFLClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        """Headers applied to every outgoing request.

        Sent per-request rather than only when constructing our own client, so
        the User-Agent is correct even when a caller injects a pre-built
        ``httpx.AsyncClient``. That matters: MFL only honours a registered
        client's higher rate limit if the User-Agent it sees matches the one
        registered, so a silently default ``python-httpx/x.y`` header would
        quietly void the registration.
        """
        return {"User-Agent": self.settings.user_agent}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout,
                headers=self._headers(),
                follow_redirects=True,
            )
        return self._client

    # -- low level ---------------------------------------------------------

    def _base_url(self, command: str) -> str:
        host = self._resolved_host or self.settings.host
        if not host:
            # No league host resolved yet: the non-league host handles the
            # handful of commands that do not need one.
            host = API_HOST
        host = host.replace("https://", "").replace("http://", "").strip("/")
        if command == "mfl_status":
            return f"https://{API_HOST}/fflnetdynamic{self.settings.year}/mfl_status.json"
        return f"https://{host}/{self.settings.year}/{command}"

    def _auth(self, params: dict[str, Any], *, cookie_ok: bool) -> dict[str, Any]:
        """Attach credentials to a query.

        The cookie takes precedence whenever one exists, because MFL's API key
        is export-only: sending ``APIKEY`` on an import makes MFL ignore the
        session and reject the write. A key is therefore attached only to a
        request that cannot use a cookie, or has none yet.
        """
        if cookie_ok and self._cookie:
            params["_cookie"] = self._cookie
            return params
        if self.settings.apikey:
            params["APIKEY"] = self.settings.apikey
        return params

    async def _throttle(self) -> None:
        gap = self.settings.request_delay
        if gap <= 0:
            return
        elapsed = self._monotonic() - self._last_request
        if elapsed < gap:
            await self._sleep(gap - elapsed)
        self._last_request = self._monotonic()

    async def _ensure_authenticated(self, *, required: bool) -> None:
        """Obtain a session cookie when one is needed but absent.

        Two situations need this. A configuration that supplies only
        ``MFL_USERNAME``/``MFL_PASSWORD`` would otherwise send every read
        completely unauthenticated, because no cookie exists until someone logs
        in. And an import always needs the cookie, since ``APIKEY`` is
        export-only, even when an API key is also configured.

        An export with a usable API key is left alone: logging in there would
        spend a request to obtain a credential the request does not need.
        """
        if self._cookie or not self.settings.has_cookie_auth:
            return
        if not required and self.settings.has_apikey_auth:
            return
        await self.login()

    async def _send(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
        *,
        cookie_ok: bool = True,
        require_cookie: bool = False,
    ) -> dict[str, Any]:
        if cookie_ok:
            await self._ensure_authenticated(required=require_cookie)

        query: dict[str, Any] = {"JSON": 1}
        for key, value in (params or {}).items():
            if value is None:
                continue
            query[key] = value

        cookie = self._cookie
        query = self._auth(query, cookie_ok=cookie_ok)
        url = self._base_url(command)
        sendable = {k: v for k, v in query.items() if not k.startswith("_")}

        await self._throttle()
        http = self._http()
        try:
            headers = self._headers()
            if cookie and "APIKEY" not in query:
                headers["Cookie"] = f"MFL_USER_ID={cookie}"
            response = await http.get(url, params=sendable, headers=headers)
        except httpx.HTTPError as exc:
            raise MFLError(f"Request to {url} failed: {exc}") from exc

        if response.status_code == 429:
            # MFL explicitly says: do not retry, cool down.
            raise MFLError(
                "MFL rate limit hit (HTTP 429). Increase MFL_REQUEST_DELAY and retry later."
            )
        if response.status_code >= 500:
            raise MFLError(f"MFL server error {response.status_code} for {url}")
        if response.status_code >= 400:
            raise MFLError(f"MFL returned HTTP {response.status_code} for {url}")

        text = response.text.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MFLError(f"Non-JSON response from MFL for {url}") from exc
        if isinstance(payload, dict) and "error" in payload:
            raise MFLError(f"MFL error: {_error_text(payload['error'])}")
        return payload if isinstance(payload, dict) else {"response": payload}

    async def export(self, request_type: str, **params: Any) -> dict[str, Any]:
        """Call ``export?TYPE=<request_type>``."""
        self.settings.require_read()
        params.setdefault("L", self.settings.league_id)
        return await self._send("export", {"TYPE": request_type, **params})

    async def _import(self, request_type: str, **params: Any) -> dict[str, Any]:
        """Call ``import?TYPE=<request_type>``, gated by the write switch."""
        self.settings.require_writes()
        params.setdefault("L", self.settings.league_id)
        return await self._send(
            "import", {"TYPE": request_type, **params}, require_cookie=True
        )

    # -- auth --------------------------------------------------------------

    async def login(self, *, force: bool = False) -> str:
        """Obtain an ``MFL_USER_ID`` cookie. Required for every write."""
        if self._logged_in and not force and self._cookie:
            return self._cookie
        if not self.settings.has_cookie_auth:
            raise MFLError(
                "MFL_USERNAME and MFL_PASSWORD are required to log in. The APIKEY "
                "cannot be used for import requests."
            )
        self.settings.require_read()
        url = f"https://{API_HOST}/{self.settings.year}/login"
        http = self._http()
        await self._throttle()
        try:
            response = await http.post(
                url,
                params={"XML": 1},
                headers=self._headers(),
                data={
                    "USERNAME": self.settings.username or "",
                    "PASSWORD": self.settings.password or "",
                },
            )
        except httpx.HTTPError as exc:
            raise MFLError(f"Login request failed: {exc}") from exc

        if response.status_code >= 400:
            raise MFLError(f"Login failed with HTTP {response.status_code}")
        body = response.text
        if "<error" in body.lower():
            raise MFLError("MFL rejected the supplied username or password")

        cookie = _extract_cookie(body)
        if not cookie:
            # Some deployments return the cookie as a Set-Cookie header.
            for header in response.headers.get_list("set-cookie"):
                for part in header.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "MFL_USER_ID" and value:
                        cookie = value
        if not cookie:
            raise MFLError("Login succeeded but no MFL_USER_ID cookie was returned")
        self._cookie = cookie
        self._logged_in = True
        log.info("Authenticated with MFL (%s)", self.settings.username)
        return cookie

    async def ensure_league_host(self) -> str:
        """Resolve and cache the league's own ``wwwNN`` host.

        Sending league requests to the wrong host works but can be slow or
        rejected under load, so we resolve it once. The request goes out on the
        api host (we have no league host yet) and MFL redirects as needed.

        Credentials are attached by :meth:`_send`, so this works with either an
        API key or a login cookie.
        """
        if self._resolved_host:
            return self._resolved_host
        self.settings.require_read()

        try:
            payload = await self._send("export", {"TYPE": "league", "L": self.settings.league_id})
        except MFLError:
            # The host is an optimisation, not a requirement: the api host
            # serves league requests too and redirects to the right server. A
            # failure to discover the host must never block real work.
            log.info(
                "Could not resolve the league host; falling back to %s. League "
                "requests will redirect, which is slower but correct.",
                API_HOST,
            )
            self._resolved_host = API_HOST
            return API_HOST

        league = payload.get("league")
        if not isinstance(league, dict):
            log.info("League export for L=%s had no 'league' object (keys: %s)",
                     self.settings.league_id, sorted(payload))
            self._resolved_host = API_HOST
            return API_HOST

        host = as_str(league.get("host"))
        if not host:
            # Observed in the wild: MFL returns the league, including its name,
            # but omits 'host' from the JSON export. Since the host is only a
            # performance hint, degrade to the api host rather than failing.
            log.info(
                "League %r (L=%s) returned no host; using %s instead. League "
                "requests will redirect, which is slower but correct.",
                as_str(league.get("name")) or "unnamed",
                self.settings.league_id,
                API_HOST,
            )
            # Cached so the probe happens once per client, not per request.
            self._resolved_host = API_HOST
            return API_HOST

        self._resolved_host = host.lower()
        return self._resolved_host

    async def my_leagues(self) -> list[dict[str, Any]]:
        """Every league the authenticated user belongs to, with its host.

        Useful for discovering a league id or host without guessing, and unlike
        ``export?TYPE=league`` this returns a host for leagues whose JSON export
        omits one.
        """
        payload = await self._send("export", {"TYPE": "myleagues"})
        return as_list(payload.get("myleagues", {}).get("league"))

    # -- read: reference data ---------------------------------------------

    async def status(self) -> dict[str, Any]:
        """Current/lineup week for the season.

        This is the one endpoint that needs no credentials at all: it lives on
        the api host rather than a league host and returns the same week
        numbers for everyone. Useful as a connectivity check before you have
        configured anything.
        """
        payload = await self._send("mfl_status", {}, cookie_ok=False)
        status = payload.get("mfl_status", {})
        return {
            "year": as_str(status.get("year")),
            "current_week": as_int(status.get("weeks", {}).get("CurrentWeek")),
            "upcoming_week": as_int(status.get("weeks", {}).get("UpcomingWeek")),
            "lineup_week": as_int(status.get("weeks", {}).get("LineupWeek")),
            "completed_week": as_int(status.get("weeks", {}).get("CompletedWeek")),
            "live_scoring_week": as_int(status.get("weeks", {}).get("LiveScoringWeek")),
            # Reported here rather than encoded into the User-Agent, because
            # the User-Agent has to stay byte-identical to the one registered
            # with MFL for the higher request limit to apply.
            "client": {
                "name": CLIENT_NAME,
                "version": __version__,
                "user_agent": self.settings.user_agent,
                "registered": self.settings.user_agent == DEFAULT_USER_AGENT,
            },
        }

    async def league(self) -> dict[str, Any]:
        self.settings.require_read()
        payload = await self.export("league")
        return payload.get("league", {})

    async def rules(self) -> dict[str, Any]:
        self.settings.require_read()
        payload = await self.export("rules")
        return payload.get("rules", {})

    async def league_settings(self) -> LeagueSettings:
        """League configuration, including the lineup slot definitions."""
        raw = await self.league()
        await self.ensure_league_host()
        franchises = as_list(raw.get("franchises", {}).get("franchise"))
        return LeagueSettings(
            name=as_str(raw.get("name")),
            season=as_str(raw.get("seasonYear")),
            host=as_str(raw.get("host"), API_HOST),
            roster_positions=_counts(parse_slot_spec(as_str(raw.get("roster_positions")))),
            starter_slots=parse_slot_spec(as_str(raw.get("starters"))),
            roster_limits=_counts(parse_slot_spec(as_str(raw.get("roster_limits")))),
            ir_slots=as_int(raw.get("franchise", {}).get("ir", 0))
            or _ir_slots(raw),
            taxi_slots=_taxi_slots(raw),
            divisions=bool(raw.get("divisions")),
            current_week=as_int(raw.get("currentWeek")),
            last_week=as_int(raw.get("lastWeek")),
            final_week=as_int(raw.get("finalWeek")),
            franchise_count=len(franchises),
        )

    async def rosters(self) -> list[dict[str, Any]]:
        """Raw roster export for every franchise."""
        self.settings.require_read()
        payload = await self.export("rosters")
        return as_list(payload.get("rosters", {}).get("franchise"))

    async def my_franchise_id(self) -> str:
        """Identify which franchise the authenticated user owns.

        Resolved from the league export, which exposes the current user's
        franchise id, and falls back to the ``abilities`` export which is
        always scoped to the calling franchise.
        """
        if self.settings.franchise_id:
            return norm_franchise_id(self.settings.franchise_id)
        raw = await self.league()
        candidates: list[str] = []

        user = raw.get("user", {})
        if isinstance(user, list):
            candidates.extend(as_str(u.get("franchise_id")) for u in user)
        else:
            candidates.append(as_str(user.get("franchise_id")))
            for u in as_list(user.get("user")):
                candidates.append(as_str(u.get("franchise_id")))

        for candidate in candidates:
            if candidate and candidate != "0" and candidate != "0000":
                return norm_franchise_id(candidate)

        payload = await self.export("abilities")
        fid = as_str(payload.get("abilities", {}).get("franchise_id"))
        if fid:
            return norm_franchise_id(fid)
        raise MFLError(
            "Could not determine your franchise id. Set MFL_FRANCHISE_ID explicitly."
        )

    async def my_franchise(self) -> dict[str, Any]:
        fid = await self.my_franchise_id()
        for fr in await self.rosters():
            if as_str(fr.get("id")).zfill(4) == fid:
                return fr
        raise MFLError(f"Franchise {fid} not found in roster export")

    async def franchises(self) -> list[Franchise]:
        raw = await self.league()
        mine = set()
        try:
            mine.add(await self.my_franchise_id())
        except MFLError:
            pass
        out = []
        for fr in as_list(raw.get("franchises", {}).get("franchise")):
            fid = norm_franchise_id(fr.get("id"))
            out.append(
                Franchise(
                    franchise_id=fid,
                    name=as_str(fr.get("name"), fid),
                    owner_name=as_str(fr.get("owner_name")),
                    is_mine=fid in mine,
                )
            )
        return out

    async def players(
        self,
        *,
        details: bool = False,
        ids: Sequence[str] | None = None,
        force_refresh: bool = False,
    ) -> list[Player]:
        """The player database. MFL updates it at most once a day, so cache it."""
        cache_key = "players"
        if not force_refresh and ids is None:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return [Player.from_json(raw) for raw in cached]

        params: dict[str, Any] = {}
        if details:
            params["DETAILS"] = 1
        if ids:
            params["PLAYERS"] = ",".join(norm_player_id(i) for i in ids)
        payload = await self.export("players", **params)
        raw = as_list(payload.get("players", {}).get("player"))
        players = [Player.from_json(p) for p in raw]
        if ids is None and not force_refresh:
            self._write_cache(cache_key, [_player_to_raw(p) for p in players])
        return players

    async def free_agents(self) -> list[str]:
        """Player ids currently available as free agents in this league."""
        self.settings.require_read()
        payload = await self.export("freeAgents")
        ids = [
            norm_player_id(p.get("id"))
            for p in as_list(payload.get("free_agents", {}).get("player"))
        ]
        return [pid for pid in ids if pid]

    async def injuries(self, week: int | None = None) -> dict[str, Any]:
        """The NFL injury report. ``week`` defaults to the latest available."""
        self.settings.require_read()
        params: dict[str, Any] = {}
        if week is not None:
            params["W"] = week
        payload = await self.export("injuries", **params)
        report = payload.get("injuries", {})
        return {
            "week": as_str(report.get("week")),
            "timestamp": as_float(report.get("timestamp")),
            "entries": as_list(report.get("injury")),
        }

    async def projected_scores(
        self,
        *,
        week: int | None = None,
        players: Sequence[str] | None = None,
        position: str | None = None,
        count: int | None = None,
        free_agents_only: bool = False,
    ) -> dict[str, float]:
        """League-scored projections for the given (or upcoming) week."""
        params: dict[str, Any] = {}
        if week is not None:
            params["W"] = week
        if players:
            params["PLAYERS"] = ",".join(norm_player_id(p) for p in players)
        if position:
            params["POSITION"] = position.upper()
        if count:
            params["COUNT"] = count
        if free_agents_only:
            params["STATUS"] = "freeagent"
        payload = await self.export("projectedScores", **params)
        return {
            norm_player_id(s.get("id")): as_float(s.get("score"))
            for s in as_list(payload.get("projectedScores", {}).get("player_score"))
        }

    async def who_should_i_start(
        self,
        *,
        week: int | None = None,
        franchise_id: str | None = None,
        players: Sequence[str] | None = None,
    ) -> dict[str, float]:
        """MFL's site-wide 'Who Should I Start?' win percentage, 0-100."""
        params: dict[str, Any] = {}
        if week is not None:
            params["WEEK"] = week
        if franchise_id is not None:
            params["FRANCHISE"] = norm_franchise_id(franchise_id)
        elif players:
            params["PLAYERS"] = ",".join(norm_player_id(p) for p in players)
        if franchise_id is None and players is None:
            fid = await self.my_franchise_id()
            params["FRANCHISE"] = fid
        payload = await self.export("whoShouldIStart", **params)
        return {
            norm_player_id(s.get("id")): as_float(s.get("score"))
            for s in as_list(payload.get("whoShouldIStart", {}).get("player_score"))
        }

    async def pending_waivers(self) -> list[dict[str, Any]]:
        payload = await self.export("pendingWaivers")
        container = payload.get("pendingWaivers", {})
        return as_list(container.get("pending_waiver")) or as_list(
            container.get("waiver")
        )

    async def bye_weeks(self, week: int | None = None) -> dict[str, int]:
        """Team -> bye week, so we never start a player on bye by accident."""
        params: dict[str, Any] = {}
        if week is not None:
            params["W"] = week
        payload = await self.export("nflByeWeeks", **params)
        teams = payload.get("nflByeWeeks", payload)
        return {
            as_str(t.get("team")).upper(): as_int(t.get("bye"))
            for t in as_list(teams.get("bye")) if as_str(t.get("team"))
        }

    # -- read: composed views ---------------------------------------------

    async def roster_entries(self) -> list[RosterEntry]:
        """Every rostered player in the league, flattened."""
        out: list[RosterEntry] = []
        for franchise in await self.rosters():
            fid = norm_franchise_id(franchise.get("id"))
            for entry in as_list(franchise.get("roster", {}).get("player")):
                out.append(
                    RosterEntry(
                        player_id=norm_player_id(entry.get("id")),
                        franchise_id=fid,
                        status=as_str(entry.get("status"), "R").upper(),
                        salary=as_str(entry.get("salary")),
                        contract=as_str(entry.get("contract")),
                    )
                )
        return out

    async def my_roster(self) -> list[RosterPlayer]:
        """My active roster joined with player metadata, projections and health."""
        franchise = await self.my_franchise()
        raw_players = as_list(franchise.get("roster", {}).get("player"))
        entries = [
            RosterEntry(
                player_id=norm_player_id(p.get("id")),
                franchise_id=norm_franchise_id(franchise.get("id")),
                status=as_str(p.get("status"), "R").upper(),
                salary=as_str(p.get("salary")),
            )
            for p in raw_players
        ]
        return await self.hydrate(entries)

    async def hydrate(self, entries: Sequence[RosterEntry]) -> list[RosterPlayer]:
        """Join roster entries with player data, injury status and projections."""
        ids = [e.player_id for e in entries]
        players = {p.player_id: p for p in await self.players()}
        try:
            report = await self.injuries()
        except MFLError:
            report = {"entries": []}
        injuries = _injury_by_player(report)
        try:
            scores = await self.projected_scores(players=ids)
        except MFLError:
            scores = {}

        out: list[RosterPlayer] = []
        for entry in entries:
            base = players.get(entry.player_id)
            if base is None:
                base = Player(player_id=entry.player_id, name=entry.player_id, position="")
            inj = injuries.get(entry.player_id)
            merged = Player(
                player_id=base.player_id,
                name=base.name,
                position=base.position,
                team=base.team,
                nfl_team=base.nfl_team,
                status=base.status,
                injury_status=(inj or {}).get("status", base.injury_status),
                injury_detail=(inj or {}).get("detail", base.injury_detail),
                bye_week=base.bye_week,
                drafted=base.drafted,
            )
            out.append(RosterPlayer(player=merged, status=entry.status, salary=entry.salary))
        self._last_projections = scores
        return out

    # -- write: all gated --------------------------------------------------

    async def set_lineup(
        self,
        week: int,
        starters: Sequence[str],
        *,
        comments: str | None = None,
        tiebreakers: Sequence[str] | None = None,
        franchise_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a starting lineup for ``week``.

        Uses ``import?TYPE=lineup`` with ``W`` and ``STARTERS`` (a
        comma-separated list of player ids).
        """
        if not starters:
            raise MFLError("A lineup must contain at least one starter")
        params: dict[str, Any] = {
            "W": week,
            "STARTERS": ",".join(norm_player_id(s) for s in starters),
        }
        if comments:
            params["COMMENTS"] = comments
        if tiebreakers:
            params["TIEBREAKERS"] = ",".join(norm_player_id(t) for t in tiebreakers)
        fid = franchise_id or self.settings.franchise_id
        if fid:
            params["FRANCHISE_ID"] = norm_franchise_id(fid)
        return await self._import("lineup", **params)

    async def fcfs_move(
        self,
        *,
        add: str | None = None,
        drop: Sequence[str] | None = None,
        franchise_id: str | None = None,
    ) -> dict[str, Any]:
        """Immediate add/drop via ``import?TYPE=fcfsWaiver``.

        This is the documented MFL mechanism for free-agent acquisitions and
        drops, including dropping several players at once.
        """
        drop = [norm_player_id(d) for d in (drop or []) if d]
        if not add and not drop:
            raise MFLError("fcfsWaiver requires at least one player to add or drop")
        params: dict[str, Any] = {}
        if add:
            params["ADD"] = norm_player_id(add)
        if drop:
            params["DROP"] = ",".join(drop)
        fid = franchise_id or self.settings.franchise_id
        if fid:
            params["FRANCHISE_ID"] = norm_franchise_id(fid)
        return await self._import("fcfsWaiver", **params)

    async def submit_waiver_round(
        self,
        picks: Sequence[tuple[str, str]],
        *,
        round_number: int | None = None,
        replace: bool = True,
        franchise_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit (or clear) a round of waiver claims.

        ``picks`` are ``(player_to_claim, player_to_drop_if_awarded)`` pairs,
        highest priority first. Passing an empty sequence clears the round.
        """
        params: dict[str, Any] = {
            "PICKS": ",".join(
                f"{norm_player_id(add)}_{norm_player_id(drop)}" for add, drop in picks
            ),
            "REPLACE": 1 if replace else 0,
        }
        if round_number is not None:
            params["ROUND"] = round_number
        fid = franchise_id or self.settings.franchise_id
        if fid:
            params["FRANCHISE_ID"] = norm_franchise_id(fid)
        return await self._import("waiverRequest", **params)

    async def set_ir(
        self,
        player: str,
        activate: bool = True,
        *,
        franchise_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a player onto or off injured reserve (``import?TYPE=ir``)."""
        params: dict[str, Any] = {"IR": norm_player_id(player)}
        if not activate:
            params["IR"] = ""
        fid = franchise_id or self.settings.franchise_id
        if fid:
            params["FRANCHISE_ID"] = norm_franchise_id(fid)
        return await self._import("ir", **params)

    # -- cache -------------------------------------------------------------

    def _cache_path(self, key: str) -> Path | None:
        base = self.settings.cache_dir
        if base is None:
            return None
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{key}.json"

    def _read_cache(self, key: str) -> list[dict[str, Any]] | None:
        path = self._cache_path(key)
        if path is None or not path.is_file():
            return None
        try:
            if time.time() - path.stat().st_mtime > self.settings.cache_ttl:
                return None
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, data: Iterable[Mapping[str, Any]]) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        try:
            path.write_text(json.dumps(list(data)))
        except (OSError, TypeError):
            pass


def _error_text(error: Any) -> str:
    """Render MFL's error payload, which is usually ``{"$t": "message"}``.

    MFL signals failures with HTTP 200 and an ``error`` key, so these show up
    as ordinary responses and are the single most common cause of a confusing
    failure. Flatten the XML-converted shape into something readable.
    """
    if isinstance(error, dict):
        parts = [as_str(v) for v in error.values() if as_str(v)]
        if parts:
            return "; ".join(parts)
    if isinstance(error, list):
        return "; ".join(_error_text(item) for item in error)
    text = as_str(error)
    return text or repr(error)


def _extract_cookie(login_body: str) -> str | None:
    """Pull the cookie value out of the login response body.

    MFL answers a successful login with XML like::

        <status cookie_name="MFL_USER_ID" cookie_value="eyJ0eXAi...">MFL</status>

    The two attributes are matched independently rather than as one ordered
    pair, because attribute order in a response is not a contract worth
    depending on, and a mismatch here fails as a confusing "login succeeded
    but no cookie".
    """
    import re

    if not login_body:
        return None
    name = re.search(r"""cookie_name\s*=\s*["']([^"']*)["']""", login_body, re.I)
    value = re.search(r"""cookie_value\s*=\s*["']([^"']*)["']""", login_body, re.I)
    if value and value.group(1):
        # A present but empty cookie_name would mean something unexpected.
        if name and name.group(1) and name.group(1).upper() != "MFL_USER_ID":
            log.debug("Unexpected MFL cookie name %r", name.group(1))
        return value.group(1)
    return None


def _counts(pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pos, _ in pairs:
        out[pos] = out.get(pos, 0) + 1
    return out


def _ir_slots(raw: Mapping[str, Any]) -> int:
    franchise = raw.get("franchise")
    if isinstance(franchise, list) and franchise:
        franchise = franchise[0]
    if isinstance(franchise, dict):
        return as_int(franchise.get("ir"))
    return 0


def _taxi_slots(raw: Mapping[str, Any]) -> int:
    franchise = raw.get("franchise")
    if isinstance(franchise, list) and franchise:
        franchise = franchise[0]
    if isinstance(franchise, dict):
        return as_int(franchise.get("taxi_squad"))
    return 0


def _injury_by_player(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in report.get("entries", []):
        pid = norm_player_id(entry.get("player_id") or entry.get("id"))
        if not pid:
            continue
        out[pid] = {
            "status": as_str(entry.get("status")),
            "detail": as_str(entry.get("details") or entry.get("injury_detail")),
        }
    return out


def _player_to_raw(player: Player) -> dict[str, Any]:
    return {
        "id": player.player_id,
        "name": player.name,
        "pos": player.position,
        "team": player.team,
        "nfl_team": player.nfl_team,
        "status": player.status,
        "injury_status": player.injury_status,
        "injury_detail": player.injury_detail,
        "bye_week": player.bye_week,
        "draft_pick": player.drafted,
    }
