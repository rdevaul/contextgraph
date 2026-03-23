"""
test_is_automated.py — Tests for _is_automated_turn() edge cases.

Tests the automated turn detection logic in logger.py.
"""

import pytest
from logger import _is_automated_turn


def test_cron_job_detection():
    """Test detection of cron job payloads."""
    assert _is_automated_turn("[cron:abc-123] Daily backup started")
    assert _is_automated_turn("[cron:task-456] Weekly report")


def test_heartbeat_detection():
    """Test detection of heartbeat messages."""
    assert _is_automated_turn("Read HEARTBEAT.md if it exists")
    assert _is_automated_turn("HEARTBEAT_OK")


def test_local_watcher_detection():
    """Test detection of local file watcher events."""
    assert _is_automated_turn("[local-watcher] File changed: config.yaml")
    assert _is_automated_turn("[local-watcher] New file detected")


def test_subagent_detection():
    """Test detection of subagent completion events."""
    assert _is_automated_turn("[subagent:abc] Task completed")
    assert _is_automated_turn("[Subagent] Analysis finished")
    assert _is_automated_turn("[SUBAGENT] Report ready")


def test_workflow_auto_detection():
    """Test detection of WORKFLOW_AUTO events."""
    assert _is_automated_turn("[WORKFLOW_AUTO] Post-compaction check")
    assert _is_automated_turn("[WORKFLOW_AUTO:123] Automated task")


def test_length_guard():
    """Test that long messages (>500 chars) are not marked as automated."""
    # Short automated message should be detected
    assert _is_automated_turn("[cron:123] Task done")

    # Long message starting with automated prefix should NOT be marked as automated
    long_message = "[cron:123] " + "x" * 500
    assert not _is_automated_turn(long_message)

    # Subagent with long content
    long_subagent = "[subagent:abc] " + "This is a long detailed report. " * 30
    assert not _is_automated_turn(long_subagent)


def test_false_positives():
    """Test messages that should NOT be marked as automated."""
    # Normal user questions
    assert not _is_automated_turn("How do I set up a cron job?")
    assert not _is_automated_turn("What's the heartbeat interval?")
    assert not _is_automated_turn("Can you explain subagents?")

    # Messages containing keywords but not matching patterns
    assert not _is_automated_turn("The cron job failed yesterday")
    assert not _is_automated_turn("I need to configure the local watcher")

    # Empty or whitespace
    assert not _is_automated_turn("")
    assert not _is_automated_turn("   ")


def test_whitespace_normalization():
    """Test that leading/trailing whitespace is handled correctly."""
    assert _is_automated_turn("  [cron:123] Task done  ")
    assert _is_automated_turn("\n[local-watcher] Event detected\n")
    assert _is_automated_turn("  HEARTBEAT_OK  ")


def test_case_sensitivity():
    """Test case sensitivity of pattern matching."""
    # Most patterns are case-sensitive except subagent
    assert not _is_automated_turn("[CRON:123] Task")  # cron is lowercase only
    assert not _is_automated_turn("heartbeat_ok")     # exact match required

    # Subagent is explicitly case-insensitive
    assert _is_automated_turn("[subagent] done")
    assert _is_automated_turn("[SUBAGENT] done")
    assert _is_automated_turn("[Subagent] done")


def test_partial_matches():
    """Test that partial matches don't trigger false positives."""
    # Should NOT match if pattern is in the middle
    assert not _is_automated_turn("The [cron:123] job failed")
    assert not _is_automated_turn("Previous: [local-watcher] event")

    # But should match if at the start
    assert _is_automated_turn("[cron:123] job completed")
    assert _is_automated_turn("[local-watcher] event detected")


def test_heartbeat_substring_match():
    """Test heartbeat detection as substring (not start-only)."""
    # Heartbeat is detected anywhere in the message
    assert _is_automated_turn("Please Read HEARTBEAT.md if it exists and report status")
    assert _is_automated_turn("System check: Read HEARTBEAT.md if it exists")


def test_edge_cases():
    """Test various edge cases."""
    # Just the prefix
    assert _is_automated_turn("[cron:")
    assert _is_automated_turn("[local-watcher]")
    assert _is_automated_turn("[subagent")

    # Multiple spaces
    assert _is_automated_turn("[cron:123]     Task completed")

    # Special characters in cron ID
    assert _is_automated_turn("[cron:abc-def-123-456] Task")
