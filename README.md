# mfl-fantasy-manager

A Python MCP server and decision engine for [MyFantasyLeague.com](https://www.myfantasyleague.com/).
It reads your league, scores your roster against your league's own scoring rules, and
recommends (and, once you explicitly enable it, submits) free-agent moves, lineups and
injured-reserve moves.

**It is read-only by default and cannot change anything until you deliberately turn
writes on.** See [Safety](#safety).

---

## What it actually does

| Job | When (America/Phoenix) | What it does |
|---|---|---|
| Waivers | Wed 6:00 PM | Ranks free-agent add/drop moves; executes only the single best one |
| Lineup | Thu 2:00 PM | Compares your submitted lineup to the optimiser and submits a better one |
| Injuries | Sun 9:00 AM | Flags out/questionable/bye players; suggests and can make IR moves |

Arizona does not observe DST, so the schedules above are fixed year round.

## How decisions are made

For each player the engine builds one value from four signals:

1. **Projected points** from MFL's `projectedScores` export, which is already scored with
   *your* league's scoring settings. This dominates.
2. **Who Should I Start** win percentage as a tie-breaker among similar projections.
3. **Availability**: `Out` discounts a player heavily, `Questionable` moderately.
4. **Positional depth** for waiver targets — the same running back is worth less to a team
   that already has three starting-quality ones.

The lineup optimiser reads the league's *own* `starters` string (e.g.
`"QB,1,RB,2,WR,3,FLX,1"`) rather than assuming a format, so it handles 4-slot leagues,
2-RB leagues, flex, and IDP/dynasty variants without changes. It fills concrete slots
greedily, fills flex from the best remaining skill player, then runs a pairwise-swap local
search so a greedy mistake cannot survive.

Every recommendation comes with a plain-English justification, e.g.:

```
1. +Z1 Free Agent WR (WR) for -E. Receiver (WR) [+11.1]
   Z1 Free Agent WR projects 15.5 (proj 15.5, wsis 70, rank 5), replacing E. Receiver,
   who is Out (Concussion) and worth little while injured; 2 surplus already at WR
```

## API notes

Everything below was verified against MFL's live Request Reference page
(`https://api.myfantasyleague.com/2026/api_info`) rather than assumed:

- **Auth is two different things.** Either an `MFL_USER_ID` cookie from the `login`
  endpoint, or the `APIKEY` query parameter. `APIKEY` works for **export only**, is
  **owner-level only**, and **cannot be used for imports**. So writes always require a
  username and password. MFL has no personal access token.
- **Free-agent add/drop is a documented import**: `TYPE=fcfsWaiver` with `ADD` and `DROP`
  parameters. No hand-rolled XML transaction payload is needed.
- **Waiver claims** are `TYPE=waiverRequest` with `PICKS` as `claim_drop` pairs,
  comma separated, processed in priority order.
- **Lineups** are `TYPE=lineup` with `W` and `STARTERS`.
- `mfl_status` is public and lives at `/fflnetdynamic<year>/mfl_status.json` — not
  `/<year>/mfl_status`. Useful as a credential-free connectivity check.
- Requests without a league parameter must go to `api.myfantasyleague.com`; everything
  else goes to the league's own `wwwNN` host, which the client resolves once.
- MFL throttles aggressive clients with **HTTP 429 and tells you not to retry**. The
  client waits at least one second between calls and treats 429 as fatal.
- Player and franchise ids are **strings with leading zeros** (`"0531"`, `"0001"`). The
  client normalises these; anything that treats them as integers will break.

Only `mfl_status` has been exercised against the live API in this repo. Everything else
is covered by tests against recorded fixtures, because the league endpoints need your
credentials.

## Setup

```bash
git clone https://github.com/mwenzel3-eng/mfl-fantasy-manager.git
cd mfl-fantasy-manager
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env      # then fill it in
```

Find your league id in your league URL: `myfantasyleague.com/2026/index?L=12345`.

Get an API key by logging into your league and going to **Help → Developer's API**.

```bash
python -m mcp_server.cli status      # connectivity + week numbers + league rules
python -m mcp_server.cli roster      # your roster with projections and injuries
python -m mcp_server.cli waivers     # ranked add/drop recommendations
python -m mcp_server.cli lineup      # optimised lineup
python -m mcp_server.cli injuries    # injury and bye report
```

Add `--json` to any command for machine-readable output.

## Safety

Writes require **three** independent conditions. Any one missing and every write path
raises `WritesDisabledError` before a request is made:

1. `MFL_ENABLE_WRITES=1` — the hard opt-in.
2. `MFL_DRY_RUN=0` — the softer override, which must be actively turned off.
3. `MFL_USERNAME` + `MFL_PASSWORD` present, because `APIKEY` cannot authorise imports.

On top of that:

- The three MCP tools that mutate state (`apply_lineup`, `apply_waiver_move`,
  `submit_waiver_claims`) additionally require an explicit `confirmed=True` argument.
- Lineup changes under `MIN_LINEUP_GAIN` (0.5 projected points) are refused unless
  `--force` is passed, and `--force` never bypasses the three conditions above.
- The Wednesday job executes at most **one** move per run, not a shopping spree.
- Secret fields are excluded from `Settings.__repr__`, so configuration can be printed
  in a traceback without leaking your password or API key.

**Recommended rollout:** run with `MFL_DRY_RUN=1` for at least three or four weeks.
Read the recommendations, compare them to your own judgement, and only then set
`MFL_DRY_RUN=0` with `MFL_ENABLE_WRITES=1`.

## MCP server

```bash
python -m mcp_server.server
```

Read-only tools: `league_status`, `my_roster`, `injury_report`, `recommend_lineup`,
`recommend_waivers`, `notify`.
Write tools: `apply_lineup`, `apply_waiver_move`, `submit_waiver_claims`.

## Scheduling

Four workflows in `.github/workflows/`. They use the `timezone` field on `on.schedule`
(IANA name), which GitHub Actions shipped in March 2026, so no manual UTC conversion is
needed.

Configure under **Settings → Secrets and variables → Actions**:

*Secrets:* `MFL_LEAGUE_ID`, `MFL_APIKEY`, `MFL_USERNAME`, `MFL_PASSWORD`, and
`TWILIO_SID` / `TWILIO_TOKEN` / `TWILIO_FROM` / `SMS_TO` if you want real SMS.

*Variables:* `MFL_YEAR`, `MFL_DRY_RUN`, `MFL_ENABLE_WRITES`, `SMS_PROVIDER`.

Every workflow also has a `workflow_dispatch` trigger so you can run any job on demand,
defaulting to a dry run.

Note that GitHub disables scheduled workflows in public repos after 60 days of
inactivity, which sends a reminder email — check your Actions tab occasionally.

## Notifications

`SMS_PROVIDER` accepts `log` (default; writes the message to the log and stdout),
`none`, or `twilio`. Twilio code only runs if the provider is selected *and* all four
credentials are present; otherwise it logs a warning and falls back to `log`, so a
misconfigured setup never silently swallows reports.

## Tests

```bash
python -m pytest -q
```

92 tests, fully offline, no credentials required. Fixtures in `tests/fixtures/` are
recorded MFL response shapes covering the shapes the client actually has to handle,
including the dict-or-list-or-missing ambiguity MFL's JSON is full of.

## Layout

```
mcp_server/
  config.py          settings, env loading, the write-switch definitions
  errors.py          shared exception types
  mfl_api.py         the API client; every import goes through one gated method
  models.py          domain models and the dict-or-list normalisers
  fantasy_engine.py  player pool, value scoring, drop ranking
  lineup.py          lineup construction and optimisation
  waivers.py         add/drop recommendations and waiver claims
  injuries.py        availability report and IR suggestions
  safety.py          the write guard, in one place
  context.py         fetches one consistent snapshot of everything
  notify.py          SMS providers (log / none / twilio)
  server.py          MCP tools
  cli.py             command line entry point
jobs/                the three scheduled jobs
tests/               offline test suite and fixtures
```

## Limitations

- **Projections come from MFL**, sourced from FantasySharks. They are only as good as
  that projection set; this project adds injury and depth adjustments on top, not a
  model of its own.
- **The optimiser is greedy plus local search**, not an exact solver. It is guaranteed to
  be a local optimum over single swaps, which is sufficient in practice, but it is not
  provably the best possible lineup.
- **No trade logic.** Trades need a different question answered (what is this player
  worth to *that* team, with their roster) and are not implemented.
- **Dynasty/K dynasty rookie slots are not modelled** beyond what the league's own
  `starters` string expresses.
- MFL's API is undocumented-stable, not documented-stable: it can change or disappear at
  any time, and MFL explicitly does not support past seasons.

## Licence

MIT. Not affiliated with, endorsed by, or licensed by the NFL or the MFL.
