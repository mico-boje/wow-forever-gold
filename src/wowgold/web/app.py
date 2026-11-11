"""Local web UI: `wowgold ui` serves it on 127.0.0.1.

Pages: Farms (discover + leads + leveling sweep), Capture (Wowhead to-do list and the
userscript endpoint), Status (collector health). The CLI stays the tool for cron and scripts.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

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
from ..discovery.discover import CLASS_KILL_SPEED, parse_levels, to_yaml_entries
from ..discovery.service import VanillaCache, run_discover
from ..methods.models import PROFESSIONS, GearTier
from ..planner.engine import PlanOptions
from ..planner.profile import CharacterProfile
from ..sessions import load_profile, save_profile
from ..wowhead.capture import MAX_BODY, captured, get_token, pick_targets, save_capture, userscript

TEMPLATES = Path(__file__).parent / "templates"
CLASSES = sorted(CLASS_KILL_SPEED)
KINDS = ("all", "farm", "gather")


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


@dataclass(frozen=True)
class SellRate:
    """How fast an item sells on the AH, for showing next to farm yields."""

    per_day: float | None  # units sold per day (measured), or estimated; None = not listed
    measured: bool
    listed: int
    market: int | None

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
    return SellRate(per_day if p.market else None, p.absorb_measured, p.listed_qty, p.market)


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
        q |= {f"prof_{p}": s for p, s in self.professions.items()}
        return urlencode(q)


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
                (r.profile.level, r.farms[0].evaluation.gold_per_hour if r.farms else 0)
                for r in res.results
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
        return render(
            request,
            "capture.html",
            targets=targets,
            vanilla=bool(exclude),
            done=captured(s.wowgold_data_dir),
        )

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
            return PlainTextResponse("bad size", status_code=413)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return PlainTextResponse("bad json", status_code=400)
        if not secrets.compare_digest(str(payload.get("token", "")), token):
            return PlainTextResponse("bad token", status_code=403)
        try:
            key = save_capture(s.wowgold_data_dir, payload)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return JSONResponse({"saved": key})

    # -- status ------------------------------------------------------------------------------

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
