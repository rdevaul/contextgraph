"""
test_reframing.py — Tests for reframing detection.

Tests positive and negative cases for reframing signal detection.
"""

import pytest
from reframing import detect_reframing, is_system_artifact, reframing_rate, detect_reference


# ── Positive cases (should detect reframing) ──────────────────────────────────

def test_memory_failure_patterns():
    """Test detection of explicit memory failure phrases."""
    assert detect_reframing("You forgot about the nginx config").is_reframing
    assert detect_reframing("You don't remember what we discussed").is_reframing
    assert detect_reframing("As I mentioned earlier, the API needs auth").is_reframing
    assert detect_reframing("I already told you the deployment steps").is_reframing
    assert detect_reframing("We already discussed this yesterday").is_reframing
    assert detect_reframing("Going back to the database schema").is_reframing
    assert detect_reframing("Earlier we talked about using Redis").is_reframing
    assert detect_reframing("Remember when we fixed the cache issue?").is_reframing


def test_re_establishment_patterns():
    """Test detection of context re-establishment phrases."""
    assert detect_reframing("To recap, we're building a chat app").is_reframing
    assert detect_reframing("To quickly summarize what we've done so far").is_reframing
    assert detect_reframing("Just to bring you up to speed").is_reframing
    assert detect_reframing("Let me remind you of the requirements").is_reframing
    assert detect_reframing("As a reminder, we need TLS support").is_reframing
    assert detect_reframing("To refresh your memory, the API is REST-based").is_reframing
    assert detect_reframing("Context: we have a microservices architecture").is_reframing
    assert detect_reframing("Background: the system uses PostgreSQL").is_reframing


def test_frustration_patterns():
    """Test detection of frustration signals."""
    assert detect_reframing("Can you check again and fix the logs?").is_reframing  # again...check pattern
    assert detect_reframing("It's still not working properly").is_reframing
    assert detect_reframing("Let me explain one more time").is_reframing
    assert detect_reframing("As I've said before, we need auth").is_reframing
    assert detect_reframing("Why is it still failing?").is_reframing
    assert detect_reframing("I've already told you the config path").is_reframing


def test_context_provision_patterns():
    """Test detection of context provision at message start."""
    assert detect_reframing("So, we have a React frontend and Node backend. Now what?").is_reframing
    assert detect_reframing("Okay, we've deployed to staging. What's next?").is_reframing
    assert detect_reframing("As we discussed, the API uses JWT. How do I refresh tokens?").is_reframing
    assert detect_reframing("Given what we built, should we add caching?").is_reframing  # matches "given...built,"
    assert detect_reframing("Since we already have Redis, can we use it for sessions?").is_reframing
    assert detect_reframing("Building on what we made, I need to add auth").is_reframing


def test_multiple_signals():
    """Test messages with multiple reframing signals."""
    result = detect_reframing(
        "As I mentioned before, to recap: we need to fix the deployment again."
    )
    assert result.is_reframing
    # Should detect multiple categories
    assert len(result.signals_found) >= 2


# ── Negative cases (should NOT detect reframing) ──────────────────────────────

def test_normal_questions():
    """Test that normal questions don't trigger false positives."""
    assert not detect_reframing("How do I deploy to production?").is_reframing
    assert not detect_reframing("What's the best way to handle errors?").is_reframing
    assert not detect_reframing("Can you help me with the API design?").is_reframing
    assert not detect_reframing("I'm working on the frontend now").is_reframing


def test_task_continuation():
    """Test that task continuation doesn't trigger reframing."""
    assert not detect_reframing("Great! Let's move on to the next step").is_reframing
    assert not detect_reframing("Now I need to add the database layer").is_reframing
    assert not detect_reframing("What should we do next?").is_reframing


def test_background_without_colon():
    """Test that 'background' without colon doesn't trigger (e.g., 'background task')."""
    # 'background:' with colon should trigger
    assert detect_reframing("Background: the system uses Redis").is_reframing

    # 'background task' without colon should NOT trigger
    assert not detect_reframing("I need to run a background task").is_reframing
    assert not detect_reframing("The background process is failing").is_reframing


