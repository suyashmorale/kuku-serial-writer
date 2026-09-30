"""Pydantic models for every structured LLM output, plus the JSON-schema sanitizer
that makes them acceptable to both Anthropic and OpenAI strict structured outputs."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel

HookType = Literal[
    "reveal", "threat", "reversal", "question", "arrival", "discovery", "decision", "loss", "deadline"
]


# ---------- Series bible ----------

class Character(BaseModel):
    name: str
    role: str
    description: str
    want: str
    need: str
    secret: str
    series_arc: str
    voice: str


class Location(BaseModel):
    name: str
    description: str


class ThreadSeed(BaseModel):
    code: str  # short stable id, e.g. "T01"
    title: str
    description: str
    introduce_by_ep: int
    payoff_ep: int


class Bible(BaseModel):
    title: str
    logline: str
    genre: str
    tone: str
    pov_and_tense: str
    setting: str
    world_rules: list[str]
    truth: str  # the hidden answer to the central mystery; never revealed early
    characters: list[Character]
    locations: list[Location]
    threads: list[ThreadSeed]
    style_guide: list[str]
    banned_phrases: list[str]
    motifs: list[str]


# ---------- Arc plan ----------

class Act(BaseModel):
    number: int
    title: str
    start_ep: int
    end_ep: int
    purpose: str
    turning_point: str


class Arc(BaseModel):
    number: int
    title: str
    start_ep: int
    end_ep: int
    goal: str
    summary: str
    threads_opened: list[str]
    threads_advanced: list[str]
    threads_resolved: list[str]
    character_focus: list[str]
    ending_turn: str


class CharacterArc(BaseModel):
    name: str
    stages: list[str]


class Outline(BaseModel):
    acts: list[Act]
    arcs: list[Arc]
    character_arcs: list[CharacterArc]


class EpisodePlan(BaseModel):
    number: int
    title: str
    logline: str
    purpose: str
    characters: list[str]
    threads: list[str]
    turning_point: bool
    hook_idea: str


class ArcEpisodes(BaseModel):
    episodes: list[EpisodePlan]


# ---------- Per-episode ----------

class BeatSheet(BaseModel):
    title: str
    beats: list[str]
    characters: list[str]
    locations: list[str]
    threads: list[str]
    in_story_time: str
    hook_type: HookType
    hook: str
    directive_notes: list[str]  # how each active directive is honoured in this episode


class Issue(BaseModel):
    severity: Literal["high", "medium", "low"]
    category: Literal[
        "continuity", "timeline", "character", "thread", "repetition",
        "directive", "hook", "pacing", "prose", "length", "plan",
    ]
    description: str
    fix: str


class DirectiveCheck(BaseModel):
    directive_id: int
    complied: bool
    note: str


class CriticReport(BaseModel):
    issues: list[Issue]
    directive_checks: list[DirectiveCheck]
    continuity_score: int  # 1-10
    hook_score: int
    momentum_score: int
    prose_score: int
    verdict: Literal["pass", "revise"]
    summary: str


class NewEntity(BaseModel):
    name: str
    kind: Literal["character", "location", "object", "organization", "other"]
    description: str


class FactUpdate(BaseModel):
    entity: str
    attribute: str  # e.g. status, location, occupation, knows_about, relationship:<Other Name>
    value: str


class ThreadUpdate(BaseModel):
    code: str  # existing thread code, or "NEW" to open a new thread
    title: str
    action: Literal["open", "advance", "resolve"]
    note: str


class Extraction(BaseModel):
    summary: str
    hook: str
    hook_type: HookType
    in_story_time: str
    characters_present: list[str]
    locations: list[str]
    new_entities: list[NewEntity]
    fact_updates: list[FactUpdate]
    thread_updates: list[ThreadUpdate]
    timeline_events: list[str]
    key_beats: list[str]
    canon_conflicts: list[str]


# ---------- Human feedback ----------

class NewDirective(BaseModel):
    rule: str
    scope: str  # "global", "character:<Name>", "relationship:<A>|<B>", "thread:<code>"
    from_ep: int
    until_ep: int  # 0 = no expiry


class PlanChange(BaseModel):
    episode: int
    new_title: str
    new_logline: str
    characters: list[str]
    threads: list[str]
    reason: str


class FeedbackInterpretation(BaseModel):
    directives: list[NewDirective]
    plan_changes: list[PlanChange]
    rewrite_current: bool
    explanation: str


class Summary(BaseModel):
    text: str


# ---------- Schema sanitizer ----------

_UNSUPPORTED = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "default",
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> strict schema: every object closed, every property required,
    unsupported constraints stripped. Works for Anthropic output_config.format and OpenAI strict mode."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k in list(node):
                if k in _UNSUPPORTED:
                    node.pop(k)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}).keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema
