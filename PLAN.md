# WoW Forever Gold Planner — Project Plan

_Status 2026-11-11: Phases 1–4 done (see README). WoW: Forever launched 2026-11-04 (Classic+ / vanilla-based, level 60 cap, new Skyborne race, all original professions with 600–880 new recipes, "Scarce" gathering mats, profession camps)._

## 1. Goal

Given a character profile — **class, level, race/faction, professions + skill, gear/spec tier, region/ruleset, time available** — recommend the **currently most gold-efficient activity**, ranked by expected gold/hour, with reasoning, risk and market-saturation notes.

## 2. Research findings: data sources

| Need | Source | Status | Notes |
|---|---|---|---|
| **AH prices (live)** | Blizzard Battle.net Game Data API, namespace `dynamic-forever-{region}` | ✅ Works since ~Nov 6, **undocumented** | `/data/wow/ruleset/index` → `/data/wow/ruleset/{id}/auctions/` (index; some report `/auctions/index`) → `/data/wow/ruleset/{id}/auctions/{houseId}` and `.../{houseId}/commodities`. 15 houses (5 × Alliance/Horde/Blackwater). Same houses appear under every ruleset — **dedupe, don't sum**. Responses 5–9 MB, use gzip + `Last-Modified`. Prices in copper (`buyout` / `unit_price`). |
| Realms / connected realms | Blizzard API | ❌ 404 | Forever is realmless/sharded; key everything by region + house ID. |
| **Item metadata** | Blizzard `static-forever-{region}` (or versioned `static-1.60.1_70399-forever-{region}`) | ⚠️ Partial | ~43% of AH item IDs 404 (e.g. 720). Need fallback: Wowhead (Forever support since 2026-09-17), WarcraftDB/Lexicon, or in-game addon export. |
| Character profile / professions | `profile-forever-{region}`, `/profile/wow/character/{name}-{surname}` (+`/professions`, `/equipment`) | ✅ Works (lvl 10+, logged out) | Lets us auto-fill the planner's input profile. |
| **Recipes / professions** | Blizzard static profession endpoints | ❓ Untested for Forever | Fallback: Wowhead / MasterCraft-style scrape; curated YAML for new Forever recipes. |
| **Creature drops / loot tables** | No API. Wowhead, WarcraftDB, wowtbc.gg loot tables | ⚠️ Incomplete, many "not yet discovered" | Treat drop data as curated + community-scraped + self-measured. |
| Gold/hour of farms | Nobody has verified Forever numbers yet | ❌ | Must be **estimated, then calibrated** from the user's own session logs. |

Key implication: prices are automatable, but **yields (drops/hour, nodes/hour) are the weak link** and the system must be designed to learn them.

