"""Local web UI: `wowgold ui` serves it on 127.0.0.1.

Pages: Farms (discover + leads + leveling sweep), Capture (Wowhead to-do list and the
userscript endpoint), Status (collector health). The CLI stays the tool for cron and scripts.
"""

from __future__ import annotations

import json
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import Settings, get_settings
from ..crafting.profit import CraftOptions
from ..crafting.recipes import recipe_counts
from ..discovery.discover import CLASS_KILL_SPEED, parse_levels, to_yaml_entries
from ..discovery.newitems import STATUS_LABEL, STATUS_ORDER
from ..discovery.service import VanillaCache, run_crafting, run_discover, run_new_items
from ..market.firstseen import new_arrivals
from ..market.myhouse import find_listing, load_my_houses, set_my_house
from ..market.query import find_items
from ..market.trends import HORIZON_DAYS, Trend
from ..methods.models import PROFESSIONS, GearTier
from ..planner.engine import PlanOptions
from ..planner.pricing import ah_cut, build_price_book
from ..planner.profile import CharacterProfile
from ..sessions import load_profile, parse_coins, save_profile
from ..wowhead.capture import (
    ITEM_LIST_PAGES,
    MAX_BODY,
    PROFESSION_PAGES,
    USERSCRIPT_VERSION,
    captured,
    get_token,
    last_capture,
    page_key,
    pick_targets,
    save_capture,
    script_version,
    userscript,
)
from ..wowhead.itemlists import list_page_counts
from ..wowhead.names import fetch_tooltip_names, unnamed_item_ids
from ..wowhead.refresh import (
    load_settings,
    mark_patch,
    recapture_queue,
    recent_changes,
    save_settings,
)

TEMPLATES = Path(__file__).parent / "templates"
CLASSES = sorted(CLASS_KILL_SPEED)
KINDS = ("all", "farm", "gather")


