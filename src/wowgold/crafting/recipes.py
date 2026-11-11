"""Profession recipes: vanilla ones from the VMaNGOS world DB, Forever ones from Wowhead
captures (the "Created by" / "Reagent for" / "Teaches" tables on item pages you opened).

A recipe is a create-item spell: reagents in, an item out. How you learn it matters for
whether it's worth it: from a trainer (cost), a recipe item (vendor or AH price), or it's
known from the start.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..wowhead.capture import capture_dir

# skill line id -> profession name as used in profiles
SKILLS = {
    171: "alchemy",
    164: "blacksmithing",
    333: "enchanting",
    202: "engineering",
    165: "leatherworking",
    197: "tailoring",
    185: "cooking",
    129: "first_aid",
    186: "mining",  # smelting
}
SPELL_EFFECT_CREATE_ITEM = 24
SPELL_EFFECT_LEARN_SPELL = 36
# Wowhead "source" codes on spell rows
TRANSMUTE_COOLDOWN_S = 2 * 86400
WOWHEAD_SOURCE = {1: "crafted", 2: "drop", 4: "quest", 5: "vendor", 6: "trainer", 7: "discovery"}


@dataclass(frozen=True)
class LearnSource:
    kind: str  # trainer | recipe | starter | drop | vendor | quest | discovery | unknown
    cost: int | None = None  # copper (trainer fee or recipe vendor price)
    item_id: int | None = None  # the recipe item, if learned from one
    vendor: bool = False  # the recipe item is sold by a vendor


@dataclass
class Recipe:
    spell_id: int
    name: str
    profession: str
    skill: int  # skill needed to learn/craft it
    reagents: list[tuple[int, float]]  # (item, count per craft)
    output: int
    output_qty: float  # mean items per craft
    cooldown_s: int = 0
    skill_estimated: bool = False
    learn: list[LearnSource] = field(default_factory=list)
    source: str = "vanilla"  # vanilla | wowhead

    @property
    def item_ids(self) -> set[int]:
        return {self.output, *(i for i, _ in self.reagents)}


def _latest_spells(conn: sqlite3.Connection, where: str) -> dict[int, sqlite3.Row]:
    out: dict[int, sqlite3.Row] = {}
    for r in conn.execute(f"SELECT * FROM spell_template WHERE {where} ORDER BY build"):
        out[r["entry"]] = r  # newest build wins
    return out


def load_vanilla_recipes(conn: sqlite3.Connection) -> dict[int, Recipe]:
    """Every item-creating profession spell, with how it's learned."""
    conn.row_factory = sqlite3.Row
    marks = ",".join(str(s) for s in SKILLS)
    ability: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        f"SELECT * FROM skill_line_ability WHERE skill_id IN ({marks}) ORDER BY build"
    ):
        ability[r["spell_id"]] = r
    spells = _latest_spells(conn, f"effect1 = {SPELL_EFFECT_CREATE_ITEM} AND effectItemType1 > 0")

    # learn spell -> craft spell (trainers and recipe items teach through these)
    teaches = {
        e: r["effectTriggerSpell1"]
        for e, r in _latest_spells(
            conn, f"effect1 = {SPELL_EFFECT_LEARN_SPELL} AND effectTriggerSpell1 > 0"
        ).items()
    }
    trainer: dict[int, tuple[int, int]] = {}  # craft spell -> (cost, skill)
    for table in ("npc_trainer", "npc_trainer_template"):
        for r in conn.execute(f"SELECT spell, spellcost, reqskillvalue FROM {table}"):
            craft = teaches.get(r["spell"], r["spell"])
            cost, skill = r["spellcost"], r["reqskillvalue"]
            if craft not in trainer or cost < trainer[craft][0]:
                trainer[craft] = (cost, skill)
    vendor_items = {r[0] for r in conn.execute("SELECT DISTINCT item FROM npc_vendor")}
    recipe_items: dict[int, list[tuple[int, int, int]]] = {}  # craft -> (item, price, skill)
    for r in conn.execute(
        "SELECT entry, spellid_1, spellid_2, buy_price, required_skill_rank FROM item_template "
        "WHERE class = 9"
    ):
        for sp in (r["spellid_1"], r["spellid_2"]):
            if sp and (craft := teaches.get(sp)):
                recipe_items.setdefault(craft, []).append(
                    (r["entry"], r["buy_price"], r["required_skill_rank"])
                )

    out: dict[int, Recipe] = {}
    for sid, ab in ability.items():
        sp = spells.get(sid)
        if sp is None:
            continue
        reagents = [
            (sp[f"reagent{i}"], float(sp[f"reagentCount{i}"]))
            for i in range(1, 9)
            if sp[f"reagent{i}"] and sp[f"reagentCount{i}"]
        ]
        if not reagents:
            continue
        die, bp = sp["effectDieSides1"], sp["effectBasePoints1"]
        qty = bp + (1 + die) / 2 if die > 0 else max(1, bp + (sp["effectBaseDice1"] or 0))
        learn: list[LearnSource] = []
        skill, estimated = None, False
        if sid in trainer:
            cost, skill = trainer[sid]
            learn.append(LearnSource("trainer", cost))
        for item, price, req in recipe_items.get(sid, []):
            vendor = item in vendor_items
            learn.append(LearnSource("recipe", price if vendor else None, item, vendor))
            skill = req if skill is None else min(skill, req)
        if ab["learn_on_get_skill"]:
            learn.append(LearnSource("starter", 0))
            skill = 1 if skill is None else skill
        if skill is None:
            # Not taught anywhere we know of; vanilla turns a recipe orange ~15 points before
            # its yellow threshold (min_value).
            skill, estimated = max(1, (ab["min_value"] or 16) - 15), True
        out[sid] = Recipe(
            spell_id=sid,
            name=sp["name"],
            profession=SKILLS[ab["skill_id"]],
            skill=int(skill),
            reagents=reagents,
            output=sp["effectItemType1"],
            output_qty=float(qty),
            cooldown_s=max(sp["recoveryTime"] or 0, sp["categoryRecoveryTime"] or 0) // 1000,
            skill_estimated=estimated,
            learn=list(dict.fromkeys(learn)) or [LearnSource("unknown")],
        )
    return out


