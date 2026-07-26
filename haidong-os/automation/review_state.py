#!/usr/bin/env python3
"""Stage-5 unified review-state CLI.

Aggregates pending review items from the four bounded domains (knowledge,
experience, project, fact) into a normalized, read-only report; records
human review decisions in a single append-only log; and strictly validates
all four domain inputs plus the unified review log.

Design constraints:

* Standard library only.
* Configurable roots with product-convention defaults; callers must pass
  explicit roots when operating outside the default host layout.
* Reads only the declared pending sources for each domain:

  - knowledge: ``<wiki-root>/_meta/knowledge-learning/*.jsonl`` rows with
    ``review_required=true`` and ``<wiki-root>/_meta/writeback-inbox/*.md``
    frontmatter (streaming until the closing ``---``; body is never read).
  - experience: ``<experience-root>/inbox/*.jsonl`` candidate events and
    ``<experience-root>/reviews/*.jsonl`` human decisions.
  - project: ``<projects-root>/inbox/project_change_*.json`` proposals and
    ``<projects-root>/audit/applied.jsonl`` applied records.
  - fact: ``<facts-root>/inbox/*.jsonl`` proposals and
    ``<facts-root>/events/*.jsonl`` rows with ``verification=disputed``.

* Normalized items carry stable ``review_item_id``, ``domain``,
  ``source_id`` / ``source_ref``, ``title``, ``summary`` (length-limited and
  secret-redacted), ``domain_status``, ``latest_review``, ``review_required``,
  and ``evidence_refs``.  Every item reports ``auto_promote``,
  ``wiki_write``, ``cass_write``, ``project_apply``, and ``fact_append`` as
  ``false`` — this tool never writes to any domain.
* ``report`` fails closed: bad JSON, non-objects, missing fields, path
  escapes, or secret-like content make ``valid=false`` and leave
  ``items`` / ``recommendations`` empty.
* ``review`` appends an ``accept`` / ``reject`` / ``defer`` decision for an
  existing ``review_item_id`` to ``<review-root>/reviews/YYYY-MM.jsonl``.
  It is opinion-only: it never mutates a domain source.  Records are
  deterministic, idempotent, written under a global flock with
  ``O_APPEND|O_NOFOLLOW|fsync``, and symlink paths are refused.
* ``validate`` walks the same four-domain inputs plus the unified review log
  and returns ``valid=false`` on any structural or safety issue.
* No full wiki scan, no ``links/`` access, no Gbrain/CASS calls, no
  anchor-event creation, no runner, no promotion/apply/append.

Exit codes: 0 ok, 1 invalid/issues found, 2 usage/safety error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


DEFAULT_WIKI_ROOT = Path("/Users/jianghaidong/hbrain/llm-wiki")
DEFAULT_EXPERIENCE_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/experience-review")
DEFAULT_PROJECTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/projects")
DEFAULT_FACTS_ROOT = Path("/Users/jianghaidong/hbrain/facts")
DEFAULT_REVIEW_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/review-state")

SCHEMA_VERSION = 1
MAX_ITEMS = 20
MAX_LINE_CHARS = 300

REVIEW_DECISIONS = ("accept", "reject", "defer")
DOMAINS = ("knowledge", "experience", "project", "fact")

# Same family as five_domain_daily.py / experience_review.py.
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"\s*(?:::|=|:)\s*\S+|\bBearer\s+[A-Za-z0-9._~+/=-]+"
)

# Domain statuses considered terminal/closed when no explicit review_required
# field says otherwise.
CLOSED_STATUSES = {
    "applied",
    "done",
    "rejected",
    "canonical",
    "closed",
    "superseded",
}


class ReviewStateError(Exception):
    """Usage or safety error -> exit 2."""


class ValidationError(Exception):
    """Raised when strict validation fails; reported as valid=false."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def parse_rfc3339(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("timestamp is naive")
    return parsed


