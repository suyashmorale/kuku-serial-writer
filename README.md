# Serial Writer — agentic 200-episode serial story writer with human-in-the-loop

Give it a one-line premise. It plans a **200-episode serial** (series bible → 5 acts → 20 arcs → 200 episode
plans), then writes **400–700-word episodes that end on a hook**, one at a time, staying consistent with
everything before it. A human approves the plan, reviews every episode (approve / edit / reject / feedback), and
feedback **carries forward** to future episodes as standing rules and plan changes. Stop anytime and resume.

**Demo output — the real submitted run** (OpenAI `gpt-6-sol`; see [demo/README.md](demo/README.md)):
[200-episode arc plan](demo/arc_plan.md) · [15 episodes](demo/episodes.md) ·
[HITL & decision log](demo/hitl_log.md) · [cost & latency report](demo/cost_report.md) ·
[full story database](demo/story.sqlite) · [all 111 LLM prompt/response traces](demo/traces/) ·
[DECISIONS.md](DECISIONS.md) · Screen recording: _link in the submission Drive folder_

> Premise: *"In a sleepy hill town, a newlywed who devours cheap crime novels becomes the prime suspect when her
> husband's charred body is found — but the next morning, her phone rings with his voice."*
> Brief: Bollywood pulp-noir romantic thriller, fictional Himachal hill town, Indian joint family.

---

## 1. Setup (under 5 minutes)

**Requirements:** macOS / Linux (Windows via WSL), an OpenAI API key. Python is handled by `uv`.

```bash
# 1. Install uv (Python package manager) if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh        # or: brew install uv

# 2. Get the code
git clone https://github.com/suyashmorale/kuku-serial-writer.git
cd kuku-serial-writer

# 3. Install dependencies (uv downloads Python 3.11+ automatically if needed)
uv sync

# 4. Add your API key
cp .env.example .env
#    then edit .env and set:  OPENAI_API_KEY=sk-...

# 5. Check everything works (offline, $0)
uv run pytest -q                 # expect: 10 passed
uv run serial --help             # lists all commands
```

> **Tip:** try the whole flow first **for free** with fake offline models by adding `--fake` right after
> `serial`, e.g. `uv run serial --fake new "any premise"`. Outputs are placeholder text but every step, prompt
> and button works. Delete `data/` afterwards before a real run.

---

## 2. Run it (step by step)

All commands are run from the project folder. `serial` always works on the most recent story
(use `--story <id>` to pick another).

### Step 1 — Give the one-line premise → full 200-episode plan (~5 min, ~$1)
```bash
uv run serial new "In a sleepy hill town, a newlywed who devours cheap crime novels becomes the prime suspect when her husband's charred body is found — but the next morning, her phone rings with his voice." \
  --brief "Bollywood pulp-noir romantic thriller, original story. Fictional Himachal hill town, Indian joint family, passionate suspicious marriage, jealous ex-lover, twisty family secrets, dark humour, occasional Hindi words."
```
`--brief` (optional) sets setting, culture and tone. The planner validates the result and prints warnings
(e.g. a storyline never paid off, a core character absent for 30+ episodes).

### Step 2 — Review, edit and approve the plan (human gate #1)
```bash
uv run serial plan                  # acts + 20 arcs
uv run serial plan --arc 1          # the 10 episodes of arc 1
uv run serial plan --full           # all 200 episode loglines

uv run serial plan-revise "Let the audience meet the dead man through flashbacks before his identity is revealed"
                                    # feedback -> proposed plan changes + standing rule -> you confirm (y/n)
uv run serial plan-export plan.yaml # or hand-edit the whole plan in YAML...
uv run serial plan-import plan.yaml # ...and load it back (already-written episodes are protected)

uv run serial plan-approve          # writing is locked until the plan is approved
```

### Step 3 — Write episodes with human review (human gate #2, ~3 min and ~$0.16 per episode)
```bash
uv run serial write --count 3
```
For each episode you see the draft, the beat sheet's hook, the critic's scores and issues, and whether every
standing rule was followed (`directive D1: ✓`). Then choose:

| Key | Action |
|---|---|
| `a` | **approve** — facts are extracted into the story memory and the episode is committed |
| `e` | **edit** — opens `$EDITOR`; your text is committed as the canonical episode |
| `r` | **reject** — say what to change; the episode is re-planned and rewritten |
| `f` | **feedback** — e.g. *"slow down the romance"*, *"kill off Tara"* → shown as proposed standing rules + future plan changes → you confirm → optionally rewrite this episode. Applies to **all future episodes** |
| `q` | **quit** — pauses safely |

