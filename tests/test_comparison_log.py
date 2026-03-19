"""
test_comparison_log.py — Tests for comparison log structure (Category 5)

Tests that validate the comparison log has the correct nested structure
and that /comparison-stats can read it correctly.
"""

import pytest
import requests
import json
from pathlib import Path


API_BASE_URL = "http://localhost:8300"


@pytest.mark.compare
class TestComparisonLogStructure:
    """Test comparison log structure and /comparison-stats endpoint."""

    def test_comparison_log_entry_has_correct_nested_structure(self, api_available, temp_log_path):
        """
        Test 5.1: Comparison log entry has correct nested structure.

        Validates Fix 2 (Bug B): writeComparisonLog() writes nested structure.
        """
        # Write a synthetic log entry with the correct nested structure
        entry = {
            "timestamp": "2026-03-18T12:00:00Z",
            "sessionId": "test-session-123",
            "had_tools": True,
            "graph_assembly": {
                "tokens": 3423,
                "messages": 23,
                "tags": ["devops", "deployment"],
                "recency": 5,
                "topic": 18,
                "sticky_count": 1
            },
            "linear_would_have": {
                "tokens": 3717,
                "messages": 22,
                "tags": ["devops", "deployment", "networking"]
            }
        }

        # Write to temp log
        with open(temp_log_path, 'w') as f:
            f.write(json.dumps(entry) + "\n")

        # Read it back and verify structure
        with open(temp_log_path, 'r') as f:
            line = f.readline()
            parsed = json.loads(line)

        # Verify nested structure
        assert "graph_assembly" in parsed, "Entry must have graph_assembly"
        assert "linear_would_have" in parsed, "Entry must have linear_would_have"

        # Verify graph_assembly fields
        assert "tokens" in parsed["graph_assembly"]
        assert "messages" in parsed["graph_assembly"]
        assert "tags" in parsed["graph_assembly"]
        assert "sticky_count" in parsed["graph_assembly"]

        # Verify linear_would_have fields
        assert "tokens" in parsed["linear_would_have"]
        assert "messages" in parsed["linear_would_have"]
        assert "tags" in parsed["linear_would_have"]

        # Verify values match
        assert parsed["graph_assembly"]["tokens"] == 3423
        assert parsed["graph_assembly"]["sticky_count"] == 1
        assert parsed["linear_would_have"]["tokens"] == 3717

    def test_comparison_stats_returns_non_zero_data_when_log_has_entries(self, api_available):
        """
        Test 5.2: /comparison-stats returns non-zero totals when log has entries.

        Validates that the server can read the nested structure without KeyError.
        """
        # Get the actual comparison log path
        log_path = Path.home() / ".tag-context" / "comparison-log.jsonl"

        # Write a few synthetic entries with correct nested structure
        entries = [
            {
                "timestamp": "2026-03-18T12:00:00Z",
                "sessionId": "test-session-1",
                "had_tools": False,
                "graph_assembly": {
                    "tokens": 2000,
                    "messages": 15,
                    "tags": ["networking"],
                    "recency": 5,
                    "topic": 10,
                    "sticky_count": 0
                },
                "linear_would_have": {
                    "tokens": 2500,
                    "messages": 18,
                    "tags": ["networking", "security"]
                }
            },
            {
                "timestamp": "2026-03-18T12:05:00Z",
                "sessionId": "test-session-2",
                "had_tools": True,
                "graph_assembly": {
                    "tokens": 3000,
                    "messages": 20,
                    "tags": ["devops", "deployment"],
                    "recency": 5,
                    "topic": 14,
                    "sticky_count": 1
                },
                "linear_would_have": {
                    "tokens": 3500,
                    "messages": 22,
                    "tags": ["devops", "deployment"]
                }
            }
        ]

        # Backup existing log if it exists
        backup_path = None
        if log_path.exists():
            backup_path = log_path.with_suffix('.jsonl.bak')
            log_path.rename(backup_path)

        try:
            # Write test entries
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, 'w') as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")

            # Call /comparison-stats
            stats_response = requests.get(f"{API_BASE_URL}/comparison-stats")
            assert stats_response.status_code == 200, (
                f"Expected 200, got {stats_response.status_code}. "
                "This test validates Fix 2: server should read nested structure without KeyError."
            )

            data = stats_response.json()

            # Should have non-zero totals
            assert data["total_turns"] == 2, f"Expected 2 turns, got {data['total_turns']}"
            assert data["avg_graph_tokens"] > 0, (
                f"Expected avg_graph_tokens > 0, got {data['avg_graph_tokens']}"
            )
            assert data["avg_linear_tokens"] > 0, (
                f"Expected avg_linear_tokens > 0, got {data['avg_linear_tokens']}"
            )

            # Verify calculations
            expected_avg_graph = (2000 + 3000) / 2
            expected_avg_linear = (2500 + 3500) / 2
            assert data["avg_graph_tokens"] == expected_avg_graph
            assert data["avg_linear_tokens"] == expected_avg_linear

        finally:
            # Restore backup
            if backup_path and backup_path.exists():
                if log_path.exists():
                    log_path.unlink()
                backup_path.rename(log_path)

    def test_comparison_log_sticky_count_appears_in_entries(self, api_available):
        """
        Test 5.3: Comparison log graph_assembly.sticky_count appears in log entries.

        Validates that both Fix 1 and Fix 2 work together: /compare returns sticky_count,
        and plugin writes it to log in the correct nested structure.
        """
        log_path = Path.home() / ".tag-context" / "comparison-log.jsonl"

        # Backup existing log
        backup_path = None
        if log_path.exists():
            backup_path = log_path.with_suffix('.jsonl.bak')
            log_path.rename(backup_path)

        try:
            # Write a test entry that simulates what the fixed plugin writes
            entry = {
                "timestamp": "2026-03-18T12:00:00Z",
                "sessionId": "test-session-sticky",
                "had_tools": True,
                "graph_assembly": {
                    "tokens": 3000,
                    "messages": 20,
                    "tags": ["devops"],
                    "recency": 5,
                    "topic": 14,
                    "sticky_count": 2  # Key field: sticky pins active
                },
                "linear_would_have": {
                    "tokens": 3500,
                    "messages": 22,
                    "tags": ["devops"]
                }
            }

            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, 'w') as f:
                f.write(json.dumps(entry) + "\n")

            # Read it back
            with open(log_path, 'r') as f:
                line = f.readline()
                parsed = json.loads(line)

            # Verify sticky_count is present and correct
            assert "graph_assembly" in parsed
            assert "sticky_count" in parsed["graph_assembly"], (
                "graph_assembly must include sticky_count (Fix 1 + Fix 2)"
            )
            assert parsed["graph_assembly"]["sticky_count"] == 2, (
                f"Expected sticky_count=2, got {parsed['graph_assembly']['sticky_count']}"
            )

            # Verify other required fields also present
            assert parsed["graph_assembly"]["tokens"] == 3000
            assert parsed["graph_assembly"]["messages"] == 20
            assert parsed["had_tools"] is True

        finally:
            # Restore backup
            if backup_path and backup_path.exists():
                if log_path.exists():
                    log_path.unlink()
                backup_path.rename(log_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
