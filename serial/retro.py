"""Retroactive edits: "a human rewrites episode 40 — what happens to 41-60?"

1. Snapshot canon, revert ep 40's contributions, re-extract from the new text, re-apply.
2. Diff canon before/after -> changed entities and threads.
3. Episodes after 40 that referenced any changed entity/thread (dependency index), plus ep 41
   for scene continuity, are marked stale.
4. `audit` re-runs the critic on stale episodes against canon *as of their own position*.
5. The human then fixes (targeted revision, which itself goes through this same path — so
   fixes cascade correctly) or accepts each flagged episode.
"""

from __future__ import annotations

import json

from .config import Config
from .context import arc_for
from .episode import apply_extraction, critique, extract, revise, rollup_arc
from .llm import LLM
from .store import Store
from .textutil import split_title, word_count


def edit_committed(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, new_text: str, reason: str) -> dict:
    row = store.episode(sid, ep)
    if not row or row["status"] != "committed":
        raise ValueError(f"episode {ep} is not committed")
    last = store.last_committed(sid)
    contributed = "SELECT entity_id, attribute, value FROM facts WHERE story_id=? AND source_ep=?"
    before = {tuple(r) for r in store.q(contributed, sid, ep)}
    old_threads = {r["thread_id"] for r in store.q("SELECT thread_id FROM thread_events WHERE story_id=? AND ep=?", sid, ep)}

    x = extract(store, cfg, llm, sid, ep, new_text)
    with store.tx() as c:
        store.revert_episode_effects(sid, ep, c)
        res = apply_extraction(store, sid, ep, x, c)
        title, _ = split_title(new_text)
        c.execute("""UPDATE episodes SET text=?, title=?, word_count=?, summary=?, hook=?, hook_type=?, in_story_time=?,
                     key_beats=?, human_edited=1, stale=0 WHERE story_id=? AND number=?""",
                  (new_text, title or row["title"], word_count(new_text), x.summary, x.hook, x.hook_type,
                   x.in_story_time, json.dumps(x.key_beats), sid, ep))

    after = {tuple(r) for r in store.q(contributed, sid, ep)}
    changed_entities = {f[0] for f in before ^ after} | (res["entities"] - {f[0] for f in before})
    changed_threads = old_threads | res["threads"]
    deps = set(store.dependents(sid, ep, changed_entities, changed_threads))
    if ep + 1 <= last:
        deps.add(ep + 1)
    names = {r["id"]: r["name"] for r in store.q("SELECT id, name FROM entities WHERE story_id=?", sid)}
    for d in sorted(deps):
        store.set_episode(sid, d, stale=1, stale_reason=f"ep {ep} edited: {reason}")

    arc = arc_for(store.outline(sid), ep)
    if store.episode(sid, arc["end_ep"]) and store.episode(sid, arc["end_ep"])["status"] == "committed":
        rollup_arc(store, cfg, llm, sid, arc)

    result = {"edited": ep, "changed_entities": sorted(names.get(e, str(e)) for e in changed_entities),
              "stale": sorted(deps), "canon_conflicts": x.canon_conflicts}
    store.log_event(sid, ep, "retro_edit", result)
    return result


def audit(store: Store, cfg: Config, llm: LLM, sid: int, episodes: list[int] | None = None) -> list[dict]:
    """Re-judge stale episodes against the *current* canon as of their position in the story."""
    rows = [store.episode(sid, n) for n in episodes] if episodes else \
        store.q("SELECT * FROM episodes WHERE story_id=? AND stale=1 ORDER BY number", sid)
    out = []
    for r in rows:
        report, det = critique(store, cfg, llm, sid, r["number"], r["text"], json.loads(r["beats"] or "{}"),
                               as_of=r["number"] - 1, step="audit")
        conflicts = [i for i in report.issues + det if i.severity == "high" and i.category in
                     ("continuity", "timeline", "character", "thread")]
        entry = {"ep": r["number"], "conflicts": [i.model_dump() for i in conflicts], "summary": report.summary}
        if not conflicts:
            store.set_episode(sid, r["number"], stale=0, stale_reason=None)
        store.set_episode(sid, r["number"], critic=report.model_dump_json())
        store.log_event(sid, r["number"], "audit", entry)
        out.append(entry)
    return out


def propose_fix(store: Store, cfg: Config, llm: LLM, sid: int, ep: int) -> str:
    """Minimal patch of a stale episode, using its latest audit report. Apply with edit_committed."""
    r = store.episode(sid, ep)
    from .schemas import CriticReport

    report = CriticReport.model_validate_json(r["critic"])
    return revise(store, cfg, llm, sid, ep, r["text"], json.loads(r["beats"] or "{}"), report.issues,
                  [f"Retcon patch: keep this episode's events, change only what conflicts with updated canon. {r['stale_reason']}"])
