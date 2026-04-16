# Agent Task — Context Graph Cleanup & Bug Fixes

You are working on /Users/rich/Projects/tag-context — a context graph system for LLM context management.

## Background — What Needs Fixing

Three separate problems:

### 1. GP Tagger Not Fully Removed
GP tagger code is still present even though SPEC.md says production uses "fixed" mode only:
- `gp_tagger.py` — delete this file
- `data/gp-tagger.pkl` — delete this file
- `api/server.py` — remove `from gp_tagger import GeneticTagger` and the pickle loading + registration code (around line 17, 107-111)
- `ensemble.py` — remove all GP/hybrid/gp-only mode code (lines ~180-200). Keep only fixed mode with FixedTagger + baseline
- `config.py` — remove GP tagger comments from TAGGER_MODE docstring
- `tagger.py` — remove `StructuredProgramTagger` class (line ~389+). Keep `assign_tags()`, `TagAssignment`, `CORE_TAGS`, `_strip_metadata()`
- `fixed_tagger.py` — clean up docstrings referencing `StructuredProgramTagger`
- Test files — update references but keep them working
- Uninstall deap: `source venv/bin/activate && pip uninstall -y deap`

### 2. Tag Matching Bugs — FixedTagger Returns Wrong Tags
Two test cases that FAIL right now:
- "tell me about context-management" → should get tag `context-management` but gets only `research`
- "tell me about finance" → should get tag `finance` but gets NO tags

Root cause investigation needed in:
- `tags.yaml` — keywords like "context management" (with space) vs tag name "context-management" (with hyphen). The word-boundary regex `\bcontext management\b` will NOT work because the space breaks it
- `tags.yaml` — "finance" tag has keywords like "bank account", "credit card" but may not include "finance" as a keyword itself
- `fixed_tagger.py` matching logic — verify how keywords are wrapped and matched
- `tagger.assign_tags()` baseline function — verify CORE_TAGS patterns

### 3. Dashboard Tag Performance Shows All Zeros
All tags show `hits: 0` in the `/registry` endpoint. Investigation:
- `tag_registry.py` — does system registry persist hits? Does the API call `record_hit()`?
- Where should hits be recorded (during /assemble / /tag calls)?
- Check that the registry is actually tracking hits on tag matches

## What to Do

1. Read README.md and docs/SPEC.md first for the intended architecture
2. Complete task 1 (remove GP tagger code)
3. Fix tag matching bugs (task 2)
4. Fix dashboard zeros (task 3)
5. Verify the server uses FixedTagger properly in fixed mode ONLY
6. Run all tests: `source venv/bin/activate && python3 -m pytest tests/ -v`
7. Fix any broken tests
8. Restart the service: `launchctl stop com.glados.tag-context && launchctl start com.glados.tag-context`  
9. Verify:
   - `curl http://localhost:8302/health`
   - `curl -s -X POST http://localhost:8302/tag -H "Content-Type: application/json" -d '{"user_text":"tell me about context-management","assistant_text":""}'`
   - `curl -s -X POST http://localhost:8302/tag -H "Content-Type: application/json" -d '{"user_text":"Tell me about finance","assistant_text":""}'`
