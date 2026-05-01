"""
doc_indexer.py — Whiteboard markdown indexer (Phase A).

Per ~/Projects/whiteboard/proposals/wikilink-relationships-02.md §2.5.

Responsibilities:
  - Parse YAML frontmatter (pyyaml). Tolerate malformed YAML by warning + continuing
    with body-only indexing.
  - Extract wikilink edges from frontmatter values only (NOT body — Phase A scope).
  - Resolve wikilinks against the current doc set via priority:
        (1) filename match (case-insensitive, `.md` optional)
        (2) frontmatter `aliases:` match
        (3) H1 title match
        (4) ghost link (dst_doc_id=NULL)
  - Upsert into docs/edges/ghost_links/docs_fts via DocStore.
  - Cold-start reconcile pass over the whole vault (idempotent: hash-skip).
  - Watch mode (watchdog) for incremental updates.

CLI:
    python -m doc_indexer --cold-start --vault <vault> --db <db>
    python -m doc_indexer --watch       --vault <vault> --db <db>

doc_id convention: relative path from vault root, '/' separators, INCLUDING `.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

try:
    from doc_store import DocRow, DocStore, EdgeRow
except ImportError:  # pragma: no cover — module form
    from .doc_store import DocRow, DocStore, EdgeRow  # type: ignore


log = logging.getLogger("doc_indexer")


# ---------- constants ----------

FRONTMATTER_DELIM = "---"
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# Matches a frontmatter line whose value is one or more bare wikilinks
# (Tolaria/Obsidian style):  related_to: [[a]] [[b]] [[c]]
# Standard YAML rejects bare [[ as a flow-sequence start, so we rewrite it
# to a quoted-string list before feeding to yaml.safe_load.
_TOLARIA_LINE_RE = re.compile(
    r"^(\s*[A-Za-z_][\w\-]*\s*:\s*)((?:\[\[[^\[\]\n]+?\]\]\s*)+)\s*$",
    re.MULTILINE,
)

# System fields stored on the doc row but NEVER treated as edges
SYSTEM_FIELDS = {
    "_indexed_at",
    "_hash",
    "_external_source",
    "_external_kind",
    "_external_retrieved_at",
}

# Frontmatter keys that hold doc metadata, not relationship edges,
# even if they happen to contain a [[wikilink]] pattern.
NON_EDGE_FIELDS = {
    "type",
    "status",
    "title",
    "aliases",
    "version",
    "author",
    "agent",
    "human_in_the_loop",
    "created",
    "last_updated",
    "date",
    "duration_seconds",
    "purpose",
    "reviewer",
}


# ---------- frontmatter parsing ----------

@dataclass
class ParsedDoc:
    """Result of parsing one markdown file."""
    body: str                         # markdown body (post-frontmatter)
    frontmatter: Dict                 # parsed YAML or {} on missing/malformed
    title: Optional[str]              # first H1 text or None
    parse_warning: Optional[str] = None


def split_frontmatter(content: str) -> Tuple[Optional[str], str]:
    """Split (frontmatter_text, body). Returns (None, content) if no frontmatter.

    A frontmatter block is a leading `---` line, then YAML, then a closing `---`.
    """
    if not content.startswith(FRONTMATTER_DELIM + "\n") and not content.startswith(
        FRONTMATTER_DELIM + "\r\n"
    ):
        return None, content
    # Find the closing ---
    lines = content.split("\n")
    # lines[0] == "---". Look for the next "---" line.
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == FRONTMATTER_DELIM:
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            return fm_text, body
    # Unterminated frontmatter — treat as no frontmatter
    return None, content


def _preprocess_tolaria_yaml(fm_text: str) -> str:
    """Rewrite Tolaria-style bare-wikilink lines into valid YAML flow lists.

        related_to: [[contextgraph]] [[whiteboard]]
    becomes
        related_to: ['[[contextgraph]]', '[[whiteboard]]']

    YAML lists with quoted strings parse cleanly, and our wikilink extractor
    pulls the targets out of the strings just fine. Idempotent: lines that
    don't match are returned unchanged.
    """
    def _repl(m: re.Match) -> str:
        prefix, rest = m.group(1), m.group(2)
        targets = WIKILINK_RE.findall(rest)
        # Re-emit each as a quoted scalar containing the original [[...]] form,
        # so the extractor downstream still sees the wikilink syntax.
        quoted = ", ".join(f"'[[{t}]]'" for t in targets)
        return f"{prefix}[{quoted}]"

    return _TOLARIA_LINE_RE.sub(_repl, fm_text)


def parse_doc(content: str) -> ParsedDoc:
    """Parse a markdown file's text into (frontmatter, body, title)."""
    fm_text, body = split_frontmatter(content)
    fm: Dict = {}
    warning: Optional[str] = None
    if fm_text is not None:
        # Tolaria-style bare wikilinks are not valid YAML; pre-quote them.
        rewritten = _preprocess_tolaria_yaml(fm_text)
        try:
            loaded = yaml.safe_load(rewritten)
            if loaded is None:
                fm = {}
            elif isinstance(loaded, dict):
                fm = loaded
            else:
                warning = "frontmatter is not a mapping; ignoring"
        except yaml.YAMLError as e:
            warning = f"malformed YAML frontmatter: {e}"
            # One more rescue attempt: try the original (un-rewritten) text.
            # If that ALSO fails, give up (warning already set).
            try:
                loaded = yaml.safe_load(fm_text)
                if isinstance(loaded, dict):
                    fm = loaded
                    warning = None
            except yaml.YAMLError:
                pass

    # Title = first H1 in body
    title: Optional[str] = None
    m = H1_RE.search(body)
    if m:
        title = m.group(1).strip()

    return ParsedDoc(body=body, frontmatter=fm, title=title, parse_warning=warning)


