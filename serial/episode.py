"""Per-episode steps. Plain functions over (store, cfg, llm) so they can be used by the
LangGraph nodes, the retro-edit auditor and tests alike."""

from __future__ import annotations

import json
import time

from . import prompts as P
from .config import Config
from .context import Pack, arc_for, build_pack
from .llm import LLM
from .schemas import BeatSheet, CriticReport, Extraction, Issue, Summary
from .store import Store
from .textutil import last_paragraph, overlap_ratio, split_title, word_count


def _system(store: Store, sid: int, role_prompt: str) -> str:
    return role_prompt + "\n\n" + P.bible_block(store.bible(sid))


def plan_beats(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, notes: list[str]) -> BeatSheet:
    pack = build_pack(store, cfg, sid, ep)
    user = pack.render() + _notes(notes) + "\n\nWrite the beat sheet for this episode."
    return llm.call("beats", _system(store, sid, P.BEATS_SYSTEM), user, schema=BeatSheet,
                    story_id=sid, ep=ep, step="beats")


def write_draft(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, beats: dict, notes: list[str]) -> str:
    pack = build_pack(store, cfg, sid, ep, beats)
    user = (pack.render("position", "story_so_far", "recent_episodes", "previous_episode_text", "canon_cards",
                        "threads", "active_directives", "recent_hooks")
            + f"\n\n## BEAT SHEET\n{json.dumps(beats, ensure_ascii=False)}" + _notes(notes)
            + f"\n\nWrite episode {ep} now ({cfg.limits['words_min']}-{cfg.limits['words_max']} words).")
    return llm.call("writer", _system(store, sid, P.WRITER_SYSTEM), user, story_id=sid, ep=ep, step="write")


def deterministic_checks(store: Store, cfg: Config, sid: int, ep: int, text: str, pack: Pack) -> list[Issue]:
    """Free checks that run before (and independently of) the LLM critic."""
    issues: list[Issue] = []
    wc = word_count(text)
    lo, hi = cfg.limits["words_min"], cfg.limits["words_max"]
    if not lo <= wc <= hi:
        issues.append(Issue(severity="medium" if lo * 0.9 <= wc <= hi * 1.1 else "high", category="length",
                            description=f"{wc} words; target {lo}-{hi}.", fix="Trim or expand to fit the range."))
    low = text.lower()
    for phrase in store.bible(sid).get("banned_phrases", []):
        if phrase.lower() in low:
            issues.append(Issue(severity="medium", category="prose", description=f"Banned phrase used: '{phrase}'.",
                                fix="Replace with a concrete, specific image."))
    for r in store.committed_episodes(sid, max(1, ep - 20), ep - 1):
        ratio = overlap_ratio(text, r["text"] or "")
        if ratio > 0.08:
            issues.append(Issue(severity="high", category="repetition",
                                description=f"{ratio:.0%} of 5-word phrases repeat episode {r['number']}.",
                                fix="Rewrite the repeated passages with new images and actions."))
    if len(last_paragraph(split_title(text)[1]).split()) > 90:
        issues.append(Issue(severity="low", category="hook", description="Final paragraph is long; hook may blur.",
                            fix="Land the hook in 1-2 short sentences."))
    return issues


def critique(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, text: str, beats: dict | None,
             as_of: int | None = None, step: str = "critic") -> tuple[CriticReport, list[Issue]]:
    pack = build_pack(store, cfg, sid, ep, beats, as_of=as_of)
    det = deterministic_checks(store, cfg, sid, ep, text, pack)
    user = (pack.render() + (f"\n\n## BEAT SHEET\n{json.dumps(beats, ensure_ascii=False)}" if beats else "")
            + (("\n\n## AUTOMATED CHECK FINDINGS\n" + "\n".join(f"- {i.description}" for i in det)) if det else "")
            + f"\n\n## DRAFT (episode {ep})\n{text}\n\nReview the draft. Active directive ids: {pack.directive_ids}")
    report = llm.call("critic", _system(store, sid, P.CRITIC_SYSTEM), user, schema=CriticReport,
                      story_id=sid, ep=ep, step=step)
    return report, det


def passes(report: CriticReport, det: list[Issue]) -> bool:
    all_issues = report.issues + det
    return (report.verdict == "pass" and not any(i.severity == "high" for i in all_issues)
            and all(d.complied for d in report.directive_checks))


def revise(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, text: str, beats: dict,
           issues: list[Issue], notes: list[str]) -> str:
    pack = build_pack(store, cfg, sid, ep, beats)
    fixes = "\n".join(f"- [{i.severity}/{i.category}] {i.description} -> FIX: {i.fix}"
                      for i in issues if i.severity in ("high", "medium"))
    user = (pack.render("canon_cards", "threads", "active_directives", "recent_hooks", "previous_episode_text")
            + f"\n\n## BEAT SHEET\n{json.dumps(beats, ensure_ascii=False)}" + _notes(notes)
            + f"\n\n## ISSUES TO FIX\n{fixes}\n\n## CURRENT DRAFT\n{text}\n\nRevise the episode.")
    return llm.call("reviser", _system(store, sid, P.REVISER_SYSTEM), user, story_id=sid, ep=ep, step="revise")


