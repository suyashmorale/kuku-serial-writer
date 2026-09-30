"""Deterministic offline provider. Produces schema-valid, plausible outputs so the full
pipeline (planning, HITL, resume, retro edits) can be exercised in tests and dry runs at $0."""

from __future__ import annotations

import json
import random
import re
import zlib
from typing import Callable

from .llm import RawResult, Usage
from .schemas import (Act, Arc, ArcEpisodes, Bible, BeatSheet, Character, CharacterArc, CriticReport, DirectiveCheck,
                      EpisodePlan, Extraction, FactUpdate, FeedbackInterpretation, Location, NewDirective, Outline,
                      PlanChange, Summary, ThreadSeed, ThreadUpdate)

NAMES = ["Ravi Kulkarni", "Meera Sen", "Inspector Dhawan", "Old Mrs. Iyer", "Kabir", "Anjali Rao"]
VOCAB = ("rain parcel stairwell neon receipt scooter lift ledger flicker balcony chai monsoon siren gate "
         "keyring phone battery corridor cold footsteps number sticker helmet whisper door dust ledger photo "
         "ticket smoke window shadow bell engine puddle signature map crate lantern clock bruise grin").split()


def _ep(user: str, pattern: str, default: int = 1) -> int:
    m = re.search(pattern, user)
    return int(m.group(1)) if m else default


def default_responder(role: str, system: str, user: str, schema):
    if role == "bible":
        return Bible(
            title="Last Drop", logline="A rider's route is a list of the dead.", genre="supernatural mystery",
            tone="tense, wry", pov_and_tense="close third, past", setting="Mumbai, monsoon",
            world_rules=["The dead cannot touch parcels", "Every address is in Kesar Towers"],
            truth="The building's builder sealed a fire exit; the dead want the ledger found.",
            characters=[Character(name=n, role="core", description=f"{n} description", want="answers", need="truth",
                                  secret="a secret", series_arc="changes", voice="clipped") for n in NAMES],
            locations=[Location(name="Kesar Towers", description="a tired tower")],
            threads=[ThreadSeed(code=f"T{i:02d}", title=f"Thread {i}", description=f"question {i}",
                                introduce_by_ep=i * 3, payoff_ep=min(200, 30 * i)) for i in range(1, 7)],
            style_guide=["concrete detail"], banned_phrases=["a chill ran down"], motifs=["rain"])
    if role == "outline":
        total = _ep(user, r"Plan (\d+) episodes", 200)
        per = _ep(user, r"arcs of (\d+) episodes", 10)
        acts = _ep(user, r"as (\d+) acts", 5)
        size = total // acts
        return Outline(
            acts=[Act(number=i + 1, title=f"Act {i + 1}", start_ep=i * size + 1, end_ep=(i + 1) * size,
                      purpose="escalate", turning_point="turn") for i in range(acts)],
            arcs=[Arc(number=i + 1, title=f"Arc {i + 1}", start_ep=i * per + 1, end_ep=(i + 1) * per, goal="goal",
                      summary=f"arc {i + 1} summary", threads_opened=[], threads_advanced=["T01"], threads_resolved=[],
                      character_focus=NAMES[:2], ending_turn="a turn") for i in range(total // per)],
            character_arcs=[CharacterArc(name=n, stages=["a", "b"]) for n in NAMES])
    if role == "arc_beats":
        m = re.search(r"\(episodes (\d+)-(\d+)\)", user)
        a, b = int(m.group(1)), int(m.group(2))
        return ArcEpisodes(episodes=[EpisodePlan(
            number=n, title=f"Episode {n}", logline=f"ep{n} {' '.join(random.Random(n).sample(VOCAB, 6))}",
            purpose="advance", characters=[NAMES[n % 6], NAMES[(n + 1) % 6]] + ([] if n % 20 else NAMES),
            threads=[f"T{(n % 6) + 1:02d}"], turning_point=n % 10 == 0, hook_idea="a reveal") for n in range(a, b + 1)])
    if role == "beats":
        n = _ep(user, r"Episode (\d+) of")
        return BeatSheet(title=f"Episode {n}", beats=["open", "turn", "hook"], characters=NAMES[:2],
                         locations=["Kesar Towers"], threads=["T01"], in_story_time=f"Day {n}",
                         hook_type="reveal", hook="the door was open", directive_notes=[])
    if role in ("writer", "reviser"):
        n = _ep(user, r"Episode (\d+) of")
        rng = random.Random(zlib.crc32(user.encode()))
        body = "\n\n".join(" ".join(rng.choice(VOCAB) for _ in range(104)) + "." for _ in range(5))
        return f"# Episode {n}\n\n{body}\n\nThe door was open."
    if role == "critic":
        ids = [int(x) for x in re.findall(r"\d+", (re.search(r"Active directive ids: \[(.*?)\]", user) or [None, ""])[1])]
        return CriticReport(issues=[], directive_checks=[DirectiveCheck(directive_id=i, complied=True, note="ok") for i in ids],
                            continuity_score=8, hook_score=8, momentum_score=8, prose_score=7, verdict="pass",
                            summary="clean")
    if role == "extractor":
        n = _ep(user, r"APPROVED EPISODE (\d+)")
        text = user.split(f"APPROVED EPISODE {n}", 1)[-1]
        facts = [FactUpdate(entity="Ravi Kulkarni", attribute="location", value=f"floor {n}")]
        for m in re.finditer(r"DIES: ([A-Za-z .]+?)\.", text):
            facts.append(FactUpdate(entity=m.group(1).strip(), attribute="status", value="dead"))
        return Extraction(summary=f"Summary of episode {n}.", hook="the door was open", hook_type="reveal",
                          in_story_time=f"Day {n}", characters_present=NAMES[:2], locations=["Kesar Towers"],
                          new_entities=[], fact_updates=facts,
                          thread_updates=[ThreadUpdate(code="T01", title="Thread 1", action="advance", note=f"ep {n}")],
                          timeline_events=[f"event {n}"], key_beats=[f"beat {n} {w}" for w in VOCAB[n % 20:n % 20 + 3]],
                          canon_conflicts=[])
    if role == "directive":
        n = _ep(user, r"given at episode (\d+)")
        m = re.search(r"## HUMAN FEEDBACK[^\n]*\n(.*?)\n\nTranslate", user, re.S)
        fb = m.group(1).strip() if m else "feedback"
        return FeedbackInterpretation(
            directives=[NewDirective(rule=f"Honor: {fb}", scope="global", from_ep=n, until_ep=0)],
            plan_changes=[PlanChange(episode=n + 1, new_title=f"Episode {n + 1} (revised)",
                                     new_logline=f"revised per feedback: {fb}", characters=NAMES[:2], threads=["T01"],
                                     reason="feedback")],
            rewrite_current=True, explanation=f"Will apply '{fb}' from ep {n}.")
    if role == "summarizer":
        return Summary(text="Compressed summary.")
    raise ValueError(f"fake responder has no output for role {role}")


class FakeProvider:
    def __init__(self, responder: Callable | None = None):
        self.responder = responder or default_responder

    def complete(self, role: str, system: str, user: str, schema) -> RawResult:
        out = self.responder(role, system, user, schema)
        text = out if isinstance(out, str) else out.model_dump_json()
        return RawResult(text=text, usage=Usage(len(system + user) // 4, len(text) // 4), stop_reason="end_turn")