def truncate(text: str, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sanitize_text(text: str) -> tuple[str, int]:
    return SECRET_RE.subn("[REDACTED]", text)


def compact(value: Any, limit: int = MAX_LINE_CHARS) -> str:
    if isinstance(value, str):
        return truncate(value, limit)
    try:
        return truncate(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)
    except (TypeError, ValueError):
        return truncate(str(value), limit)


def safe_text(value: Any, limit: int = MAX_LINE_CHARS) -> str:
    raw = compact(value, limit)
    clean, _ = sanitize_text(raw)
    return clean


def ensure_safe_root(root: Path, *children: str) -> None:
    if root.is_symlink():
        raise ReviewStateError(f"root must not be a symlink: {root}")
    current = root
    for child in children:
        current = current / child
        if current.is_symlink():
            raise ReviewStateError(f"path component must not be a symlink: {current}")


def iter_jsonl(directory: Path) -> Iterable[tuple[Path, int, str]]:
    """Yield (path, line_number, line) for regular non-symlink JSONL files.

    Callers are responsible for parsing each line with ``json.loads()``.
    """
    if not directory.exists():
        return
    if directory.is_symlink():
        raise ReviewStateError(f"directory must not be a symlink: {directory}")
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                yield path, line_number, line


def iter_json(directory: Path, issues: list[dict[str, Any]]) -> Iterable[tuple[Path, dict[str, Any]]]:
    """Yield (path, object) for regular non-symlink JSON files.

    Malformed, unreadable, or non-object files are reported through
    ``issues`` instead of raising, so callers can fail closed with
    ``valid=false`` and empty ``items`` / ``recommendations``.
    """
    if not directory.exists():
        return
    if directory.is_symlink():
        issues.append({"path": str(directory), "line": None, "issue": "directory must not be a symlink"})
        return
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": None, "issue": f"invalid_json: {exc.msg}"})
            continue
        except UnicodeError as exc:
            issues.append({"path": str(path), "line": None, "issue": f"unicode_error: {exc}"})
            continue
        except OSError as exc:
            issues.append({"path": str(path), "line": None, "issue": f"os_error: {exc}"})
            continue
        if not isinstance(data, dict):
            issues.append({"path": str(path), "line": None, "issue": "row is not an object"})
            continue
        yield path, data


def iter_markdown(directory: Path) -> Iterable[tuple[Path, str]]:
    """Yield (path, frontmatter text) for regular non-symlink Markdown files.

    Only the frontmatter (text up to the first closing ``---`` line) is read;
    the body is never loaded into memory.
    """
    if not directory.exists():
        return
    if directory.is_symlink():
        raise ReviewStateError(f"directory must not be a symlink: {directory}")
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
            if first != "---\n":
                continue
            lines = [first]
            while True:
                line = handle.readline()
                if not line:
                    break
                lines.append(line)
                if line == "---\n":
                    break
            if lines[-1:] != ["---\n"]:
                continue
            yield path, "".join(lines)


def parse_frontmatter(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not text.startswith("---\n"):
        return values
    end = text.find("\n---\n", 4)
    if end < 0:
        return values
    for line in text[4:end].splitlines():
        if not line or line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().split(" #", 1)[0].strip("\"'")
    return values


def validate_no_secrets(value: Any) -> list[str]:
    """Return issue strings if secret-like content is detected."""
    text = canonical_json(value)
    if SECRET_RE.search(text):
        return ["secret-like content detected"]
    return []


def evidence_refs_from(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "fact_id" and isinstance(v, str) and v:
                refs.append(v)
            elif k == "decision_ref" and isinstance(v, str) and v:
                refs.append(v)
            elif isinstance(v, str) and v:
                refs.append(v)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                refs.append(item)
            elif isinstance(item, dict):
                refs.extend(evidence_refs_from(item))
    return [safe_text(r) for r in refs[:10]]


# ---------------------------------------------------------------------------
# Domain readers
# ---------------------------------------------------------------------------


def scan_symlinks(root: Path, subpaths: list[str], issues: list[dict[str, Any]]) -> None:
    """Report symlink files/directories under root/subpath as path-safety issues."""
    if not root.exists():
        return
    if root.is_symlink():
        issues.append({"path": str(root), "line": None, "issue": "root path is a symlink"})
        return
    for sub in subpaths:
        target = root / sub
        if target.is_symlink():
            issues.append({"path": str(target), "line": None, "issue": "path component is a symlink"})
            continue
        if not target.exists():
            continue
        if not target.is_dir():
            continue
        for entry in sorted(target.iterdir()):
            if entry.is_symlink():
                issues.append({"path": str(entry), "line": None, "issue": "symlinked entry in review source tree"})


def knowledge_items(wiki_root: Path, issues: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ensure_safe_root(wiki_root)
    scan_symlinks(wiki_root, ["_meta/knowledge-learning", "_meta/writeback-inbox"], issues)
    items: list[dict[str, Any]] = []
    learning_dir = wiki_root / "_meta" / "knowledge-learning"
    for path, line_number, line in iter_jsonl(learning_dir):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        if row.get("review_required") is not True:
            continue
        for issue in validate_no_secrets({k: row.get(k) for k in ("title", "summary", "sources")}):
            issues.append({"path": str(path), "line": line_number, "issue": issue})
            break
        else:
            learning_id = row.get("learning_id")
            item_id = f"knowledge_{learning_id}" if isinstance(learning_id, str) and learning_id else stable_id("knowledge_", row)
            items.append(
                {
                    "review_item_id": item_id,
                    "domain": "knowledge",
                    "source_id": learning_id if isinstance(learning_id, str) else None,
                    "source_ref": row.get("path") if isinstance(row.get("path"), str) else None,
                    "title": safe_text(row.get("title"), MAX_LINE_CHARS),
                    "summary": safe_text(row.get("summary"), MAX_LINE_CHARS),
                    "domain_status": str(row.get("status", "proposal")),
                    "latest_review": None,
                    "review_required": True,
                    "evidence_refs": evidence_refs_from(row.get("sources")),
                    "auto_promote": False,
                    "wiki_write": False,
                    "cass_write": False,
                    "project_apply": False,
                    "fact_append": False,
                }
            )

    inbox_dir = wiki_root / "_meta" / "writeback-inbox"
    for path, frontmatter in iter_markdown(inbox_dir):
        meta = parse_frontmatter(frontmatter)
        status = meta.get("status", "").split()[0]
        if status not in {"proposed", "candidate"}:
            continue
        title = meta.get("title") or path.stem
        learning_id = meta.get("learning_id")
        item_id = f"knowledge_{learning_id}" if isinstance(learning_id, str) and learning_id else stable_id("knowledge_", str(path.relative_to(wiki_root)))
        items.append(
            {
                "review_item_id": item_id,
                "domain": "knowledge",
                "source_id": learning_id if isinstance(learning_id, str) else None,
                "source_ref": str(path.relative_to(wiki_root)),
                "title": safe_text(title, MAX_LINE_CHARS),
                "summary": safe_text(meta.get("summary") or "", MAX_LINE_CHARS),
                "domain_status": status,
                "latest_review": None,
                "review_required": True,
                "evidence_refs": evidence_refs_from(meta.get("sources")),
                "auto_promote": False,
                "wiki_write": False,
                "cass_write": False,
                "project_apply": False,
                "fact_append": False,
            }
        )

    items.sort(key=lambda item: (item["domain_status"], item["review_item_id"]))
    return items[:limit]


def experience_items(experience_root: Path, issues: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    ensure_safe_root(experience_root)
    scan_symlinks(experience_root, ["inbox", "reviews"], issues)
    inbox_dir = experience_root / "inbox"
    reviews_dir = experience_root / "reviews"

    events: list[dict[str, Any]] = []
    for path, line_number, line in iter_jsonl(inbox_dir):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        events.append((path, line_number, row))

    reviews_by_event: dict[str, list[dict[str, Any]]] = {}
    for path, line_number, line in iter_jsonl(reviews_dir):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        event_id = row.get("event_id")
        if isinstance(event_id, str):
            reviews_by_event.setdefault(event_id, []).append(row)

    items: list[dict[str, Any]] = []
    for path, line_number, row in events:
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            issues.append({"path": str(path), "line": line_number, "issue": "missing event_id"})
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
        for issue in validate_no_secrets({"source": source, "candidate": candidate}):
            issues.append({"path": str(path), "line": line_number, "issue": issue})
            break
        else:
            domain_review = latest_review(reviews_by_event.get(event_id, []))
            items.append(
                {
                    "review_item_id": event_id,
                    "domain": "experience",
                    "source_id": event_id,
                    "source_ref": source.get("receipt_id") if isinstance(source.get("receipt_id"), str) else None,
                    "title": safe_text(candidate.get("pattern") or candidate.get("title") or event_id, MAX_LINE_CHARS),
                    "summary": safe_text(candidate, MAX_LINE_CHARS),
                    "domain_status": str(row.get("status", "inbox")),
                    "latest_review": domain_review,
                    "review_required": True,
                    "evidence_refs": evidence_refs_from([source.get("receipt_id"), source.get("project_id")]),
                    "auto_promote": False,
                    "wiki_write": False,
                    "cass_write": False,
                    "project_apply": False,
                    "fact_append": False,
                }
            )

    items.sort(key=lambda item: item["review_item_id"])
    return items[:limit], reviews_by_event


def project_items(projects_root: Path, issues: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Project domain items — inbox proposals + applied audit, deduped by proposal_id.

    Inbox proposals and applied audit entries share the same ``proposal_id``.
    This function deduplicates them so that:

    * When a ``proposal_id`` appears in a **valid** applied audit, the inbox
      copy is suppressed — the applied audit entry is authoritative.

    * Conflict detection: if the same ``proposal_id`` carries a different
      ``project_id`` or ``changes`` between the inbox proposal and the applied
      audit, an issue is raised (fail closed) and **both** copies are excluded.
      The issue message does not leak which field differs or the actual values.

    * Dedup completes **before** the ``limit`` is applied, so a tight limit
      cannot truncate an applied entry while letting its inbox copy slip through.

    * A bad audit (``applied.jsonl``) still fails closed with existing
      validation logic.
    """
    ensure_safe_root(projects_root)
    scan_symlinks(projects_root, ["inbox", "audit"], issues)

    # Phase 1 — index inbox proposals by proposal_id.
    inbox_by_pid: dict[str, dict[str, Any]] = {}
    inbox_sigs: dict[str, tuple[str, str]] = {}  # (project_id, canonical-changes)
    inbox_dir = projects_root / "inbox"
    for path, row in iter_json(inbox_dir, issues):
        if not path.name.startswith("project_change_"):
            continue
        proposal_id = row.get("proposal_id")
        project_id = row.get("project_id")
        if not isinstance(proposal_id, str) or not isinstance(project_id, str):
            issues.append({"path": str(path), "line": None, "issue": "missing proposal_id or project_id"})
            continue
        for issue in validate_no_secrets({k: row.get(k) for k in ("changes", "evidence")}):
            issues.append({"path": str(path), "line": None, "issue": issue})
            break
        else:
            inbox_by_pid[proposal_id] = {
                "review_item_id": proposal_id,
                "domain": "project",
                "source_id": proposal_id,
                "source_ref": project_id,
                "title": safe_text(f"{project_id}: {sorted((row.get('changes') or {}).keys())}", MAX_LINE_CHARS),
                "summary": safe_text(row.get("changes"), MAX_LINE_CHARS),
                "domain_status": str(row.get("status", "proposed")),
                "latest_review": None,
                "review_required": True,
                "evidence_refs": evidence_refs_from(row.get("evidence")),
                "auto_promote": False,
                "wiki_write": False,
                "cass_write": False,
                "project_apply": False,
                "fact_append": False,
            }
            inbox_sigs[proposal_id] = (project_id, canonical_json(row.get("changes", {})))

    # Phase 2 — process applied audit with conflict detection.
    applied_ids: set[str] = set()
    applied_items: list[dict[str, Any]] = []
    audit_path = projects_root / "audit" / "applied.jsonl"
    if audit_path.exists():
        if audit_path.is_symlink() or not audit_path.is_file():
            issues.append({"path": str(audit_path), "line": None, "issue": "applied audit is not a regular file"})
        else:
            with audit_path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        issues.append({"path": str(audit_path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
                        continue
                    if not isinstance(row, dict):
                        issues.append({"path": str(audit_path), "line": line_number, "issue": "row is not an object"})
                        continue
                    proposal_id = row.get("proposal_id")
                    project_id = row.get("project_id")
                    if not isinstance(proposal_id, str) or not isinstance(project_id, str):
                        issues.append({"path": str(audit_path), "line": line_number, "issue": "missing proposal_id or project_id"})
                        continue

                    # Conflict detection — same proposal_id disagreeing on identity.
                    if proposal_id in inbox_sigs:
                        in_pid, in_changes_json = inbox_sigs[proposal_id]
                        audit_changes_json = canonical_json(row.get("changes", {}))
                        if in_pid != project_id or in_changes_json != audit_changes_json:
                            issues.append({
                                "path": str(audit_path),
                                "line": line_number,
                                "issue": f"conflicting identity for proposal_id {proposal_id} between inbox proposal and applied audit",
                            })
                            applied_ids.add(proposal_id)  # suppress both copies
                            continue

                    applied_items.append({
                        "review_item_id": proposal_id,
                        "domain": "project",
                        "source_id": proposal_id,
                        "source_ref": project_id,
                        "title": safe_text(f"{project_id}: applied", MAX_LINE_CHARS),
                        "summary": safe_text(row.get("changes"), MAX_LINE_CHARS),
                        "domain_status": "applied",
                        "latest_review": None,
                        "review_required": False,
                        "evidence_refs": evidence_refs_from(row.get("evidence")),
                        "auto_promote": False,
                        "wiki_write": False,
                        "cass_write": False,
                        "project_apply": False,
                        "fact_append": False,
                    })
                    applied_ids.add(proposal_id)

    # Phase 3 — dedup (suppress inbox copies whose proposal_id is in applied)
    # then combine and sort.  Dedup before limit prevents a tight limit from
    # truncating the applied entry while its inbox copy survives.
    items = [item for pid, item in inbox_by_pid.items() if pid not in applied_ids]
    items.extend(applied_items)

    items.sort(key=lambda item: (item["domain_status"], item["review_item_id"]))
    return items[:limit]


def fact_items(facts_root: Path, issues: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ensure_safe_root(facts_root)
    scan_symlinks(facts_root, ["inbox", "events"], issues)
    items: list[dict[str, Any]] = []
    inbox_dir = facts_root / "inbox"
    for path, line_number, line in iter_jsonl(inbox_dir):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        proposal_id = row.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            issues.append({"path": str(path), "line": line_number, "issue": "missing proposal_id"})
            continue
        for issue in validate_no_secrets({k: row.get(k) for k in ("summary", "source_ref")}):
            issues.append({"path": str(path), "line": line_number, "issue": issue})
            break
        else:
            items.append(
                {
                    "review_item_id": proposal_id,
                    "domain": "fact",
                    "source_id": proposal_id,
                    "source_ref": row.get("source_ref") if isinstance(row.get("source_ref"), str) else None,
                    "title": safe_text(row.get("summary"), MAX_LINE_CHARS),
                    "summary": safe_text(row.get("summary"), MAX_LINE_CHARS),
                    "domain_status": str(row.get("status", "proposed")),
                    "latest_review": None,
                    "review_required": True,
                    "evidence_refs": evidence_refs_from(row.get("source_ref")),
                    "auto_promote": False,
                    "wiki_write": False,
                    "cass_write": False,
                    "project_apply": False,
                    "fact_append": False,
                }
            )

    events_dir = facts_root / "events"
    for path, line_number, line in iter_jsonl(events_dir):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        if row.get("verification") != "disputed":
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            issues.append({"path": str(path), "line": line_number, "issue": "missing event_id"})
            continue
        for issue in validate_no_secrets({k: row.get(k) for k in ("summary", "source_ref", "refs")}):
            issues.append({"path": str(path), "line": line_number, "issue": issue})
            break
        else:
            items.append(
                {
                    "review_item_id": event_id,
                    "domain": "fact",
                    "source_id": event_id,
                    "source_ref": row.get("source_ref") if isinstance(row.get("source_ref"), str) else None,
                    "title": safe_text(row.get("summary"), MAX_LINE_CHARS),
                    "summary": safe_text(row.get("summary"), MAX_LINE_CHARS),
                    "domain_status": "disputed",
                    "latest_review": None,
                    "review_required": True,
                    "evidence_refs": evidence_refs_from(row.get("refs")),
                    "auto_promote": False,
                    "wiki_write": False,
                    "cass_write": False,
                    "project_apply": False,
                    "fact_append": False,
                }
            )

    items.sort(key=lambda item: (item["domain_status"], item["review_item_id"]))
    return items[:limit]


# ---------------------------------------------------------------------------
# Review log
# ---------------------------------------------------------------------------


def review_time_key(review: dict[str, Any]) -> tuple[int, dt.datetime, str]:
    stamp = review.get("reviewed_at")
    if isinstance(stamp, str):
        try:
            return (1, parse_rfc3339(stamp), str(review.get("review_id", "")))
        except ValueError:
            pass
    return (0, dt.datetime.min.replace(tzinfo=dt.timezone.utc), str(review.get("review_id", "")))


def latest_review(reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not reviews:
        return None
    return max(reviews, key=review_time_key)


def unified_reviews(review_root: Path, issues: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    ensure_safe_root(review_root)
    scan_symlinks(review_root, ["reviews"], issues)
    reviews_dir = review_root / "reviews"
    by_item: dict[str, list[dict[str, Any]]] = {}
    for path, line_number, line in iter_jsonl(reviews_dir):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        item_id = row.get("review_item_id")
        if not isinstance(item_id, str) or not item_id:
            issues.append({"path": str(path), "line": line_number, "issue": "missing review_item_id"})
            continue
        by_item.setdefault(item_id, []).append(row)
    return by_item


def is_closed(item: dict[str, Any]) -> bool:
    if item.get("review_required") is False:
        return True
    if str(item.get("domain_status", "")) in CLOSED_STATUSES:
        return True
    latest = item.get("latest_review")
    if isinstance(latest, dict) and latest.get("decision") in {"accept", "reject"}:
        return True
    return False


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(
    wiki_root: Path,
    experience_root: Path,
    projects_root: Path,
    facts_root: Path,
    review_root: Path,
    *,
    include_closed: bool = False,
    max_items: int = MAX_ITEMS,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    knowledge = knowledge_items(wiki_root, issues, max_items)
    experience, _reviews_by_event = experience_items(experience_root, issues, max_items)
    projects = project_items(projects_root, issues, max_items)
    facts = fact_items(facts_root, issues, max_items)
    unified = unified_reviews(review_root, issues)

    # Attach latest review — take whichever has the later reviewed_at.
    all_items = knowledge + experience + projects + facts
    for item in all_items:
        unified_latest = latest_review(unified.get(item["review_item_id"], []))
        domain_latest = item.get("latest_review")
        if unified_latest is not None and domain_latest is not None:
            if review_time_key(unified_latest) > review_time_key(domain_latest):
                item["latest_review"] = unified_latest
        elif unified_latest is not None:
            item["latest_review"] = unified_latest

    if issues:
        return {
            "schema_version": SCHEMA_VERSION,
            "command": "report",
            "valid": False,
            "item_count": 0,
            "items": [],
            "recommendations": [],
            "issue_count": len(issues),
            "issues": issues[:100],
            "auto_promote": False,
            "wiki_write": False,
            "cass_write": False,
            "project_apply": False,
            "fact_append": False,
        }

    if not include_closed:
        all_items = [item for item in all_items if not is_closed(item)]

    recommendations = [
        {
            "review_item_id": item["review_item_id"],
            "domain": item["domain"],
            "title": item["title"],
            "recommendation": "review",
            "auto_promote": False,
            "wiki_write": False,
            "cass_write": False,
            "project_apply": False,
            "fact_append": False,
        }
        for item in all_items
        if item.get("review_required") and not item.get("latest_review")
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "command": "report",
        "valid": True,
        "item_count": len(all_items),
        "items": all_items,
        "recommendations": recommendations,
        "issue_count": 0,
        "issues": [],
        "auto_promote": False,
        "wiki_write": False,
        "cass_write": False,
        "project_apply": False,
        "fact_append": False,
    }


# ---------------------------------------------------------------------------
# Review recording
# ---------------------------------------------------------------------------


def review_lock(review_root: Path, *, exclusive: bool):
    if review_root.is_symlink():
        raise ReviewStateError("review root must not be a symlink")
    review_root.mkdir(parents=True, exist_ok=True)
    if review_root.is_symlink():
        raise ReviewStateError("review root must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(review_root / ".review-state.lock", flags, 0o600)
    except OSError as exc:
        raise ReviewStateError(f"cannot open review lock safely: {exc}") from exc
    handle = os.fdopen(fd, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return handle


def deterministic_review_id(review: dict[str, Any]) -> str:
    identity = {
        "review_item_id": review["review_item_id"],
        "domain": review["domain"],
        "decision": review["decision"],
        "reviewer": review["reviewer"],
        "rationale": review["rationale"],
    }
    return stable_id("review_", identity)


def build_review_record(
    review_item_id: str,
    domain: str,
    decision: str,
    reviewer: str,
    rationale: str,
    reviewed_at: str | None,
) -> dict[str, Any]:
    if domain not in DOMAINS:
        raise ReviewStateError(f"domain must be one of {DOMAINS}")
    if decision not in REVIEW_DECISIONS:
        raise ReviewStateError(f"decision must be one of {REVIEW_DECISIONS}")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ReviewStateError("reviewer must be non-empty")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ReviewStateError("rationale must be non-empty")
    stamp = reviewed_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        parse_rfc3339(stamp)
    except ValueError as exc:
        raise ReviewStateError(f"reviewed_at must be timezone-aware RFC3339: {exc}") from exc
    record = {
        "schema_version": SCHEMA_VERSION,
        "review_item_id": review_item_id,
        "domain": domain,
        "decision": decision,
        "reviewer": reviewer.strip(),
        "rationale": rationale.strip(),
        "reviewed_at": stamp,
        "auto_promote": False,
        "wiki_write": False,
        "cass_write": False,
        "project_apply": False,
        "fact_append": False,
    }
    record["review_id"] = deterministic_review_id(record)
    return record


def append_review_record(review_root: Path, record: dict[str, Any]) -> bool:
    reviews_dir = review_root / "reviews"
    if reviews_dir.exists() and reviews_dir.is_symlink():
        raise ReviewStateError("reviews directory must not be a symlink")
    reviews_dir.mkdir(parents=True, exist_ok=True)
    if reviews_dir.is_symlink():
        raise ReviewStateError("reviews directory must not be a symlink")
    month = parse_rfc3339(record["reviewed_at"]).strftime("%Y-%m")
    path = reviews_dir / f"{month}.jsonl"
    if path.is_symlink():
        raise ReviewStateError(f"review month file must not be a symlink: {path}")

    existing: set[str] = set()
    for p in sorted(reviews_dir.glob("*.jsonl")):
        if p.is_symlink() or not p.is_file():
            continue
        with p.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("review_id"), str):
                    existing.add(row["review_id"])

    if record["review_id"] in existing:
        return False

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReviewStateError(f"cannot open review month file safely: {exc}") from exc
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def record_review(
    wiki_root: Path,
    experience_root: Path,
    projects_root: Path,
    facts_root: Path,
    review_root: Path,
    review_item_id: str,
    domain: str,
    decision: str,
    reviewer: str,
    rationale: str,
    reviewed_at: str | None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not isinstance(review_item_id, str) or not review_item_id:
        raise ReviewStateError("review_item_id must be non-empty")
    # Check reviewer/rationale for secrets before any side effects.
    for field, val in (("reviewer", reviewer), ("rationale", rationale)):
        if SECRET_RE.search(str(val)):
            raise ReviewStateError("review input contains sensitive content")
    record = build_review_record(review_item_id, domain, decision, reviewer, rationale, reviewed_at)
    if dry_run:
        return {"status": "dry_run", "written": False, "review": record}

    # Verify the item exists in the current report before recording a decision.
    report = build_report(
        wiki_root, experience_root, projects_root, facts_root, review_root, include_closed=True, max_items=1_000_000
    )
    if not report["valid"]:
        raise ReviewStateError("cannot record review while domain inputs are invalid")
    matching = [item for item in report["items"] if item["review_item_id"] == review_item_id]
    if not matching:
        raise ReviewStateError(f"unknown review_item_id: {review_item_id}")
    if matching[0]["domain"] != domain:
        raise ReviewStateError(
            f"domain mismatch for {review_item_id}: expected {matching[0]['domain']}, got {domain}"
        )

    lock = review_lock(review_root, exclusive=True)
    try:
        written = append_review_record(review_root, record)
    finally:
        lock.close()
    return {"status": "recorded" if written else "exists", "written": written, "review": record}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_domain_file(
    path: Path,
    line_number: int | None,
    row: Any,
    expected_fields: set[str],
    item_id_field: str,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(row, dict):
        return ["row is not an object"]
    missing = sorted(expected_fields - set(row))
    if missing:
        issues.append("missing fields: " + ", ".join(missing))
    item_id = row.get(item_id_field)
    if not isinstance(item_id, str) or not item_id:
        issues.append(f"{item_id_field} must be a non-empty string")
    issues.extend(validate_no_secrets(row))
    return issues


def validate_inputs(
    wiki_root: Path,
    experience_root: Path,
    projects_root: Path,
    facts_root: Path,
    review_root: Path,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    scan_symlinks(wiki_root, ["_meta/knowledge-learning", "_meta/writeback-inbox"], issues)
    scan_symlinks(experience_root, ["inbox", "reviews"], issues)
    scan_symlinks(projects_root, ["inbox", "audit"], issues)
    scan_symlinks(facts_root, ["inbox", "events"], issues)
    scan_symlinks(review_root, ["reviews"], issues)

    # Knowledge: learning ledger.
    learning_fields = {"schema_version", "learning_id", "title", "summary", "status", "path", "review_required"}
    for path, line_number, line in iter_jsonl(wiki_root / "_meta" / "knowledge-learning"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        for issue in validate_domain_file(path, line_number, row, learning_fields, "learning_id"):
            issues.append({"path": str(path), "line": line_number, "issue": issue})

    # Knowledge: writeback inbox frontmatter (must have title + status).
    for path, frontmatter in iter_markdown(wiki_root / "_meta" / "writeback-inbox"):
        meta = parse_frontmatter(frontmatter)
        if not meta.get("title"):
            issues.append({"path": str(path), "line": None, "issue": "missing title frontmatter"})
        if not meta.get("status"):
            issues.append({"path": str(path), "line": None, "issue": "missing status frontmatter"})
        issues.extend(validate_no_secrets(meta))

    # Experience: inbox events.
    event_fields = {"schema_version", "event_id", "source", "candidate", "status", "auto_promote", "cass_write"}
    for path, line_number, line in iter_jsonl(experience_root / "inbox"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        for issue in validate_domain_file(path, line_number, row, event_fields, "event_id"):
            issues.append({"path": str(path), "line": line_number, "issue": issue})

    # Experience: reviews.
    review_fields = {
        "schema_version",
        "review_id",
        "event_id",
        "decision",
        "reviewer",
        "rationale",
        "reusable",
        "reviewed_at",
        "auto_promote",
        "cass_write",
    }
    for path, line_number, line in iter_jsonl(experience_root / "reviews"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        for issue in validate_domain_file(path, line_number, row, review_fields, "review_id"):
            issues.append({"path": str(path), "line": line_number, "issue": issue})

    # Project: inbox proposals.
    project_proposal_fields = {
        "schema_version",
        "proposal_id",
        "project_id",
        "changes",
        "evidence",
        "base_hash",
        "status",
        "high_impact",
    }
    for path, row in iter_json(projects_root / "inbox", issues):
        for issue in validate_domain_file(path, None, row, project_proposal_fields, "proposal_id"):
            issues.append({"path": str(path), "line": None, "issue": issue})

    # Project: applied audit.
    for path, line_number, line in iter_jsonl(projects_root / "audit"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        for issue in validate_domain_file(path, line_number, row, {"proposal_id", "project_id", "evidence", "changes"}, "proposal_id"):
            issues.append({"path": str(path), "line": line_number, "issue": issue})

    # Fact: inbox proposals.
    fact_proposal_fields = {"schema_version", "proposal_id", "summary", "source_ref", "status"}
    for path, line_number, line in iter_jsonl(facts_root / "inbox"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        for issue in validate_domain_file(path, line_number, row, fact_proposal_fields, "proposal_id"):
            issues.append({"path": str(path), "line": line_number, "issue": issue})

    # Fact: events.
    fact_event_fields = {
        "schema_version",
        "event_id",
        "occurred_at",
        "recorded_at",
        "subject",
        "event_type",
        "summary",
        "source_ref",
        "actor",
        "confidence",
        "verification",
        "privacy",
        "project_id",
        "supersedes",
        "refs",
    }
    for path, line_number, line in iter_jsonl(facts_root / "events"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        for issue in validate_domain_file(path, line_number, row, fact_event_fields, "event_id"):
            issues.append({"path": str(path), "line": line_number, "issue": issue})

    # Unified review log.
    unified_review_fields = {
        "schema_version",
        "review_id",
        "review_item_id",
        "domain",
        "decision",
        "reviewer",
        "rationale",
        "reviewed_at",
    }
    for path, line_number, line in iter_jsonl(review_root / "reviews"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if not isinstance(row, dict):
            issues.append({"path": str(path), "line": line_number, "issue": "row is not an object"})
            continue
        missing = sorted(unified_review_fields - set(row))
        if missing:
            issues.append({"path": str(path), "line": line_number, "issue": "missing fields: " + ", ".join(missing)})
        if row.get("domain") not in DOMAINS:
            issues.append({"path": str(path), "line": line_number, "issue": f"domain must be one of {DOMAINS}"})
        if row.get("decision") not in REVIEW_DECISIONS:
            issues.append({"path": str(path), "line": line_number, "issue": f"decision must be one of {REVIEW_DECISIONS}"})
        issues.extend({"path": str(path), "line": line_number, "issue": issue} for issue in validate_no_secrets(row))

    return {
        "schema_version": SCHEMA_VERSION,
        "command": "validate",
        "valid": not issues,
        "issue_count": len(issues),
        "issues": issues[:100],
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def atomic_write_report(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ReviewStateError(f"output path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ReviewStateError(f"output path must not be a symlink: {path}")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage-5 unified review state")
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--experience-root", type=Path, default=DEFAULT_EXPERIENCE_ROOT)
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--facts-root", type=Path, default=DEFAULT_FACTS_ROOT)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS)
    commands = parser.add_subparsers(dest="command", required=True)

    report_cmd = commands.add_parser("report", help="derive normalized review-state report")
    report_cmd.add_argument("--output", type=Path, help="report output path")
    report_cmd.add_argument("--no-write", action="store_true", help="print JSON only; do not write report")
    report_cmd.add_argument("--include-closed", action="store_true", help="include terminal/closed items")
    report_cmd.add_argument("--json", action="store_true", help="print full JSON report")

    review_cmd = commands.add_parser("review", help="append an accept/reject/defer decision")
    review_cmd.add_argument("--review-item-id", required=True)
    review_cmd.add_argument("--domain", choices=DOMAINS, required=True)
    review_cmd.add_argument("--decision", choices=REVIEW_DECISIONS, required=True)
    review_cmd.add_argument("--reviewer", required=True)
    review_cmd.add_argument("--rationale", required=True)
    review_cmd.add_argument("--reviewed-at", help="RFC3339 timestamp; defaults to current UTC")
    review_cmd.add_argument("--dry-run", action="store_true")
    review_cmd.add_argument("--json", action="store_true")

    commands.add_parser("validate", help="strictly validate all domain inputs and review log")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = {
        "wiki_root": args.wiki_root.expanduser().absolute(),
        "experience_root": args.experience_root.expanduser().absolute(),
        "projects_root": args.projects_root.expanduser().absolute(),
        "facts_root": args.facts_root.expanduser().absolute(),
        "review_root": args.review_root.expanduser().absolute(),
    }
    for label, root in roots.items():
        if root.is_symlink():
            print(json.dumps({"status": "error", "error": f"{label} must not be a symlink: {root}"}, ensure_ascii=False), file=sys.stderr)
            return 2

    try:
        if args.command == "report":
            result = build_report(
                roots["wiki_root"],
                roots["experience_root"],
                roots["projects_root"],
                roots["facts_root"],
                roots["review_root"],
                include_closed=args.include_closed,
                max_items=args.max_items,
            )
            output = args.output
            written = False
            if output and not args.no_write:
                atomic_write_report(output.expanduser().absolute(), pretty_json(result))
                written = True
            result["report_path"] = str(output.expanduser().absolute()) if output else None
            result["written"] = written
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                status = "有效" if result["valid"] else "无效"
                print(f"复核状态报告｜{status}｜条目 {result['item_count']}｜建议 {len(result['recommendations'])}")
                if result["issue_count"]:
                    print(f"问题：{result['issue_count']} 条")
                if output:
                    print(f"报告：{output}{'（未写入）' if args.no_write else ''}")
            return 0 if result["valid"] else 1

        if args.command == "review":
            result = record_review(
                roots["wiki_root"],
                roots["experience_root"],
                roots["projects_root"],
                roots["facts_root"],
                roots["review_root"],
                args.review_item_id,
                args.domain,
                args.decision,
                args.reviewer,
                args.rationale,
                args.reviewed_at,
                dry_run=args.dry_run,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"复核记录：{result['status']}｜{result['review']['review_id']}")
            return 0

        # validate
        result = validate_inputs(
            roots["wiki_root"],
            roots["experience_root"],
            roots["projects_root"],
            roots["facts_root"],
            roots["review_root"],
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["valid"] else 1

    except ReviewStateError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
