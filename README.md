# Serial Writer — agentic 200-episode serial story generator with human-in-the-loop

Given a one-line premise, plans a 200-episode serial (bible → 5 acts → 20 arcs → 200 episode plans),
then writes 400–700-word episodes one at a time. A human approves the plan, and reviews, edits, rejects or
gives feedback on episodes; feedback becomes standing directives and future plan changes. Stop anytime and resume.

## Setup (< 5 min)

```bash
uv sync
cp .env.example .env        # add OPENAI_API_KEY (Anthropic also supported, see config.yaml)
uv run pytest -q            # offline tests, $0
```

Try the whole flow offline first with fake models: `uv run serial --fake new "…"`.

## Usage

```bash
uv run serial new "A delivery rider realizes every address on today's route belongs to someone who died in the same building."
uv run serial plan --full                 # read the 200-episode plan
uv run serial plan-export plan.yaml       # hand-edit, then:
uv run serial plan-import plan.yaml
uv run serial plan-revise "the ghost reveal should come later"
uv run serial plan-approve

uv run serial write --count 5             # per episode: [a]pprove [e]dit [r]eject [f]eedback [q]uit
uv run serial feedback "slow down the romance"   # between episodes
uv run serial directives                  # standing rules created from feedback
uv run serial write                       # resumes exactly where you stopped (even mid-review)

uv run serial edit 4 --file ep4.md        # retroactive rewrite -> canon re-derived, dependents flagged
uv run serial audit                       # re-check stale episodes against new canon
uv run serial fix 5                       # minimal retcon patch, or keep original

uv run serial status | stats | show 7 | export
```

## How it works

```
plan_beats → write → validate (deterministic checks + critic) ──pass──→ HUMAN REVIEW ──approve/edit──→ commit
                ↑         └── revise (≤2) ──┘                              │   (extract canon, version facts,
                └──────────── reject / feedback→interpret→confirm ─────────┘    index dependencies, roll up summaries)
```

- **Orchestration:** LangGraph for control flow only — `interrupt()` for human review, SQLite checkpointer
  for crash-safe resume. **Story canon lives in our own SQLite tables**, versioned by episode.
- **Memory (bounded context at any episode):** bible (prompt-cached) · plan window · story-so-far + arc recap +
  recent summaries · previous episode verbatim · canon cards for on-stage entities + roster · thread ledger with
  overdue/payoff alerts · recent hooks + similar past beats · active directives. See [DECISIONS.md](DECISIONS.md).
- **Models:** per-role routing in `config.yaml` (OpenAI, Anthropic or offline fake). Default: `gpt-6-sol`
  for planning, writing, critique and extraction (reasoning effort tuned per role), `gpt-6-luna` for summaries.
  Upgrade the one-off planning roles to `gpt-6-astra` for a stronger season structure.
- **Bounded:** per-episode cost cap (default $0.60), story cap, ≤2 critic revisions, ≤3 regenerations; on any
  limit the episode goes to the human with a flag instead of looping.
- **Traceable:** every LLM call → `llm_calls` (tokens, cache hits, cost, latency, retries, status) + a full
  prompt/response JSON under `data/traces/`; every decision → `events`. `serial stats` projects the full run.

## Layout

```
serial/
  config.py    schemas.py (all structured outputs)    store.py (canon: versioned facts, threads, directives, traces)
  llm.py       (providers, caching, cost caps, tracing)  prompts.py   context.py (memory layers → context pack)
  planner.py   episode.py (beats/write/critic/revise/extract/commit)  feedback.py  retro.py  graph.py (LangGraph)
  cli.py       fake.py (offline $0 provider)
tests/         end-to-end with the fake provider: HITL, feedback propagation, resume, retro edit, budget
```

## Cost & time (estimate — replace with `serial stats` numbers from the real run)

| per episode | model | ≈ $ |
|---|---|---|
| beats | gpt-6-sol (medium) | 0.03 |
| write (~12K in, ~half cached at $0.20/M; ~4K out incl. reasoning) | gpt-6-sol (high) | 0.06 |
| critic | gpt-6-sol (high) | 0.04 |
| revision (~40% of eps) | gpt-6-sol (medium) | 0.02 |
| extract | gpt-6-sol (low) | 0.03 |
| **total** | | **≈ $0.18 → ≈ $35–45 for 200 eps + ≈ $1–3 planning; ≈ 4–5 h sequential** |

Reduce: `gpt-6-luna` ($0.10/$0.50) for beats and extraction with a Sol check on high-impact facts (deaths,
relationships); skip the LLM critic when deterministic checks and a Luna pre-screen are clean; Batch API (−50%)
for retro audits; keep the system prompt stable so automatic prompt caching hits (`prompt_cache_key` is set per
story+role). Realistic floor ≈ $12–18.
