# Merge Channel Labels — Admin Guide

## What This Does

Merges channel labels in the context graph database, consolidating message rows
from multiple source labels into a single target label. Handles both SQL rows AND
user tag YAML files.

## Endpoints

All endpoints require `POST` to `http://localhost:8302/admin/...`

### 1. `POST /admin/channel-labels` — View current labels

Returns counts of messages and sessions per channel label.

```bash
curl http://localhost:8302/admin/channel-labels
```

### 2. `POST /admin/merge-channel-labels` — Merge specific labels

**Request body:**

```json
{
  "source_labels": ["994902066", "(null)"],
  "target_label": "rich",
  "dry_run": true,
  "merge_tags": true
}
```

- `source_labels` — list of labels to merge. Use `""` (empty string) to match
  `NULL` channel_label values in SQLite.
- `dry_run: true` — preview what would change, no modifications made
- `merge_tags: true` — also merge user tag YAML files
- **Always run dry_run first!**

### 3. `POST /admin/merge-all-channel-labels` — Merge everything into target

Same request format, but merges ALL non-target labels into the specified target.
Use with extra caution.

### 4. `POST /admin/retag` — Re-tag messages for a channel label

Re-runs the tagger on all messages from a given channel label. Useful after a
merge to ensure tags are up to date.

## Migration History

### 2026-04-09: Merged (null) → rich

- **Before:** `rich` = 3,022 messages, `NULL` = 2,362 messages
- **After:** `rich` = 5,384 messages (single label)
- **Dry run preview:** 2,362 messages / 728 sessions affected
- **Execution:** 2,362 messages / 728 sessions merged successfully
- **Backups:**
  - `backup_20260409_170052_pre_merge.db` — manual pre-run backup
  - `store_20260409_170432.db` — auto-backup (first attempt)
  - `store_20260409_170528.db` — auto-backup (successful run)

## Migration Steps (General)

### Step 1: Dry Run

```bash
curl -s -X POST http://localhost:8302/admin/merge-channel-labels \
  -H "Content-Type: application/json" \
  -d '{"source_labels":["(null)"],"target_label":"rich","dry_run":true}'
```

### Step 2: Execute

⚠️ **DANGER: Destructive operation. Verify dry run first.**

```bash
curl -s -X POST http://localhost:8302/admin/merge-channel-labels \
  -H "Content-Type: application/json" \
  -d '{"source_labels":["(null)"],"target_label":"rich","dry_run":false}'
```

### Step 3: Verify

```bash
curl http://localhost:8302/admin/channel-labels
```

## Rollback

If something goes wrong:

```bash
# Stop the service
launchctl stop com.contextgraph.api

# Restore from backup
ls ~/.tag-context/backups/
cp ~/.tag-context/backups/backup_YYYYMMDD_HHMMSS.db \
   ~/.tag-context/store.db

# Restart
launchctl start com.contextgraph.api
```

## Technical Details

- **File:** `store.py` → `merge_channel_labels()` method
  - Handles `NULL` channel_label values by treating empty string as `IS NULL`
  - Uses `WHERE channel_label IS NULL` when source contains `""` or `null`
- **Backup location:** `~/.tag-context/backups/`
- **Max backups retained:** 5 (older ones are pruned automatically)
- **Tag YAML files:** `~/.tag-context/user_tags/{source}.yaml` → merged/removed after SQL merge
