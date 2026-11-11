"""Command-line entry point: `wowgold ...`."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import db
from .blizzard import BlizzardAuthError, BlizzardClient, ForeverApi
from .config import Settings, get_settings
from .items import load_overrides, sync_items
from .market.ingest import ingest_pending, prune
from .market.query import find_items, format_money, house_price
from .methods.calibration import apply_calibration, calibrate, load_session_totals
from .methods.check import check_items, missing_item_ids
from .methods.models import GearTier, load_methods
from .planner.engine import PlanOptions, make_plan, upgrade_targets
from .planner.pricing import build_price_book, pick_houses
from .planner.profile import CharacterProfile, parse_professions
from .probe import Prober, save_report
from .segments import (
    OTHER,
    Assignment,
    Proposal,
    draft_method,
    load_item_meta,
    load_segment,
    propose,
    split_activities,
)
from .sessions import (
    SessionTimer,
    apply_segment,
    clear_segment_sessions,
    delete_session,
    import_addon_data,
    list_sessions,
    load_profile,
    log_session,
    parse_coins,
    parse_item_spec,
    read_addon_file,
    save_profile,
    set_segment_status,
)
from .snapshot import KINDS, run_snapshot

app = typer.Typer(help="Gold-making planner for WoW: Forever.", no_args_is_help=True)
houses_app = typer.Typer(help="Auction house bookkeeping.", no_args_is_help=True)
app.add_typer(houses_app, name="houses")
items_app = typer.Typer(help="Item metadata.", no_args_is_help=True)
app.add_typer(items_app, name="items")
methods_app = typer.Typer(help="Curated gold-making methods.", no_args_is_help=True)
app.add_typer(methods_app, name="methods")
profile_app = typer.Typer(help="Your saved character profile.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
segments_app = typer.Typer(
    help="Review activity the addon recorded and match it to methods.", no_args_is_help=True
)
app.add_typer(segments_app, name="segments")
vanilla_app = typer.Typer(
    help="Vanilla world database (VMaNGOS) used by `discover`.", no_args_is_help=True
)
app.add_typer(vanilla_app, name="vanilla")
capture_app = typer.Typer(
    help="Save Wowhead pages you open in your browser (personal use).", no_args_is_help=True
)
app.add_typer(capture_app, name="capture")
session_app = typer.Typer(
    help="Log play sessions to measure real yields per method.", no_args_is_help=True
)
app.add_typer(session_app, name="session")
console = Console()

RegionOpt = Annotated[str | None, typer.Option("--region", "-r", help="us | eu | kr | tw")]
ClassOpt = Annotated[str | None, typer.Option("--class", "-c", help="Your class, e.g. mage.")]
LevelOpt = Annotated[int | None, typer.Option("--level", "-l", min=1, max=60)]
FactionOpt = Annotated[str | None, typer.Option("--faction", "-f", help="alliance | horde")]
ProfOpt = Annotated[
    list[str] | None,
    typer.Option("--prof", "-p", help="profession:skill, e.g. herbalism:300,alchemy:275"),
]
GearOpt = Annotated[GearTier | None, typer.Option(help="Your gear tier.")]
EpicOpt = Annotated[bool | None, typer.Option("--epic-mount/--no-epic-mount")]


def _resolve_profile(
    s: Settings,
    cls: str | None,
    level: int | None,
    faction: str | None,
    prof: list[str] | None,
    gear: GearTier | None,
    epic_mount: bool | None,
) -> CharacterProfile:
    """Saved profile, overridden by any flags given."""
    saved = load_profile(s.profile_path)
    base = saved.model_dump(by_alias=True) if saved else {}
    overrides = {
        "class": cls,
        "level": level,
        "faction": faction.lower() if faction else None,
        "gear": gear,
        "epic_mount": epic_mount,
    }
    try:
        if prof:
            overrides["professions"] = parse_professions(prof)
        merged = {**base, **{k: v for k, v in overrides.items() if v is not None}}
        missing = [k for k in ("class", "level", "faction") if k not in merged]
        if missing:
            raise typer.BadParameter(
                f"missing {', '.join(missing)}: pass --class/--level/--faction or save a "
                "profile with `wowgold profile set`"
            )
        return CharacterProfile.model_validate(merged)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _client(settings: Settings, region: str | None) -> BlizzardClient:
    if not settings.has_credentials:
        console.print(
            "[red]Missing Blizzard API credentials.[/] Copy .env.example to .env and set "
            "BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET (https://develop.battle.net/access/clients)."
        )
        raise typer.Exit(2)
    return BlizzardClient(
        region or settings.wowgold_region,
        settings.blizzard_client_id,
        settings.blizzard_client_secret.get_secret_value(),
        locale=settings.wowgold_locale,
    )


@app.command()
def config() -> None:
    """Show the effective configuration (secrets masked)."""
    s = get_settings()
    table = Table(show_header=False)
    table.add_row("client id", s.blizzard_client_id or "[red]not set[/]")
    table.add_row(
        "client secret", "set" if s.blizzard_client_secret.get_secret_value() else "[red]not set[/]"
    )
    table.add_row("region", s.wowgold_region)
    table.add_row("locale", s.wowgold_locale)
    table.add_row("data dir", str(s.wowgold_data_dir))
    table.add_row("static version", s.wowgold_static_version)
    console.print(table)


@app.command()
def probe(
    region: RegionOpt = None,
    skip_houses: Annotated[
        bool, typer.Option(help="Don't download a full house body (saves ~5-9 MB).")
    ] = False,
) -> None:
    """Check which WoW: Forever API endpoints currently work."""
    s = get_settings()
    try:
        with _client(s, region) as client:
            report = Prober(client, s.wowgold_static_version).run(
                include_house_bodies=not skip_houses
            )
    except BlizzardAuthError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    table = Table(title=f"Forever API probe ({report.region})")
    for col in ("endpoint", "status", "size", "namespace", "notes"):
        table.add_column(col)
    for r in report.results:
        color = "green" if 200 <= r.status < 300 else "red"
        notes = r.note or ", ".join(f"{k}={v}" for k, v in r.shape.items() if k.startswith("len("))
        table.add_row(r.name, f"[{color}]{r.status}[/]", f"{r.bytes:,}", r.namespace, notes)
    console.print(table)
    console.print(f"Report saved to {save_report(report, s.wowgold_data_dir)}")


@app.command()
def snapshot(
    region: RegionOpt = None,
    house: Annotated[
        list[int] | None, typer.Option("--house", "-h", help="Only these house IDs.")
    ] = None,
    kind: Annotated[list[str] | None, typer.Option(help="auctions and/or commodities.")] = None,
) -> None:
    """Download raw auction house snapshots (deduplicated across rulesets)."""
    s = get_settings()
    kinds = kind or list(KINDS)
    if bad := set(kinds) - set(KINDS):
        raise typer.BadParameter(f"unknown kind(s): {', '.join(sorted(bad))}")
    conn = db.connect(s.db_path)
    try:
        with _client(s, region) as client:
            summary = run_snapshot(
                ForeverApi(client),
                conn,
                s.wowgold_data_dir,
                house_ids=set(house) if house else None,
                kinds=kinds,
                on_progress=lambda msg: console.print(f"  {msg}"),
            )
    except BlizzardAuthError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    console.print(
        f"Houses: {summary.houses}  stored: {summary.stored}  unchanged: {summary.unchanged}  "
        f"not modified: {summary.not_modified}  errors: {len(summary.errors)}"
    )
    for err in summary.errors:
        console.print(f"  [red]{err}[/]")
    if summary.errors and not (summary.stored or summary.unchanged or summary.not_modified):
        raise typer.Exit(1)


@houses_app.command("list")
def houses_list(region: RegionOpt = None) -> None:
    """List known auction houses and their latest listing counts."""
    s = get_settings()
    conn = db.connect(s.db_path)
    rows = db.list_houses(conn, region or s.wowgold_region)
    conn.close()
    if not rows:
        console.print("No houses recorded yet. Run `wowgold snapshot` first.")
        return
    table = Table(title="Auction houses")
    for col in ("id", "name", "faction", "rulesets", "auctions", "commodities"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["house_id"]),
            r["name"] or "",
            r["faction"] or "[dim]unset[/]",
            r["rulesets"] or "",
            f"{r['last_auctions']:,}" if r["last_auctions"] is not None else "",
            f"{r['last_commodities']:,}" if r["last_commodities"] is not None else "",
        )
    console.print(table)


@houses_app.command("set-faction")
def houses_set_faction(
    house_id: int,
    faction: Annotated[str, typer.Argument(help="alliance | horde | blackwater | none")],
    region: RegionOpt = None,
) -> None:
    """Label a house with its faction (the API doesn't expose this)."""
    faction = faction.lower()
    if faction not in {"alliance", "horde", "blackwater", "none"}:
        raise typer.BadParameter("faction must be alliance, horde, blackwater or none")
    s = get_settings()
    conn = db.connect(s.db_path)
    ok = db.set_faction(
        conn, region or s.wowgold_region, house_id, None if faction == "none" else faction
    )
    conn.commit()
    conn.close()
    if not ok:
        console.print(f"[red]House {house_id} not found.[/] Run `wowgold snapshot` first.")
        raise typer.Exit(1)
    console.print(f"House {house_id} → {faction}")


@houses_app.command("find")
def houses_find(
    item: Annotated[str, typer.Argument(help="Item name or ID you listed.")],
    price: Annotated[str, typer.Argument(help="Exact price you listed it at, e.g. 12g34s56c.")],
    region: RegionOpt = None,
) -> None:
    """Find which house is yours: list an item in game at an unusual price, wait for the next
    snapshot (or run `wowgold collect --once`), then search for that exact listing."""
    from .market.myhouse import find_listing

    s = get_settings()
    region = region or s.wowgold_region
    try:
        copper = parse_coins(price)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    conn = db.connect(s.db_path)
    try:
        matches = find_items(conn, item, limit=1)
        if not matches:
            raise typer.BadParameter(f"no item called {item!r}")
        found = find_listing(conn, region, matches[0].item_id, copper)
    finally:
        conn.close()
    name = matches[0].name or item
    if not found:
        console.print(
            f"No house has {name} listed at {format_money(copper)} in its latest snapshot. "
            "Snapshots are hourly: wait for the next one or run `wowgold collect --once`."
        )
        raise typer.Exit(1)
    for m in found:
        console.print(
            f"House [bold]{m.house_id}[/] ({m.faction or '?'}, {m.kind}): {m.quantity} × {name} "
            f"at {format_money(copper)} · snapshot {m.fetched_at}"
        )
    if len(found) == 1:
        console.print(
            f"Save it with `wowgold houses mine {found[0].house_id}` (or on the Status page)."
        )


@houses_app.command("mine")
def houses_mine(
    house_id: Annotated[int | None, typer.Argument(help="House ID; omit to show yours.")] = None,
    clear: Annotated[str | None, typer.Option(help="Forget the house for this faction.")] = None,
    region: RegionOpt = None,
) -> None:
    """Show or set the house you trade on (one per faction)."""
    from .market.myhouse import load_my_houses, set_my_house

    s = get_settings()
    region = region or s.wowgold_region
    if clear:
        set_my_house(s.wowgold_data_dir, region, clear.lower(), None)
    elif house_id is not None:
        conn = db.connect(s.db_path)
        row = conn.execute(
            "SELECT faction FROM houses WHERE region = ? AND house_id = ?", (region, house_id)
        ).fetchone()
        conn.close()
        if row is None or not row[0]:
            raise typer.BadParameter(f"house {house_id} unknown or without a faction")
        set_my_house(s.wowgold_data_dir, region, row[0], house_id)
    mine = load_my_houses(s.wowgold_data_dir, region)
    if not mine:
        console.print("No house confirmed yet. Find yours with `wowgold houses find`.")
    for faction, hid in sorted(mine.items()):
        console.print(f"{faction}: house {hid}")


OVERRIDES_FILE = "item_overrides.csv"


def _ingest(conn) -> None:
    summary = ingest_pending(conn)
    console.print(
        f"Ingested {summary.snapshots} snapshot(s) into {summary.rows:,} price rows"
        + (f" ({summary.skipped} missing raw files)" if summary.skipped else "")
    )


def _sync_items(conn, client: BlizzardClient, data_dir, retry_missing: bool = False) -> None:
    summary = sync_items(
        conn,
        client,
        retry_missing=retry_missing,
        on_progress=lambda n, total: console.print(f"  items {n}/{total}"),
    )
    n_over = load_overrides(conn, data_dir / OVERRIDES_FILE)
    console.print(
        f"Items: {summary.requested} looked up, {summary.found} from Forever, "
        f"{summary.classic_era} from Classic Era fallback, {summary.missing} unknown"
        + (f", {n_over} overrides applied" if n_over else "")
    )


def _prune(conn, keep_raw: int, keep_hourly_days: int) -> None:
    summary = prune(conn, keep_raw=keep_raw, keep_hourly_days=keep_hourly_days)
    console.print(
        f"Pruned {summary.raw_files_deleted} raw file(s); "
        f"rolled {summary.hourly_rows_rolled_up:,} hourly rows into daily"
    )


@app.command()
def ingest() -> None:
    """Parse stored raw snapshots into per-item price statistics."""
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        _ingest(conn)
    finally:
        conn.close()


@items_app.command("sync")
def items_sync(
    region: RegionOpt = None,
    retry_missing: Annotated[
        bool, typer.Option(help="Retry items that previously weren't found, now.")
    ] = False,
) -> None:
    """Fetch names/vendor prices for items seen on the AH (plus data/item_overrides.csv)."""
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        with _client(s, region) as client:
            _sync_items(conn, client, s.wowgold_data_dir, retry_missing)
    finally:
        conn.close()


@app.command("prune")
def prune_cmd(
    keep_raw: Annotated[int, typer.Option(help="Raw snapshots to keep per house/kind.")] = 2,
    keep_hourly_days: Annotated[int, typer.Option(help="Days of hourly stats to keep.")] = 14,
) -> None:
    """Delete old raw files and roll old hourly stats into daily rows."""
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        _prune(conn, keep_raw, keep_hourly_days)
    finally:
        conn.close()


@app.command()
def collect(
    region: RegionOpt = None,
    every: Annotated[int, typer.Option(help="Minutes between runs.")] = 60,
    once: Annotated[bool, typer.Option(help="Run a single cycle and exit.")] = False,
    keep_raw: Annotated[int, typer.Option(help="Raw snapshots to keep per house/kind.")] = 2,
    keep_hourly_days: Annotated[int, typer.Option(help="Days of hourly stats to keep.")] = 14,
) -> None:
    """Snapshot -> ingest -> item sync -> prune, repeated every N minutes."""
    import time
    from datetime import datetime

    s = get_settings()
    while True:
        started = time.monotonic()
        console.rule(f"collect {datetime.now():%Y-%m-%d %H:%M}")
        conn = db.connect(s.db_path)
        try:
            with _client(s, region) as client:
                summary = run_snapshot(ForeverApi(client), conn, s.wowgold_data_dir)
                console.print(
                    f"Snapshot: {summary.stored} stored, {summary.unchanged} unchanged, "
                    f"{summary.not_modified} not modified, {len(summary.errors)} errors"
                )
                for err in summary.errors:
                    console.print(f"  [red]{err}[/]")
                _ingest(conn)
                _sync_items(conn, client, s.wowgold_data_dir)
                _prune(conn, keep_raw, keep_hourly_days)
        except BlizzardAuthError as exc:
            console.print(f"[red]{exc}[/]")
            if once:
                raise typer.Exit(1) from exc
        except Exception as exc:  # keep the loop alive across transient failures
            if once:
                raise
            console.print(f"[red]cycle failed: {exc!r}[/]")
        finally:
            conn.close()
        if once:
            return
        time.sleep(max(0.0, every * 60 - (time.monotonic() - started)))


@app.command()
def prices(
    query: Annotated[str, typer.Argument(help="Item name (partial) or item ID.")],
    house: Annotated[list[int] | None, typer.Option("--house", "-h", help="House ID(s).")] = None,
    faction: Annotated[
        str | None, typer.Option("--faction", "-f", help="alliance | horde | blackwater")
    ] = None,
    region: RegionOpt = None,
    limit: Annotated[int, typer.Option(help="Max matching items to show.")] = 5,
) -> None:
    """Show current AH prices, 24h sales estimate and averages for an item."""
    s = get_settings()
    region = region or s.wowgold_region
    conn = db.connect(s.db_path)
    try:
        matches = find_items(conn, query, limit=limit)
        if not matches:
            console.print(f"No item matching '{query}'. Run `wowgold items sync` first?")
            raise typer.Exit(1)
        house_ids = house or s.wowgold_houses
        if not house_ids:
            house_ids = [
                r["house_id"]
                for r in db.list_houses(conn, region)
                if (r["last_auctions"] or r["last_commodities"])
                and (faction is None or r["faction"] == faction.lower())
            ]
        for m in matches:
            title = f"{m.name or '(unknown name)'}  [dim]#{m.item_id}[/]"
            if m.sell_price:
                title += f"  [dim]vendor {format_money(m.sell_price)}[/]"
            table = Table(title=title, title_justify="left")
            for col in (
                "house",
                "market",
                "min",
                "p25",
                "median",
                "qty",
                "listings",
                "sold 24h*",
                "avg 24h",
                "avg 7d",
            ):
                table.add_column(col, justify="right" if col != "house" else "left")
            any_rows = False
            for hid in house_ids:
                hp = house_price(conn, region, m.item_id, hid)
                if hp is None:
                    continue
                any_rows = True
                sold = "-" if hp.sold_24h is None else f"{hp.sold_24h:,}"
                if hp.ambiguous_24h:
                    sold += f" (+{hp.ambiguous_24h:,}?)"
                table.add_row(
                    f"{hid} {hp.faction or ''}".strip()
                    + (f" [dim]{hp.variants} variants[/]" if hp.variants > 1 else ""),
                    format_money(hp.market_unit),
                    format_money(hp.min_unit),
                    format_money(hp.p25_unit),
                    format_money(hp.median_unit),
                    f"{hp.quantity:,}",
                    f"{hp.num_auctions:,}",
                    sold,
                    format_money(hp.market_avg_24h),
                    format_money(hp.market_avg_7d),
                )
            if any_rows:
                console.print(table)
        console.print(
            "[dim]* sold = listings that vanished with time left (bought or cancelled); "
            "(+N?) = could also have expired. Needs two snapshots.[/]"
        )
    finally:
        conn.close()


def _gold(copper: float | None) -> str:
    """Compact gold display for tables: 12.3g / 45s / 7c."""
    if copper is None:
        return "-"
    if abs(copper) >= 10000:
        g = copper / 10000
        return f"{g:,.0f}g" if abs(g) >= 100 else f"{g:,.1f}g"
    if abs(copper) >= 100:
        return f"{copper / 100:.0f}s"
    return f"{copper:.0f}c"


def _pick_houses(conn, region: str, faction: str, explicit: list[int] | None, s: Settings):
    from .market.myhouse import load_my_houses

    return pick_houses(
        conn,
        region,
        faction,
        explicit,
        s.wowgold_houses,
        load_my_houses(s.wowgold_data_dir, region),
    )


@app.command()
def plan(
    cls: ClassOpt = None,
    level: LevelOpt = None,
    faction: FactionOpt = None,
    prof: ProfOpt = None,
    gear: GearOpt = None,
    epic_mount: EpicOpt = None,
    hours: Annotated[float, typer.Option(help="Length of the play session.")] = 2.0,
    sell_days: Annotated[float, typer.Option(help="Days you're willing to wait to sell.")] = 3.0,
    share: Annotated[
        float, typer.Option(help="Share of daily demand you expect to capture (0-1).")
    ] = 0.5,
    house: Annotated[list[int] | None, typer.Option("--house", "-h", help="House ID(s).")] = None,
    top: Annotated[int, typer.Option(help="How many methods to list.")] = 10,
    details: Annotated[int, typer.Option(help="Show item breakdown for the top N.")] = 3,
    region: RegionOpt = None,
) -> None:
    """Rank gold-making methods for a character at current AH prices.

    Character flags default to the saved profile (`wowgold profile set`)."""
    s = get_settings()
    region = region or s.wowgold_region
    profile = _resolve_profile(s, cls, level, faction, prof, gear, epic_mount)
    opts = PlanOptions(hours=hours, sell_days=sell_days, market_share=share)

    conn = db.connect(s.db_path)
    try:
        methods = apply_calibration(conn, load_methods(s.wowgold_methods_dir))
        house_ids = _pick_houses(conn, region, profile.faction, house, s)
        if not house_ids:
            console.print("[red]No auction data for that faction.[/] Run `wowgold collect --once`.")
            raise typer.Exit(1)
        item_ids = {i for m in methods for i in m.item_ids}
        book = build_price_book(conn, region, house_ids, item_ids)
    finally:
        conn.close()
    result = make_plan(methods, profile, book, opts)

    profs = ", ".join(f"{p} {v}" for p, v in profile.professions.items()) or "no professions"
    console.print(
        f"[bold]Level {profile.level} {profile.faction} {profile.cls}[/] ({profs}, {profile.gear} "
        f"gear) · house {', '.join(map(str, house_ids))} · {hours:g}h session, "
        f"selling within {sell_days:g} days"
    )
    if book.sales_coverage_h < 12:
        console.print(
            f"[yellow]Only {book.sales_coverage_h:.1f}h of sales history: demand is estimated "
            "from current listings until the collector has run ~12h.[/]"
        )

    table = Table(title="Best methods right now", title_justify="left")
    for col, just in (
        ("#", "right"),
        ("method", "left"),
        ("kind", "left"),
        ("gold/h", "right"),
        ("range", "right"),
        ("notes", "left"),
    ):
        table.add_column(col, justify=just)
    for n, ev in enumerate(result.ranked[:top], start=1):
        m = ev.method
        rng = "" if ev.low == ev.high else f"{_gold(ev.low)}-{_gold(ev.high)}"
        note = ""
        if m.kind == "craft":
            note = (
                f"{ev.crafts} crafts, {_gold(ev.profit_per_craft)}/craft"
                if ev.crafts
                else (ev.flags[0] if ev.flags else "")
            )
        elif m.risk:
            note = ", ".join(m.risk)
        table.add_row(
            str(n),
            f"{m.name}\n[dim]{m.zone}[/]" if m.zone else m.name,
            m.kind,
            _gold(ev.gold_per_hour),
            rng,
            note,
        )
    console.print(table)

    for ev in result.ranked[:details]:
        console.print(f"\n[bold]{ev.method.name}[/] — {_gold(ev.total)} over {hours:g}h")
        for line in ev.lines:
            if line.item_id == 0:
                console.print(f"  {line.name:<34} {_gold(line.value):>8}")
                continue
            sold = ""
            if line.vendor_units >= 0.5 and line.ah_units:
                sold = f"  [dim]({line.ah_units:.0f} AH / {line.vendor_units:.0f} vendor)[/]"
            elif line.vendor_units >= 0.5:
                sold = "  [dim](vendor)[/]"
            console.print(f"  {line.produced:>6.0f} × {line.name:<25} {_gold(line.value):>8}{sold}")
        for flag in ev.flags:
            console.print(f"  [dim]• {flag}[/]")

    targets = upgrade_targets(result, profile, book, opts)
    if targets:
        console.print("\n[bold]Worth working towards[/]")
        for ev, reasons in targets:
            console.print(
                f"  {ev.method.name}: ~{_gold(ev.gold_per_hour)}/h — needs "
                + "; ".join(str(r) for r in reasons)
            )


@methods_app.command("list")
def methods_list() -> None:
    """List all curated methods and their requirements."""
    s = get_settings()
    table = Table(title="Methods")
    for col in ("id", "kind", "level", "professions", "other", "confidence"):
        table.add_column(col)
    for m in load_methods(s.wowgold_methods_dir):
        req = m.requirements
        other = []
        if req.classes:
            other.append("/".join(req.classes))
        if req.faction != "any":
            other.append(req.faction)
        if req.gear != "leveling":
            other.append(f"{req.gear} gear")
        table.add_row(
            m.id,
            m.kind,
            str(req.level),
            ", ".join(f"{p} {v}" for p, v in req.professions.items()),
            ", ".join(other),
            m.confidence,
        )
    console.print(table)


@methods_app.command("check")
def methods_check(region: RegionOpt = None) -> None:
    """Verify every item ID in the method files matches its name in Blizzard's item data."""
    s = get_settings()
    methods = load_methods(s.wowgold_methods_dir)
    conn = db.connect(s.db_path)
    try:
        if missing := missing_item_ids(conn, methods):
            with _client(s, region) as client:
                sync_items(conn, client, item_ids=missing)
        issues = check_items(conn, methods)
    finally:
        conn.close()
    console.print(f"{len(methods)} methods loaded")
    for issue in issues:
        console.print(f"  [red]{issue}[/]")
    if issues:
        raise typer.Exit(1)
    console.print("[green]All item IDs match their names.[/]")


# -- profile -------------------------------------------------------------------------


def _describe(profile: CharacterProfile) -> str:
    profs = ", ".join(f"{p} {v}" for p, v in profile.professions.items()) or "no professions"
    mount = ", epic mount" if profile.epic_mount else ""
    return (
        f"level {profile.level} {profile.faction} {profile.cls} ({profs}; {profile.gear} gear"
        f"{mount})"
    )


@profile_app.command("set")
def profile_set(
    cls: ClassOpt = None,
    level: LevelOpt = None,
    faction: FactionOpt = None,
    prof: ProfOpt = None,
    gear: GearOpt = None,
    epic_mount: EpicOpt = None,
) -> None:
    """Save (or update) your character; `plan` and `session` use it by default."""
    s = get_settings()
    profile = _resolve_profile(s, cls, level, faction, prof, gear, epic_mount)
    save_profile(s.profile_path, profile)
    console.print(f"Saved: {_describe(profile)}")


@profile_app.command("show")
def profile_show() -> None:
    """Show the saved profile."""
    profile = load_profile(get_settings().profile_path)
    console.print(_describe(profile) if profile else "No profile saved yet.")


# -- sessions ----------------------------------------------------------------------------


def _method(s: Settings, method_id: str):
    methods = {m.id: m for m in load_methods(s.wowgold_methods_dir)}
    if method_id not in methods:
        close = [m for m in methods if method_id.lower() in m]
        hint = (
            f" Did you mean: {', '.join(close[:5])}?" if close else " See `wowgold methods list`."
        )
        raise typer.BadParameter(f"unknown method {method_id!r}.{hint}")
    return methods[method_id]


ItemsOpt = Annotated[
    list[str] | None,
    typer.Option("--item", "-i", help="item:qty (name or ID), repeatable, e.g. -i Dreamfoil:14"),
]
CoinsOpt = Annotated[str, typer.Option(help="Coins looted, e.g. 3g20s.")]


def _record(
    s: Settings,
    method_id: str,
    seconds: int,
    item: list[str] | None,
    coins: str,
    source: str,
    started_at: str | None = None,
    notes: str | None = None,
) -> None:
    method = _method(s, method_id)
    profile = _resolve_profile(s, None, None, None, None, None, None)
    conn = db.connect(s.db_path)
    try:
        try:
            items: dict[int, int] = {}
            for spec in item or []:
                item_id, qty = parse_item_spec(conn, spec)
                items[item_id] = items.get(item_id, 0) + qty
            copper = parse_coins(coins) if coins else 0
            sid = log_session(
                conn,
                method,
                profile,
                duration_s=seconds,
                items=items,
                coins_copper=copper,
                source=source,
                started_at=started_at,
                notes=notes,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        totals = load_session_totals(conn).get(method.id)
    finally:
        conn.close()
    console.print(
        f"Logged session #{sid}: {method.name}, {seconds / 60:.0f} min, "
        f"{sum(items.values())} items, {format_money(copper)} coins"
    )
    if totals:
        console.print(
            f"{method.id} now has {totals.sessions} session(s), "
            f"{totals.base_hours:.1f} base-hours. "
            f"See `wowgold methods show {method.id}`."
        )


@session_app.command("log")
def session_log(
    method_id: Annotated[str, typer.Argument(help="Method ID (see `wowgold methods list`).")],
    minutes: Annotated[float, typer.Option(help="Active minutes played.")],
    item: ItemsOpt = None,
    coins: CoinsOpt = "",
    notes: Annotated[str | None, typer.Option(help="Free text, e.g. server busy.")] = None,
) -> None:
    """Record a finished session by hand."""
    _record(get_settings(), method_id, round(minutes * 60), item, coins, "manual", notes=notes)


@session_app.command("start")
def session_start(method_id: str) -> None:
    """Start a timer for a method. Finish with `session stop`."""
    s = get_settings()
    method = _method(s, method_id)
    try:
        SessionTimer(s.timer_path).start(method.id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Timer started for {method.name}. Pause with `session pause`.")


@session_app.command("pause")
def session_pause() -> None:
    """Pause the running timer (AFK, repairs, ...)."""
    try:
        active = SessionTimer(get_settings().timer_path).pause()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Paused at {active.active_seconds() / 60:.0f} active minutes.")


@session_app.command("resume")
def session_resume() -> None:
    """Resume a paused timer."""
    try:
        SessionTimer(get_settings().timer_path).resume()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print("Resumed.")


@session_app.command("status")
def session_status() -> None:
    """Show the running timer."""
    active = SessionTimer(get_settings().timer_path).get()
    if not active:
        console.print("No session running.")
        return
    state = " (paused)" if active.paused_at else ""
    console.print(f"{active.method_id}: {active.active_seconds() / 60:.0f} active minutes{state}")


@session_app.command("stop")
def session_stop(item: ItemsOpt = None, coins: CoinsOpt = "") -> None:
    """Stop the timer and record what you gathered."""
    s = get_settings()
    timer = SessionTimer(s.timer_path)
    active = timer.get()
    if active is None:
        raise typer.BadParameter("no session is running")
    _record(
        s,
        active.method_id,
        active.active_seconds(),
        item,
        coins,
        "timer",
        started_at=active.started_at,
    )
    timer.stop()  # only after the session was stored, so bad input doesn't lose the timer


@session_app.command("cancel")
def session_cancel() -> None:
    """Discard the running timer without logging anything."""
    try:
        SessionTimer(get_settings().timer_path).stop()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print("Timer discarded.")


@session_app.command("import")
def session_import(
    path: Annotated[
        str, typer.Argument(help="WTF/Account/<ACCOUNT>/SavedVariables/WowGoldLogger.lua")
    ],
    region: RegionOpt = None,
) -> None:
    """Import what the WowGoldLogger addon recorded (safe to re-run).

    Segments that clearly match one method are applied automatically; mixed or unclear
    ones are listed for review (`wowgold segments`)."""
    from pathlib import Path

    s = get_settings()
    methods = {m.id: m for m in load_methods(s.wowgold_methods_dir)}
    profile = _resolve_profile(s, None, None, None, None, None, None)
    try:
        data = read_addon_file(Path(path).expanduser())
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"can't read {path}: {exc}") from exc
    conn = db.connect(s.db_path)
    try:
        # Activity detection uses item types, so learn about new items first.
        known = {r[0] for r in conn.execute("SELECT item_id FROM items WHERE name IS NOT NULL")}
        unknown = sorted(data.item_ids - known)
        if unknown and s.has_credentials:
            with _client(s, region) as client:
                sync_items(conn, client, item_ids=unknown)
        elif unknown:
            console.print(
                f"[yellow]{len(unknown)} items are unknown and no API credentials are set; "
                "activity detection may be less accurate.[/]"
            )
        summary = import_addon_data(conn, data, methods, profile)
    finally:
        conn.close()

    if data.sessions:
        console.print(
            f"Labelled sessions (addon v0.1): {summary.imported} imported, "
            f"{summary.duplicates} already imported."
        )
    for msg in summary.skipped:
        console.print(f"  [yellow]skipped {msg}[/]")
    console.print(
        f"Segments: {len(summary.segments)} new, {summary.segment_duplicates} already imported"
        + (" (one still in progress, import it next time)" if data.open_segment else "")
    )
    if summary.segments:
        _segment_table([(r.row_id, r.segment, r.proposal, None) for r in summary.segments])
    review = [r for r in summary.segments if r.proposal.status in ("review", "unmatched")]
    if review:
        console.print(
            f"{len(review)} segment(s) need a look: `wowgold segments show <#>`, then "
            "`segments accept`, `segments assign` or `segments draft`."
        )


@session_app.command("list")
def session_list(method_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """List logged sessions."""
    s = get_settings()
    conn = db.connect(s.db_path)
    rows = list_sessions(conn, method_id)
    conn.close()
    if not rows:
        console.print("No sessions logged yet.")
        return
    table = Table(title="Sessions")
    for col in ("#", "method", "started", "minutes", "char", "items", "coins", "source"):
        table.add_column(col, no_wrap=col in ("method", "started"))
    for r in rows:
        table.add_row(
            str(r["id"]),
            r["method_id"],
            r["started_at"][:16].replace("T", " "),
            f"{r['duration_s'] / 60:.0f}",
            f"{r['level']} {r['cls']}",
            str(r["items"]),
            _gold(r["coins_copper"]),
            r["source"],
        )
    console.print(table)


@session_app.command("delete")
def session_delete(session_id: int) -> None:
    """Delete a logged session."""
    conn = db.connect(get_settings().db_path)
    ok = delete_session(conn, session_id)
    conn.close()
    if not ok:
        raise typer.BadParameter(f"no session #{session_id}")
    console.print(f"Deleted session #{session_id}.")


@methods_app.command("show")
def methods_show(method_id: str) -> None:
    """Show a method's estimated vs measured yields."""
    s = get_settings()
    method = _method(s, method_id)
    conn = db.connect(s.db_path)
    try:
        totals = load_session_totals(conn).get(method.id)
        names = dict(conn.execute("SELECT item_id, name FROM items WHERE name IS NOT NULL"))
    finally:
        conn.close()
    req = method.requirements
    console.print(f"[bold]{method.name}[/] ({method.id}, {method.kind}) {method.zone}")
    reqs = [f"level {req.level}"] + [f"{p} {v}" for p, v in req.professions.items()]
    console.print("Requires: " + ", ".join(reqs))
    if method.kind == "craft":
        for iq in method.reagents:
            console.print(f"  reagent {iq.qty:g} × {iq.name}" + (" (vendor)" if iq.vendor else ""))
        for iq in method.outputs:
            console.print(f"  makes   {iq.qty:g} × {iq.name}")
        return
    calibrated, report = calibrate(method, totals, names)
    if not report:
        console.print("No sessions logged yet; yields below are the method-file estimates.")
        for iq in method.yields:
            console.print(f"  {iq.qty:>6.1f} ± {iq.sd:<4.1f} /h  {iq.name}")
        return
    console.print(
        f"{totals.sessions} session(s), {totals.hours:.1f}h played = {totals.base_hours:.1f} "
        f"base-hours (class/mount/gear normalized) · confidence: {calibrated.confidence}"
    )
    table = Table()
    for col in ("item", "estimate /h", "measured /h", "calibrated /h"):
        table.add_column(col, justify="left" if col == "item" else "right")
    for r in report:
        table.add_row(
            r.name + (" [yellow](new)[/]" if r.new else ""),
            "-" if r.new else f"{r.prior_mean:.1f} ± {r.prior_sd:.1f}",
            f"{r.observed_rate:.1f}",
            f"{r.posterior_mean:.1f} ± {r.posterior_sd:.1f}",
        )
    table.add_row(
        "coins + vendor trash",
        format_money(method.vendor_copper_per_hour),
        format_money(round(totals.vendor_copper / totals.base_hours)),
        format_money(calibrated.vendor_copper_per_hour),
    )
    console.print(table)
    if any(r.new for r in report):
        console.print("[dim]Items marked (new) aren't in the method file; consider adding them.[/]")


# -- segments ----------------------------------------------------------------------------

STATUS_STYLE = {
    "applied": "green",
    "auto": "green",
    "labelled": "green",
    "review": "yellow",
    "unmatched": "red",
    "ignored": "dim",
}


def _segment_table(rows) -> None:
    """rows: (row_id, Segment, Proposal | None, status | None)"""
    table = Table(title="Segments", title_justify="left")
    for col in ("#", "started", "zone", "min", "activities", "status", "method / note"):
        table.add_column(col, no_wrap=col in ("started", "status"))
    for row_id, seg, proposal, status in rows:
        status = status or (proposal.status if proposal else "?")
        acts = ""
        detail = ""
        if proposal:
            acts = ", ".join(
                f"{a.activity} {a.share:.0%}"
                for a in proposal.activities
                if a.activity != OTHER and a.share >= 0.05
            )
            detail = ", ".join(a.method_id for a in proposal.assignments) or (
                proposal.notes[0] if proposal.notes else ""
            )
        style = STATUS_STYLE.get(status, "")
        table.add_row(
            str(row_id),
            seg.started_at[:16].replace("T", " "),
            seg.zone,
            f"{seg.active_s / 60:.0f}",
            acts,
            f"[{style}]{status}[/]" if style else status,
            detail,
        )
    console.print(table)


def _load_proposal(raw: str | None) -> Proposal | None:
    """Rebuild enough of a stored proposal for display (activities + assignments)."""
    if not raw:
        return None
    import json

    from .segments import ActivityTotals, Candidate

    data = json.loads(raw)
    return Proposal(
        status=data["status"],
        assignments=[Assignment(**a) for a in data["assignments"]],
        notes=data["notes"],
        activities=[
            ActivityTotals(
                **{
                    **a,
                    "npc_ids": set(a["npc_ids"]),
                    "items": {int(k): v for k, v in a["items"].items()},
                }
            )
            for a in data["activities"]
        ],
        candidates={k: [Candidate(**c) for c in v] for k, v in data["candidates"].items()},
    )


@segments_app.command("list")
def segments_list(
    status: Annotated[
        str | None, typer.Option(help="applied | review | unmatched | ignored")
    ] = None,
    limit: Annotated[int, typer.Option()] = 30,
) -> None:
    """List imported segments, newest first."""
    import json

    from .segments import parse_segment

    conn = db.connect(get_settings().db_path)
    query = "SELECT * FROM addon_segments {} ORDER BY started_at DESC LIMIT ?"
    rows = (
        conn.execute(query.format("WHERE status = ?"), (status, limit)).fetchall()
        if status
        else conn.execute(query.format(""), (limit,)).fetchall()
    )
    conn.close()
    if not rows:
        console.print("No segments imported yet. Run `wowgold session import <file>`.")
        return
    _segment_table(
        [
            (
                r["id"],
                parse_segment(json.loads(r["payload"])),
                _load_proposal(r["proposal"]),
                r["status"],
            )
            for r in rows
        ]
    )


def _open_segment(segment_id: int):
    s = get_settings()
    conn = db.connect(s.db_path)
    try:
        row, seg = load_segment(conn, segment_id)
    except ValueError as exc:
        conn.close()
        raise typer.BadParameter(str(exc)) from exc
    return s, conn, row, seg


@segments_app.command("show")
def segments_show(segment_id: int) -> None:
    """Show what a segment contains and how it matches the methods."""
    s, conn, row, seg = _open_segment(segment_id)
    try:
        meta = load_item_meta(conn)
        sessions = conn.execute(
            "SELECT method_id, duration_s FROM sessions WHERE substr(external_id, 1, ?) = ?",
            (len(f"seg:{seg.external_id}:"), f"seg:{seg.external_id}:"),
        ).fetchall()
    finally:
        conn.close()
    proposal = propose(seg, load_methods(s.wowgold_methods_dir), meta)
    console.print(
        f"[bold]Segment #{segment_id}[/] · {seg.zone} · {seg.started_at[:16].replace('T', ' ')} · "
        f"{seg.active_s / 60:.0f} active min · level {seg.level} {seg.cls} · status {row['status']}"
        + (f" · labelled {seg.label}" if seg.label else "")
    )
    kills = sum(seg.kills.values())
    if kills:
        console.print(f"Kills: {kills} ({len(seg.kills)} kinds of NPC)")
    for t in proposal.activities:
        console.print(
            f"\n[bold]{t.activity}[/] — {t.share:.0%} of events, ~{t.seconds / 60:.0f} min, "
            f"{format_money(t.copper)} coins"
        )
        top = sorted(t.items.items(), key=lambda kv: -kv[1])[:8]
        for item_id, qty in top:
            m = meta.get(item_id)
            name = m.name if m and m.name else f"item {item_id}"
            console.print(
                f"  {qty:>5} × {name}" + (" [dim](grey)[/]" if m and m.quality == "POOR" else "")
            )
        if len(t.items) > len(top):
            console.print(f"  [dim]... and {len(t.items) - len(top)} more items[/]")
        for c in proposal.candidates.get(t.activity, [])[:3]:
            console.print(f"  [dim]candidate {c.method_id}: {c.overlap:.0%} of loot matches[/]")
    console.print(f"\nProposal: [bold]{proposal.status}[/]")
    for a in proposal.assignments:
        console.print(f"  → {a.method_id} ({a.activity}, {a.seconds / 60:.0f} min)")
    for note in proposal.notes:
        console.print(f"  [dim]• {note}[/]")
    if sessions:
        console.print(
            "Currently applied: "
            + ", ".join(f"{r['method_id']} ({r['duration_s'] / 60:.0f} min)" for r in sessions)
        )


@segments_app.command("accept")
def segments_accept(segment_id: int) -> None:
    """Apply the proposed match (including split mixed segments)."""
    s, conn, row, seg = _open_segment(segment_id)
    try:
        methods = {m.id: m for m in load_methods(s.wowgold_methods_dir)}
        proposal = propose(seg, list(methods.values()), load_item_meta(conn))
        if not proposal.assignments:
            raise typer.BadParameter(
                "nothing to accept: no method matched. Use `segments assign` or `segments draft`."
            )
        profile = _resolve_profile(s, None, None, None, None, None, None)
        n = apply_segment(conn, row["id"], seg, proposal, methods, profile, "applied")
    finally:
        conn.close()
    console.print(f"Applied segment #{segment_id} as {n} session(s).")


@segments_app.command("assign")
def segments_assign(
    segment_id: int,
    method_id: str,
    activity: Annotated[
        str | None,
        typer.Option(
            help="Only this activity's loot and time (herbalism, mining, skinning, "
            "fishing, kill). Default: the whole segment."
        ),
    ] = None,
) -> None:
    """Assign a segment (or one of its activities) to a method by hand."""
    s, conn, row, seg = _open_segment(segment_id)
    try:
        methods = {m.id: m for m in load_methods(s.wowgold_methods_dir)}
        if method_id not in methods:
            raise typer.BadParameter(f"unknown method {method_id!r}")
        acts = split_activities(seg, load_item_meta(conn))
        if activity:
            chosen = [a for a in acts if a.activity == activity]
            if not chosen:
                found = ", ".join(a.activity for a in acts)
                raise typer.BadParameter(f"segment has no {activity} activity (it has: {found})")
            t = chosen[0]
            assignment = Assignment(method_id, t.activity, t.seconds, dict(t.items), t.copper)
        else:
            items: dict[int, int] = {}
            for t in acts:
                if t.activity != OTHER:
                    for i, q in t.items.items():
                        items[i] = items.get(i, 0) + q
            assignment = Assignment(method_id, "manual", seg.active_s, items, seg.copper)
        proposal = Proposal(status="manual", assignments=[assignment], activities=acts)
        profile = _resolve_profile(s, None, None, None, None, None, None)
        apply_segment(conn, row["id"], seg, proposal, methods, profile, "applied")
    finally:
        conn.close()
    console.print(
        f"Segment #{segment_id} → {method_id} ({assignment.seconds / 60:.0f} min). "
        f"See `wowgold methods show {method_id}`."
    )


@segments_app.command("ignore")
def segments_ignore(segment_id: int) -> None:
    """Don't use this segment for calibration (removes any sessions it created)."""
    _s, conn, row, seg = _open_segment(segment_id)
    try:
        clear_segment_sessions(conn, seg.external_id)
        set_segment_status(conn, row["id"], "ignored")
    finally:
        conn.close()
    console.print(f"Segment #{segment_id} ignored.")


@segments_app.command("draft")
def segments_draft(
    segment_id: int,
    activity: Annotated[
        str | None, typer.Option(help="Which activity (default: the main one).")
    ] = None,
) -> None:
    """Print a method definition built from a segment, to paste into data/methods/."""
    s, conn, _row, seg = _open_segment(segment_id)
    try:
        meta = load_item_meta(conn)
    finally:
        conn.close()
    acts = [a for a in split_activities(seg, meta) if a.activity != OTHER]
    if activity:
        acts = [a for a in acts if a.activity == activity]
    if not acts:
        raise typer.BadParameter("no gathering or killing activity in this segment")
    t = acts[0]
    profile = load_profile(s.profile_path)
    skill = profile.professions.get(t.activity) if profile else None
    console.print(draft_method(seg, t, meta, skill), markup=False, highlight=False)
    console.print(
        "\n[dim]Review the TODOs, paste into a file in data/methods/, run "
        "`wowgold methods check`, then `wowgold segments assign "
        f"{segment_id} <new id>` to use this segment as its first measurement.[/]"
    )


if __name__ == "__main__":
    app()


@vanilla_app.command("fetch")
def vanilla_fetch() -> None:
    """Download the VMaNGOS world database (SQLite, ~140 MB, GPL-2.0) into data/vanilla/."""
    from .discovery import vanilla

    s = get_settings()
    with console.status("Downloading VMaNGOS world database..."):
        path, name = vanilla.fetch(s.wowgold_data_dir)
    console.print(f"[green]Installed[/] {name} -> {path}")


@app.command()
def discover(
    cls: ClassOpt = None,
    level: LevelOpt = None,
    faction: FactionOpt = None,
    prof: ProfOpt = None,
    gear: GearOpt = None,
    epic_mount: EpicOpt = None,
    kind: Annotated[str, typer.Option(help="What to look for: all | farm | gather.")] = "all",
    elites: Annotated[bool, typer.Option(help="Include elite mobs.")] = False,
    hours: Annotated[float, typer.Option(help="Length of the play session.")] = 2.0,
    sell_days: Annotated[float, typer.Option(help="Days you're willing to wait to sell.")] = 3.0,
    share: Annotated[
        float, typer.Option(help="Share of daily demand you expect to capture (0-1).")
    ] = 0.5,
    house: Annotated[list[int] | None, typer.Option("--house", "-h", help="House ID(s).")] = None,
    top: Annotated[int, typer.Option(help="How many hypotheses to list.")] = 15,
    details: Annotated[int, typer.Option(help="Show evidence for the top N.")] = 3,
    levels: Annotated[
        str | None,
        typer.Option(help="Leveling sweep: best farms per level, e.g. 20-60 or 15,30,45."),
    ] = None,
    step: Annotated[int, typer.Option(help="Level step for a --levels range.")] = 5,
    per_level: Annotated[int, typer.Option(help="Farms to show per level in a sweep.")] = 3,
    leads: Annotated[
        int, typer.Option(help="Leads to show: captured Forever-only sources without spawn data.")
    ] = 8,
    write: Annotated[
        bool, typer.Option(help="Write the listed hypotheses to data/discovered/hypotheses.yaml.")
    ] = False,
    region: RegionOpt = None,
) -> None:
    """Find farm hypotheses: vanilla loot and spawn data, priced at current AH prices.

    Ranks for your class (kill speed, mage AoE, rogue pickpocketing), level, faction and
    professions. With --levels, shows the best farms at each level while leveling, assuming
    profession skill keeps pace with level (5 per level, capped at what --prof says).

    Mob camps and gathering zones are generated from the VMaNGOS world DB (`wowgold vanilla
    fetch`), then scored like curated methods. Rates are rough guesses: test the promising
    ones in game, then record sessions to measure them."""

    from .discovery.discover import parse_levels, to_yaml_entries
    from .discovery.service import VanillaCache, run_discover

    if kind not in ("all", "farm", "gather"):
        raise typer.BadParameter("--kind must be all, farm or gather")
    try:
        sweep_levels = parse_levels(levels, step) if levels else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    s = get_settings()
    region = region or s.wowgold_region
    if sweep_levels and level is None and not load_profile(s.profile_path):
        level = sweep_levels[-1]
    profile = _resolve_profile(s, cls, level, faction, prof, gear, epic_mount)
    opts = PlanOptions(hours=hours, sell_days=sell_days, market_share=share)
    conn = db.connect(s.db_path)
    try:
        with console.status("Scoring camps, gathering zones and leads..."):
            res = run_discover(
                VanillaCache(s.wowgold_data_dir),
                conn,
                profile,
                opts,
                region=region,
                house=house,
                configured_houses=s.wowgold_houses,
                kind=kind,
                elites=elites,
                levels=sweep_levels,
                top=per_level if sweep_levels else top,
                leads=1 if sweep_levels else leads,
            )
    except (FileNotFoundError, LookupError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if res.sweep:
        _print_sweep(
            profile,
            res.house_ids,
            [(r.profile, r.farms) for r in res.results],
            per_level,
            res.placed,
            res.unplaced,
        )
        best_leads = [(r.profile.level, r.leads[0]) for r in res.results if r.leads]
        if leads and best_leads:
            _print_leads(
                [lead for _, lead in best_leads],
                "Best lead per level",
                levels=[lvl for lvl, _ in best_leads],
            )
        if write:
            chosen, seen = [], set()
            for r in res.results:
                for c in r.farms:
                    if c.method.id not in seen:
                        seen.add(c.method.id)
                        chosen.append(c)
            _write_hypotheses(s, chosen, to_yaml_entries)
        return

    ranked, found_leads = res.results[0].farms, res.results[0].leads
    console.print(
        f"[bold]Hypotheses for {_describe(profile)}[/] · house {', '.join(map(str, res.house_ids))}"
    )
    if res.placed or res.unplaced:
        console.print(
            f"Wowhead captures: {len(res.placed)} item source(s) added to vanilla mobs/nodes, "
            f"{len(res.unplaced)} without open-world spawns (shown as leads)."
        )
    console.print(
        "[yellow]From vanilla 1.12 data: drop rates, spawns and respawns may differ in Forever, "
        "and Forever-only items are missing. Kill and gather rates are rough guesses.[/]"
    )
    table = Table(title="Farms worth testing", title_justify="left")
    for col, just in (
        ("#", "right"),
        ("hypothesis", "left"),
        ("needs", "left"),
        ("gold/h", "right"),
        ("range", "right"),
        ("best items", "left"),
    ):
        table.add_column(col, justify=just)
    for n, c in enumerate(ranked, start=1):
        ev, m = c.evaluation, c.method
        best = sorted((ln for ln in ev.lines if ln.item_id), key=lambda ln: -ln.value)[:3]
        table.add_row(
            str(n),
            f"{m.name}\n[dim]{m.zone} · {m.id}[/]",
            _needs(m),
            _gold(ev.gold_per_hour),
            f"{_gold(ev.low)}-{_gold(ev.high)}",
            ", ".join(ln.name for ln in best),
        )
    console.print(table)
    for c in ranked[:details]:
        console.print(f"\n[bold]{c.method.name}[/] — {c.method.description}")
        for line in c.evidence[:6]:
            console.print(f"  [dim]•[/] {line}")
        for ln in sorted(c.evaluation.lines, key=lambda ln: -ln.value)[:6]:
            label = (
                "coins + vendor trash" if ln.item_id == 0 else f"{ln.produced:>5.0f} × {ln.name}"
            )
            console.print(f"    {label:<34} {_gold(ln.value):>8}")
    if leads and found_leads:
        _print_leads(found_leads, "Leads: Forever-only sources from your Wowhead captures")
    elif leads and not elites and res.hidden_elite_leads:
        console.print(
            f"\n[dim]{res.hidden_elite_leads} captured source(s) without open-world spawns are "
            "elite (mostly dungeon mobs): add --elites to list them as leads.[/]"
        )
    if write:
        _write_hypotheses(s, ranked, to_yaml_entries)


def _print_leads(found, title: str, levels: list[int] | None = None) -> None:
    console.print()
    table = Table(title=title, title_justify="left")
    cols = [("level", "right")] if levels else []
    cols += [
        ("source", "left"),
        ("zone", "left"),
        ("drops (chance, samples)", "left"),
        ("if plentiful", "right"),
        ("notes", "left"),
    ]
    for col, just in cols:
        table.add_column(col, justify=just)
    for n, lead in enumerate(found):
        src = lead.source
        lvl = f"lvl {src.level[0]}-{src.level[1]}" if src.level else ""
        what = {"drop": "kill", "skinning": "skin", "pickpocket": "pickpocket"}.get(
            lead.kind, lead.kind
        )
        drops = "\n".join(
            f"{i.name} {i.chance:.1%}" + (f" (n={i.samples})" if i.samples else "")
            for i in lead.items[:3]
        )
        row = [str(levels[n])] if levels else []
        row += [
            f"{src.source_name}\n[dim]{what} · {src.source_type} {src.source_id} {lvl}[/]",
            ", ".join(lead.zones[:2]) or "?",
            drops,
            f"{_gold(lead.gold_per_hour)}/h",
            ", ".join(lead.flags),
        ]
        table.add_row(*row)
    console.print(table)
    console.print(
        "[dim]No spawn data for these: 'if plentiful' assumes you can keep killing (or "
        "gathering) at full speed and counts only the captured items. Check numbers and "
        "competition in game; Wowhead chances with few samples are noisy.[/]"
    )


def _needs(m) -> str:
    req = m.requirements
    return ", ".join(
        [f"lvl {req.level}"]
        + [f"{p} {v}" for p, v in req.professions.items()]
        + (["elites"] if "elite_mobs" in m.risk else [])
    )


def _write_hypotheses(s: Settings, cands, to_yaml_entries) -> None:
    import yaml

    if not cands:
        return
    out = s.wowgold_data_dir / "discovered" / "hypotheses.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Generated by `wowgold discover`. Copy the ones you want to test into\n"
        "# data/methods/ (e.g. data/methods/hypotheses.yaml), then record sessions.\n"
        + yaml.safe_dump(to_yaml_entries(cands), sort_keys=False, allow_unicode=True)
    )
    console.print(f"\n[green]Wrote {len(cands)} hypotheses[/] to {out}")


def _print_sweep(profile, house_ids, results, per_level, placed, unplaced) -> None:
    profs = ", ".join(f"{p} {v}" for p, v in profile.professions.items()) or "no professions"
    console.print(
        f"[bold]Leveling sweep: {profile.faction} {profile.cls}[/] ({profs} at most; skill "
        f"assumed to keep pace with level) · house {', '.join(map(str, house_ids))}"
    )
    if placed or unplaced:
        console.print(f"Wowhead captures: {len(placed)} item source(s) used.")
    console.print(
        "[yellow]Vanilla data at today's prices: rates are rough guesses, and prices will move "
        "as the server ages.[/]"
    )
    table = Table(title="Best farms by level", title_justify="left")
    for col, just in (
        ("level", "right"),
        ("#", "right"),
        ("hypothesis", "left"),
        ("needs", "left"),
        ("gold/h", "right"),
        ("best items", "left"),
    ):
        table.add_column(col, justify=just)
    for at, ranked in results:
        if not ranked:
            table.add_row(str(at.level), "", "[dim]nothing found[/]", "", "", "", end_section=True)
            continue
        shown = ranked[:per_level]
        for n, c in enumerate(shown, start=1):
            ev = c.evaluation
            best = sorted((ln for ln in ev.lines if ln.item_id), key=lambda ln: -ln.value)[:3]
            table.add_row(
                str(at.level) if n == 1 else "",
                str(n),
                f"{c.method.name}\n[dim]{c.method.zone} · {c.method.id}[/]",
                _needs(c.method),
                _gold(ev.gold_per_hour),
                ", ".join(ln.name for ln in best),
                end_section=n == len(shown),
            )
    console.print(table)


def _serve_ui(port: int, path: str = "/") -> None:
    import uvicorn

    from .web.app import create_app

    url = f"http://127.0.0.1:{port}{path}"
    console.print(f"wowgold UI on [bold]{url}[/]  (Ctrl+C to stop)")
    console.print(f"[dim]Wowhead userscript: http://127.0.0.1:{port}/wowgold-capture.user.js[/]")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def ui(port: Annotated[int, typer.Option(help="Local port.")] = 8765) -> None:
    """Open the local web UI: farms, leveling sweep, leads, Wowhead capture, collector status.

    Listens on 127.0.0.1 only. On WSL, open the printed address in your Windows browser."""
    _serve_ui(port)


@capture_app.command("serve")
def capture_serve(port: Annotated[int, typer.Option(help="Local port.")] = 8765) -> None:
    """Start the web UI on its Capture page (same as `wowgold ui`)."""
    _serve_ui(port, "/capture")


@capture_app.command("sources")
def capture_sources() -> None:
    """Show item sources parsed from your Wowhead captures, and whether discover can use them."""
    from .discovery import vanilla
    from .discovery.discover import VanillaData
    from .wowhead.sources import load_sources

    s = get_settings()
    sources = load_sources(s.wowgold_data_dir)
    if not sources:
        console.print("No sources captured yet. Run `wowgold capture serve`.")
        return
    placed_ids: set[tuple] = set()
    try:
        vconn = vanilla.connect(vanilla.db_path(s.wowgold_data_dir))
        data = VanillaData.load(vconn)
        placed, _ = data.add_sources(sources)
        placed_ids = {(p.item_id, p.kind, p.source_id) for p in placed}
        vconn.close()
    except FileNotFoundError:
        pass
    conn = db.connect(s.db_path)
    try:
        names = {r[0]: r[1] for r in conn.execute("SELECT item_id, name FROM items")}
    finally:
        conn.close()
    table = Table(title="Captured item sources", title_justify="left")
    for col, just in (
        ("item", "left"),
        ("how", "left"),
        ("source", "left"),
        ("chance", "right"),
        ("samples", "right"),
        ("used by discover", "left"),
    ):
        table.add_column(col, justify=just)
    for src in sorted(sources, key=lambda x: (x.item_id, -(x.chance or 0))):
        used = (src.item_id, src.kind, src.source_id) in placed_ids
        table.add_row(
            names.get(src.item_id) or str(src.item_id),
            src.kind,
            f"{src.source_name} [dim]{src.source_type} {src.source_id}[/]",
            f"{src.chance:.1%}" if src.chance is not None else "-",
            f"{src.samples:,}" if src.samples else "-",
            "camp" if used else "[dim]lead (no open-world spawns)[/]",
        )
    console.print(table)


@app.command()
def crafts(
    faction: FactionOpt = None,
    prof: ProfOpt = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Include recipes your skill can't make yet.")
    ] = False,
    min_sales: Annotated[
        float, typer.Option(help="Only outputs selling at least this many per day.")
    ] = 0.0,
    cooldowns: Annotated[bool, typer.Option(help="Include cooldown crafts.")] = True,
    sell_days: Annotated[float, typer.Option(help="Days you're willing to wait to sell.")] = 3.0,
    share: Annotated[
        float, typer.Option(help="Share of daily demand you expect to capture (0-1).")
    ] = 0.5,
    house: Annotated[list[int] | None, typer.Option("--house", "-h", help="House ID(s).")] = None,
    top: Annotated[int, typer.Option(help="How many crafts to list.")] = 20,
    region: RegionOpt = None,
) -> None:
    """Crafts that make money buying the materials, and what to craft from your own mats.

    Vanilla recipes plus Forever recipes from your Wowhead captures. Profession skills and
    faction default to your saved profile."""
    from .crafting.profit import CraftOptions
    from .discovery.service import VanillaCache, run_crafting

    s = get_settings()
    region = region or s.wowgold_region
    saved = load_profile(s.profile_path)
    fac = (faction or (saved.faction if saved else "")).lower()
    try:
        profs = parse_professions(prof) if prof else dict(saved.professions if saved else {})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if fac not in ("horde", "alliance"):
        raise typer.BadParameter("pass --faction or save a profile")
    conn = db.connect(s.db_path)
    try:
        with console.status("Pricing recipes..."):
            res = run_crafting(
                VanillaCache(s.wowgold_data_dir),
                conn,
                faction=fac,
                professions=profs,
                opts=CraftOptions(sell_days=sell_days, market_share=share),
                region=region,
                house=house,
                configured_houses=s.wowgold_houses,
                only_craftable=not everything,
                min_sales=min_sales,
                cooldowns=cooldowns,
                top=top,
            )
    except (FileNotFoundError, LookupError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    shown = ", ".join(f"{p} {v}" for p, v in profs.items()) or "no professions"
    console.print(
        f"[bold]Crafts for {fac} ({shown})[/] · house {', '.join(map(str, res.house_ids))} · "
        f"{res.recipes_total} recipes, {res.recipes_forever} from Wowhead captures"
    )
    table = Table(title="Crafts that make money (buying the materials)", title_justify="left")
    for col, just in (
        ("recipe", "left"),
        ("mats", "right"),
        ("sells for", "right"),
        ("profit", "right"),
        ("crafts/day", "right"),
        ("per day", "right"),
        ("learn", "left"),
    ):
        table.add_column(col, justify=just)
    for e in res.crafts:
        r = e.recipe
        tag = " [yellow]Forever[/]" if r.source == "wowhead" else ""
        table.add_row(
            f"{r.name}{tag}\n[dim]{r.profession} {r.skill} → {e.output_name}[/]",
            _gold(e.cost),
            _gold(e.output_value),
            _gold(e.profit),
            f"{e.crafts_per_day:.1f}\n[dim]{e.limited_by}[/]",
            _gold(e.profit_per_day),
            e.learn_label + (f" {_gold(e.learn_cost)}" if e.learn_cost else ""),
        )
    console.print(table)
    if res.processing:
        proc = Table(title="Craft what you farm", title_justify="left")
        for col, just in (
            ("material", "left"),
            ("sell raw", "right"),
            ("best craft", "left"),
            ("worth crafted", "right"),
            ("per day", "right"),
        ):
            proc.add_column(col, justify=just)
        for p in res.processing[:10]:
            proc.add_row(
                p.name,
                _gold(p.raw_value),
                f"{p.via.output_name}\n[dim]{p.via.recipe.name}[/]",
                _gold(p.value_per_unit),
                f"{p.units_per_day:.1f}" if p.units_per_day < 10 else f"{p.units_per_day:.0f}",
            )
        console.print(proc)