class NamesJob:
    """Background lookup of item names on Wowhead's tooltip API (one per second)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = {"running": False, "done": 0, "total": 0, "found": 0, "last": None}

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, db_path, ids: list[int]) -> None:
        with self._lock:
            if self._state["running"] or not ids:
                return
            self._stop.clear()
            self._state = {
                "running": True,
                "done": 0,
                "total": len(ids),
                "found": 0,
                "last": None,
            }

        def progress(n: int, total: int, name: str | None) -> None:
            with self._lock:
                self._state.update(done=n, last=name)
                if name:
                    self._state["found"] += 1

        def run() -> None:
            conn = db.connect(db_path)
            try:
                fetch_tooltip_names(conn, ids, on_progress=progress, should_stop=self._stop.is_set)
            finally:
                conn.close()
                with self._lock:
                    self._state["running"] = False
                log(f"name lookup finished: {self._state['found']} of {len(ids)} found")

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def ah_item_ids(conn, house_ids: list[int]) -> set[int]:
    """Items listed in the latest snapshots of these houses."""
    if not house_ids:
        return set()
    marks = ",".join("?" * len(house_ids))
    return {
        r[0]
        for r in conn.execute(
            f"""SELECT DISTINCT item_id FROM price_stats WHERE snapshot_id IN (
                    SELECT MAX(id) FROM snapshots WHERE house_id IN ({marks})
                      AND outcome = 'stored' AND ingested_at IS NOT NULL GROUP BY house_id, kind)
                  AND quantity > 0""",
            house_ids,
        )
    }


def log(msg: str) -> None:
    """One line in the terminal running `wowgold ui`."""
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def gold(copper: float | None) -> str:
    if copper is None:
        return "-"
    if abs(copper) >= 10000:
        g = copper / 10000
        return f"{g:,.0f}g" if abs(g) >= 100 else f"{g:,.1f}g"
    if abs(copper) >= 100:
        return f"{copper / 100:.0f}s"
    return f"{copper:.0f}c"


def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    mins = int((datetime.now(UTC) - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def duration(seconds: float | None) -> str:
    """1 h 05 min, 12 min, 40 s."""
    if seconds is None:
        return "—"
    secs = int(seconds)
    if secs < 60:
        return f"{secs} s"
    mins = secs // 60
    return f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"


@dataclass(frozen=True)
class SellRate:
    """How fast an item sells on the AH, for showing next to farm yields."""

    per_day: float | None  # units sold per day (measured), or estimated; None = not listed
    measured: bool
    listed: int
    market: int | None
    trend: Trend | None = None
    sell_factor: float = 1.0

    def trend_label(self) -> str:
        return self.trend.label() if self.trend and self.market is not None else ""

    def trend_title(self) -> str:
        t = self.trend
        if not t or self.market is None:
            return ""
        parts = []
        if t.drift is not None and t.trust:
            parts.append(f"trend {t.drift * 100:+.0f}% per day over {t.history_h:.0f}h")
        if t.change_24h is not None:
            parts.append(f"{t.change_24h * 100:+.0f}% vs a day ago")
        if t.change_7d is not None:
            parts.append(f"{t.change_7d * 100:+.0f}% vs a week ago")
        if t.swing is not None and t.trust:
            parts.append(f"swings ±{t.swing * 100:.0f}% around the trend")
        if self.sell_factor < 0.995:
            parts.append(
                f"valued at {self.sell_factor * 100:.0f}% of today's price (where the trend "
                f"puts it in {HORIZON_DAYS:g} days)"
            )
        return "; ".join(parts)

    @property
    def trend_class(self) -> str:
        t = self.trend
        if not t or self.market is None or t.drift is None or not t.trust:
            return ""
        return "down" if t.falling else "swing" if t.swings else "up" if t.drift > 0.01 else ""

    def label(self) -> str:
        if self.market is None:
            return "not on AH"
        if self.per_day is None:
            return "?"
        n = self.per_day
        txt = f"{n:,.0f}" if n >= 10 else f"{n:.1f}"
        return f"{txt}/day" if self.measured else f"~{txt}/day"

    def days_to_sell(self, units: float) -> float | None:
        """Days of current AH sales that `units` would take (None if unknown)."""
        if self.market is None or not self.per_day:
            return None
        return units / self.per_day


def sell_rate(book, item_id: int) -> SellRate:
    p = book.get(item_id)
    per_day = p.sold_per_day if p.absorb_measured else p.absorb_per_day
    return SellRate(
        per_day if p.market else None,
        p.absorb_measured,
        p.listed_qty,
        p.market,
        p.trend,
        p.sell_factor,
    )


# -- the farms form --------------------------------------------------------------------------


@dataclass
class FarmForm:
    cls: str = ""
    level: int | None = None
    faction: str = ""
    gear: str = GearTier.FRESH.value
    epic_mount: bool = False
    professions: dict[str, int] = field(default_factory=dict)
    kind: str = "all"
    elites: bool = False
    levels: str = ""
    step: int = 5
    hours: float = 2.0
    sell_days: float = 3.0
    share: float = 0.5
    house: int | None = None
    top: int = 15
    leads: int = 8
    craft: bool = False
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_request(cls, params, saved: CharacterProfile | None) -> FarmForm:
        f = cls()
        if saved:
            f.cls, f.level, f.faction = saved.cls, saved.level, saved.faction
            f.gear, f.epic_mount = saved.gear.value, saved.epic_mount
            f.professions = dict(saved.professions)
        if not params.get("submitted"):
            return f

        def num(name, conv, default):
            raw = (params.get(name) or "").strip()
            if not raw:
                return default
            try:
                return conv(raw)
            except ValueError:
                f.errors.append(f"{name}: not a number")
                return default

        f.cls = params.get("cls", f.cls)
        f.level = num("level", int, None)
        f.faction = params.get("faction", f.faction)
        f.gear = params.get("gear", f.gear)
        f.epic_mount = params.get("epic_mount") == "on"
        f.professions = {}
        for p in PROFESSIONS:
            if (skill := num(f"prof_{p}", int, None)) is not None and skill > 0:
                f.professions[p] = min(skill, 300)
        f.kind = params.get("kind", "all") if params.get("kind") in KINDS else "all"
        f.elites = params.get("elites") == "on"
        f.craft = params.get("craft") == "on"
        f.levels = (params.get("levels") or "").strip()
        f.step = num("step", int, 5)
        f.hours = num("hours", float, 2.0)
        f.sell_days = num("sell_days", float, 3.0)
        f.share = num("share", float, 0.5)
        f.house = num("house", int, None)
        f.top = max(1, min(num("top", int, 15), 100))
        f.leads = max(0, min(num("leads", int, 8), 100))
        return f

    def profile(self) -> CharacterProfile | None:
        try:
            return CharacterProfile.model_validate(
                {
                    "class": self.cls,
                    "level": self.level or 60,
                    "faction": self.faction,
                    "professions": self.professions,
                    "gear": self.gear,
                    "epic_mount": self.epic_mount,
                }
            )
        except ValueError as exc:
            self.errors.append(str(exc).splitlines()[-1] if str(exc) else "invalid profile")
            return None

    def sweep_levels(self) -> list[int] | None:
        if not self.levels:
            return None
        try:
            return parse_levels(self.levels, self.step)
        except ValueError as exc:
            self.errors.append(str(exc))
            return None

    def query(self) -> str:
        q = {
            "submitted": "1",
            "cls": self.cls,
            "level": self.level or "",
            "faction": self.faction,
            "gear": self.gear,
            "kind": self.kind,
            "levels": self.levels,
            "step": self.step,
            "hours": self.hours,
            "sell_days": self.sell_days,
            "share": self.share,
            "house": self.house or "",
            "top": self.top,
            "leads": self.leads,
        }
        if self.epic_mount:
            q["epic_mount"] = "on"
        if self.elites:
            q["elites"] = "on"
        if self.craft:
            q["craft"] = "on"
        q |= {f"prof_{p}": s for p, s in self.professions.items()}
        return urlencode(q)


CRAFT_PROFESSIONS = (
    "alchemy",
    "blacksmithing",
    "enchanting",
    "engineering",
    "leatherworking",
    "tailoring",
    "cooking",
    "first_aid",
    "mining",
)


@dataclass
class CraftForm:
    faction: str = ""
    professions: dict[str, int] = field(default_factory=dict)
    only_craftable: bool = True
    cooldowns: bool = True
    min_sales: float = 0.0
    sell_days: float = 3.0
    share: float = 0.5
    house: int | None = None
    top: int = 50
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_request(cls, params, saved: CharacterProfile | None) -> CraftForm:
        f = cls()
        if saved:
            f.faction = saved.faction
            f.professions = {p: v for p, v in saved.professions.items() if p in CRAFT_PROFESSIONS}
        if not params.get("submitted"):
            return f

        def num(name, conv, default):
            raw = (params.get(name) or "").strip()
            if not raw:
                return default
            try:
                return conv(raw)
            except ValueError:
                f.errors.append(f"{name}: not a number")
                return default

        f.faction = params.get("faction", f.faction)
        f.professions = {}
        for p in CRAFT_PROFESSIONS:
            if (skill := num(f"prof_{p}", int, None)) is not None and skill > 0:
                f.professions[p] = min(skill, 300)
        f.only_craftable = params.get("only_craftable") == "on"
        f.cooldowns = params.get("cooldowns") == "on"
        f.min_sales = max(0.0, num("min_sales", float, 0.0))
        f.sell_days = num("sell_days", float, 3.0)
        f.share = num("share", float, 0.5)
        f.house = num("house", int, None)
        f.top = max(1, min(num("top", int, 50), 300))
        if f.faction not in ("horde", "alliance"):
            f.errors.append("pick a faction")
        return f


@dataclass
class VerifyPage:
    key: str  # capture key, e.g. "npc-6559"
    kind: str  # npc | object
    entity_id: int
    name: str
    farms: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.wowhead.com/forever/{self.kind}={self.entity_id}"


# -- app -------------------------------------------------------------------------------------


def create_app(settings: Settings | None = None, cache: VanillaCache | None = None) -> FastAPI:
    s = settings or get_settings()
    app = FastAPI(title="wowgold", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=TEMPLATES)
    templates.env.filters["gold"] = gold
    templates.env.filters["ago"] = ago
    templates.env.filters["dur"] = duration
    cache = cache or VanillaCache(s.wowgold_data_dir)
    token = get_token(s.wowgold_data_dir)

    @contextmanager
    def connect():
        conn = db.connect(s.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"page": name.split(".")[0], **ctx})

    def same_origin(request: Request) -> bool:
        """Block form posts from other websites (the app has no login)."""
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return True
        return urlparse(origin).netloc == request.url.netloc

    def compute(form: FarmForm):
        profile = form.profile()
        levels = form.sweep_levels()
        if form.errors or profile is None:
            return None, None
        if not cache.available:
            return None, "The vanilla database is missing: run `wowgold vanilla fetch`."
        opts = PlanOptions(hours=form.hours, sell_days=form.sell_days, market_share=form.share)
        with connect() as conn:
            try:
                res = run_discover(
                    cache,
                    conn,
                    profile,
                    opts,
                    region=s.wowgold_region,
                    house=[form.house] if form.house else None,
                    configured_houses=s.wowgold_houses,
                    kind=form.kind,
                    elites=form.elites,
                    levels=levels,
                    top=3 if levels else form.top,
                    leads=1 if levels else form.leads,
                    craft=form.craft,
                )
            except LookupError as exc:
                return None, str(exc)
        return res, None

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/farms")

    @app.get("/farms", response_class=HTMLResponse)
    def farms(request: Request) -> HTMLResponse:
        saved = load_profile(s.profile_path)
        form = FarmForm.from_request(request.query_params, saved)
        run = bool(request.query_params.get("submitted")) or saved is not None
        res, error = compute(form) if run and form.cls and form.faction else (None, None)
        with connect() as conn:
            houses = [dict(r) for r in db.list_houses(conn, s.wowgold_region)]
        chart = None
        if res and res.sweep:
            best = [
                (r.profile.level, r.farms[0].total_per_hour if r.farms else 0) for r in res.results
            ]
            top = max((v for _, v in best), default=0) or 1
            chart = [(lvl, v, v / top) for lvl, v in best]
        return render(
            request,
            "farms.html",
            form=form,
            res=res,
            error=error,
            chart=chart,
            houses=houses,
            classes=CLASSES,
            professions=PROFESSIONS,
            gears=[g.value for g in GearTier],
            saved=saved,
            saved_msg=request.query_params.get("saved"),
            rate=(lambda item_id: sell_rate(res.book, item_id)) if res and res.book else None,
        )

    @app.post("/profile")
    async def save(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        form = FarmForm.from_request(await request.form(), None)
        profile = form.profile()
        if profile is None or form.level is None:
            return RedirectResponse(f"/farms?{form.query()}&saved=error", status_code=303)
        save_profile(s.profile_path, profile)
        return RedirectResponse(f"/farms?{form.query()}&saved=1", status_code=303)

    @app.get("/farms/hypotheses.yaml")
    def hypotheses(request: Request) -> Response:
        form = FarmForm.from_request(request.query_params, load_profile(s.profile_path))
        res, error = compute(form)
        if res is None:
            return PlainTextResponse(error or "; ".join(form.errors), status_code=400)
        chosen, seen = [], set()
        for r in res.results:
            for c in r.farms:
                if c.method.id not in seen:
                    seen.add(c.method.id)
                    chosen.append(c)
        body = (
            "# Generated by wowgold. Copy the ones you want to test into data/methods/,\n"
            "# then record sessions.\n"
            + yaml.safe_dump(to_yaml_entries(chosen), sort_keys=False, allow_unicode=True)
        )
        return Response(
            body,
            media_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="hypotheses.yaml"'},
        )

    # -- crafting ----------------------------------------------------------------------------

    @app.get("/crafting", response_class=HTMLResponse)
    def crafting(request: Request) -> HTMLResponse:
        saved = load_profile(s.profile_path)
        form = CraftForm.from_request(request.query_params, saved)
        res, error = None, None
        run = bool(request.query_params.get("submitted")) or bool(saved and form.professions)
        if run and not form.errors and form.faction:
            if not cache.available:
                error = "The vanilla database is missing: run `wowgold vanilla fetch`."
            else:
                with connect() as conn:
                    try:
                        res = run_crafting(
                            cache,
                            conn,
                            faction=form.faction,
                            professions=form.professions,
                            opts=CraftOptions(sell_days=form.sell_days, market_share=form.share),
                            region=s.wowgold_region,
                            house=[form.house] if form.house else None,
                            configured_houses=s.wowgold_houses,
                            only_craftable=form.only_craftable,
                            min_sales=form.min_sales,
                            cooldowns=form.cooldowns,
                            top=form.top,
                        )
                    except LookupError as exc:
                        error = str(exc)
        with connect() as conn:
            houses = [dict(r) for r in db.list_houses(conn, s.wowgold_region)]
        return render(
            request,
            "crafting.html",
            form=form,
            res=res,
            error=error,
            houses=houses,
            professions=CRAFT_PROFESSIONS,
            rate=(lambda item_id: sell_rate(res.book, item_id)) if res else None,
        )

    # -- capture -----------------------------------------------------------------------------

    @app.get("/capture", response_class=HTMLResponse)
    def capture_page(request: Request) -> HTMLResponse:
        saved = load_profile(s.profile_path)
        if request.query_params.get("submitted"):
            return verify_page(request, saved)
        exclude = set()
        if cache.available:
            data, *_ = cache.with_captures()
            exclude = set(data.items)
        with connect() as conn:
            houses = s.wowgold_houses or [
                r["house_id"]
                for r in db.list_houses(conn, s.wowgold_region)
                if not saved or r["faction"] == saved.faction
            ]
            targets = pick_targets(conn, s.wowgold_region, houses, exclude, limit=40)
            done = captured(s.wowgold_data_dir)
            # what items trade for per day, to rank recaptures and new arrivals
            book = build_price_book(conn, s.wowgold_region, houses, ah_item_ids(conn, houses))
            arrivals = [
                (i, ts, book.get(i))
                for i, ts in new_arrivals(conn, s.wowgold_region).items()
                if i not in exclude and f"item-{i}" not in done
            ]
            names = dict(conn.execute("SELECT item_id, name FROM items WHERE name IS NOT NULL"))

        def item_value(item_id: int) -> float:
            p = book.get(item_id)
            if not p.market:
                return 0.0
            return p.market * ((p.sold_per_day if p.absorb_measured else p.absorb_per_day) or 0)

        arrivals.sort(key=lambda a: -item_value(a[0]))
        queue = recapture_queue(s.wowgold_data_dir, item_value)
        counts = recipe_counts(s.wowgold_data_dir)
        recipe_pages = [
            (key, name, url, counts.get(key))
            for name, url in PROFESSION_PAGES
            if (key := page_key(url))
        ]
        last = last_capture(s.wowgold_data_dir)
        return render(
            request,
            "capture.html",
            targets=targets,
            recipe_pages=recipe_pages,
            last=last,
            script_version=USERSCRIPT_VERSION,
            vanilla=bool(exclude),
            done=done,
            arrivals=[(i, ts, p, names.get(i) or f"item {i}") for i, ts, p in arrivals[:40]],
            queue=queue[:60],
            queue_total=len(queue),
            changes=recent_changes(s.wowgold_data_dir),
            refresh=load_settings(s.wowgold_data_dir),
            saved_msg=request.query_params.get("refresh"),
        )

    @app.post("/capture/refresh")
    async def capture_refresh(request: Request) -> Response:
        """Refresh settings: outdated after N days, or mark a new patch (all outdated now)."""
        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        form = await request.form()
        action = form.get("action")
        if action == "mark-patch":
            mark_patch(s.wowgold_data_dir)
        elif action == "clear-mark":
            save_settings(s.wowgold_data_dir, clear_mark=True)
        elif form.get("stale_days"):
            try:
                save_settings(s.wowgold_data_dir, stale_days=int(form["stale_days"]))
            except ValueError:
                return PlainTextResponse("bad number", status_code=400)
        return RedirectResponse(f"/capture?refresh={action or 'days'}", status_code=303)

    def verify_page(request: Request, saved: CharacterProfile | None) -> HTMLResponse:
        """To-do list of the Wowhead NPC/object pages behind the farms on the Farms page."""
        form = FarmForm.from_request(request.query_params, saved)
        res, error = compute(form)
        pages: dict[str, VerifyPage] = {}
        farms = 0
        for r in res.results if res else []:
            for c in r.farms:
                farms += 1
                label = c.method.name + (f" (lvl {r.profile.level})" if res.sweep else "")
                for kind, eid, name in c.pages:
                    key = f"{kind}-{eid}"
                    page = pages.setdefault(key, VerifyPage(key, kind, eid, name))
                    if label not in page.farms:
                        page.farms.append(label)
        return render(
            request,
            "capture.html",
            verify=list(pages.values()),
            verify_farms=farms,
            error=error or "; ".join(form.errors),
            back=f"/farms?{form.query()}",
            done=captured(s.wowgold_data_dir),
            targets=[],
            vanilla=True,
        )

    @app.get("/capture/status")
    def capture_status() -> JSONResponse:
        return JSONResponse(captured(s.wowgold_data_dir))

    @app.get("/wowgold-capture.user.js")
    def script(request: Request) -> Response:
        return Response(
            userscript(request.url.port or 80, token),
            media_type="application/javascript; charset=utf-8",
        )

    @app.post("/capture")
    async def capture_post(request: Request) -> Response:
        body = await request.body()
        if not 0 < len(body) <= MAX_BODY:
            log(f"capture refused: {len(body):,} bytes")
            return PlainTextResponse("bad size", status_code=413)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return PlainTextResponse("bad json", status_code=400)
        if not secrets.compare_digest(str(payload.get("token", "")), token):
            log(f"capture refused: wrong token from {payload.get('url', '?')}")
            return PlainTextResponse("bad token", status_code=403)
        try:
            key = save_capture(s.wowgold_data_dir, payload)
        except ValueError as exc:
            log(f"capture refused: {exc} ({payload.get('url', '?')})")
            return PlainTextResponse(str(exc), status_code=400)
        version = script_version(payload)
        old = " — outdated userscript, reinstall it" if version < USERSCRIPT_VERSION else ""
        log(f"captured {key} (userscript v{version}){old}")
        return JSONResponse({"saved": key})

    # -- new in Forever ----------------------------------------------------------------------

    names_job = NamesJob()

    @app.get("/new", response_class=HTMLResponse)
    def new_items(request: Request) -> HTMLResponse:
        saved = load_profile(s.profile_path)
        q = request.query_params
        faction = q.get("faction") or (saved.faction if saved else "")
        status = q.get("status") or ""
        only_new = q.get("new") == "1"
        search = (q.get("q") or "").strip().lower()
        res, error = None, None
        if faction not in ("horde", "alliance"):
            error = "Pick a faction (or save a profile on the Farms page)."
        elif not cache.available:
            error = "The vanilla database is missing: run `wowgold vanilla fetch`."
        else:
            with connect() as conn:
                try:
                    res = run_new_items(
                        cache,
                        conn,
                        faction=faction,
                        region=s.wowgold_region,
                        configured_houses=s.wowgold_houses,
                    )
                except LookupError as exc:
                    error = str(exc)
        items = []
        if res:
            items = [
                it
                for it in res.overview.items
                if (not status or it.status == status)
                and (not only_new or it.is_new)
                and (not search or search in it.name.lower() or search == str(it.item_id))
            ]
        counts = list_page_counts(s.wowgold_data_dir)
        list_pages = [
            (key, name, url, counts.get(key))
            for name, url in ITEM_LIST_PAGES
            if (key := page_key(url))
        ]
        return render(
            request,
            "new.html",
            res=res,
            error=error,
            faction=faction,
            status=status,
            only_new=only_new,
            search=q.get("q") or "",
            items=items[:300],
            shown_of=len(items),
            labels=STATUS_LABEL,
            order=STATUS_ORDER,
            list_pages=list_pages,
            done=captured(s.wowgold_data_dir),
            job=names_job.state(),
            rate=(lambda item_id: sell_rate(res.book, item_id)) if res and res.book else None,
        )

    @app.post("/new/fetch-names")
    async def fetch_names(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        saved = load_profile(s.profile_path)
        faction = (await request.form()).get("faction") or (saved.faction if saved else "horde")
        with connect() as conn:
            res = run_new_items(
                cache,
                conn,
                faction=faction,
                region=s.wowgold_region,
                configured_houses=s.wowgold_houses,
            )
            ids = unnamed_item_ids(
                conn, {it.item_id for it in res.overview.items if it.name.startswith("item ")}
            )
        names_job.start(s.db_path, ids)
        return RedirectResponse(f"/new?faction={faction}", status_code=303)

    @app.get("/new/fetch-names/status")
    def fetch_names_status() -> JSONResponse:
        return JSONResponse(names_job.state())

    @app.post("/new/fetch-names/stop")
    async def fetch_names_stop(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        names_job.stop()
        return JSONResponse(names_job.state())

    # -- houses ------------------------------------------------------------------------------

    @app.get("/houses", response_class=HTMLResponse)
    def houses_page(request: Request) -> HTMLResponse:
        q = request.query_params
        item_q, price_q = (q.get("item") or "").strip(), (q.get("price") or "").strip()
        matches, error, item_name, copper = None, None, None, None
        with connect() as conn:
            houses = [dict(r) for r in db.list_houses(conn, s.wowgold_region)]
            if item_q and price_q:
                try:
                    copper = parse_coins(price_q)
                    found = find_items(conn, item_q, limit=1)
                    if not found:
                        error = f"No item called {item_q!r} in the item list."
                    else:
                        item_name = found[0].name or item_q
                        matches = find_listing(conn, s.wowgold_region, found[0].item_id, copper)
                except ValueError as exc:
                    error = str(exc)
        mine = load_my_houses(s.wowgold_data_dir, s.wowgold_region)
        for h in houses:
            h["cut"] = ah_cut(h["faction"])
            h["mine"] = mine.get(h["faction"]) == h["house_id"]
        return render(
            request,
            "houses.html",
            houses=houses,
            mine=mine,
            configured=s.wowgold_houses,
            item=item_q,
            price=price_q,
            item_name=item_name,
            copper=copper,
            matches=matches,
            error=error,
            saved=q.get("saved"),
        )

    @app.post("/houses/mine")
    async def houses_mine(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        form = await request.form()
        try:
            house_id = int(form.get("house_id") or "")
        except ValueError:
            return PlainTextResponse("bad house", status_code=400)
        with connect() as conn:
            row = conn.execute(
                "SELECT faction FROM houses WHERE region = ? AND house_id = ?",
                (s.wowgold_region, house_id),
            ).fetchone()
        if row is None or not row[0]:
            return PlainTextResponse("unknown house", status_code=400)
        set_my_house(s.wowgold_data_dir, s.wowgold_region, row[0], house_id)
        return RedirectResponse(f"/houses?saved={house_id}", status_code=303)

    # -- status ------------------------------------------------------------------------------

    # -- recorded sessions (WowGoldLogger addon) ---------------------------------------------

    def _addon_files():
        from ..play.recordings import find_files

        return find_files(s.wowgold_addon_file)

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions_page(request: Request) -> HTMLResponse:
        from ..play.service import sessions_view

        with connect() as conn:
            view = sessions_view(conn, cache, s.wowgold_region, s.wowgold_houses)
        return render(
            request,
            "sessions.html",
            view=view,
            files=_addon_files(),
            configured=s.wowgold_addon_file,
            imported=request.query_params.get("imported"),
        )

    @app.get("/sessions/{rid}", response_class=HTMLResponse)
    def session_page(request: Request, rid: str) -> HTMLResponse:
        from ..play.service import sessions_view

        with connect() as conn:
            view = sessions_view(conn, cache, s.wowgold_region, s.wowgold_houses, rid=rid)
        if not view.digests:
            return HTMLResponse("No such session.", status_code=404)
        d = view.digests[0]
        return render(
            request,
            "session.html",
            d=d,
            book=view.books.get(d.rec.faction or "horde"),
            vanilla=view.vanilla,
            speed=view.calibration.kill_speed.get(d.rec.cls or ""),
        )

    @app.post("/sessions/import")
    def sessions_import(request: Request) -> Response:
        from ..play.recordings import auto_import

        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        with connect() as conn:
            res = auto_import(conn, s.wowgold_data_dir, s.wowgold_addon_file, force=True)
        r = res.result
        msg = (
            f"{r.new} new, {r.updated} updated, {r.unchanged} unchanged"
            if res.files
            else "no addon file found"
        )
        if res.errors:
            msg += "; " + "; ".join(res.errors)
        return RedirectResponse(f"/sessions?imported={quote(msg)}", status_code=303)

    @app.post("/sessions/{rid}/hide")
    async def session_hide(request: Request, rid: str) -> Response:
        from ..play.recordings import set_hidden

        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", status_code=403)
        hidden = (await request.form()).get("hidden") == "1"
        with connect() as conn:
            set_hidden(conn, rid, hidden)
        return RedirectResponse(f"/sessions/{quote(rid)}", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request) -> HTMLResponse:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        with connect() as conn:
            last = conn.execute(
                "SELECT MAX(fetched_at) FROM snapshots WHERE outcome = 'stored'"
            ).fetchone()[0]
            first = conn.execute("SELECT MIN(fetched_at) FROM snapshots").fetchone()[0]
            day = conn.execute(
                "SELECT outcome, COUNT(*) FROM snapshots WHERE fetched_at >= ? GROUP BY outcome",
                (since,),
            ).fetchall()
            houses = len(db.list_houses(conn, s.wowgold_region))
            items, named = conn.execute("SELECT COUNT(*), COUNT(name) FROM items").fetchone()
        log = s.wowgold_data_dir / "collect.log"
        tail = log.read_text(errors="replace").splitlines()[-40:] if log.exists() else []
        version = s.wowgold_data_dir / "vanilla" / "VERSION"
        return render(
            request,
            "status.html",
            last=last,
            first=first,
            day={r[0]: r[1] for r in day},
            houses=houses,
            items=items,
            named=named,
            tail=tail,
            vanilla=version.read_text().strip() if version.exists() else None,
            captures=len(captured(s.wowgold_data_dir)),
        )

    return app
