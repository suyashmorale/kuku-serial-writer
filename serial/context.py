"""Context-pack assembler: the layered memory that keeps episode 150 as consistent as episode 5.

Pack size is bounded regardless of episode number:
  L0 bible (system prompt, cached)      L1 arc plan window (this ep + next 9)
  L2 story-so-far + current-arc recap   L3 canon cards for on-stage entities + full roster (1 line each)
  L4 open/overdue/due threads           L5 recent hooks + similar past beats (anti-repetition)
  L6 active directives                  + previous episode verbatim for voice continuity
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Config
from .store import Store
from .textutil import content_tokens, jaccard


@dataclass
class Pack:
    ep: int
    plan: dict
    arc: dict
    sections: dict[str, str] = field(default_factory=dict)
    directive_ids: list[int] = field(default_factory=list)
    entity_ids: list[int] = field(default_factory=list)

    def render(self, *names: str) -> str:
        names = names or tuple(self.sections)
        return "\n\n".join(f"## {n.upper().replace('_', ' ')}\n{self.sections[n]}"
                           for n in names if self.sections.get(n))


def arc_for(outline: dict, ep: int) -> dict:
    for a in outline.get("arcs", []):
        if a["start_ep"] <= ep <= a["end_ep"]:
            return a
    return {"number": 0, "title": "", "start_ep": ep, "end_ep": ep, "goal": "", "summary": "", "ending_turn": ""}


def build_pack(store: Store, cfg: Config, sid: int, ep: int, beats: dict | None = None,
               as_of: int | None = None) -> Pack:
    """Build the context for writing/judging episode `ep`. Canon is read as of `as_of`
    (default ep-1), which is what makes auditing an old episode after a retro edit correct."""
    as_of = ep - 1 if as_of is None else as_of
    outline = store.outline(sid)
    plan = store.plan_episode(sid, ep) or {}
    arc = arc_for(outline, ep)
    pack = Pack(ep=ep, plan=plan, arc=arc)
    s = pack.sections
    scfg = cfg.story

    # L1 — plan window
    act = next((a for a in outline.get("acts", []) if a["start_ep"] <= ep <= a["end_ep"]), None)
    s["position"] = (f"Episode {ep} of {scfg['total_episodes']}. "
                     + (f"Act {act['number']} '{act['title']}': {act['purpose']} (act turn: {act['turning_point']}). " if act else "")
                     + f"Arc {arc['number']} '{arc['title']}' (eps {arc['start_ep']}-{arc['end_ep']}): {arc['goal']} "
                     f"Arc must end on: {arc.get('ending_turn', '')}")
    s["this_episode_plan"] = json.dumps(plan, ensure_ascii=False)
    upcoming = store.plan_episodes(sid, ep + 1, ep + 9)
    s["upcoming_plan"] = "\n".join(f"ep {p['number']}: {p['logline']}" for p in upcoming)

    # L2 — hierarchical recap
    story = store.story(sid)
    s["story_so_far"] = story["story_so_far"] or ""
    recent = store.committed_episodes(sid, max(1, min(arc["start_ep"], as_of - scfg["recent_summaries"] + 1)), as_of)
    s["recent_episodes"] = "\n".join(f"ep {r['number']} '{r['title']}' [{r['in_story_time'] or ''}]: {r['summary']}"
                                     for r in recent)
    prev = store.committed_episodes(sid, as_of - scfg["recent_full_text_episodes"] + 1, as_of)
    s["previous_episode_text"] = "\n\n".join(f"(ep {r['number']})\n{r['text']}" for r in prev)

    # L3 — canon cards for everyone likely on stage, plus a compact roster of everyone
    names = set(plan.get("characters", []))
    if beats:
        names |= set(beats.get("characters", [])) | set(beats.get("locations", []))
    for r in prev:
        for ref in store.q("""SELECT e.name FROM episode_refs x JOIN entities e ON e.id=x.ref_id
                              WHERE x.story_id=? AND x.ep=? AND x.kind='entity'""", sid, r["number"]):
            names.add(ref["name"])
    wanted_ids = {r["id"] for r in (store.entity_by_name(sid, n) for n in names) if r}
    cards, roster = [], []
    for e in store.q("SELECT * FROM entities WHERE story_id=? AND first_ep<=? ORDER BY first_ep, id", sid, max(as_of, 0)):
        facts = store.facts_as_of(sid, e["id"], max(as_of, 0))
        status = facts.get("status", "alive" if e["kind"] == "character" else "")
        if e["kind"] == "character":
            roster.append(f"{e['name']} — {status}" + (f", at {facts['location']}" if "location" in facts else ""))
        if e["id"] in wanted_ids:
            pack.entity_ids.append(e["id"])
            fl = "; ".join(f"{k}={v}" for k, v in facts.items())
            cards.append(f"- {e['name']} ({e['kind']}): {e['description']}" + (f" | CANON: {fl}" if fl else ""))
    s["canon_cards"] = "\n".join(cards)
    s["character_roster"] = "\n".join(roster[:80])
    tl = store.q("SELECT * FROM timeline WHERE story_id=? AND ep<=? ORDER BY ep DESC, id DESC LIMIT 8", sid, as_of)
    s["timeline"] = "\n".join(f"ep {t['ep']} [{t['in_story_time']}]: {t['event']}" for t in reversed(tl))

    # L4 — thread ledger
    lines = []
    wanted = set(plan.get("threads", [])) | set((beats or {}).get("threads", []))
    for t in store.threads(sid):
        if t["status"] == "resolved" and t["resolved_ep"] and t["resolved_ep"] <= as_of:
            continue
        tags = []
        if t["code"] in wanted:
            tags.append("IN THIS EPISODE")
        if t["status"] == "open" and t["last_touched_ep"] and ep - t["last_touched_ep"] > scfg["thread_overdue_after"]:
            tags.append(f"OVERDUE (last touched ep {t['last_touched_ep']})")
        if t["planned_payoff_ep"] and ep <= t["planned_payoff_ep"] <= ep + 5:
            tags.append(f"PAYOFF DUE ep {t['planned_payoff_ep']}")
        if t["status"] == "planned" and not tags:
            continue
        lines.append(f"- {t['code']} [{t['status']}] {t['title']}: {t['description']}" + (f"  <{'; '.join(tags)}>" if tags else ""))
    s["threads"] = "\n".join(lines)

    # L5 — anti-repetition
    hooks = store.committed_episodes(sid, max(1, as_of - 7), as_of)
    s["recent_hooks"] = "\n".join(f"ep {h['number']} ({h['hook_type']}): {h['hook']}" for h in hooks)
    probe = content_tokens(plan.get("logline", "") + " " + " ".join((beats or {}).get("beats", [])))
    scored = []
    for r in store.committed_episodes(sid, 1, as_of - 1):
        kb = json.loads(r["key_beats"] or "[]")
        sim = jaccard(probe, content_tokens(" ".join(kb)))
        if sim >= 0.12:
            scored.append((sim, r["number"], kb))
    scored.sort(reverse=True)
    s["do_not_repeat"] = "\n".join(f"ep {n}: {'; '.join(kb)}" for _, n, kb in scored[:5])

    # L6 — directives
    ds = store.active_directives(sid, ep)
    pack.directive_ids = [d["id"] for d in ds]
    s["active_directives"] = "\n".join(f"[D{d['id']}] ({d['scope']}) {d['rule']}" for d in ds)
    return pack