def extract(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, text: str) -> Extraction:
    pack = build_pack(store, cfg, sid, ep)
    known = ", ".join(r["name"] for r in store.q("SELECT name FROM entities WHERE story_id=?", sid))
    codes = "\n".join(f"{t['code']}: {t['title']}" for t in store.threads(sid))
    user = (pack.render("canon_cards", "character_roster", "timeline", "threads")
            + f"\n\n## KNOWN ENTITIES\n{known}\n\n## THREAD CODES\n{codes}"
            + f"\n\n## APPROVED EPISODE {ep}\n{text}\n\nExtract canon updates.")
    return llm.call("extractor", P.EXTRACTOR_SYSTEM, user, schema=Extraction, story_id=sid, ep=ep, step="extract")


def apply_extraction(store: Store, sid: int, ep: int, x: Extraction, c) -> dict:
    """Write one episode's extraction into canon inside transaction `c`. Returns what changed."""
    changed_entities: set[int] = set()
    touched_threads: set[int] = set()
    for ne in x.new_entities:
        eid = store.ensure_entity(sid, ne.name, ne.kind, ne.description, ep, c)
        changed_entities.add(eid)
    refs: set[int] = set()
    for name in set(x.characters_present) | set(x.locations):
        row = store.entity_by_name(sid, name)
        eid = row["id"] if row else store.ensure_entity(sid, name, "character" if name in x.characters_present else "location",
                                                          "", ep, c)
        refs.add(eid)
    for f in x.fact_updates:
        row = store.entity_by_name(sid, f.entity)
        eid = row["id"] if row else store.ensure_entity(sid, f.entity, "other", "", ep, c)
        refs.add(eid)
        if store.set_fact(sid, eid, f.attribute, f.value, ep, c):
            changed_entities.add(eid)
    for tu in x.thread_updates:
        code = tu.code
        if code == "NEW" or not c.execute("SELECT 1 FROM threads WHERE story_id=? AND code=?", (sid, code)).fetchone():
            code = store.next_thread_code(sid, c) if code == "NEW" else code
            store.upsert_thread(sid, code, tu.title, tu.note, None, False, c)
        tid = c.execute("SELECT id FROM threads WHERE story_id=? AND code=?", (sid, code)).fetchone()["id"]
        c.execute("INSERT INTO thread_events (story_id, thread_id, ep, action, note) VALUES (?,?,?,?,?)",
                  (sid, tid, ep, tu.action, tu.note))
        store.recompute_thread(sid, tid, c)
        touched_threads.add(tid)
    for ev in x.timeline_events:
        c.execute("INSERT INTO timeline (story_id, ep, in_story_time, event) VALUES (?,?,?,?)",
                  (sid, ep, x.in_story_time, ev))
    for eid in refs | changed_entities:
        c.execute("INSERT OR IGNORE INTO episode_refs VALUES (?,?,?,?)", (sid, ep, "entity", eid))
    for tid in touched_threads:
        c.execute("INSERT OR IGNORE INTO episode_refs VALUES (?,?,?,?)", (sid, ep, "thread", tid))
    return {"entities": changed_entities, "threads": touched_threads}


def commit_episode(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, text: str, beats: dict | None,
                   report: dict | None, human_edited: bool) -> Extraction:
    """Extract canon from the approved text and commit it atomically. Canon only ever holds approved text."""
    x = extract(store, cfg, llm, sid, ep, text)
    title, _ = split_title(text)
    with store.tx() as c:
        store.revert_episode_effects(sid, ep, c)  # idempotent if the node re-runs after a crash
        apply_extraction(store, sid, ep, x, c)
        c.execute("""UPDATE episodes SET status='committed', title=?, text=?, word_count=?, summary=?, hook=?,
                     hook_type=?, in_story_time=?, key_beats=?, beats=?, critic=?, human_edited=?, stale=0,
                     committed_at=? WHERE story_id=? AND number=?""",
                  (title or (beats or {}).get("title", f"Episode {ep}"), text, word_count(text), x.summary, x.hook,
                   x.hook_type, x.in_story_time, json.dumps(x.key_beats), json.dumps(beats or {}),
                   json.dumps(report or {}), int(human_edited), time.time(), sid, ep))
    store.log_event(sid, ep, "committed", {"human_edited": human_edited, "canon_conflicts": x.canon_conflicts})
    arc = arc_for(store.outline(sid), ep)
    if ep == arc["end_ep"]:
        rollup_arc(store, cfg, llm, sid, arc)
    return x


def rollup_arc(store: Store, cfg: Config, llm: LLM, sid: int, arc: dict) -> None:
    """Arc summary from *episode* summaries (not from the previous summary) to limit drift,
    then story-so-far from arc summaries."""
    eps = store.committed_episodes(sid, arc["start_ep"], arc["end_ep"])
    if not eps:
        return
    body = "\n".join(f"ep {e['number']}: {e['summary']}" for e in eps)
    s = llm.call("summarizer", P.SUMMARY_SYSTEM, f"Summarize arc {arc['number']} '{arc['title']}' in <=250 words:\n{body}",
                 schema=Summary, story_id=sid, ep=arc["end_ep"], step="rollup:arc")
    store.set_arc_summary(sid, arc["number"], s.text)
    arcs = "\n".join(f"Arc {a['arc']}: {a['text']}" for a in store.arc_summaries(sid))
    s2 = llm.call("summarizer", P.SUMMARY_SYSTEM, f"Merge into one 'story so far' of <=700 words:\n{arcs}",
                  schema=Summary, story_id=sid, ep=arc["end_ep"], step="rollup:story")
    store.set_story(sid, story_so_far=s2.text)


def _notes(notes: list[str]) -> str:
    return ("\n\n## HUMAN NOTES FOR THIS EPISODE\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