Options: `--autopilot` auto-approves episodes the critic passes cleanly (still stops on any flag and at every
arc boundary); `--episode-cap 1.0` changes the per-episode cost cap.

### Step 4 — Stop and resume
Press `q` (or close the terminal, or crash). Later:
```bash
uv run serial write                 # resumes exactly where you left — same paused draft, nothing regenerated
```

### Step 5 — Feedback between episodes, and standing rules
```bash
uv run serial feedback "Less police questioning, more family drama at the Sood dinner table"
uv run serial directives            # every standing rule created from feedback
uv run serial directives --off 2    # retire rule D2
```

### Step 6 — Inspect the story
```bash
uv run serial status                # progress, cost, paused episode, stale episodes, every thread (OVERDUE flags)
uv run serial show 7                # read episode 7 with its summary and hook type
uv run serial stats                 # tokens / cost / latency / retries per step + projection for 200 episodes
```

### Step 7 — Change history ("a human rewrites episode 4 — what happens to 5–15?")
```bash
uv run serial edit 4                # rewrite a committed episode (or: --file new_ep4.md)
                                    # -> canon re-derived, later episodes that depended on changed facts marked STALE
uv run serial audit                 # re-check stale episodes against the new canon (ok / CONFLICT + fix)
uv run serial fix 6                 # minimal retcon patch: apply / edit / keep original / skip
```

### Step 8 — Export deliverables
```bash
uv run serial export                # demo/arc_plan.md, episodes.md, hitl_log.md, cost_report.md
```

### Explore the submitted run without spending anything
```bash
mkdir -p data && cp demo/story.sqlite data/story.sqlite
uv run serial status
uv run serial show 10
uv run serial stats
```

---

## 3. How it works

```
 premise ─► bible ─► acts/arcs ─► 200 episode plans ─► validator ─► HUMAN: edit / revise / approve
                                                                              │
 ┌────────────────────────────── per episode (LangGraph) ────────────────────┘
 ▼
 assemble context ─► plan beats ─► write ─► checks + critic ──pass──► HUMAN REVIEW ──approve/edit──► commit
 (memory layers)          ▲                    │ fail                   │ reject        │ feedback      │
                          │                 revise (≤2) ◄───────────────┘               ▼               ▼
                          └──────────────── rewrite ◄── confirm ◄── interpret into rules + plan changes
                                                                                  extract canon → versioned
                                                                                  facts, threads, summaries
```

- **Orchestration — LangGraph, as a thin control layer only.** `interrupt()` pauses for human review and
  feedback confirmation; a SQLite checkpointer makes every pause and crash resumable. Bounded loops: ≤2 critic
  revisions, ≤3 human regenerations, per-episode and per-story cost caps — on any limit the episode goes to
  the human with a flag instead of looping.
- **Story canon lives in our own database, not in LangGraph state** (`serial/store.py`): versioned facts
  (`valid_from_ep / valid_to_ep / source_ep`), a thread ledger, directives, summaries, and an
  episode → entity/thread **dependency index**. That is what makes retroactive edits and "what was true at
  episode N?" possible.
- **Memory at episode 150** — a context pack of roughly constant size built from 7 layers: series bible
  (cached system prompt) · plan window · story-so-far + arc recap + recent summaries + previous episode verbatim ·
  canon cards for on-stage characters + one-line roster of everyone (alive/dead/where) · open threads with
  OVERDUE / PAYOFF-DUE tags · recent hooks + similar past beats (anti-repetition) · active human directives.
  Details and trade-offs in [DECISIONS.md](DECISIONS.md).
- **Consistency checks before a human sees it:** deterministic (length, banned clichés, 5-gram overlap with the
  last 20 episodes, hook shape) → LLM critic (continuity vs canon, timeline, threads, repetition, directive
  compliance, hook, prose) → after approval, the extractor diffs the episode against canon.
- **Feedback propagation:** feedback → interpreter → standing directives + edits to future episode plans
  (versioned in `plan_history`) → human confirms → every later context pack includes the directive and the
  critic must report compliance per directive.
- **Name aliases:** "Ira", "Inspector Ritu" and "Ira Sood, née Sharma" resolve to one character; ambiguous
  names ("Sood") resolve to nobody rather than guessing.

### Models (per role, `config.yaml`)

| Role | Model | Why |
|---|---|---|
| bible, outline, arc plans | `gpt-6-sol` (effort high / medium) | one-off structure; swap to `gpt-6-astra` for stronger planning |
| beats, writer, reviser | `gpt-6-sol` (medium / high / medium) | prose quality is graded |
| critic | `gpt-6-sol` (high) | continuity judgment |
| extractor, feedback interpreter | `gpt-6-sol` (low / medium) | canon accuracy matters |
| summaries | `gpt-6-luna` | cheap compression |

