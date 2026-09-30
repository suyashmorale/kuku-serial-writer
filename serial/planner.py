"""Hierarchical season planning: premise -> bible -> acts/arcs -> 200 episode plans -> validation.

Planned in stages (never one giant call) so each stage is checkable and a human can edit
between them. Arc episode plans are generated in parallel: each arc call sees the whole outline,
so arcs stay coherent without needing strictly sequential generation."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import yaml

from . import prompts as P
from .config import Config
from .llm import LLM
from .schemas import ArcEpisodes, Bible, Outline
from .store import Store
from .textutil import content_tokens, jaccard


class PlanError(Exception):
    pass


def validate_outline(o: Outline, total: int, per_arc: int) -> list[str]:
    errs = []
    arcs = sorted(o.arcs, key=lambda a: a.start_ep)
    expect = 1
    for a in arcs:
        if a.start_ep != expect:
            errs.append(f"arc {a.number} starts at {a.start_ep}, expected {expect}")
        if a.end_ep - a.start_ep + 1 != per_arc:
            errs.append(f"arc {a.number} has {a.end_ep - a.start_ep + 1} episodes, expected {per_arc}")
        expect = a.end_ep + 1
    if expect != total + 1:
        errs.append(f"arcs cover episodes 1..{expect - 1}, expected 1..{total}")
    return errs


def generate_plan(store: Store, cfg: Config, llm: LLM, sid: int, on_progress=lambda msg: None) -> list[str]:
    story = store.story(sid)
    premise, brief = story["premise"], story["brief"]
    total, per_arc, n_acts = cfg.story["total_episodes"], cfg.story["episodes_per_arc"], cfg.story["acts"]

    on_progress("Designing series bible…")
    bible = llm.call("bible", P.BIBLE_SYSTEM, P.bible_user(premise, total, brief), schema=Bible, story_id=sid, step="plan:bible")
    store.set_story(sid, bible=bible.model_dump_json())

    on_progress("Structuring acts and arcs…")
    user = P.outline_user(bible.model_dump(), total, per_arc, n_acts, brief)
    outline = None
    for _ in range(2):
        outline = llm.call("outline", P.OUTLINE_SYSTEM, user, schema=Outline, story_id=sid, step="plan:outline")
        errs = validate_outline(outline, total, per_arc)
        if not errs:
            break
        user += "\n\nYour previous outline was invalid: " + "; ".join(errs) + ". Fix it."
    else:
        raise PlanError("outline invalid after retry: " + "; ".join(errs))
    store.set_story(sid, outline=outline.model_dump_json())
    seed_threads(store, sid, bible.model_dump())

    on_progress(f"Planning {total} episodes across {len(outline.arcs)} arcs…")
    od = outline.model_dump()
    arcs = sorted(od["arcs"], key=lambda a: a["number"])
    results = {}

    def plan_arc(i: int):
        arc = arcs[i]
        prev = [f"(arc {arcs[i-1]['number']} summary) {arcs[i-1]['summary']}"] if i else []
        nxt = arcs[i + 1] if i + 1 < len(arcs) else None
        user = P.arc_beats_user(od, arc, prev, nxt)
        for _ in range(2):
            res = llm.call("arc_beats", P.ARC_BEATS_SYSTEM, user, schema=ArcEpisodes, story_id=sid,
                           step=f"plan:arc{arc['number']}")
            nums = sorted(e.number for e in res.episodes)
            if nums == list(range(arc["start_ep"], arc["end_ep"] + 1)):
                return arc["number"], res
            user += f"\n\nYou returned episodes {nums}; return exactly {arc['start_ep']}..{arc['end_ep']}."
        raise PlanError(f"arc {arc['number']} episode numbering invalid")

    with ThreadPoolExecutor(max_workers=4) as pool:
        for num, res in pool.map(plan_arc, range(len(arcs))):
            results[num] = res
            on_progress(f"  arc {num} planned")
    for num, res in results.items():
        store.save_plan_episodes(sid, num, [e.model_dump() for e in res.episodes])

    warnings = validate_plan(store, cfg, sid)
    store.set_story(sid, status="plan_review")
    store.log_event(sid, None, "plan_generated", {"warnings": warnings})
    return warnings


def seed_threads(store: Store, sid: int, bible: dict) -> None:
    with store.tx() as c:
        for t in bible.get("threads", []):
            store.upsert_thread(sid, t["code"], t["title"], t["description"], t["payoff_ep"], True, c)
        for ch in bible.get("characters", []):
            store.ensure_entity(sid, ch["name"], "character", f"{ch['role']}. {ch['description']}", 0, c)
        for loc in bible.get("locations", []):
            store.ensure_entity(sid, loc["name"], "location", loc["description"], 0, c)


def validate_plan(store: Store, cfg: Config, sid: int) -> list[str]:
    """Deterministic plan checks a human would otherwise have to eyeball."""
    total = cfg.story["total_episodes"]
    eps = store.plan_episodes(sid)
    warnings = []
    have = {e["number"] for e in eps}
    missing = sorted(set(range(1, total + 1)) - have)
    if missing:
        warnings.append(f"missing episode plans: {missing[:20]}")
    bible = store.bible(sid)
    used = {t for e in eps for t in e["threads"]}
    for t in bible.get("threads", []):
        if t["code"] not in used:
            warnings.append(f"thread {t['code']} '{t['title']}' never appears in any episode plan")
        else:
            last = max(e["number"] for e in eps if t["code"] in e["threads"])
            if last < t["payoff_ep"] - 5:
                warnings.append(f"thread {t['code']} last appears ep {last} but payoff planned ep {t['payoff_ep']}")
    toks = [(e["number"], content_tokens(e["logline"])) for e in eps]
    for i, (n1, a) in enumerate(toks):
        for n2, b in toks[i + 1:i + 40]:
            if jaccard(a, b) > 0.55:
                warnings.append(f"eps {n1} and {n2} have near-duplicate loglines")
    from .store import name_tokens

    core = [c["name"] for c in bible.get("characters", [])]
    for name in core:
        key = name_tokens(name)
        apps = [e["number"] for e in eps
                if any(name_tokens(n) and (name_tokens(n) <= key or key <= name_tokens(n)) for n in e["characters"])]
        if not apps:
            warnings.append(f"core character {name} never appears")
            continue
        gaps = [b - a for a, b in zip(apps, apps[1:])]
        if gaps and max(gaps) > 30:
            warnings.append(f"{name} disappears for {max(gaps)} episodes")
    return warnings


# ---------- human editing of the plan ----------

def export_plan(store: Store, sid: int, path: str) -> None:
    data = {
        "bible": store.bible(sid),
        "outline": store.outline(sid),
        "episodes": [{k: e[k] for k in ("number", "arc", "title", "logline", "purpose", "characters",
                                         "threads", "turning_point", "hook_idea")} for e in store.plan_episodes(sid)],
    }
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=110)


def import_plan(store: Store, sid: int, path: str) -> list[str]:
    """Human-edited YAML back into canon. Only unwritten episodes may change."""
    with open(path) as f:
        data = yaml.safe_load(f)
    Bible.model_validate(data["bible"])
    Outline.model_validate(data["outline"])
    written = store.last_committed(sid)
    store.set_story(sid, bible=json.dumps(data["bible"]), outline=json.dumps(data["outline"]))
    seed_threads(store, sid, data["bible"])
    changed = []
    for e in data["episodes"]:
        old = store.plan_episode(sid, e["number"])
        if old and all(old[k] == e[k] for k in ("title", "logline", "purpose", "characters", "threads")):
            continue
        if e["number"] <= written:
            changed.append(f"ep {e['number']} already written — plan edit ignored (use `serial edit` for history)")
            continue
        if old:
            store.update_plan_episode(sid, e["number"], "human plan edit", title=e["title"], logline=e["logline"],
                                      purpose=e["purpose"], characters=e["characters"], threads=e["threads"])
        else:
            store.save_plan_episodes(sid, e["arc"], [e])
        changed.append(f"ep {e['number']} updated")
    store.log_event(sid, None, "plan_imported", {"changes": changed})
    return changed
