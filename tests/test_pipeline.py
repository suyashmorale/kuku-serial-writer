import json

from conftest import approve_until_committed

from serial import retro
from serial.context import build_pack
from serial.graph import EpisodeGraph
from serial.store import Store


def test_plan_covers_every_episode(env):
    store, cfg, _, sid = env
    eps = store.plan_episodes(sid)
    assert [e["number"] for e in eps] == list(range(1, 21))
    assert len(store.outline(sid)["arcs"]) == 2
    assert store.threads(sid)  # bible threads seeded into the ledger


def test_fact_versioning_and_revert(tmp_path):
    s = Store(tmp_path / "f.sqlite")
    sid = s.create_story("p")
    eid = s.ensure_entity(sid, "Meera", "character", "", 1)
    with s.tx() as c:
        s.set_fact(sid, eid, "status", "alive", 1, c)
        s.set_fact(sid, eid, "status", "missing", 5, c)
        s.set_fact(sid, eid, "status", "dead", 9, c)
    assert s.facts_as_of(sid, eid, 4)["status"] == "alive"
    assert s.facts_as_of(sid, eid, 7)["status"] == "missing"
    assert s.facts_as_of(sid, eid, 12)["status"] == "dead"
    with s.tx() as c:  # retro: episode 5 no longer says she went missing
        s.revert_episode_effects(sid, 5, c)
    assert s.facts_as_of(sid, eid, 7)["status"] == "alive"
    assert s.facts_as_of(sid, eid, 12)["status"] == "dead"


def test_approve_commits_canon(env, graph):
    store, _, _, sid = env
    approve_until_committed(graph, sid, 1)
    row = store.episode(sid, 1)
    assert row["status"] == "committed" and 400 <= row["word_count"] <= 700
    ravi = store.entity_by_name(sid, "Ravi Kulkarni")
    assert store.facts_as_of(sid, ravi["id"], 1)["location"] == "floor 1"
    assert store.q1("SELECT COUNT(*) n FROM llm_calls WHERE story_id=? AND ep=1", sid)["n"] >= 4


def test_feedback_propagates_to_future_episodes(env, graph):
    store, cfg, _, sid = env
    approve_until_committed(graph, sid, 1)
    approve_until_committed(graph, sid, 2, first={"action": "feedback", "text": "slow down the romance"})
    ds = store.active_directives(sid, 3)
    assert ds and "slow down the romance" in ds[0]["rule"]
    assert store.plan_episode(sid, 3)["version"] == 2  # future plan changed, not just the current draft
    pack = build_pack(store, cfg, sid, 3)
    assert f"[D{ds[0]['id']}]" in pack.sections["active_directives"]
    approve_until_committed(graph, sid, 3)
    report = json.loads(store.episode(sid, 3)["critic"])
    assert any(d["directive_id"] == ds[0]["id"] for d in report["directive_checks"])


def test_human_edit_is_committed_verbatim(env, graph):
    store, _, _, sid = env
    edited = "# Hand Written\n\n" + " ".join(["word"] * 450) + "\n\nThe lift stopped at 13."
    approve_until_committed(graph, sid, 1, first={"action": "edit", "text": edited})
    row = store.episode(sid, 1)
    assert row["text"] == edited and row["human_edited"] == 1 and row["title"] == "Hand Written"


def test_resume_after_process_restart(env):
    store, cfg, llm, sid = env
    g1 = EpisodeGraph(store, cfg, llm)
    payload = g1.run(sid, 1)
    assert payload["kind"] == "review"
    calls_before = store.q1("SELECT COUNT(*) n FROM llm_calls", )["n"]
    # "Come back later": fresh objects, same files on disk
    store2 = Store(cfg.path("db"))
    from serial.llm import LLM
    g2 = EpisodeGraph(store2, cfg, LLM(cfg, store2))
    again = g2.run(sid, 1)
    assert again["draft"] == payload["draft"]  # same paused draft, no regeneration
    assert store2.q1("SELECT COUNT(*) n FROM llm_calls")["n"] == calls_before
    assert g2.run(sid, 1, resume={"action": "approve"}) is None
    assert store2.episode(sid, 1)["status"] == "committed"


def test_retro_edit_marks_dependents_and_audit_clears(env, graph):
    store, cfg, llm, sid = env
    for ep in range(1, 5):
        approve_until_committed(graph, sid, ep)
    old = store.episode(sid, 2)["text"]
    res = retro.edit_committed(store, cfg, llm, sid, 2, old + "\n\nDIES: Meera Sen.", "Meera dies in ep 2 now")
    meera = store.entity_by_name(sid, "Meera Sen")
    assert store.facts_as_of(sid, meera["id"], 3)["status"] == "dead"
    assert "Meera Sen" in res["changed_entities"]
    assert {3, 4} <= set(res["stale"])
    out = retro.audit(store, cfg, llm, sid)
    assert {r["ep"] for r in out} == set(res["stale"])
    assert not store.q("SELECT 1 FROM episodes WHERE story_id=? AND stale=1", sid)  # fake critic finds no conflict


def test_episode_cost_cap_stops_and_flags(env):
    store, cfg, llm, sid = env
    cfg.pricing["fake"] = {"input": 1000.0, "output": 1000.0, "cache_read": 0, "cache_write": 0}
    cfg.limits["episode_cost_cap_usd"] = 0.01
    g = EpisodeGraph(store, cfg, llm)
    payload = g.run(sid, 1)
    assert any("budget" in f for f in payload["flags"])
    assert store.q1("SELECT COUNT(*) n FROM events WHERE kind='budget_stop'")["n"] >= 1


def test_context_pack_is_bounded(env, graph):
    store, cfg, _, sid = env
    for ep in range(1, 12):
        approve_until_committed(graph, sid, ep)
    small = len(build_pack(store, cfg, sid, 3).render())
    large = len(build_pack(store, cfg, sid, 12).render())
    assert large < small * 3  # grows with arc position, not with total history


def test_alias_resolution(tmp_path):
    s = Store(tmp_path / "a.sqlite")
    sid = s.create_story("p")
    for n in ("Ira Sood, née Sharma", "Inspector Ritu Chauhan", "Kamla Sood", "Pratap Sood"):
        s.ensure_entity(sid, n, "character", "", 0)
    assert s.entity_by_name(sid, "Ira")["name"] == "Ira Sood"
    assert s.entity_by_name(sid, "Ritu")["name"] == "Inspector Ritu Chauhan"
    assert s.entity_by_name(sid, "Sood") is None  # ambiguous -> no guess
    assert s.ensure_entity(sid, "Kamla", "character", "", 3) == s.entity_by_name(sid, "Kamla Sood")["id"]