Provider-agnostic: any role can point to Anthropic (`claude-opus-5-5`, …) or `fake` by editing one line.
All structured outputs use strict JSON schemas (Pydantic → `response_format: json_schema`).

### Libraries
LangGraph (+ SQLite checkpointer) · OpenAI SDK (Anthropic SDK optional) · Pydantic · SQLite (stdlib) ·
Typer + Rich (CLI) · PyYAML · python-dotenv · pytest · uv.

---

## 4. Observability, limits, cost

- **Every LLM call** → `llm_calls` table (step, model, input/cached/output tokens, cost, latency, attempt,
  status) + full prompt/response JSON in `data/traces/`. **Every decision** (critic verdicts, human actions,
  feedback, budget stops, retro edits) → `events` table. `serial stats` / `demo/cost_report.md` summarise them.
- **Stopping rules:** critic pass (no high-severity issue, hook ≥ 7, every directive complied) or 2 revisions →
  human; cost cap **$0.60 / episode** and **$40 / story** (checked before every call); 3 rejects → change the plan.

### Measured on the submitted run (15 episodes, `gpt-6-sol`)

| | Measured |
|---|---|
| Planning (bible + outline + 200 episode plans) | **$1.08**, ~10 min |
| Per episode | **$0.156** avg (min $0.087, max $0.42 when rewritten after feedback), **3.1 min** |
| 15 episodes + 3 human interventions, total | **$3.43** |
| Retries / invalid JSON / budget stops | 0 / 0 / 0 |
| **Projection: all 200 episodes** | **≈ $32** and **≈ 10 h** sequential model time (+ human review) |

Where the money goes: critic 27%, beats 14%, writer 14%, extraction 6%, revisions 5%, planning 31% (one-off).

**How to cut it (≈ $12–15 and ≈ 5 h for 200):**
1. Critic at effort `medium`, and skip the LLM critic when deterministic checks + a `gpt-6-luna` pre-screen are
   clean (critic is the largest per-episode cost).
2. `gpt-6-luna` for beats and extraction, with a `gpt-6-sol` check only on high-impact facts (deaths,
   relationships, secrets).
3. Lower writer effort on non-turning-point episodes; keep `high` for arc finales.
4. Batch API (−50%) for retro-edit audits; keep system prompts stable so automatic prompt caching hits
   (`prompt_cache_key` is set per story + role — 50–70% of beat/writer/critic input was already cached).
5. Latency: prepare the next episode's beat sheet while the human reviews the current one.

---

## 5. Project layout

```
serial/
  cli.py        all commands (Typer + Rich)
  graph.py      LangGraph episode workflow: nodes, routing, interrupts, resume
  planner.py    bible -> outline -> 200 episode plans (parallel per arc) -> validator; YAML export/import
  episode.py    beats, writer, deterministic checks, critic, reviser, extractor, commit, arc roll-ups
  context.py    memory layers -> bounded context pack
  feedback.py   human feedback -> directives + future plan changes
  retro.py      retroactive edits: revert, re-extract, diff, stale marking, audit, fix
  store.py      SQLite canon: versioned facts, threads, directives, dependency index, traces; alias resolution
  llm.py        OpenAI / Anthropic providers, strict JSON, prompt caching, cost caps, retries, tracing
  prompts.py    every prompt           schemas.py  every structured output
  fake.py       offline $0 provider for tests and dry runs
tests/          end-to-end with the fake provider: plan, HITL, feedback propagation, resume, retro edit,
                budget cap, bounded context, alias resolution
demo/           the real submitted run: plan, episodes, HITL log, cost report, story.sqlite, all LLM traces
config.yaml     models per role, prices, limits, story shape
```

## 6. Known limitations (honest)

- **CLI only**, no web UI.
- **Extractor mistakes become canon** — the biggest risk; mitigations in DECISIONS.md.
- **Feedback about characters that don't exist** (in the demo: a rule about "Veer and Ira", names from an
  earlier plan draft) is not invented into the story — the interpreter reports they're not in the bible and
  the rule stays dormant. It should instead ask the human to fix the names before saving the rule.
- Repetition checks are lexical; paraphrased repeats can slip through (embeddings would fix this).
- The critic uses the same model family as the writer; a different provider for the critic would reduce
  self-approval bias.
- Episodes are generated sequentially, so ~10 h for 200 episodes unless the latency ideas above are applied.
