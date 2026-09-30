import pytest

from serial.config import load_config
from serial.graph import EpisodeGraph
from serial.llm import LLM
from serial.planner import generate_plan
from serial.store import Store


@pytest.fixture
def cfg(tmp_path):
    c = load_config(provider_override="fake")
    c.paths = {"db": str(tmp_path / "s.sqlite"), "checkpoints": str(tmp_path / "cp.sqlite"),
               "traces": str(tmp_path / "traces")}
    c.story.update(total_episodes=20, episodes_per_arc=10, acts=2)
    c.hitl.update(autopilot=False, pause_at_arc_boundary=True)
    return c


@pytest.fixture
def env(cfg):
    store = Store(cfg.path("db"))
    llm = LLM(cfg, store)
    sid = store.create_story("A delivery rider realizes every address on today's route belongs to someone who died.")
    generate_plan(store, cfg, llm, sid)
    store.set_story(sid, status="writing")
    return store, cfg, llm, sid


@pytest.fixture
def graph(env):
    store, cfg, llm, _ = env
    return EpisodeGraph(store, cfg, llm)


def approve_until_committed(g, sid, ep, first=None):
    """Drive one episode to commit, answering every review with approve (after an optional first answer)."""
    payload = g.run(sid, ep)
    answers = [first] if first else []
    while payload is not None:
        ans = answers.pop(0) if answers else ({"accept": True, "rewrite": True}
                                              if payload["kind"] == "confirm_feedback" else {"action": "approve"})
        payload = g.run(sid, ep, resume=ans)
