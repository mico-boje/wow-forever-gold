# wowgold

Gold-making planner for World of Warcraft: Forever. Design and roadmap: [PLAN.md](PLAN.md).

## Setup

```bash
uv sync
cp .env.example .env   # add BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET
```

Create API credentials at https://develop.battle.net/access/clients. Use a supported locale
(`en_GB`, `en_US`, `de_DE`, …) — an unsupported one makes Blizzard return empty names.

## Commands

| Command | What it does |
|---|---|
| `wowgold probe` | Checks which (undocumented) Forever API endpoints currently work; report in `data/probes/`. |
| `wowgold collect` | Every 60 min: snapshot all auction houses → ingest prices → sync item names → prune. `--once` for one cycle. |
| `wowgold snapshot` | Download raw auction house data only (deduplicated across rulesets). |
| `wowgold ingest` | Parse stored snapshots into per-item price statistics. |
| `wowgold items sync` | Fetch names/vendor prices (Forever static API, falling back to Classic Era). |
| `wowgold prices "Dreamfoil" -f horde` | Market/min/percentiles, quantity, 24h sales estimate, 24h/7d averages per house. |
| `wowgold plan -c druid -l 60 -f horde -p herbalism:300,alchemy:300` | Rank gold-making methods for a character at current prices (see below). |
| `wowgold profile set -c druid -l 60 -f horde -p herbalism:300` | Save your character; `plan` and `session` use it when flags are omitted. |
| `wowgold session start/stop/log/import` | Record real farming sessions to calibrate yields (see below). |
| `wowgold methods show herb_felwood` | Estimated vs measured vs calibrated yields for a method. |
| `wowgold methods list` / `methods check` | Show curated methods; verify their item IDs against Blizzard's item data. |
| `wowgold houses list` | Houses with faction and listing counts. `houses set-faction` to override. |
| `wowgold prune` | Delete old raw files (keeps 2 per house) and roll hourly stats older than 14 days into daily rows. |

Set `WOWGOLD_HOUSES=[113]` in `.env` to limit `prices` to the house(s) you trade on.

Names for items the API doesn't know can be added to `data/item_overrides.csv`
(`item_id,name,quality`).

### Running the collector in the background

The simplest option is a terminal tab running `uv run wowgold collect`. For an unattended setup, a
cron entry such as:

```
5 * * * * cd /path/to/wow-forever && /path/to/uv run wowgold collect --once >> data/collect.log 2>&1
```

## Planning

`wowgold plan` filters the curated methods in `data/methods/*.yaml` by your level, professions,
class, faction and gear, values each one at the latest AH prices, and ranks them by expected
gold per hour over a session.

- **Selling is capped by demand.** Per item, the market is assumed to absorb
  `daily demand × --sell-days × --share` units; anything beyond that is valued at the vendor price.
  Daily demand comes from measured sales once the collector has ~12h of history, and from
  25% of current listings before that.
- **AH sales are net of the 5% cut**, and never valued below the vendor price.
- **Throughput modifiers:** per-class multipliers from the method file, 0.75× travel before level 40
  (no mount), 1.15× with `--epic-mount`, and +10% per gear tier above what a farm requires.
- **Crafts** are valued as output price minus reagent cost (vendor reagents at vendor price, the rest
  at AH market price), limited by output demand and by reagents listed on the AH.
- **Range** is the 10th–90th percentile from each method's yield uncertainty.
- **Worth working towards** lists methods you can't do yet only because of level or profession skill
  that would beat your current best.

The house defaults to `WOWGOLD_HOUSES`, otherwise the busiest house of your faction; override with
`--house`. Yields in the method files are estimates from Classic Era experience until they're
measured in Forever (Phase 4).

### Adding a method

Append to a file in `data/methods/` and run `wowgold methods check`:

```yaml
- id: herb_felwood
  name: Felwood herbs
  kind: gather            # gather | farm | fishing | craft
  zone: Felwood
  requirements: {level: 52, professions: {herbalism: 285}}
  yields:                 # per hour; crafts use reagents/outputs per craft instead
    - {item: 13463, name: Dreamfoil, qty: 8, sd: 3}
  vendor_copper_per_hour: 0
  class_modifiers: {druid: 1.15}
  risk: [pvp, competition]
```

