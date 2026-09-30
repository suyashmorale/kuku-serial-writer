# DECISIONS

**How does the system remember the story at episode 150?** Nothing is "stuffed" — each episode gets a
context pack of roughly constant size (~12–15K tokens), assembled by code from seven layers:
(L0) the series bible incl. the hidden truth, style guide and banned phrases — the system prompt, prompt-cached;
(L1) the plan window: act/arc position, this episode's plan, the next 9 loglines;
(L2) hierarchical summaries: story-so-far (rebuilt at each arc end from *arc* summaries, which are rebuilt from
*episode* summaries — never summary-of-summary drift), plus the current arc's episode summaries, plus the previous
episode verbatim for voice;
(L3) a versioned fact store: every attribute (status, location, knowledge, relationship:X) has
`valid_from_ep / valid_to_ep / source_ep`; the pack includes full canon cards only for entities on stage and a
one-line roster (name — alive/dead — where) for everyone;
(L4) a thread ledger with OVERDUE and PAYOFF DUE tags; (L5) recent hook types and lexically similar past beats
("do not repeat"); (L6) active human directives. Retrieval is **structured by ID** (the plan/beat sheet names
who and which threads appear), not vector search — more precise for continuity.

**Where does the human step in, and why there?** (1) The **arc plan**, before any prose — the cheapest point to
change direction. (2) **Every episode** by default (approve / edit / reject / feedback); with `--autopilot`
only flagged episodes (critic unsatisfied, budget hit) stop, because reviewing 200 clean episodes is not
realistic. (3) **Every arc boundary** always stops — the natural place to steer. (4) **Feedback confirmation**:
feedback is interpreted into directives + future plan changes and shown as a diff; the human confirms before it
touches canon. Feedback persists as directives the beat planner must address and the critic must check, so
"slow down the romance" is verified on every later episode, not just applied once.

**How do we detect inconsistency or repetition before a human has to?** Three layers: deterministic checks
(length, banned phrases, 5-gram overlap with the last 20 episodes, hook shape); an LLM critic with the canon
cards, roster, timeline, threads and directives, returning severity-ranked issues + per-directive compliance —
high issues trigger up to 2 targeted revisions; and the extractor, which after approval diffs the episode against
canon and records `canon_conflicts`. Canon only ever contains approved text.

**What breaks first as the story grows, and how would we fix it?** (1) **Extractor errors poison canon** — the
biggest risk, because everything downstream trusts it. Fix: a second-model check on high-impact facts (deaths,
relationships, secrets revealed) and a human canon review at arc boundaries. (2) **Entity-name drift**
("Mrs Iyer" vs "Old Mrs. Iyer") splits one character into two; fix with alias resolution at extraction.
(3) **Plan drift** — episodes diverge from loglines written up front; fix with an arc-boundary replan of the next
arc from actual events (the plan is versioned, so this is safe). (4) **Lexical repetition checks miss paraphrased
repeats**; add embeddings over key beats. (5) **Latency** — episodes are inherently sequential; prepare the next
beat sheet speculatively while the human reviews. **Retroactive edits** (e.g. rewrite ep 40) are handled by
reverting ep 40's contributions, re-extracting, diffing, marking dependent later episodes stale through the
episode→entity/thread index, auditing them against canon as of their position, and patching or accepting each.