# -- Forever recipes from Wowhead captures -------------------------------------------------------

RECIPE_TABS = ("created-by-spell", "reagent-for", "teaches-recipe")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def recipe_from_wowhead_row(row: dict, recipe_item: int | None = None) -> Recipe | None:
    """A Wowhead spell row: {"id", "name", "creates": [item, min, max], "reagents":
    [[item, n], ...], "learnedat", "skill": [skill line], "source": [...], "trainingcost"}."""
    creates, reagents = row.get("creates"), row.get("reagents")
    skill_line = (row.get("skill") or [None])[0]
    if not (isinstance(creates, list) and creates and isinstance(reagents, list) and reagents):
        return None
    profession = SKILLS.get(int(_num(skill_line) or 0))
    if profession is None or _num(row.get("id")) is None:
        return None
    lo = _num(creates[1]) if len(creates) > 1 else 1
    hi = _num(creates[2]) if len(creates) > 2 else lo
    learn: list[LearnSource] = []
    for code in row.get("source") or []:
        kind = WOWHEAD_SOURCE.get(int(_num(code) or 0), "unknown")
        if kind == "trainer":
            learn.append(LearnSource("trainer", int(_num(row.get("trainingcost")) or 0)))
        elif kind != "crafted":
            learn.append(LearnSource(kind))
    if recipe_item:
        learn.append(LearnSource("recipe", None, recipe_item))
    name = str(row.get("name") or "").lstrip("0123456789")
    return Recipe(
        spell_id=int(row["id"]),
        name=name,
        profession=profession,
        skill=int(_num(row.get("learnedat")) or 1),
        reagents=[
            (int(r[0]), float(r[1]))
            for r in reagents
            if isinstance(r, list) and len(r) == 2 and _num(r[0]) and _num(r[1])
        ],
        output=int(creates[0]),
        output_qty=((lo or 1) + (hi or lo or 1)) / 2,
        # Wowhead rows carry no cooldown; vanilla transmutes share a 2-day one.
        cooldown_s=TRANSMUTE_COOLDOWN_S if name.startswith("Transmute") else 0,
        learn=list(dict.fromkeys(learn)) or [LearnSource("unknown")],
        source="wowhead",
    )


def load_wowhead_recipes(data_dir: Path) -> dict[int, Recipe]:
    out: dict[int, Recipe] = {}
    for path in sorted(capture_dir(data_dir).glob("item-*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        page_item = int(path.stem.split("-", 1)[1])
        for tab, lv in (record.get("listviews") or {}).items():
            if tab not in RECIPE_TABS or not isinstance(lv, dict):
                continue
            for row in lv.get("data") or []:
                if not isinstance(row, dict):
                    continue
                recipe = recipe_from_wowhead_row(
                    row, page_item if tab == "teaches-recipe" else None
                )
                if recipe is None:
                    continue
                prev = out.get(recipe.spell_id)
                if prev and not any(ls.kind == "recipe" for ls in recipe.learn):
                    # keep the recipe-item link learned from a "teaches" tab
                    recipe.learn += [ls for ls in prev.learn if ls.kind == "recipe"]
                out[recipe.spell_id] = recipe
    return out


def merge_recipes(vanilla: dict[int, Recipe], forever: dict[int, Recipe]) -> dict[int, Recipe]:
    """Wowhead (Forever) data wins for the same spell; its learn sources are added to vanilla's
    when Wowhead doesn't say how it's learned."""
    out = dict(vanilla)
    for sid, r in forever.items():
        if sid in out and all(ls.kind == "unknown" for ls in r.learn):
            r.learn = out[sid].learn
        out[sid] = r
    return out
