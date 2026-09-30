"""LangGraph orchestration for one episode.

LangGraph owns control flow only: routing, bounded loops, human interrupts, crash-safe resume.
Graph state holds working drafts for the in-flight episode; canon lives in Store.

    plan_beats -> write -> validate --pass/exhausted--> review --approve/edit--> commit -> END
                    ^         |                           |  \\--reject---------> plan_beats
                    |      revise (<= max_revisions)      \\--feedback--> interpret -> confirm --> plan_beats | commit
"""

from __future__ import annotations

import sqlite3
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import episode as E
from . import feedback as F
from .config import Config
from .llm import LLM, BudgetExceeded
from .store import Store


class EpisodeState(TypedDict, total=False):
    story_id: int
    ep: int
    beats: dict
    draft: str
    report: dict
    det_issues: list[dict]
    passed: bool
    revisions: int
    regenerations: int
    notes: list[str]
    flags: list[str]
    decision: dict
    interpretation: dict
    human_edited: bool


class EpisodeGraph:
    def __init__(self, store: Store, cfg: Config, llm: LLM):
        self.store, self.cfg, self.llm = store, cfg, llm
        path = cfg.path("checkpoints")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.saver = SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
        self.graph = self._build().compile(checkpointer=self.saver)

    # ---------- nodes ----------

    def _budget_guard(self, state: EpisodeState, fn) -> dict:
        try:
            return fn()
        except BudgetExceeded as e:
            self.store.log_event(state["story_id"], state["ep"], "budget_stop", {"reason": str(e)})
            return {"flags": state.get("flags", []) + [f"budget: {e}"], "passed": False}

    def plan_beats(self, s: EpisodeState) -> dict:
        def run():
            b = E.plan_beats(self.store, self.cfg, self.llm, s["story_id"], s["ep"], s.get("notes", []))
            return {"beats": b.model_dump(), "revisions": 0, "draft": "", "report": {}, "det_issues": []}
        return self._budget_guard(s, run)

    def write(self, s: EpisodeState) -> dict:
        if not s.get("beats"):
            return {}
        return self._budget_guard(s, lambda: {"draft": E.write_draft(
            self.store, self.cfg, self.llm, s["story_id"], s["ep"], s["beats"], s.get("notes", []))})

    def validate(self, s: EpisodeState) -> dict:
        if not s.get("draft"):
            return {"passed": False}

        def run():
            report, det = E.critique(self.store, self.cfg, self.llm, s["story_id"], s["ep"], s["draft"], s["beats"])
            ok = E.passes(report, det)
            self.store.log_event(s["story_id"], s["ep"], "critic", {
                "attempt": s.get("revisions", 0), "passed": ok, "verdict": report.verdict,
                "scores": {"continuity": report.continuity_score, "hook": report.hook_score,
                           "momentum": report.momentum_score, "prose": report.prose_score},
                "high": [i.description for i in report.issues + det if i.severity == "high"],
                "directives": [d.model_dump() for d in report.directive_checks]})
            return {"report": report.model_dump(), "det_issues": [i.model_dump() for i in det], "passed": ok}
        return self._budget_guard(s, run)

    def revise(self, s: EpisodeState) -> dict:
        from .schemas import Issue

        issues = [Issue(**i) for i in s["report"].get("issues", []) + s.get("det_issues", [])]
        return self._budget_guard(s, lambda: {
            "draft": E.revise(self.store, self.cfg, self.llm, s["story_id"], s["ep"], s["draft"], s["beats"],
                              issues, s.get("notes", [])),
            "revisions": s.get("revisions", 0) + 1})

    def review(self, s: EpisodeState) -> dict:
        flags = list(s.get("flags", []))
        if not s.get("passed") and s.get("draft"):
            flags.append(f"critic not satisfied after {s.get('revisions', 0)} revision(s)")
        arc_end = self._is_arc_end(s)
        if (self.cfg.hitl.get("autopilot") and s.get("passed") and not flags
                and not (arc_end and self.cfg.hitl.get("pause_at_arc_boundary"))):
            self.store.log_event(s["story_id"], s["ep"], "human", {"action": "approve", "auto": True})
            return {"decision": {"action": "approve", "auto": True}}
        decision = interrupt({"kind": "review", "ep": s["ep"], "draft": s.get("draft", ""), "beats": s.get("beats", {}),
                              "report": s.get("report", {}), "det_issues": s.get("det_issues", []),
                              "flags": flags, "arc_end": arc_end})
        self.store.log_event(s["story_id"], s["ep"], "human", decision)
        out: dict[str, Any] = {"decision": decision, "flags": []}
        if decision["action"] == "edit":
            out.update(draft=decision["text"], human_edited=True)
        elif decision["action"] == "reject":
            out.update(notes=s.get("notes", []) + ([decision["note"]] if decision.get("note") else []),
                       regenerations=s.get("regenerations", 0) + 1)
        return out

    def interpret(self, s: EpisodeState) -> dict:
        fb = s["decision"]["text"]
        def run():
            fi = F.interpret(self.store, self.cfg, self.llm, s["story_id"], s["ep"], fb, s.get("draft"))
            return {"interpretation": F.to_dict(fi)}
        return self._budget_guard(s, run)

    def confirm(self, s: EpisodeState) -> dict:
        from .schemas import FeedbackInterpretation

        if not s.get("interpretation"):
            return {"decision": {"action": "reject", "note": s["decision"]["text"]}}
        fi = FeedbackInterpretation.model_validate(s["interpretation"])
        answer = interrupt({"kind": "confirm_feedback", "ep": s["ep"], "text": F.describe(fi),
                            "rewrite_current": fi.rewrite_current})
        if not answer.get("accept"):
            return {"decision": {"action": "discard_feedback"}, "interpretation": {}}
        F.apply(self.store, s["story_id"], s["ep"], s["decision"]["text"], fi)
        rewrite = answer.get("rewrite", fi.rewrite_current)
        return {"decision": {"action": "rewrite" if rewrite else "approve"},
                "notes": s.get("notes", []) + ([f"Human feedback: {s['decision']['text']}"] if rewrite else [])}

    def commit(self, s: EpisodeState) -> dict:
        E.commit_episode(self.store, self.cfg, self.llm, s["story_id"], s["ep"], s["draft"], s.get("beats"),
                         s.get("report"), bool(s.get("human_edited")))
        return {}

    # ---------- routing ----------

    def _after_validate(self, s: EpisodeState) -> str:
        if s.get("passed") or not s.get("draft") or s.get("flags"):
            return "review"
        return "revise" if s.get("revisions", 0) < self.cfg.limits["max_revisions"] else "review"

    def _after_review(self, s: EpisodeState) -> str:
        a = s["decision"]["action"]
        if a in ("approve", "edit"):
            return "commit" if s.get("draft") else "review"
        if a == "feedback":
            return "interpret"
        if s.get("regenerations", 0) > self.cfg.limits["max_regenerations"]:
            return "review"  # stop regenerating; the human must edit, give feedback, or change the plan
        return "plan_beats"

    def _after_confirm(self, s: EpisodeState) -> str:
        return {"rewrite": "plan_beats", "approve": "commit"}.get(s["decision"]["action"], "review")

    def _is_arc_end(self, s: EpisodeState) -> bool:
        from .context import arc_for

        return arc_for(self.store.outline(s["story_id"]), s["ep"])["end_ep"] == s["ep"]

    def _build(self) -> StateGraph:
        g = StateGraph(EpisodeState)
        for name in ("plan_beats", "write", "validate", "revise", "review", "interpret", "confirm", "commit"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "plan_beats")
        g.add_edge("plan_beats", "write")
        g.add_edge("write", "validate")
        g.add_conditional_edges("validate", self._after_validate, ["revise", "review"])
        g.add_edge("revise", "validate")
        g.add_conditional_edges("review", self._after_review, ["commit", "interpret", "plan_beats", "review"])
        g.add_edge("interpret", "confirm")
        g.add_conditional_edges("confirm", self._after_confirm, ["plan_beats", "commit", "review"])
        g.add_edge("commit", END)
        return g

    # ---------- driver API ----------

    @staticmethod
    def thread_id(sid: int, ep: int) -> str:
        return f"story{sid}-ep{ep}"

    def run(self, sid: int, ep: int, resume: Any = None) -> dict | None:
        """Advance episode `ep` until it commits (returns None) or pauses (returns the interrupt payload).
        Safe to call repeatedly: an existing thread resumes from its last checkpoint."""
        tid = self.thread_id(sid, ep)
        config = {"configurable": {"thread_id": tid}, "recursion_limit": 60}
        self.store.start_episode(sid, ep, tid)
        state = self.graph.get_state(config)
        if resume is not None:
            self.graph.invoke(Command(resume=resume), config)
        elif state.next:
            if any(t.interrupts for t in state.tasks):
                return self.pending(sid, ep)
            self.graph.invoke(None, config)  # crashed mid-node: continue from last checkpoint
        else:
            self.graph.invoke({"story_id": sid, "ep": ep, "notes": [], "flags": [], "regenerations": 0}, config)
        return self.pending(sid, ep)

    def pending(self, sid: int, ep: int) -> dict | None:
        state = self.graph.get_state({"configurable": {"thread_id": self.thread_id(sid, ep)}})
        for t in state.tasks:
            if t.interrupts:
                return t.interrupts[0].value
        return None

    def reset(self, sid: int, ep: int) -> None:
        self.saver.delete_thread(self.thread_id(sid, ep))
