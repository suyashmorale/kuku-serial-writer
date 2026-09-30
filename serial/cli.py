"""Interactive CLI: plan -> review plan -> write episodes with approve/edit/reject/feedback -> resume anytime."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import feedback as F
from . import planner, retro
from .config import load_config
from .context import arc_for
from .graph import EpisodeGraph
from .llm import LLM
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Agentic serial story writer (HITL).")
con = Console()
_state: dict = {}


@app.callback()
def main(fake: bool = typer.Option(False, "--fake", help="Offline fake models ($0) for dry runs"),
         config: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml")):
    cfg = load_config(config, "fake" if fake else None)
    store = Store(cfg.path("db"))
    _state.update(cfg=cfg, store=store, llm=LLM(cfg, store))


def _ctx():
    return _state["cfg"], _state["store"], _state["llm"]


def _sid(story: Optional[int]) -> int:
    sid = story or _state["store"].latest_story_id()
    if not sid:
        con.print("[red]No story yet. Run `serial new \"<premise>\"`.[/]")
        raise typer.Exit(1)
    return sid


def _edit_in_editor(text: str) -> str:
    editor = os.environ.get("EDITOR", "nano")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(text)
        path = f.name
    subprocess.call([editor, path])
    new = Path(path).read_text()
    os.unlink(path)
    return new


# ---------------- planning ----------------

@app.command()
def new(premise: str, brief: str = typer.Option("", help="Setting/tone/culture brief, e.g. 'Bollywood noir, Himachal town'"),
        story: Optional[int] = typer.Option(None, help="Re-plan an existing story id")):
    """Create a story from a one-line premise and generate the full arc plan."""
    cfg, store, llm = _ctx()
    sid = story or store.create_story(premise, brief)
    con.print(f"[bold]Story {sid}[/]: {premise}")
    warnings = planner.generate_plan(store, cfg, llm, sid, on_progress=lambda m: con.print(f"[dim]{m}[/]"))
    _show_plan_overview(sid)
    if warnings:
        con.print(Panel("\n".join(warnings), title="Plan validator warnings", border_style="yellow"))
    con.print(f"\nPlan cost so far: ${store.story_cost(sid):.3f}")
    con.print("Next: `serial plan` to read it, `serial plan-export plan.yaml` to edit, "
              "`serial plan-revise \"...\"` for feedback, `serial plan-approve` to start writing.")


def _show_plan_overview(sid: int):
    _, store, _ = _ctx()
    b, o = store.bible(sid), store.outline(sid)
    con.print(Panel(f"[bold]{b.get('title')}[/] — {b.get('logline')}\n{b.get('genre')} · {b.get('tone')}",
                    title="Bible"))
    t = Table("Act", "Eps", "Title", "Turning point")
    for a in o.get("acts", []):
        t.add_row(str(a["number"]), f"{a['start_ep']}-{a['end_ep']}", a["title"], a["turning_point"])
    con.print(t)


@app.command()
def plan(story: Optional[int] = None, arc: Optional[int] = typer.Option(None, help="Show one arc's episodes"),
         full: bool = typer.Option(False, help="Show all 200 loglines")):
    """Show the arc plan."""
    _, store, _ = _ctx()
    sid = _sid(story)
    _show_plan_overview(sid)
    o = store.outline(sid)
    t = Table("Arc", "Eps", "Title", "Goal", "Ending turn")
    for a in o.get("arcs", []):
        t.add_row(str(a["number"]), f"{a['start_ep']}-{a['end_ep']}", a["title"], a["goal"], a["ending_turn"])
    con.print(t)
    if arc or full:
        rng = next(((a["start_ep"], a["end_ep"]) for a in o["arcs"] if a["number"] == arc), (1, 10_000))
        t = Table("Ep", "Title", "Logline", "Threads", "v")
        for e in store.plan_episodes(sid, *rng):
            t.add_row(str(e["number"]), e["title"], e["logline"], ",".join(e["threads"]), str(e["version"]))
        con.print(t)


@app.command("plan-export")
def plan_export(path: str = "plan.yaml", story: Optional[int] = None):
    """Export bible + outline + episode plans to YAML for hand-editing."""
    _, store, _ = _ctx()
    planner.export_plan(store, _sid(story), path)
    con.print(f"Wrote {path}. Edit it, then `serial plan-import {path}`.")


@app.command("plan-import")
def plan_import(path: str = "plan.yaml", story: Optional[int] = None):
    """Import a hand-edited plan YAML (only unwritten episodes change)."""
    cfg, store, _ = _ctx()
    sid = _sid(story)
    for c in planner.import_plan(store, sid, path):
        con.print(f"- {c}")
    for w in planner.validate_plan(store, cfg, sid):
        con.print(f"[yellow]! {w}[/]")


@app.command("plan-revise")
def plan_revise(feedback: str, story: Optional[int] = None):
    """Give feedback on the plan (e.g. 'the romance should start later'); applied as plan changes + directives."""
    _feedback_flow(_sid(story), feedback)


@app.command("plan-approve")
def plan_approve(story: Optional[int] = None):
    """Approve the plan; writing can begin."""
    _, store, _ = _ctx()
    sid = _sid(story)
    store.set_story(sid, status="writing")
    store.log_event(sid, None, "plan_approved", {})
    con.print("[green]Plan approved.[/] Run `serial write`.")


# ---------------- writing loop ----------------

@app.command()
def write(story: Optional[int] = None, count: int = typer.Option(1, help="Episodes to write this session"),
          autopilot: Optional[bool] = typer.Option(None, help="Auto-approve clean episodes (still stops on flags/arc ends)"),
          episode_cap: Optional[float] = typer.Option(None, help="Override per-episode cost cap (USD)")):
    """Write the next episode(s) with human review. Quit anytime; `serial write` resumes exactly where you left."""
    cfg, store, llm = _ctx()
    sid = _sid(story)
    if store.story(sid)["status"] not in ("writing",):
        con.print("[red]Approve the plan first (`serial plan-approve`).[/]")
        raise typer.Exit(1)
    if autopilot is not None:
        cfg.hitl["autopilot"] = autopilot
    if episode_cap is not None:
        cfg.limits["episode_cost_cap_usd"] = episode_cap
    g = EpisodeGraph(store, cfg, llm)
    total = cfg.story["total_episodes"]
    for _ in range(count):
        ep = store.last_committed(sid) + 1
        if ep > total:
            store.set_story(sid, status="done")
            con.print("[green]All episodes written.[/]")
            return
        con.rule(f"Episode {ep}")
        payload = g.run(sid, ep)
        while payload is not None:
            answer = _handle_interrupt(sid, payload)
            if answer is None:
                con.print(f"[yellow]Paused at episode {ep}. `serial write` resumes here.[/]")
                return
            payload = g.run(sid, ep, resume=answer)
        row = store.episode(sid, ep)
        con.print(f"[green]Committed ep {ep}[/] '{row['title']}' · {row['word_count']} words · "
                  f"${store.episode_cost(sid, ep):.3f} · story total ${store.story_cost(sid):.2f}")


def _handle_interrupt(sid: int, p: dict) -> dict | None:
    if p["kind"] == "confirm_feedback":
        con.print(Panel(p["text"], title="Proposed changes from your feedback", border_style="cyan"))
        if not Confirm.ask("Apply these changes?", default=True):
            return {"accept": False}
        rewrite = Confirm.ask("Rewrite the current episode with them?", default=p["rewrite_current"])
        return {"accept": True, "rewrite": rewrite}

    ep, report = p["ep"], p.get("report") or {}
    if p.get("draft"):
        con.print(Panel(Markdown(p["draft"]), title=f"Episode {ep} draft", border_style="white"))
    beats = p.get("beats") or {}
    if beats:
        con.print(f"[dim]Hook ({beats.get('hook_type')}): {beats.get('hook')} · directive notes: "
                  f"{'; '.join(beats.get('directive_notes', [])) or '—'}[/]")
    if report:
        con.print(f"Critic: [bold]{report.get('verdict')}[/] · continuity {report.get('continuity_score')} · "
                  f"hook {report.get('hook_score')} · momentum {report.get('momentum_score')} · "
                  f"prose {report.get('prose_score')} — {report.get('summary')}")
        for i in report.get("issues", []) + p.get("det_issues", []):
            color = {"high": "red", "medium": "yellow"}.get(i["severity"], "dim")
            con.print(f"  [{color}]{i['severity']}/{i['category']}[/]: {i['description']}")
        for d in report.get("directive_checks", []):
            con.print(f"  directive D{d['directive_id']}: {'✓' if d['complied'] else '✗'} {d['note']}")
    for f in p.get("flags", []):
        con.print(f"[red]FLAG: {f}[/]")
    if p.get("arc_end"):
        con.print("[cyan]This episode ends an arc — good moment for feedback on direction.[/]")
    choice = Prompt.ask(r"\[a]pprove  \[e]dit  \[r]eject+regenerate  \[f]eedback  \[q]uit",
                        choices=["a", "e", "r", "f", "q"], default="a")
    if choice == "a":
        return {"action": "approve"}
    if choice == "e":
        return {"action": "edit", "text": _edit_in_editor(p.get("draft", ""))}
    if choice == "r":
        return {"action": "reject", "note": Prompt.ask("What should change? (optional)", default="")}
    if choice == "f":
        return {"action": "feedback", "text": Prompt.ask("Feedback (carries forward to future episodes)")}
    return None


@app.command()
def feedback(text: str, story: Optional[int] = None):
    """Give story-level feedback between episodes (e.g. 'kill off Kabir', 'slow down the romance')."""
    _feedback_flow(_sid(story), text)


def _feedback_flow(sid: int, text: str):
    cfg, store, llm = _ctx()
    ep = store.last_committed(sid) + 1
    fi = F.interpret(store, cfg, llm, sid, ep, text)
    con.print(Panel(F.describe(fi), title="Proposed changes", border_style="cyan"))
    if Confirm.ask("Apply?", default=True):
        res = F.apply(store, sid, ep, text, fi)
        con.print(f"[green]Applied[/]: directives {res['directives']}, plan changes {res['plan_changes']}")


@app.command()
def directives(story: Optional[int] = None, off: Optional[int] = typer.Option(None, help="Deactivate directive id")):
    """List standing directives (human feedback in force)."""
    _, store, _ = _ctx()
    sid = _sid(story)
    if off:
        store.set_directive_active(sid, off, False)
    t = Table("ID", "Scope", "From", "Until", "Active", "Rule")
    for d in store.q("SELECT * FROM directives WHERE story_id=? ORDER BY id", sid):
        t.add_row(f"D{d['id']}", d["scope"], str(d["from_ep"]), str(d["until_ep"] or "∞"), "✓" if d["active"] else "", d["rule"])
    con.print(t)


# ---------------- history / retro edits ----------------

@app.command()
def show(ep: int, story: Optional[int] = None):
    """Print a committed episode."""
    _, store, _ = _ctx()
    r = store.episode(_sid(story), ep)
    if not r or not r["text"]:
        con.print("not written")
        raise typer.Exit(1)
    con.print(Markdown(r["text"]))
    con.print(f"[dim]{r['word_count']} words · hook {r['hook_type']} · summary: {r['summary']}"
              + (" · [red]STALE[/]" if r["stale"] else "") + "[/]")


@app.command()
def edit(ep: int, file: Optional[Path] = typer.Option(None, help="Replacement text file (else opens $EDITOR)"),
         reason: str = typer.Option("human rewrite", help="Why it changed"), story: Optional[int] = None):
    """Retroactively rewrite a committed episode; re-derive canon and flag dependent later episodes."""
    cfg, store, llm = _ctx()
    sid = _sid(story)
    old = store.episode(sid, ep)["text"]
    new_text = file.read_text() if file else _edit_in_editor(old)
    if new_text.strip() == old.strip():
        con.print("No change.")
        return
    res = retro.edit_committed(store, cfg, llm, sid, ep, new_text, reason)
    con.print(Panel(f"Canon changed for: {', '.join(res['changed_entities']) or '—'}\n"
                    f"Stale episodes: {res['stale'] or '—'}", title=f"Retro edit ep {ep}"))
    if res["stale"]:
        con.print("Next: `serial audit` to check them against the new canon.")


@app.command()
def audit(story: Optional[int] = None, ep: list[int] = typer.Option(None, help="Specific episodes (default: stale)")):
    """Re-check stale episodes against current canon."""
    cfg, store, llm = _ctx()
    for r in retro.audit(store, cfg, llm, _sid(story), ep or None):
        mark = "[red]CONFLICT[/]" if r["conflicts"] else "[green]ok[/]"
        con.print(f"ep {r['ep']}: {mark} {r['summary']}")
        for c in r["conflicts"]:
            con.print(f"   - {c['description']} → {c['fix']}")


@app.command()
def fix(ep: int, story: Optional[int] = None):
    """Propose a minimal retcon patch for a stale episode; approve to apply (cascades through retro edit)."""
    cfg, store, llm = _ctx()
    sid = _sid(story)
    patched = retro.propose_fix(store, cfg, llm, sid, ep)
    con.print(Panel(Markdown(patched), title=f"Proposed patch for ep {ep}"))
    choice = Prompt.ask(r"\[a]pply  \[e]dit then apply  \[k]eep original (accept retcon)  \[s]kip",
                        choices=["a", "e", "k", "s"], default="a")
    if choice in ("a", "e"):
        text = _edit_in_editor(patched) if choice == "e" else patched
        res = retro.edit_committed(store, cfg, llm, sid, ep, text, f"retcon patch after upstream edit")
        con.print(f"Applied. Newly stale: {res['stale'] or '—'}")
    elif choice == "k":
        store.set_episode(sid, ep, stale=0, stale_reason=None)
        store.log_event(sid, ep, "retcon_accepted", {})


# ---------------- observability ----------------

@app.command()
def status(story: Optional[int] = None):
    """Where the story is: episodes, threads, directives, stale flags."""
    cfg, store, _ = _ctx()
    sid = _sid(story)
    s = store.story(sid)
    last = store.last_committed(sid)
    con.print(f"[bold]Story {sid}[/] · status {s['status']} · {last}/{cfg.story['total_episodes']} episodes · "
              f"cost ${store.story_cost(sid):.2f}")
    inflight = store.q("SELECT number FROM episodes WHERE story_id=? AND status='in_progress'", sid)
    if inflight:
        con.print(f"In progress (resumable): {[r['number'] for r in inflight]}")
    stale = store.q("SELECT number, stale_reason FROM episodes WHERE story_id=? AND stale=1", sid)
    for r in stale:
        con.print(f"[red]stale ep {r['number']}[/]: {r['stale_reason']}")
    t = Table("Thread", "Status", "Intro", "Last", "Payoff", "Title")
    for th in store.threads(sid):
        overdue = th["status"] == "open" and th["last_touched_ep"] and last - th["last_touched_ep"] > cfg.story["thread_overdue_after"]
        t.add_row(th["code"], th["status"] + (" [red]OVERDUE[/]" if overdue else ""), str(th["introduced_ep"] or ""),
                  str(th["last_touched_ep"] or ""), str(th["planned_payoff_ep"] or ""), th["title"])
    con.print(t)


@app.command()
def stats(story: Optional[int] = None):
    """Token cost and latency by step, plus a projection for the full run."""
    cfg, store, _ = _ctx()
    sid = _sid(story)
    t = Table("Step", "Calls", "In tok", "Cache read", "Out tok", "Cost $", "Avg latency s", "Retries")
    for r in store.q("""SELECT CASE WHEN step LIKE 'plan:arc%' THEN 'plan:arcs' ELSE step END s, COUNT(*) n,
                        SUM(input_tokens) i, SUM(cache_read_tokens) cr, SUM(output_tokens) o, SUM(cost_usd) c,
                        AVG(latency_ms) l, SUM(attempt>1) rt FROM llm_calls WHERE story_id=? GROUP BY s ORDER BY s""", sid):
        t.add_row(r["s"], str(r["n"]), f"{r['i']:,}", f"{r['cr']:,}", f"{r['o']:,}", f"{r['c']:.3f}", f"{r['l'] / 1000:.1f}", str(r["rt"]))
    con.print(t)
    eps = store.committed_episodes(sid)
    if eps:
        per = store.q1("""SELECT SUM(cost_usd) c, SUM(latency_ms) l FROM llm_calls WHERE story_id=? AND ep IS NOT NULL
                          AND step NOT LIKE 'plan:%'""", sid)
        plan_cost = store.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE story_id=? AND step LIKE 'plan:%'", sid)["c"]
        avg_c, avg_l = per["c"] / len(eps), per["l"] / len(eps) / 1000
        total = cfg.story["total_episodes"]
        con.print(f"Per episode: ${avg_c:.3f}, {avg_l:.0f}s model time (n={len(eps)}). "
                  f"Projection for {total}: [bold]${plan_cost + avg_c * total:.2f}[/] and "
                  f"{avg_l * total / 3600:.1f} h sequential (excl. human review).")


@app.command()
def export(story: Optional[int] = None, out: Path = typer.Option(Path("demo"), help="Output folder")):
    """Write demo artifacts: arc plan, episodes, HITL log, cost report."""
    cfg, store, _ = _ctx()
    sid = _sid(story)
    out.mkdir(parents=True, exist_ok=True)
    b, o = store.bible(sid), store.outline(sid)
    lines = [f"# {b.get('title')} — 200-episode arc plan", "", f"**Premise:** {store.story(sid)['premise']}", "",
             *( [f"**Brief:** {store.story(sid)['brief']}", ""] if store.story(sid)["brief"] else [] ),
             f"**Logline:** {b.get('logline')}", "", "## Characters", ""]
    lines += [f"- **{c['name']}** ({c['role']}): {c['description']} *Arc:* {c['series_arc']}" for c in b.get("characters", [])]
    lines += ["", "## Threads", ""] + [f"- **{t['code']}** {t['title']} (payoff ep {t['payoff_ep']}): {t['description']}"
                                        for t in b.get("threads", [])]
    lines += ["", "## Acts", ""] + [f"- **Act {a['number']} — {a['title']}** (eps {a['start_ep']}-{a['end_ep']}): "
                                     f"{a['purpose']} *Turn:* {a['turning_point']}" for a in o.get("acts", [])]
    for a in o.get("arcs", []):
        lines += ["", f"### Arc {a['number']}: {a['title']} (eps {a['start_ep']}-{a['end_ep']})", "", f"*{a['goal']}*", ""]
        for e in store.plan_episodes(sid, a["start_ep"], a["end_ep"]):
            lines.append(f"{e['number']}. **{e['title']}** — {e['logline']}" + (f" _(plan v{e['version']})_" if e["version"] > 1 else ""))
    (out / "arc_plan.md").write_text("\n".join(lines))

    eps = store.committed_episodes(sid)
    (out / "episodes.md").write_text("\n\n---\n\n".join(
        f"<!-- ep {e['number']}{' · human-edited' if e['human_edited'] else ''} -->\n{e['text']}" for e in eps))

    log = ["# HITL & decision log", ""]
    for ev in store.q("""SELECT * FROM events WHERE story_id=? AND kind IN
                         ('plan_approved','human','feedback_applied','retro_edit','audit','retcon_accepted','budget_stop')
                         ORDER BY id""", sid):
        log.append(f"- ep {ev['ep'] or '-'} · **{ev['kind']}** · `{ev['payload'][:400]}`")
    log += ["", "## Directives", ""] + [f"- D{d['id']} ({d['scope']}, from ep {d['from_ep']}): {d['rule']}  \n  _from feedback:_ {d['source_feedback']}"
                                        for d in store.q("SELECT * FROM directives WHERE story_id=?", sid)]
    log += ["", "## Plan changes caused by feedback", ""] + [
        f"- ep {h['number']}: {h['reason']}  \n  _was:_ {json.loads(h['old'])['logline']}  \n  _now:_ {store.plan_episode(sid, h['number'])['logline']}"
        for h in store.q("SELECT * FROM plan_history WHERE story_id=? ORDER BY id", sid)]
    (out / "hitl_log.md").write_text("\n".join(log))

    rows = store.q("""SELECT CASE WHEN step LIKE 'plan:arc%' THEN 'plan:arcs' ELSE step END s, COUNT(*) n,
                      SUM(input_tokens) i, SUM(cache_read_tokens) cr, SUM(output_tokens) o, SUM(cost_usd) c,
                      AVG(latency_ms) l, SUM(attempt>1) rt FROM llm_calls WHERE story_id=? GROUP BY s ORDER BY c DESC""", sid)
    plan_c = sum(r["c"] for r in rows if r["s"].startswith("plan:"))
    ep_rows = store.q("""SELECT ep, SUM(cost_usd) c, SUM(latency_ms) l FROM llm_calls WHERE story_id=? AND ep IS NOT NULL
                         AND step NOT LIKE 'plan:%' GROUP BY ep ORDER BY ep""", sid)
    total = cfg.story["total_episodes"]
    rep = ["# Cost & latency report", "", "| Step | Calls | Input tok | Cached tok | Output tok | Cost $ | Avg latency s | Retries |",
           "|---|---|---|---|---|---|---|---|"]
    rep += [f"| {r['s']} | {r['n']} | {r['i']:,} | {r['cr']:,} | {r['o']:,} | {r['c']:.3f} | {r['l'] / 1000:.1f} | {r['rt']} |" for r in rows]
    if ep_rows:
        avg_c = sum(r["c"] for r in ep_rows) / len(ep_rows)
        avg_l = sum(r["l"] for r in ep_rows) / len(ep_rows) / 1000
        rep += ["", f"- Planning (one-off): **${plan_c:.2f}**",
                f"- Per episode (n={len(ep_rows)}): **${avg_c:.3f}** avg (min ${min(r['c'] for r in ep_rows):.3f}, "
                f"max ${max(r['c'] for r in ep_rows):.3f}), **{avg_l / 60:.1f} min** model time",
                f"- Projection for {total} episodes: **${plan_c + avg_c * total:.0f}** and **{avg_l * total / 3600:.1f} h** "
                f"sequential model time (excluding human review)", "", "| Ep | Cost $ | Model time s |", "|---|---|---|"]
        rep += [f"| {r['ep']} | {r['c']:.3f} | {r['l'] / 1000:.0f} |" for r in ep_rows]
    (out / "cost_report.md").write_text("\n".join(rep))
    con.print(f"Wrote {out}/arc_plan.md, episodes.md, hitl_log.md, cost_report.md")


if __name__ == "__main__":
    app()
