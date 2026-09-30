# DECISIONS

**How does the system remember the story at episode 150?** Nothing is stuffed into context. Every episode gets a
context pack of roughly constant size, assembled by code from seven layers: (L0) the series bible — characters,
world rules, the hidden truth, style guide, banned clichés — as the system prompt, prompt-cached; (L1) the plan
window: act/arc position, this episode's plan, the next 9 loglines; (L2) hierarchical summaries — story-so-far
(rebuilt at each arc end from *arc* summaries, which are rebuilt from *episode* summaries, so errors don't
compound summary-of-summary), the current arc's episode summaries, and the previous episode verbatim for voice;
(L3) a **versioned fact store**: every attribute (status, location, knowledge, relationship:X) has
`valid_from_ep / valid_to_ep / source_ep`; the pack carries full canon cards only for characters on stage plus a
one-line roster of everyone (alive/dead/where); (L4) a thread ledger tagging OVERDUE and PAYOFF-DUE threads;
(L5) recent hook types and similar past beats ("do not repeat"); (L6) active human directives. Retrieval is
**structured by ID** — the plan and beat sheet name who and which threads appear — not vector search, because
continuity needs precision. Name aliases ("Ira" / "Ira Sood, née Sharma") resolve to one entity.

**Where does the human step in, and why there?** (1) The **arc plan**, before any prose — the cheapest place to
change direction (in the demo: "let the audience meet the dead man through flashbacks" changed eps 4, 23, 41 and
added a standing rule). (2) **Every episode** by default — approve, edit, reject, feedback; with `--autopilot`
only flagged episodes stop, because reviewing 200 clean episodes isn't realistic. (3) **Every arc boundary**
always stops — the natural point to steer (demo: at ep 10, "less police questioning, more family drama"
rewrote ep 10, re-planned ep 11, and the critic confirmed that rule on eps 10–15). (4) **Feedback confirmation**:
feedback is turned into standing directives + future plan changes and shown as a diff before it touches canon.

**How do we detect inconsistency or repetition before a human has to?** Deterministic checks (length, banned
phrases, 5-gram overlap with the last 20 episodes, hook shape); an LLM critic that sees canon cards, roster,
timeline, threads and directives and returns severity-ranked issues plus per-directive compliance — high issues
trigger up to 2 targeted revisions (9 revisions across the 15 demo episodes); and the extractor, which after
approval diffs the episode against canon and records `canon_conflicts`. Canon only ever contains approved text.

**What breaks first as the story grows, and how would we fix it?** (1) **Extractor errors poison canon** —
everything downstream trusts it. Fix: second-model verification of high-impact facts (deaths, relationships,
revealed secrets) and a human canon review at arc boundaries. (2) **Plan drift** — episodes diverge from loglines
written up front; fix with an arc-boundary re-plan of the next arc from what actually happened (plans are
versioned, so this is safe). (3) **Paraphrased repetition** slips past lexical checks; add embeddings over key
beats. (4) **Feedback naming unknown characters** (seen in the demo) should be rejected back to the human, not
stored as a dormant rule. (5) **Cost and latency** — measured $0.156 and 3.1 min per episode (≈ $32 / ≈ 10 h for
200); the critic is the largest cost, so a cheap pre-screen and lower critic effort come first. **Retroactive
edits** ("rewrite ep 40"): revert ep 40's facts, re-extract, diff, mark later episodes that referenced changed
facts stale via the episode→entity/thread index, audit them against canon as of their own position, then patch
or accept each — every patch goes through the same path, so fixes cascade correctly.
