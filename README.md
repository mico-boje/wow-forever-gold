# wowgold

Gold-making planner for World of Warcraft: Forever. Design and roadmap: [PLAN.md](PLAN.md).

## Setup

```bash
uv sync
cp .env.example .env   # add BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET
```

Create API credentials at https://develop.battle.net/access/clients. Use a supported locale
(`en_GB`, `en_US`, `de_DE`, …) — an unsupported one makes Blizzard return empty names.

## Web UI

```bash
uv run wowgold ui            # http://127.0.0.1:8765/
```

A local web app (127.0.0.1 only) with three pages:

- **Farms:** enter class, level, faction and professions (or use your saved profile) and get
  ranked farms with their evidence, leads from your Wowhead captures, a leveling sweep with a
  chart of the best gold/h per level, and a YAML download for testing. "Save as my profile"
  saves the character for the CLI too.
- **Crafting:** crafts that make money buying the materials, and what to craft from your own
  materials (see [Crafting](#crafting)).
- **Capture:** the Wowhead to-do list and the userscript endpoint (see below).
- **Status:** when the collector last ran, failures in the last 24 hours, and the log tail.

The CLI stays the tool for cron and scripting; both use the same code. On WSL, open the address
in your Windows browser (WSL forwards localhost).

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
| `wowgold vanilla fetch` | Download the VMaNGOS vanilla world database (loot tables, spawns) used by `discover`. |
| `wowgold discover` | Generate farm hypotheses (mob camps, gathering loops) from vanilla data at current prices. |
| `wowgold crafts -f horde -p alchemy:300` | Profitable crafts from bought materials, and what to craft from your own mats. |
| `wowgold ui` | Local web UI: farms, leveling sweep, leads, Wowhead capture, collector status. |
| `wowgold capture serve` | Same as `wowgold ui`, opening on the Capture page. |
| `wowgold capture sources` | Item sources parsed from your captures, and whether `discover` can use them. |
| `wowgold prune` | Delete old raw files (keeps 2 per house) and roll hourly stats older than 14 days into daily rows. |

Set `WOWGOLD_HOUSES=[113]` in `.env` to limit `prices` to the house(s) you trade on.

Names for items the API doesn't know can be added to `data/item_overrides.csv`
(`item_id,name,quality`).

### Running the collector in the background

The simplest option is a terminal tab running `uv run wowgold collect`. For an unattended setup, use
cron with [scripts/collect-cron.sh](scripts/collect-cron.sh), which runs one cycle, appends output to
`data/collect.log`, and skips a run if the previous one hasn't finished:

```
5 * * * * /path/to/wow-forever-gold/scripts/collect-cron.sh
```

Install it with `crontab -e`. On WSL, cron only runs while the WSL VM is up.

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

## Discovering new farms

`wowgold discover` looks for farms nobody has written down yet. It reads the
[VMaNGOS](https://github.com/vmangos/core) vanilla 1.12 world database (GPL-2.0, ~140 MB;
`wowgold vanilla fetch` downloads it into `data/vanilla/`) and prices what it finds at current
AH prices:

- **Mob camps.** Each hostile mob gets an expected value per kill: loot table × price, plus
  coins. Spawns are grouped into ~450-yard squares; in each, you kill the most valuable mobs
  first, limited by your kill speed (40/h at even level, faster when you out-level them) and
  by respawn times. The best squares become candidates, with a skinning variant when the mobs
  are skinnable.
- **Gathering loops.** Per zone and profession, node pools tell how many herbs/veins are up at
  once. Their spacing gives travel time per node, capped by each node type's respawn rate.

Candidates are ordinary methods with `confidence: hypothesis`, scored by the same planner
(AH cut, demand caps, mount and gear modifiers). Zones are inferred from node pools and quest
objectives, so they're approximate.

Kill speed depends on your class (`CLASS_KILL_SPEED` in `discovery/discover.py`: hunters
and warlocks fastest, priests slowest), mages get an AoE bonus on mobs 5+ levels below them,
and rogues add pickpocket loot from humanoids.

```bash
wowgold discover                         # uses your saved profile
wowgold discover -l 45 -p skinning:225 --kind farm --top 20
wowgold discover --levels 20-60          # best farms at 20, 25, ..., 60 while leveling
wowgold discover --levels 15,30,45 --per-level 5
wowgold discover --write                 # also writes data/discovered/hypotheses.yaml
```

A `--levels` sweep keeps your class, faction and professions, and assumes profession skill
keeps pace with level: 5 points per level, never above what `--prof` says. Below 60 it also
assumes leveling gear and no epic mount.

To test one, copy its entry from `data/discovered/hypotheses.yaml` into a file in
`data/methods/`, play it with the addon running (or `wowgold session start <id>`), and import
the session. Calibration then replaces the guessed yields with measured ones.

**What it can't know:** vanilla drop rates, spawns and respawn times may differ in Forever, and
Forever-only items (Scarce materials, new recipes) aren't in the data. White/grey weapons and
armor and soulbound items are valued at vendor price only, which also hides troll listings.
Fishing, pickpocketing and dungeons aren't covered yet.

### Adding Forever-only items from Wowhead

Forever-only items (Fel Crystal, Torbernite, Scarce materials, ...) aren't in the vanilla data,
so `discover` can't see where they drop. You can look them up on Wowhead yourself and let
wowgold save what the page shows:

1. `wowgold ui`, then open http://127.0.0.1:8765/capture — a list of the most valuable
   Forever-only items on your AH, linked to Wowhead.
2. Install the userscript linked on that page in Tampermonkey or Violentmonkey (once).
3. Click through the items. On each Wowhead page you open, the userscript reads the "Dropped by",
   "Skinned from", "Mined from"... tables and posts them to the local server
   (a green "wowgold: saved" badge confirms it). Pages are stored in `data/wowhead/captures/`.

Nothing is fetched automatically: only pages you open are saved, at your browsing pace. The
data is Wowhead's; keep it for personal use (it's gitignored) and don't redistribute it.

`discover` then adds each drop chance to the matching vanilla mob or node, so a mob that now
drops Fel Crystal can surface as a new camp.

**Verifying farms.** On the Farms page, *Verify these farms on Wowhead* turns the farms on
screen (a single level or a whole leveling sweep) into a capture to-do list: the Wowhead
NPC page of each camp's mobs and the object page of each gathering loop's nodes, with the farms
each page belongs to. A captured NPC/object page brings that mob's or node's Forever loot table.
With 50+ samples it replaces the vanilla loot table outright (items Forever removed disappear),
below that it only patches the items it lists. Re-run the search and fully covered farms show
as *verified* (partly covered ones as e.g. *2/3 verified*).

Sources without open-world spawn points in the vanilla data (NPCs new in Forever, or mobs that
only live inside dungeons) become **leads**, listed under the farms. A lead is filtered like a
farm: hostile to your faction, at most 2 levels above you, your class (pickpocketing is
rogue-only), your skinning/gathering skill, and elites only with `--elites` (dungeon bosses
never). Its "if plentiful" value counts only the captured items at your full kill speed, since
there's no respawn data. Treat it as "worth a look", then measure it. `--leads N` sets how many
to show; a `--levels` sweep shows the best lead per level.

## Crafting

Recipes come from the vanilla database (about 1,130 item-creating profession spells, with
reagents, cooldowns, and how each is learned: trainer and fee, recipe item, or known from the
start) plus Forever recipes from your Wowhead captures: the "Created by", "Reagent for" and
"Teaches" tables on item pages you open. Forever data wins for the same recipe.

**Buy, craft, sell.** For each recipe your skills can make (or all, with "show everything"):

- materials at today's AH price, or a vendor's when cheaper (vendor stock is unlimited);
- output at the AH price after the 5% cut, or vendor price when that's better (white
  weapons/armor and soulbound items always at vendor);
- crafts per day = the smallest of: your share of the output's daily sales, the materials
  listed right now, the recipe's cooldown (transmutes, Mooncloth), and ~200 crafts of time;
- learning cost (trainer fee, vendor or AH price of the recipe item) and how many crafts it
  takes to pay back.

**Craft what you farm.** For each material, the best recipe that turns it into more money than
selling it raw, with the other materials bought, and how many a day the output market takes.
On the Farms page, *Craft before selling* adds that to each farm's gold/h and re-ranks; the
details say what to craft (e.g. "47 Sungrass → Invisibility Potion: 0.85g each instead of
0.19g").

Caveats: buying at the average price assumes you don't move the market; several materials
feeding the same recipe each assume they get its full demand; Forever recipes from Wowhead
have no cooldown data except transmutes (assumed 2 days).

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