# ---------- wikilink extraction ----------

def extract_wikilinks_from_value(value) -> List[str]:
    """Pull wikilink targets out of a frontmatter value.

    Supports:
      - bare scalar: "[[foo]]"
      - alias scalar: "[[foo|Display]]" (alias stripped, target retained)
      - YAML list: ["[[a]]", "[[b]]"]
      - space-separated on one line: "[[a]] [[b]] [[c]]" (Tolaria style)
      - mixed: list whose elements each contain multiple wikilinks
    Returns just the bare target names (left side of any |), in order encountered,
    deduplicated while preserving order.
    """
    out: List[str] = []
    seen: set = set()

    def push_str(s: str) -> None:
        for m in WIKILINK_RE.finditer(s):
            target = m.group(1).strip()
            # alias form [[Name|Display]] → keep "Name"
            if "|" in target:
                target = target.split("|", 1)[0].strip()
            if target and target not in seen:
                seen.add(target)
                out.append(target)

    if value is None:
        return out
    if isinstance(value, str):
        push_str(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                push_str(item)
            # silently ignore non-string list items (numbers, dicts, etc.)
    # ints/bools/dicts: no wikilinks possible
    return out


def extract_edges(frontmatter: Dict) -> List[Tuple[str, str]]:
    """Walk frontmatter and return [(rel_type, dst_name), ...] edges.

    Edges come from any field that is NOT system-prefixed (`_`) and NOT in
    NON_EDGE_FIELDS, whose value contains at least one [[wikilink]].
    """
    edges: List[Tuple[str, str]] = []
    for key, value in frontmatter.items():
        if not isinstance(key, str):
            continue
        if key.startswith("_") or key in NON_EDGE_FIELDS:
            continue
        targets = extract_wikilinks_from_value(value)
        for t in targets:
            edges.append((key, t))
    return edges


def extract_aliases(frontmatter: Dict) -> List[str]:
    """Return aliases list from frontmatter, robust to scalar or list form."""
    raw = frontmatter.get("aliases")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


# ---------- resolver ----------

@dataclass
class Resolver:
    """In-memory index over the current doc set for fast wikilink resolution.

    Built once at the start of a cold-start pass and rebuilt incrementally on
    upsert/delete. Resolution priority: filename → alias → title → ghost.
    """
    by_filename: Dict[str, List[str]]   # lower-case filename (no .md) -> [doc_id, ...]
    by_alias: Dict[str, List[str]]      # lower-case alias -> [doc_id, ...]
    by_title: Dict[str, List[str]]      # lower-case title -> [doc_id, ...]

    @classmethod
    def empty(cls) -> "Resolver":
        return cls({}, {}, {})

    @classmethod
    def from_store(cls, store: DocStore) -> "Resolver":
        r = cls.empty()
        for fname, doc_id in store.all_filenames():
            r.by_filename.setdefault(fname, []).append(doc_id)
        for alias, doc_id in store.all_aliases():
            r.by_alias.setdefault(alias, []).append(doc_id)
        for title, doc_id in store.all_titles():
            r.by_title.setdefault(title, []).append(doc_id)
        return r

    def add_doc(self, doc_id: str, aliases: List[str], title: Optional[str]) -> None:
        base = doc_id.rsplit("/", 1)[-1]
        if base.lower().endswith(".md"):
            base = base[:-3]
        self.by_filename.setdefault(base.lower(), [])
        if doc_id not in self.by_filename[base.lower()]:
            self.by_filename[base.lower()].append(doc_id)
        for a in aliases:
            self.by_alias.setdefault(a.lower(), [])
            if doc_id not in self.by_alias[a.lower()]:
                self.by_alias[a.lower()].append(doc_id)
        if title:
            self.by_title.setdefault(title.lower(), [])
            if doc_id not in self.by_title[title.lower()]:
                self.by_title[title.lower()].append(doc_id)

    def remove_doc(self, doc_id: str) -> None:
        for table in (self.by_filename, self.by_alias, self.by_title):
            for k, v in list(table.items()):
                if doc_id in v:
                    v.remove(doc_id)
                    if not v:
                        del table[k]

    def resolve(self, name: str) -> Tuple[Optional[str], bool]:
        """Resolve a wikilink. Returns (doc_id_or_None, ambiguous_flag)."""
        target = name.strip()
        if target.lower().endswith(".md"):
            target = target[:-3]
        key = target.lower()

        for table in (self.by_filename, self.by_alias, self.by_title):
            hits = table.get(key)
            if hits:
                if len(hits) > 1:
                    # Stable ordering: alphabetical
                    return sorted(hits)[0], True
                return hits[0], False
        return None, False


# ---------- vault scan ----------

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_md_files(vault_root: Path) -> List[Path]:
    """Yield all `.md` files under vault_root (excluding dotted directories
    like `.git`)."""
    results: List[Path] = []
    for root, dirs, files in os.walk(vault_root):
        # Skip dot-prefixed dirs (.git, .obsidian, etc.) in-place
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.endswith(".md"):
                results.append(Path(root) / fn)
    results.sort()
    return results


def doc_id_for(vault_root: Path, abs_path: Path) -> str:
    """Convert an absolute path to a doc_id (POSIX relative path with .md)."""
    rel = abs_path.resolve().relative_to(vault_root.resolve())
    return rel.as_posix()


# ---------- core indexer ----------

class DocIndexer:
    """Glue between vault filesystem and DocStore.

    Holds a resolver in memory; mutates it as docs are added/removed/updated.
    """

    def __init__(self, store: DocStore, vault_root: Path):
        self.store = store
        self.vault_root = vault_root.resolve()
        self.resolver = Resolver.from_store(store)

    # ---- core upsert path ----

    def _read_file_safe(self, abs_path: Path) -> Optional[Tuple[str, bytes]]:
        """Return (text, raw_bytes) or None if the file is unreadable."""
        try:
            raw = abs_path.read_bytes()
        except (FileNotFoundError, PermissionError) as e:
            log.warning("read_failed path=%s err=%s", abs_path, e)
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Fall back to lossy decode so we still index something
            text = raw.decode("utf-8", errors="replace")
            log.warning("non_utf8 path=%s", abs_path)
        return text, raw

    def index_file(self, abs_path: Path, force: bool = False) -> str:
        """Index one file. Returns one of: 'inserted', 'updated', 'skipped', 'failed'.

        If the file's hash matches the stored hash, it's skipped (idempotent).
        Updates the in-memory resolver after upsert.
        """
        try:
            doc_id = doc_id_for(self.vault_root, abs_path)
        except ValueError:
            log.error("path_outside_vault path=%s vault=%s", abs_path, self.vault_root)
            return "failed"

        result = self._read_file_safe(abs_path)
        if result is None:
            return "failed"
        text, raw = result

        new_hash = hashlib.sha256(raw).hexdigest()
        if not force:
            existing = self.store.get_doc_hash(doc_id)
            if existing == new_hash:
                return "skipped"

        parsed = parse_doc(text)
        if parsed.parse_warning:
            log.warning(
                "frontmatter_warn doc_id=%s warning=%s",
                doc_id,
                parsed.parse_warning,
            )

        fm = parsed.frontmatter or {}
        aliases = extract_aliases(fm)

        # Preliminary resolver bookkeeping: remove any old version of this doc
        # (in case alias/title changed) before resolving its edges.
        existed = self.store.get_doc(doc_id) is not None
        self.resolver.remove_doc(doc_id)
        self.resolver.add_doc(doc_id, aliases, parsed.title)

        # Build edges
        raw_edges = extract_edges(fm)
        edge_rows: List[EdgeRow] = []
        for rel_type, dst_name in raw_edges:
            resolved, ambiguous = self.resolver.resolve(dst_name)
            if ambiguous:
                log.warning(
                    "ambiguous_wikilink doc_id=%s rel=%s target=%s resolved_to=%s",
                    doc_id, rel_type, dst_name, resolved,
                )
            edge_rows.append(
                EdgeRow(
                    src_doc_id=doc_id,
                    rel_type=rel_type,
                    dst_name=dst_name,
                    dst_doc_id=resolved,
                )
            )

        try:
            mtime = abs_path.stat().st_mtime
        except OSError:
            mtime = time.time()

        # System fields → row columns
        external_source = fm.get("_external_source")
        external_kind = fm.get("_external_kind")
        if external_source is not None and not isinstance(external_source, str):
            external_source = str(external_source)
        if external_kind is not None and not isinstance(external_kind, str):
            external_kind = str(external_kind)

        type_field = fm.get("type")
        type_str = str(type_field) if type_field is not None else None

        status_field = fm.get("status")
        status_str = str(status_field) if status_field is not None else None

        doc_row = DocRow(
            doc_id=doc_id,
            abs_path=str(abs_path.resolve()),
            type=type_str,
            status=status_str,
            title=parsed.title,
            aliases=aliases,
            body_text=parsed.body,
            hash=new_hash,
            mtime=mtime,
            indexed_at=time.time(),
            external_source=external_source,
            external_kind=external_kind,
            is_reference=(type_str == "Reference"),
        )

        try:
            self.store.upsert_doc_with_edges(doc_row, edge_rows)
        except Exception as e:
            log.error("db_upsert_failed doc_id=%s err=%s", doc_id, e)
            return "failed"

        return "updated" if existed else "inserted"

    def delete_file(self, abs_path: Path) -> bool:
        try:
            doc_id = doc_id_for(self.vault_root, abs_path)
        except ValueError:
            return False
        self.resolver.remove_doc(doc_id)
        try:
            self.store.delete_doc(doc_id)
            return True
        except Exception as e:
            log.error("db_delete_failed doc_id=%s err=%s", doc_id, e)
            return False

    # ---- cold-start ----

    def cold_start(self) -> Dict[str, int]:
        """One-shot full reconcile. Returns stats dict.

        Strategy:
          1. Walk the vault.
          2. For every file, compute hash and check against the store (fast-path skip).
          3. For files that changed (or are new), parse frontmatter + extract title +
             aliases. Stash the parse result in memory.
          4. Build a complete resolver from BOTH (a) the store's existing doc set
             (covers unchanged files) and (b) all newly-parsed docs (covers new/
             changed). This guarantees forward references resolve regardless of
             vault traversal order.
          5. Upsert each changed doc with its edges resolved against the unified
             resolver. Single pass over changed files; one transaction per file.

        Idempotent: a second run with no changes performs zero DB writes.
        """
        files = discover_md_files(self.vault_root)
        log.info("cold_start_begin vault=%s files=%d", self.vault_root, len(files))

        # --- Phase 1: scan + parse changed files into memory ---
        @dataclass
        class _Pending:
            abs_path: Path
            doc_id: str
            raw_hash: str
            mtime: float
            parsed: ParsedDoc
            aliases: List[str]
            existed: bool

        pending: List[_Pending] = []
        stats = {"inserted": 0, "updated": 0, "skipped": 0, "failed": 0}

        for abs_path in files:
            try:
                doc_id = doc_id_for(self.vault_root, abs_path)
            except ValueError:
                log.error("path_outside_vault path=%s", abs_path)
                stats["failed"] += 1
                continue

            read = self._read_file_safe(abs_path)
            if read is None:
                stats["failed"] += 1
                continue
            text, raw = read
            new_hash = hashlib.sha256(raw).hexdigest()

            existing_hash = self.store.get_doc_hash(doc_id)
            if existing_hash == new_hash:
                stats["skipped"] += 1
                continue

            parsed = parse_doc(text)
            if parsed.parse_warning:
                log.warning(
                    "frontmatter_warn doc_id=%s warning=%s",
                    doc_id, parsed.parse_warning,
                )
            try:
                mtime = abs_path.stat().st_mtime
            except OSError:
                mtime = time.time()

            pending.append(_Pending(
                abs_path=abs_path,
                doc_id=doc_id,
                raw_hash=new_hash,
                mtime=mtime,
                parsed=parsed,
                aliases=extract_aliases(parsed.frontmatter or {}),
                existed=(existing_hash is not None),
            ))

        # --- Phase 2: build a unified resolver ---
        # Start from what's already in the store (covers the unchanged docs that
        # we're skipping but whose names are still valid resolution targets).
        resolver = Resolver.from_store(self.store)
        # Layer in pending docs (overrides any stale alias/title indexing).
        for p in pending:
            resolver.remove_doc(p.doc_id)
            resolver.add_doc(p.doc_id, p.aliases, p.parsed.title)

        # --- Phase 2.5: ghost-promotion sweep ---
        # If any doc in the store has a ghost edge whose target the resolver
        # can now resolve, that doc must be re-indexed even if its content
        # hash didn't change. Without this, a child doc with `belongs_to:
        # [[parent]]` written before parent.md exists will never get its
        # edge promoted when parent.md later appears.
        pending_ids = {p.doc_id for p in pending}
        ghost_dst_names = {
            r["dst_name"]
            for r in self.store._conn.execute(
                "SELECT DISTINCT dst_name FROM edges WHERE dst_doc_id IS NULL"
            ).fetchall()
        }
        promotable = {n for n in ghost_dst_names if resolver.resolve(n)[0] is not None}
        if promotable:
            promote_src_ids = {
                r["src_doc_id"]
                for r in self.store._conn.execute(
                    "SELECT DISTINCT src_doc_id FROM edges "
                    "WHERE dst_doc_id IS NULL AND dst_name IN "
                    "(" + ",".join("?" * len(promotable)) + ")",
                    tuple(promotable),
                ).fetchall()
            }
            # Add any not-already-pending docs to the pending list so they
            # get edge-rewritten in Phase 3.
            for src_id in promote_src_ids:
                if src_id in pending_ids:
                    continue
                # Find the file on disk + re-parse it (without changing its hash row)
                doc_row = self.store.get_doc(src_id)
                if doc_row is None:
                    continue
                abs_path = Path(doc_row.abs_path)
                if not abs_path.exists():
                    continue
                read = self._read_file_safe(abs_path)
                if read is None:
                    continue
                text, raw = read
                parsed = parse_doc(text)
                pending.append(_Pending(
                    abs_path=abs_path,
                    doc_id=src_id,
                    raw_hash=doc_row.hash,         # keep existing hash
                    mtime=doc_row.mtime,
                    parsed=parsed,
                    aliases=extract_aliases(parsed.frontmatter or {}),
                    existed=True,
                ))
                stats["skipped"] -= 1   # we're un-skipping it
                pending_ids.add(src_id)
            log.info(
                "ghost_promotion_sweep promotable_targets=%d affected_docs=%d",
                len(promotable), len(promote_src_ids),
            )

        # --- Phase 3: upsert each pending doc with resolved edges ---
        for p in pending:
            fm = p.parsed.frontmatter or {}
            edge_rows: List[EdgeRow] = []
            for rel_type, dst_name in extract_edges(fm):
                resolved, ambiguous = resolver.resolve(dst_name)
                if ambiguous:
                    log.warning(
                        "ambiguous_wikilink doc_id=%s rel=%s target=%s resolved_to=%s",
                        p.doc_id, rel_type, dst_name, resolved,
                    )
                edge_rows.append(EdgeRow(
                    src_doc_id=p.doc_id, rel_type=rel_type,
                    dst_name=dst_name, dst_doc_id=resolved,
                ))

            external_source = fm.get("_external_source")
            external_kind = fm.get("_external_kind")
            if external_source is not None and not isinstance(external_source, str):
                external_source = str(external_source)
            if external_kind is not None and not isinstance(external_kind, str):
                external_kind = str(external_kind)
            type_field = fm.get("type")
            type_str = str(type_field) if type_field is not None else None
            status_field = fm.get("status")
            status_str = str(status_field) if status_field is not None else None

            doc_row = DocRow(
                doc_id=p.doc_id,
                abs_path=str(p.abs_path.resolve()),
                type=type_str,
                status=status_str,
                title=p.parsed.title,
                aliases=p.aliases,
                body_text=p.parsed.body,
                hash=p.raw_hash,
                mtime=p.mtime,
                indexed_at=time.time(),
                external_source=external_source,
                external_kind=external_kind,
                is_reference=(type_str == "Reference"),
            )

            try:
                self.store.upsert_doc_with_edges(doc_row, edge_rows)
                stats["updated" if p.existed else "inserted"] += 1
            except Exception as e:
                log.error("db_upsert_failed doc_id=%s err=%s", p.doc_id, e)
                stats["failed"] += 1

        # Update our long-lived resolver with the latest state
        self.resolver = resolver

        log.info(
            "cold_start_end inserted=%d updated=%d skipped=%d failed=%d "
            "doc_count=%d edge_count=%d ghost_count=%d",
            stats["inserted"], stats["updated"], stats["skipped"], stats["failed"],
            self.store.doc_count(), self.store.edge_count(), self.store.ghost_link_count(),
        )
        return stats


# ---------- watch mode ----------

def run_watch(indexer: DocIndexer) -> None:
    """Block forever, watching vault for changes and reindexing on the fly."""
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class Handler(FileSystemEventHandler):
        def _is_md(self, path: str) -> bool:
            return path.endswith(".md") and "/." not in path

        def on_modified(self, event):
            if event.is_directory or not self._is_md(event.src_path):
                return
            log.info("watch_modified path=%s", event.src_path)
            indexer.index_file(Path(event.src_path))

        def on_created(self, event):
            if event.is_directory or not self._is_md(event.src_path):
                return
            log.info("watch_created path=%s", event.src_path)
            indexer.index_file(Path(event.src_path))

        def on_deleted(self, event):
            if event.is_directory or not self._is_md(event.src_path):
                return
            log.info("watch_deleted path=%s", event.src_path)
            indexer.delete_file(Path(event.src_path))

        def on_moved(self, event):
            if event.is_directory:
                return
            if hasattr(event, "src_path") and self._is_md(event.src_path):
                indexer.delete_file(Path(event.src_path))
            if hasattr(event, "dest_path") and self._is_md(event.dest_path):
                indexer.index_file(Path(event.dest_path))

    obs = Observer()
    obs.schedule(Handler(), str(indexer.vault_root), recursive=True)
    obs.start()
    log.info("watch_started vault=%s", indexer.vault_root)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("watch_interrupt")
    finally:
        obs.stop()
        obs.join()


# ---------- logging setup ----------

def configure_logging(log_path: Optional[Path], verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


# ---------- CLI ----------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="doc_indexer", description="Whiteboard doc indexer (Phase A)")
    p.add_argument("--vault", required=True, help="Path to whiteboard vault root")
    p.add_argument("--db", required=True, help="Path to SQLite DB (will be created)")
    p.add_argument("--cold-start", action="store_true", help="Run a full reconcile and exit")
    p.add_argument("--watch", action="store_true", help="Run as a daemon watching for changes")
    p.add_argument("--log-file", default=None, help="Path to structured log file")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not (args.cold_start or args.watch):
        # Default: cold-start once
        args.cold_start = True

    log_path = Path(args.log_file) if args.log_file else (
        Path(args.db).parent / "doc_indexer.log"
    )
    configure_logging(log_path, verbose=args.verbose)

    vault = Path(args.vault).expanduser().resolve()
    if not vault.exists():
        log.error("vault_missing path=%s", vault)
        return 2

    db_path = Path(args.db).expanduser()
    store = DocStore(db_path)
    try:
        indexer = DocIndexer(store, vault)

        if args.cold_start:
            t0 = time.time()
            stats = indexer.cold_start()
            elapsed = time.time() - t0
            log.info("cold_start_elapsed seconds=%.3f", elapsed)
            print(json.dumps({
                "cold_start_seconds": round(elapsed, 3),
                "stats": stats,
                "doc_count": store.doc_count(),
                "edge_count": store.edge_count(),
                "ghost_count": store.ghost_link_count(),
            }))

        if args.watch:
            # In watch mode, also do a startup reconcile to catch anything
            # that happened while the watcher was down.
            if not args.cold_start:
                indexer.cold_start()
            run_watch(indexer)
    finally:
        store.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
