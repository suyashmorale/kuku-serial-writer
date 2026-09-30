# Build plan (locked)

## Decisions
- **Architecture:** planner → writer → critic → extractor workflow; a bounded critic/revise loop; human gates.
  Not a multi-agent crew — the flow is deterministic and needs hard pause points and cost caps.
- **Orchestration:** LangGraph as a thin control layer (`interrupt()`, SQLite checkpointer). Canon is **not**
  in checkpoints; it lives in `store.py`, versioned by episode. No CrewAI (overlaps LangGraph; adds nondeterminism).
- **Models:** per-role config, provider-agnostic (OpenAI/Anthropic/fake). Default gpt-6-sol for all core roles,
  gpt-6-luna summaries; gpt-6-astra optional for planning. Bake-off vs Claude on eps 1–3 if credits allow.
- **Storage:** SQLite (story canon + traces) + SQLite checkpoints.

## Status
- [x] Schema, store with versioned facts/threads/directives/dependency index, traces
- [x] LLM layer: structured outputs, prompt caching, refusal fallback, per-episode + story cost caps, retry
- [x] Planner: bible → outline (validated) → 20 arcs in parallel → plan validator; YAML export/import; plan feedback
- [x] Episode graph: beats → write → deterministic checks + critic → revise ≤2 → review interrupt → commit
- [x] Feedback → directives + plan changes, human-confirmed; directive compliance checked by critic
- [x] Resume (checkpointed mid-review, crash-safe commit)
- [x] Retro edit → stale marking → audit → fix/accept
- [x] CLI + stats projection + export of demo artifacts
- [x] Offline tests (fake provider)
- [ ] Real run on the premise they send: plan + ≥15 episodes with ≥2 interventions; tune prompts on eps 1–3
- [ ] Writer bake-off (Claude vs OpenAI) on eps 1–3; record in DECISIONS.md
- [ ] Replace cost estimates in README with measured `serial stats`
- [ ] Screen recording (< 5 min) of the HITL flow
- [ ] Optional: FastAPI + minimal UI, deploy (Fly/Railway), spending cap on the key
- [ ] Optional: alias resolution for entity names; embeddings for paraphrase-level repetition
