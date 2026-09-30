"""Human feedback -> durable directives + future plan changes (so feedback propagates)."""

from __future__ import annotations

import json

from . import prompts as P
from .config import Config
from .context import build_pack
from .llm import LLM
from .schemas import FeedbackInterpretation
from .store import Store


def interpret(store: Store, cfg: Config, llm: LLM, sid: int, ep: int, feedback: str,
              draft: str | None = None) -> FeedbackInterpretation:
    pack = build_pack(store, cfg, sid, ep)
    horizon = store.plan_episodes(sid, ep, ep + 40)
    plan = "\n".join(f"ep {p['number']} [{', '.join(p['characters'])}] [{', '.join(p['threads'])}]: {p['logline']}"
                     for p in horizon)
    user = (pack.render("position", "story_so_far", "recent_episodes", "character_roster", "threads", "active_directives")
            + f"\n\n## FUTURE PLAN (eps {ep}-{ep + 40})\n{plan}"
            + (f"\n\n## CURRENT DRAFT (ep {ep})\n{draft}" if draft else "")
            + f"\n\n## HUMAN FEEDBACK (given at episode {ep})\n{feedback}\n\nTranslate this feedback.")
    return llm.call("directive", P.DIRECTIVE_SYSTEM + "\n\n" + P.bible_block(store.bible(sid)), user,
                    schema=FeedbackInterpretation, story_id=sid, ep=ep, step="feedback")


def apply(store: Store, sid: int, ep: int, feedback: str, fi: FeedbackInterpretation) -> dict:
    written = store.last_committed(sid)
    added, changed, skipped = [], [], []
    for d in fi.directives:
        did = store.add_directive(sid, d.rule, d.scope, max(d.from_ep, written + 1), d.until_ep, feedback)
        added.append(did)
    for pc in fi.plan_changes:
        if pc.episode <= written:
            skipped.append(pc.episode)
            continue
        store.update_plan_episode(sid, pc.episode, f"feedback: {feedback} ({pc.reason})", title=pc.new_title,
                                  logline=pc.new_logline, characters=pc.characters, threads=pc.threads)
        changed.append(pc.episode)
    result = {"feedback": feedback, "directives": added, "plan_changes": changed, "skipped": skipped,
              "explanation": fi.explanation}
    store.log_event(sid, ep, "feedback_applied", result)
    return result


def describe(fi: FeedbackInterpretation) -> str:
    lines = [fi.explanation, ""]
    for d in fi.directives:
        lines.append(f"+ directive ({d.scope}, from ep {d.from_ep}{'' if not d.until_ep else f' to {d.until_ep}'}): {d.rule}")
    for pc in fi.plan_changes:
        lines.append(f"~ plan ep {pc.episode}: {pc.new_logline}  ({pc.reason})")
    lines.append(f"rewrite current draft: {'yes' if fi.rewrite_current else 'no'}")
    return "\n".join(lines)


def to_dict(fi: FeedbackInterpretation) -> dict:
    return json.loads(fi.model_dump_json())