Sources: [Blizzard forum – Forever API what works](https://us.forums.blizzard.com/en/blizzard/t/wow-forever-api-what-works-so-far-plus-a-ts-client/60334), [Blizzard forum – Forever namespace](https://us.forums.blizzard.com/en/blizzard/t/world-of-warcraft-forever-api-namespace/59774), [goldbook issue #67](https://github.com/jkc-be/goldbook/issues/67), [Game Data API docs](https://community.developer.battle.net/documentation/world-of-warcraft/game-data-apis), [Warcraft Tavern – Professions](https://www.warcrafttavern.com/forever/guides/professions/), [Icy Veins – Scarce materials](https://www.icy-veins.com/wow-forever/news/every-gathering-node-in-wow-forever-can-drop-a-scarce-material-youll-want/), [wowtbc.gg loot tables](https://wowtbc.gg/warcraftforever/loot-tables/dungeons/shadowfang-keep/), [Lexicon client](https://www.warcrafttavern.com/forever/news/help-build-a-better-database-for-wow-forever-with-the-lexicon-client/).

## 3. Architecture decision: deterministic Python core + optional agent layer

**Recommendation: build a pure-Python core first, and expose it as tools to an optional Claude agent later.**

- The planning itself (prices × yields − costs, constraints, ranking) is arithmetic and must be reproducible, testable and cheap → plain Python.
- An LLM is genuinely useful for the *fuzzy* parts: turning guide pages / patch notes into structured method definitions, explaining recommendations, and conversational "what should I do tonight?" queries. That layer calls the same Python functions as tools — never invents numbers.
- So: `core` is usable alone via CLI; `agent` is a thin add-on (Claude API tool-use, model `claude-sonnet-5-5` or `claude-opus-5-5`).

```
             ┌───────────── ingest ─────────────┐
 Blizzard AH API ──► snapshots ─┐               │
 Blizzard static/profile ───────┤               │
 Wowhead/WarcraftDB scrape ─────┼──► SQLite ◄── curated YAML (methods, routes, recipes, drop rates)
 Session logs (user, addon) ────┘      │
                                       ▼
                     pricing (robust price per item, liquidity)
                                       ▼
            methods engine (gather / farm / craft / vendor / dungeon)
                                       ▼
                planner (filter by profile → score → rank → explain)
                         │                          │
                       CLI (typer)            agent (Claude tools)
```

## 4. Domain model

- **Item**: id, name, quality, vendor sell/buy price, source flags (scarce, BoP).
- **PriceSnapshot**: region, house_id, faction, item_id, ts, min/p10/median buyout, quantity listed, num auctions.
- **MarketStats** (derived): robust price (e.g. p10–p25 of listings, trimmed), 7-day trend, depth, est. sell-through/day (from listing disappearance between snapshots).
- **Recipe**: profession, skill req, reagents, output (+qty), source (trainer/vendor/drop), Forever-new flag.
- **Method** (the core abstraction) — a YAML-defined activity:
  ```yaml
  id: herb_felwood_route
  kind: gather            # gather | farm_mobs | craft | dungeon | vendor_flip | fishing | skinning
  requirements: {level: 52, professions: {herbalism: 270}, faction: any}
  class_modifiers: {druid: 1.15, rogue: 1.05}   # speed/safety multipliers
  yields_per_hour:        # expected items/hr, with uncertainty
    - {item: 13463, qty: 18, sd: 6}   # Dreamfoil
    - {item: 13465, qty: 9,  sd: 4}
  costs_per_hour: {repairs: 0.5g}
  risk: {pvp: medium, competition: high}
  confidence: estimate    # estimate | community | measured
  ```
- **Character profile**: class, race/faction, level, professions{name: skill}, gear tier (fresh/leveling/pre-raid), region, house, hours available, preferences (solo, no-PvP).
- **SessionLog**: method_id, duration, items gained → calibrates `yields_per_hour` (Bayesian update of estimate toward measured).

## 5. Planner logic

1. **Filter** methods by hard requirements (level, profession skill, faction, solo-ability for class/gear).
2. **Value** each method: `Σ qty × sell_value(item)` where sell_value = min(AH robust price × (1 − 5% cut), …) vs vendor price, whichever is higher; crafting = output value − reagent cost (buy-or-farm, recursive).
3. **Liquidity cap**: don't value more units than the market absorbs in the planning horizon (sell-through/day); excess falls back toward vendor price. This prevents "farm 500 Black Lotus" nonsense.
4. **Adjust** by class modifiers, risk, confidence (penalize pure estimates slightly).
5. **Rank** by expected gold/hour, show range (p10–p90 from yield sd), plus "best long-term" options (e.g. leveling a profession that unlocks a high-value recipe = investment with payback time).
6. **Explain**: top items driving value, price trend, saturation warning.

## 6. Project layout

```
wow-forever/
  pyproject.toml            # uv, Python 3.12+, httpx, pydantic, sqlmodel/sqlite, typer, pyyaml, rich
  .env.example              # BLIZZARD_CLIENT_ID / SECRET, ANTHROPIC_API_KEY (agent only)
  src/wowgold/
    config.py
    blizzard/               # OAuth client-credentials, namespaces, auctions, items, profile; gzip, Last-Modified, retries
    ingest/                 # snapshot job, item backfill, scrapers (wowhead, loot tables)
    db/                     # models + migrations
    market/                 # robust pricing, trends, sell-through estimation
    methods/                # YAML loader + valuation per kind
    planner/                # filter/score/rank/explain
    cli.py                  # wowgold snapshot | prices | plan | log-session
    agent/                  # (phase 4) Claude tool-use wrapper over planner/market functions
  data/methods/*.yaml       # curated methods
  data/recipes/*.yaml
  tests/                    # fixtures with recorded API responses
```

## 7. Phases

1. ✅ **Foundation & API probe** — project skeleton, Blizzard client, a probe command that hits all candidate Forever endpoints and records what works (the API is undocumented and shifting). Store raw auction snapshots.
2. ✅ **Market layer** — scheduled snapshotting (hourly, Last-Modified-aware), dedupe houses across rulesets, house→faction mapping (manual config, since API doesn't label it), robust prices, item-name backfill with fallbacks. CLI: `wowgold prices "Black Lotus"`.
3. ✅ **Methods + planner MVP** — ~20 curated methods (key herb/ore routes, skinning, common farm spots, a handful of crafts incl. new Forever recipes and Scarce mats), profile input, ranked output. CLI: `wowgold plan --class mage --level 60 --prof herbalism:300,alchemy:300`.
4. ✅ **Calibration loop** — `log-session` command (and later a tiny in-game addon writing SavedVariables loot logs) to replace estimates with measurements.
5. **Agent layer** — Claude tool-use: tools `get_prices`, `list_methods`, `plan`, `profile_from_armory`, plus a research tool that drafts new method YAML from a guide URL for human review.
6. **Later** — web UI/dashboard, crafting-tree optimizer (buy vs farm reagents), alerts for price spikes.

### Findings from live runs (EU, 2026-11-11)

- All 15 houses appear under every ruleset with identical data → each is fetched once.
- With a valid locale, house names include the faction ("Horde Auction House"), so the faction is set automatically.
- Houses form 5 groups of Alliance/Horde/Blackwater: 110/113/116 and 111/114/117 are large (~170–206k auctions per faction house); 112/115/118 are empty; 119–125 are small. Which group is which realm cluster still needs confirming in-game.
- Regular auctions all have quantity 1 (buyout = unit price); commodities use `unit_price`.
- ~43% of auctioned item IDs 404 on `static-forever`; the Classic Era namespace `static-classic1x-{region}` covers almost all of them. ~2% of AH value is still unnamed (mostly new Forever item IDs >200000).
- Snapshot `Last-Modified` did not change for more than 2 hours on launch week, so Blizzard's refresh cadence is slower than hourly or irregular. Sales estimates only become meaningful once the collector has run for a while.

### Phase 3 notes

- 30 curated methods (13 gathering, 8 farming, 2 fishing, 8 crafts), all item IDs verified against Blizzard item data.
- Missing so far: Forever-specific content (Scarce materials, new recipes, camps), dungeon farms, enchanting/disenchanting, and cooking.
- Demand uses a listings-based heuristic until ~12h of sales history exists; worth revisiting the 25%/day guess once real sales data is available.

### Phase 4 notes

- Addon v0.2 records automatically: segments by zone/idle gap, loot attributed to its source (NPC ID / game-object ID via `GetLootSourceInfo`), gathering spell that opened it, kills via `PARTY_KILL`. The importer splits segments into activities, matches them to methods by zone + activity + item overlap, auto-applies only clear cases, and can draft new methods from unmatched segments.
- Methods now have `zones` (English zone names, matched against `GetZoneText`). **Non-English clients won't match zones yet**; the addon also records the uiMapID for a future fix.
- Sessions come from three sources: the WowGoldLogger addon (imported from its SavedVariables), a CLI timer, or manual entry. All feed a Gamma–Poisson update of each yield, normalized by class/mount/gear multipliers.
- The addon is tested in embedded Lua 5.1 (lupa) with a stubbed WoW API, but **not yet in the real Forever client**. The TOC interface number (`11507, 16001`) is a guess. Loot is parsed from the localized `LOOT_ITEM_SELF*` and money strings.
- Not handled yet: overdispersion (route competition makes real variance higher than Poisson), and gathering-only vs. kill time in farms.

## 8. Risks / open questions

- Forever API is **undocumented** and changed daily during launch week → keep the client defensive and the probe command around.
- Which house ID = which faction/ruleset? Needs manual mapping on first run (compare against in-game prices).
- Drop rates/yields are largely unknown for Forever → the calibration loop is essential, not optional.
- Scraping Wowhead etc.: respect ToS/robots, cache aggressively, prefer their data exports if offered.
- Scope: this is a planning/analysis tool only — no automation of gameplay (that would violate Blizzard ToS).

## 9. Decisions needed from you

1. Region and faction you play (US/EU; Alliance/Horde) — sets defaults and the first house mapping.
2. Do you have / will you create a Battle.net developer client (free, develop.battle.net) for API keys?
3. Agent layer: include from the start, or after the core works (recommended: after)?