## Measuring real yields

Method yields start as estimates. Every session you log pulls them towards what you actually get
in Forever. `wowgold methods show <id>` shows the comparison, and `plan` uses the calibrated numbers
automatically.

Save your character once with `wowgold profile set ...`. Then use any of these:

**In-game addon (recommended): records automatically.** Copy `addon/WowGoldLogger` into the
Forever client's `Interface/AddOns` folder. If it's listed as out of date, tick "Load out of date
AddOns": the Forever interface version wasn't known when this was written. Then just play.

The addon records, per stretch of play in one zone (a *segment*):
- what you looted and **where it came from**: which NPC, or which herb node, ore vein or fishing
  bobber;
- which gathering spell opened the loot (Herb Gathering, Mining, Skinning, Fishing);
- what you killed, and the coins you looted.

A segment ends when you change zone or after 10 minutes without loot or kills. Active time is the
time between loot/kill events, each gap capped at 5 minutes, so AFK time doesn't count.

```
/wgl status                 what the current segment has recorded
/wgl list                   recently finished segments
/wgl pause  /  /wgl resume  stop recording entirely (e.g. while questing)
/wgl start herb_felwood     optional: label what you're doing, which skips auto-matching
/wgl stop                   end the label
```

After a `/reload` or logout, import:

```bash
uv run wowgold session import "/path/to/World of Warcraft/<forever folder>/WTF/Account/<ACCOUNT>/SavedVariables/WowGoldLogger.lua"
```

On import, each segment is split into activities (herbalism, mining, skinning, fishing, killing).
Each activity is then matched to the methods for that activity in that zone, by how much of the loot
is the method's items. A beast's own loot counts towards the skinning route when you skinned it.

- **Applied automatically:** one clear activity (≥70% of events), and one method covering ≥50% of
  the loot and clearly ahead of the next candidate.
- **Review:** mixed segments (e.g. herbing while killing satyrs) are split by events per activity.
  Check them with `wowgold segments show <#>` and apply with `segments accept <#>`.
- **Unmatched:** no method fits. `wowgold segments draft <#>` prints a method definition built from
  what you actually got, ready to paste into `data/methods/`. Then
  `wowgold segments assign <#> <new id>` uses the segment as its first measurement.
- `segments assign <#> <method> [--activity kill]` overrides by hand; `segments ignore <#>` excludes
  a segment.

Re-importing the same file is safe.

**Timer.** `wowgold session start herb_felwood`, then `pause`/`resume`, then
`wowgold session stop -i Dreamfoil:14 -i "Golden Sansam:6" --coins 2g10s`.

**By hand.** `wowgold session log herb_felwood --minutes 45 -i Dreamfoil:14 --coins 2g10s`.

How calibration works:
- Each yield is a Poisson rate with a Gamma prior taken from the method file (mean ± sd). A typical
  estimate is worth about half an hour of real data, so a few sessions outweigh it.
- Sessions are normalized by your character's throughput multiplier (class, mount, gear). A druid's
  hour on a druid-friendly route counts as more than one "base hour", and other classes get their
  own modifier applied back on top.
- Grey items are converted to vendor income. Items that drop but aren't in the method file are added
  to it and marked "(new)".
- A method counts as **measured** after 3 base-hours of sessions.

## How prices are computed

Per item and house, from each snapshot (quantity-weighted, copper per unit):

- **market** – average of the cheapest 25% of units (resists both troll listings and single undercuts)
- **min / p25 / median** – percentiles of the listing book
- **sold** – listings that vanished between snapshots while they still had time left (bought or
  cancelled; the API can't distinguish). Listings that could have expired are counted separately as
  ambiguous. Partial commodity buys are counted from quantity drops.

Random-suffix items ("of the Bear") are tracked as variants of the base item via their bonus IDs.

## Development

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```
