# Demo output — the real submitted run

Everything in this folder was produced by the pipeline in one real run with OpenAI `gpt-6-sol` / `gpt-6-luna`
(story id 3). Nothing was hand-written except the human feedback typed during review.

**Screen recording of the HITL flow:** https://drive.google.com/drive/folders/1kuXX0cvYXJPisadM17PHT5irWfz8uVkj?usp=sharing

**Premise:** *"In a sleepy hill town, a newlywed who devours cheap crime novels becomes the prime suspect when
her husband's charred body is found — but the next morning, her phone rings with his voice."*
**Brief:** Bollywood pulp-noir romantic thriller, original story; fictional Himachal hill town, Indian joint
family, passionate suspicious marriage, jealous ex-lover, twisty family secrets, dark humour.

| File | What it is |
|---|---|
| [arc_plan.md](arc_plan.md) | The full plan: series bible (characters, threads), 5 acts, 20 arcs, all 200 episode loglines. Episodes changed by feedback are marked _(plan v2)_ |
| [plan.yaml](plan.yaml) | The same plan in machine-readable form (bible, outline, 200 episode plans) — the format used by `serial plan-export / plan-import` |
| [episodes.md](episodes.md) | Episodes 1–15 as committed after human review |
| [hitl_log.md](hitl_log.md) | Every human action (approve / feedback, auto-approvals), every standing directive and the feedback it came from, and every plan change caused by feedback (old vs new logline) |
| [cost_report.md](cost_report.md) | Tokens, cached tokens, cost, latency and retries per step; per-episode cost and time; projection for 200 episodes |
| [story.sqlite](story.sqlite) | The complete story database: plan, episodes, versioned facts, entities, threads, timeline, dependency index, directives, every LLM call and every decision |
| [traces/](traces/) | Full prompt + response JSON for every one of the 111 LLM calls (the `trace_file` column in `llm_calls` points here) |

## Human interventions in this run

1. **Plan revision before writing** — *"Let the audience meet the dead man through flashbacks, memories and
   letters in acts 1-2, before his true identity is revealed."* → plan changes to eps 4, 23, 41 + directive D1;
   ep 4 then contains a memory of Manu, and the critic confirmed D1 on every episode.
2. **Feedback at episode 2** — *"Veer should become darker and more obsessive about Ira…"* → the interpreter
   reported that no characters by those names exist in this story (they came from an earlier plan draft) and
   made no plan changes; stored as dormant directive D2. Kept in the log as an honest example of how the
   system handles feedback it can't apply.
3. **Feedback at the arc-1 boundary (episode 10)** — *"Less police questioning, more family drama and secrets at
   the Sood dinner table."* → episode 10 rewritten around a dinner-table argument, episode 11 re-planned,
   directives D3/D4 added; the critic confirmed D3 on episodes 10–15.

## Explore it yourself (no API key needed)

From the repo root:

```bash
mkdir -p data && cp demo/story.sqlite data/story.sqlite
uv run serial status          # threads, progress, cost
uv run serial directives      # standing rules from feedback
uv run serial show 10         # the episode rewritten after feedback
uv run serial stats           # cost / latency per step
sqlite3 data/story.sqlite "SELECT e.name, f.attribute, f.value, f.valid_from_ep, f.valid_to_ep
                           FROM facts f JOIN entities e ON e.id=f.entity_id ORDER BY f.valid_from_ep;"
```