def test_context_without_colon():
    """Test that 'context' without colon doesn't trigger."""
    # 'context:' with colon should trigger
    assert detect_reframing("Context: we're using microservices").is_reframing

    # 'context' in other usage should NOT trigger
    assert not detect_reframing("The context window is too small").is_reframing
    assert not detect_reframing("In the context of this project").is_reframing


# ── System artifact exclusion ──────────────────────────────────────────────────

def test_system_artifacts_excluded():
    """Test that system artifacts are excluded from reframing detection."""
    # These contain reframing-like phrases but are system-generated
    assert not detect_reframing(
        "The session ran out of context and had to compact. To recap: ..."
    ).is_reframing

    assert not detect_reframing(
        "[cron:abc-123] Daily backup completed. As mentioned, backups run at midnight."
    ).is_reframing

    assert not detect_reframing(
        "[System Message] post.compaction check. Context: system health is good."
    ).is_reframing

    assert not detect_reframing(
        "WORKFLOW_AUTO: As a reminder, the workflow runs every hour."
    ).is_reframing


def test_is_system_artifact():
    """Test system artifact detection directly."""
    assert is_system_artifact("ran out of context and had to compact")
    assert is_system_artifact("[cron:123] Task done")
    assert is_system_artifact("post.compaction check")
    assert is_system_artifact("WORKFLOW_AUTO event")
    assert is_system_artifact("[System Message] Alert")

    assert not is_system_artifact("Normal user message")
    assert not is_system_artifact("I need to set up a cron job")


# ── Reframing rate calculation ──────────────────────────────────────────────────

def test_reframing_rate_calculation():
    """Test reframing rate computation."""
    messages = [
        "How do I deploy?",                      # Not reframing
        "You forgot the config",                 # Reframing
        "What's the API endpoint?",              # Not reframing
        "As I mentioned, we need TLS",           # Reframing
    ]

    rate = reframing_rate(messages)
    assert rate == 0.5  # 2 out of 4


def test_reframing_rate_empty():
    """Test reframing rate with empty input."""
    assert reframing_rate([]) == 0.0


def test_reframing_rate_all_normal():
    """Test reframing rate when all messages are normal."""
    messages = [
        "How do I do X?",
        "What about Y?",
        "Help with Z please",
    ]
    assert reframing_rate(messages) == 0.0


def test_reframing_rate_all_reframing():
    """Test reframing rate when all messages are reframing."""
    messages = [
        "You forgot X",
        "As I said before, Y",
        "To recap, Z",
    ]
    assert reframing_rate(messages) == 1.0


# ── Confidence scoring ──────────────────────────────────────────────────────

def test_confidence_single_category():
    """Test that single category match gives 0.25 confidence."""
    result = detect_reframing("You forgot the config")
    assert result.confidence == 0.25
    assert len(result.signals_found) == 1


def test_confidence_multiple_categories():
    """Test that multiple category matches increase confidence."""
    # Contains both memory_failure ("you forgot") and frustration ("again")
    result = detect_reframing("You forgot again to check the logs")
    assert result.confidence == 0.5  # 2 categories = 0.5
    assert len(result.signals_found) == 2


def test_confidence_capped_at_one():
    """Test that confidence is capped at 1.0."""
    # Construct a message that triggers all 4 categories
    result = detect_reframing(
        "So we have the API. You forgot I mentioned one more time as a reminder to recap"
    )
    assert result.confidence <= 1.0


# ── Reference detection (for sticky threads) ────────────────────────────────────

def test_reference_detection_positive():
    """Test detection of anaphoric references."""
    assert detect_reference("Any updates?")
    assert detect_reference("What's the status?")
    assert detect_reference("Did that work?")
    assert detect_reference("What happened with the deployment?")
    assert detect_reference("How did it go?")
    assert detect_reference("Can you check on that?")
    assert detect_reference("Is it done?")
    assert detect_reference("Any luck with the fix?")


def test_reference_detection_negative():
    """Test that explicit context doesn't trigger reference detection."""
    assert not detect_reference("How do I deploy the API?")
    assert not detect_reference("Can you help with authentication?")
    assert not detect_reference("I need to add a new feature")


def test_reference_excludes_system_artifacts():
    """Test that reference detection excludes system artifacts."""
    assert not detect_reference("[cron:123] Any updates on the backup?")
    assert not detect_reference("WORKFLOW_AUTO: Did that work?")
