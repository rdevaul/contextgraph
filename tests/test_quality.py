"""Tests for quality.py and reframing.py"""
import tempfile
from assembler import AssemblyResult
from reframing import detect_reframing, reframing_rate
from quality import QualityAgent


# ── reframing ─────────────────────────────────────────────────────────────────

def test_memory_failure_detected():
    r = detect_reframing("As I mentioned earlier, the gateway bind should be loopback.")
    assert r.is_reframing
    assert "memory_failure" in r.signals_found


def test_re_establishment_detected():
    r = detect_reframing("Just to bring you up to speed — we decided to use loopback bind.")
    assert r.is_reframing
    assert "re_establishment" in r.signals_found


def test_context_provision_detected():
    r = detect_reframing("As we discussed, the tagger should use structured programs. Can you now write the fitness function?")
    assert r.is_reframing


def test_no_false_positive_simple():
    r = detect_reframing("Can you add milk to the shopping list?")
    assert not r.is_reframing


def test_no_false_positive_technical():
    r = detect_reframing("How does the DEAP primitive set work?")
    assert not r.is_reframing


def test_reframing_rate():
    texts = [
        "As I mentioned, we use loopback.",   # reframing
        "Add milk to the list.",               # not
        "You forgot the gateway config.",      # reframing
        "What's the weather?",                 # not
    ]
    rate = reframing_rate(texts)
    assert 0.4 <= rate <= 0.6   # 2 out of 4


def test_reframing_rate_empty():
    assert reframing_rate([]) == 0.0


# ── quality agent ─────────────────────────────────────────────────────────────

def _mock_result(recency=3, topic=2, sticky=0, total_tokens=500):
    return AssemblyResult(
        messages=[],
        total_tokens=total_tokens,
        sticky_count=sticky,
        recency_count=recency,
        topic_count=topic,
        tags_used=["security"],
    )


def test_quality_record_and_fitness(tmp_path):
    agent = QualityAgent(state_path=str(tmp_path / "state.json"))
    result = _mock_result(recency=2, topic=3)  # 60% topic → density=0.6
    iq = agent.record(
        tagger_id="test-tagger",
        assembly_result=result,
        user_text="How does the gateway security work?",
        recent_user_texts=["Add milk", "Fix the gateway", "How does this work?"],
    )
    assert 0.0 <= iq.context_density <= 1.0
    assert 0.0 <= iq.composite <= 1.0
    fitness = agent.fitness("test-tagger")
    assert 0.0 <= fitness <= 1.0


def test_quality_unknown_tagger_returns_neutral(tmp_path):
    agent = QualityAgent(state_path=str(tmp_path / "state.json"))
    assert agent.fitness("nonexistent") == 0.5


def test_quality_rank_taggers(tmp_path):
    agent = QualityAgent(state_path=str(tmp_path / "state.json"))
    agent.record("good-tagger", _mock_result(recency=1, topic=9),
                 "plain query", [])
    agent.record("bad-tagger",  _mock_result(recency=9, topic=1),
                 "as I mentioned before, fix this again", [])
    ranked = agent.rank_taggers()
    assert ranked[0][0] == "good-tagger"


def test_quality_persists_and_reloads(tmp_path):
    state = str(tmp_path / "state.json")
    agent1 = QualityAgent(state_path=state)
    agent1.record("t1", _mock_result(), "query", [])
    f1 = agent1.fitness("t1")

    agent2 = QualityAgent(state_path=state)
    assert abs(agent2.fitness("t1") - f1) < 0.001
