#!/usr/bin/env python3
"""Experience-candidate review queue compiler (five-domain stage 4, slice 2).

Compiles ``experience_candidate`` entries from completion receipts into an
independent, append-only review inbox. The compiler is deliberately one-way:

  * It reads ONLY the receipt month file for the requested day
    (``<receipts-root>/inbox/YYYY-MM.jsonl``) and selects receipts whose
    ``completed_at`` falls on that exact local calendar day.
  * Each candidate becomes one deterministic review event carrying
    ``status=inbox``, ``auto_promote=false`` and ``cass_write=false``.
    There is NO review/apply/promotion command here and this tool never
    writes to CASS, the Fact Ledger, the Project Registry, or wiki pages.
  * Receipt ``action`` / ``result`` / ``query`` fields are never copied;
    only the candidate payload plus minimal source attribution survives.
  * Human review decisions are separate append-only records under
    ``<inbox-root>/reviews/YYYY-MM.jsonl``. They never mutate candidates or
    write to CASS.

Failure policy (fail-closed):
  * Unparseable receipt JSON lines, malformed receipt source fields, and
    secret-like candidate content are all counted as ``issues``.
  * If ANY issue exists, compile writes nothing and exits nonzero — a bad
    receipt must never let a sibling candidate slip through silently.
  * Secret-like content is REJECTED (not redacted): an experience destined
    for a review queue must arrive intact or not at all; silently mutating
    it would create a candidate nobody ever reviewed.

Safety: a global flock serializes compiles; event_ids are deduplicated
within each compile batch and across ALL month files, so concurrent or
repeated runs are idempotent.
Symlink inbox roots, inbox directories, and target month leaves are
refused; writes use O_NOFOLLOW + O_APPEND + fsync. ``--dry-run`` writes
nothing and creates no lock file.

Standard library only. Exit codes: 0 ok, 1 issues found (nothing written),
2 usage/safety error.
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
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RECEIPTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/receipts")
DEFAULT_INBOX_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/experience-review")

SCHEMA_VERSION = 1
PRIVACY_LEVELS = ("private", "sensitive", "shareable")
MAX_ISSUES_REPORTED = 100
# Same pattern family as five_domain_runtime.py / five_domain_daily.py.
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"\s*(?:::|=|:)\s*\S+|\bBearer\s+[A-Za-z0-9._~+/=-]+"
)
# Keys that would imply a promotion happened or is requested; review events
# must never carry them (checked by validate).
ILLEGAL_PROMOTION_KEYS = ("promoted", "promoted_at", "promotion", "applied", "cass_id")
REVIEW_DECISIONS = ("accept", "reject", "defer")


class ReviewError(Exception):
    """Usage/safety error -> exit 2."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def day_of(value: Any) -> str | None:
    """Tolerant RFC3339 -> YYYY-MM-DD; None for unparseable values."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.date().isoformat()


def parse_for_date(value: str | None) -> dt.date:
    if value:
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ReviewError(f"--for-date must be YYYY-MM-DD: {exc}") from exc
    return dt.date.today() - dt.timedelta(days=1)


def build_event(receipt: dict[str, Any], candidate: dict[str, Any], for_date: dt.date) -> dict[str, Any]:
    """One deterministic review event. Only candidate + minimal source
    attribution; never action/result/query."""
    source = {
        "receipt_id": receipt["receipt_id"],
        "agent": receipt["agent"],
        "project_id": receipt["project_id"],
        "completed_at": receipt["completed_at"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": stable_id("xreview_", {"receipt_id": source["receipt_id"], "candidate": candidate}),
        "candidate_key": stable_id("xcand_", {"project_id": source["project_id"], "candidate": candidate}),
        "source": source,
        "candidate": candidate,
        "status": "inbox",
        "auto_promote": False,
        "cass_write": False,
        "privacy": receipt["privacy"],
        "compiled_for": for_date.isoformat(),
    }


def collect_candidates(
    receipts_root: Path, for_date: dt.date, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Read ONLY the for-date month file; return events for same-day receipts.

    Every data problem is appended to ``issues`` (fail-closed: the caller
    writes nothing when issues is non-empty)."""
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    month_path = receipts_root / "inbox" / f"{for_date:%Y-%m}.jsonl"
    if month_path.is_symlink():
        raise ReviewError(f"receipt month file must not be a symlink: {month_path}")
    if not month_path.is_file():
        return events  # a day with no receipts is not an error
    try:
        handle = month_path.open(encoding="utf-8")
    except OSError as exc:
        issues.append({"path": str(month_path), "line": None, "issue": f"unreadable: {exc}"})
        return events
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            where = {"path": str(month_path), "line": line_number}
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append({**where, "issue": f"invalid_json: {exc.msg}"})
                continue
            if not isinstance(receipt, dict):
                issues.append({**where, "issue": "receipt is not an object"})
                continue
            if day_of(receipt.get("completed_at")) != for_date.isoformat():
                continue  # only completed_at on the requested day
            bad = False
            for field in ("receipt_id", "agent", "project_id", "completed_at"):
                if not isinstance(receipt.get(field), str) or not receipt[field].strip():
                    issues.append({**where, "issue": f"missing/invalid source field: {field}"})
                    bad = True
            if receipt.get("privacy") not in PRIVACY_LEVELS:
                issues.append({**where, "issue": f"privacy must be one of {PRIVACY_LEVELS}"})
                bad = True
            candidates = receipt.get("experience_candidate")
            if candidates is None:
                continue  # no candidates on this receipt: nothing to do
            if not isinstance(candidates, list):
                issues.append({**where, "issue": "experience_candidate must be a list"})
                continue
            if bad:
                continue
            for index, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    issues.append({**where, "issue": f"experience_candidate[{index}] must be an object"})
                    continue
                if SECRET_RE.search(canonical_json(candidate)):
                    # Reject, never redact: review-queue candidates arrive
                    # intact or not at all.
                    issues.append({**where, "issue": f"experience_candidate[{index}] contains secret-like content; rejected"})
                    continue
                event = build_event(receipt, candidate, for_date)
                # An identical candidate repeated within the same receipt (or
                # across receipts in this batch) compiles to ONE review event:
                # a single compile must never append a duplicate event_id.
                if event["event_id"] in seen_ids:
                    continue
                seen_ids.add(event["event_id"])
                events.append(event)
    events.sort(key=lambda e: (e["source"]["completed_at"], e["event_id"]))
    return events


def iter_event_files(inbox_root: Path) -> Iterable[Path]:
    inbox = inbox_root / "inbox"
    if not inbox.exists():
        return []
    if inbox.is_symlink():
        raise ReviewError("experience-review inbox must not be a symlink")
    return sorted(p for p in inbox.glob("*.jsonl") if p.is_file() and not p.is_symlink())


def existing_event_ids(inbox_root: Path, issues: list[dict[str, Any]] | None = None) -> set[str]:
    """Event ids across ALL month files (cross-month dedup). Tolerates bad
    lines here; ``validate`` is the strict checker."""
    ids: set[str] = set()
    for path in iter_event_files(inbox_root):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("event_id"), str):
                    ids.add(row["event_id"])
    return ids


def review_lock(inbox_root: Path, *, exclusive: bool):
    """Global flock on <inbox-root>/.experience-review.lock (symlink-safe)."""
    # is_symlink() (lstat) catches broken links too; exists() would miss them
    # and mkdir(exist_ok=True) would then raise FileExistsError instead of
    # the intended refusal.
    if inbox_root.is_symlink():
        raise ReviewError("experience-review inbox root must not be a symlink")
    inbox_root.mkdir(parents=True, exist_ok=True)
    if inbox_root.is_symlink():
        raise ReviewError("experience-review inbox root must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(inbox_root / ".experience-review.lock", flags, 0o600)
    except OSError as exc:
        raise ReviewError(f"cannot open review lock safely: {exc}") from exc
    handle = os.fdopen(fd, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return handle


def append_events(inbox_root: Path, for_date: dt.date, events: list[dict[str, Any]]) -> int:
    """Append new events under the target month leaf (O_NOFOLLOW, fsync)."""
    inbox = inbox_root / "inbox"
    if inbox.exists() and inbox.is_symlink():
        raise ReviewError("experience-review inbox must not be a symlink")
    inbox.mkdir(parents=True, exist_ok=True)
    if inbox.is_symlink():
        raise ReviewError("experience-review inbox must not be a symlink")
    path = inbox / f"{for_date:%Y-%m}.jsonl"
    if path.is_symlink():
        raise ReviewError(f"review month file must not be a symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReviewError(f"cannot open review month file safely: {exc}") from exc
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(events)


def compile_day(
    receipts_root: Path, inbox_root: Path, for_date: dt.date, *, dry_run: bool = False
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    events = collect_candidates(receipts_root, for_date, issues)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": "compile",
        "for_date": for_date.isoformat(),
        "dry_run": dry_run,
        "candidates_found": len(events),
        "already_present": 0,
        "would_append": 0,
        "appended": 0,
        "issue_count": len(issues),
        "issues": issues[:MAX_ISSUES_REPORTED],
        "cass_write": False,
        "auto_promote": False,
    }
    if issues:
        # Fail closed: a bad receipt line or secret-like candidate blocks the
        # whole write so nothing is silently promoted.
        result["status"] = "issues"
        return result
    if dry_run:
        # No lock file, no directories, no writes — pure read.
        present = existing_event_ids(inbox_root)
        fresh = [e for e in events if e["event_id"] not in present]
        result.update(
            status="dry_run",
            already_present=len(events) - len(fresh),
            would_append=len(fresh),
        )
        return result
    lock = review_lock(inbox_root, exclusive=True)
    try:
        # Re-check dedup under the exclusive lock: concurrent compiles are
        # idempotent because the loser re-reads after the winner appended.
        present = existing_event_ids(inbox_root)
        fresh = [e for e in events if e["event_id"] not in present]
        result["already_present"] = len(events) - len(fresh)
        if fresh:
            result["appended"] = append_events(inbox_root, for_date, fresh)
    finally:
        lock.close()
    result["status"] = "ok"
    return result


def validate_event(event: Any) -> list[str]:
    if not isinstance(event, dict):
        return ["review event is not an object"]
    errors: list[str] = []
    required = {
        "schema_version", "event_id", "candidate_key", "source", "candidate",
        "status", "auto_promote", "cass_write", "privacy", "compiled_for",
    }
    missing = sorted(required - set(event))
    if missing:
        return ["missing fields: " + ", ".join(missing)]
    if event["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("event_id", "candidate_key"):
        if not isinstance(event[field], str) or not event[field].strip():
            errors.append(f"{field} must be a non-empty string")
    source = event["source"]
    if not isinstance(source, dict):
        errors.append("source must be an object")
        source = {}
    else:
        for field in ("receipt_id", "agent", "project_id", "completed_at"):
            if not isinstance(source.get(field), str) or not source[field].strip():
                errors.append(f"source.{field} must be a non-empty string")
        extra = sorted(set(source) - {"receipt_id", "agent", "project_id", "completed_at"})
        if extra:
            errors.append("source carries unexpected fields: " + ", ".join(extra))
    if not isinstance(event["candidate"], dict):
        errors.append("candidate must be an object")
    if event["status"] != "inbox":
        errors.append("status must remain 'inbox'")
    if event["auto_promote"] is not False:
        errors.append("auto_promote must be false")
    if event["cass_write"] is not False:
        errors.append("cass_write must be false")
    illegal = sorted(set(event) & set(ILLEGAL_PROMOTION_KEYS))
    if illegal:
        errors.append("illegal promotion fields present: " + ", ".join(illegal))
    if event["privacy"] not in PRIVACY_LEVELS:
        errors.append(f"privacy must be one of {PRIVACY_LEVELS}")
    if SECRET_RE.search(canonical_json(event.get("candidate"))):
        errors.append("candidate contains secret-like content")
    if not errors and isinstance(event["candidate"], dict):
        if event["event_id"] != stable_id(
            "xreview_", {"receipt_id": source.get("receipt_id"), "candidate": event["candidate"]}
        ):
            errors.append("event_id does not match deterministic identity")
        if event["candidate_key"] != stable_id(
            "xcand_", {"project_id": source.get("project_id"), "candidate": event["candidate"]}
        ):
            errors.append("candidate_key does not match deterministic identity")
    return errors


def validate_inbox(inbox_root: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    count = 0
    seen: set[str] = set()
    lock = review_lock(inbox_root, exclusive=False)
    try:
        for path in iter_event_files(inbox_root):
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    count += 1
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
                        continue
                    for error in validate_event(event):
                        issues.append({"path": str(path), "line": line_number, "issue": error})
                    event_id = event.get("event_id") if isinstance(event, dict) else None
                    if event_id in seen:
                        issues.append({"path": str(path), "line": line_number, "issue": f"duplicate event_id: {event_id}"})
                    elif isinstance(event_id, str):
                        seen.add(event_id)
    finally:
        lock.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "validate",
        "valid": not issues,
        "event_count": count,
        "issue_count": len(issues),
        "issues": issues[:MAX_ISSUES_REPORTED],
    }


def event_ids(inbox_root: Path) -> set[str]:
    """Return candidate ids without treating review records as candidates."""
    ids: set[str] = set()
    for path in iter_event_files(inbox_root):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("event_id"), str):
                ids.add(row["event_id"])
    return ids


def review_files(inbox_root: Path) -> Iterable[Path]:
    reviews = inbox_root / "reviews"
    if not reviews.exists():
        return []
    if reviews.is_symlink():
        raise ReviewError("experience-review reviews directory must not be a symlink")
    return sorted(p for p in reviews.glob("*.jsonl") if p.is_file() and not p.is_symlink())


def review_id(value: dict[str, Any]) -> str:
    identity = {
        key: value[key]
        for key in ("event_id", "decision", "reviewer", "rationale", "reusable")
    }
    return stable_id("xreview_decision_", identity)


def build_review(
    event_id: str,
    decision: str,
    reviewer: str,
    rationale: str,
    reusable: bool,
    reviewed_at: str,
) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "decision": decision,
        "reviewer": reviewer,
        "rationale": rationale,
        "reusable": reusable,
        "reviewed_at": reviewed_at,
        "auto_promote": False,
        "cass_write": False,
        "cass_recommendation": bool(decision == "accept" and reusable),
    }
    value["review_id"] = review_id(value)
    return value


def append_review(inbox_root: Path, review: dict[str, Any]) -> bool:
    reviews = inbox_root / "reviews"
    if reviews.exists() and reviews.is_symlink():
        raise ReviewError("experience-review reviews directory must not be a symlink")
    reviews.mkdir(parents=True, exist_ok=True)
    if reviews.is_symlink():
        raise ReviewError("experience-review reviews directory must not be a symlink")
    reviewed_at = review["reviewed_at"]
    try:
        month = dt.datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")).strftime("%Y-%m")
    except ValueError as exc:
        raise ReviewError("reviewed_at must be RFC3339") from exc
    path = reviews / f"{month}.jsonl"
    if path.is_symlink():
        raise ReviewError(f"review month file must not be a symlink: {path}")
    existing: set[str] = set()
    for review_path in review_files(inbox_root):
        for line in review_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("review_id"), str):
                existing.add(row["review_id"])
    if review["review_id"] in existing:
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def record_review(
    inbox_root: Path,
    event_id: str,
    decision: str,
    reviewer: str,
    rationale: str,
    reusable: bool,
    *,
    reviewed_at: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not isinstance(event_id, str) or not event_id.startswith("xreview_"):
        raise ReviewError("event_id must start with xreview_")
    if decision not in REVIEW_DECISIONS:
        raise ReviewError(f"decision must be one of {REVIEW_DECISIONS}")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ReviewError("reviewer must be non-empty")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ReviewError("rationale must be non-empty")
    if not isinstance(reusable, bool):
        raise ReviewError("reusable must be boolean")
    if event_id not in event_ids(inbox_root):
        raise ReviewError(f"unknown candidate event: {event_id}")
    stamp = reviewed_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    review = build_review(event_id, decision, reviewer.strip(), rationale.strip(), reusable, stamp)
    if dry_run:
        return {"status": "dry_run", "written": False, "review": review}
    lock = review_lock(inbox_root, exclusive=True)
    try:
        written = append_review(inbox_root, review)
    finally:
        lock.close()
    return {"status": "recorded" if written else "exists", "written": written, "review": review}


def validate_review(review: Any, candidates: set[str]) -> list[str]:
    if not isinstance(review, dict):
        return ["review is not an object"]
    required = {"schema_version", "review_id", "event_id", "decision", "reviewer", "rationale", "reusable", "reviewed_at", "auto_promote", "cass_write", "cass_recommendation"}
    errors: list[str] = []
    missing = sorted(required - set(review))
    if missing:
        return ["missing fields: " + ", ".join(missing)]
    if review["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(review["event_id"], str) or review["event_id"] not in candidates:
        errors.append("event_id must reference an inbox candidate")
    if review["decision"] not in REVIEW_DECISIONS:
        errors.append(f"decision must be one of {REVIEW_DECISIONS}")
    for field in ("reviewer", "rationale", "reviewed_at"):
        if not isinstance(review[field], str) or not review[field].strip():
            errors.append(f"{field} must be non-empty")
    if not isinstance(review["reusable"], bool):
        errors.append("reusable must be boolean")
    if review["auto_promote"] is not False:
        errors.append("auto_promote must be false")
    if review["cass_write"] is not False:
        errors.append("cass_write must be false")
    if review["cass_recommendation"] is not (review["decision"] == "accept" and review["reusable"]):
        errors.append("cass_recommendation does not match decision/reusable")
    try:
        dt.datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        errors.append("reviewed_at must be RFC3339")
    if not errors and review["review_id"] != review_id(review):
        errors.append("review_id does not match deterministic identity")
    return errors


def validate_reviews(inbox_root: Path) -> dict[str, Any]:
    candidates = event_ids(inbox_root)
    issues: list[dict[str, Any]] = []
    count = 0
    seen: set[str] = set()
    for path in review_files(inbox_root):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
                continue
            for error in validate_review(row, candidates):
                issues.append({"path": str(path), "line": line_number, "issue": error})
            rid = row.get("review_id") if isinstance(row, dict) else None
            if isinstance(rid, str) and rid in seen:
                issues.append({"path": str(path), "line": line_number, "issue": f"duplicate review_id: {rid}"})
            elif isinstance(rid, str):
                seen.add(rid)
    return {"schema_version": SCHEMA_VERSION, "command": "review-validate", "valid": not issues, "review_count": count, "issue_count": len(issues), "issues": issues[:MAX_ISSUES_REPORTED]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipts-root", type=Path, default=DEFAULT_RECEIPTS_ROOT,
                        help="completion receipts root (contains inbox/YYYY-MM.jsonl)")
    parser.add_argument("--inbox-root", type=Path, default=DEFAULT_INBOX_ROOT,
                        help="experience review queue root (contains inbox/YYYY-MM.jsonl)")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser("compile", help="compile one day's receipt candidates into the review inbox")
    compile_cmd.add_argument("--for-date", help="receipt completed_at day (default: previous local calendar day)")
    compile_cmd.add_argument("--dry-run", action="store_true",
                             help="report what would be appended; writes nothing and creates no lock file")
    compile_cmd.add_argument("--json", action="store_true", help="print the full JSON summary")
    review_cmd = commands.add_parser("review", help="append one human review decision; never changes the candidate")
    review_cmd.add_argument("--event-id", required=True)
    review_cmd.add_argument("--decision", choices=REVIEW_DECISIONS, required=True)
    review_cmd.add_argument("--reviewer", required=True)
    review_cmd.add_argument("--rationale", required=True)
    review_cmd.add_argument("--reusable", action="store_true")
    review_cmd.add_argument("--reviewed-at", help="RFC3339 timestamp; defaults to current UTC")
    review_cmd.add_argument("--dry-run", action="store_true")
    review_cmd.add_argument("--json", action="store_true")
    commands.add_parser("validate", help="strictly validate every review inbox JSONL")
    commands.add_parser("review-status", help="validate the append-only human review records")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # expanduser only: resolving would dereference a symlinked root and
        # defeat the symlink refusals below.
        receipts_root = args.receipts_root.expanduser()
        inbox_root = args.inbox_root.expanduser()
        if args.command == "compile":
            for_date = parse_for_date(args.for_date)
            result = compile_day(receipts_root, inbox_root, for_date, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    f"经验候选编译｜{result['for_date']}：候选 {result['candidates_found']}，"
                    f"新增 {result['appended'] or result['would_append']}，"
                    f"已存在 {result['already_present']}，问题 {result['issue_count']}"
                    f"（{'dry-run，未写入' if result['dry_run'] else result['status']}）"
                )
            return 0 if result["issue_count"] == 0 else 1
        if args.command == "review":
            result = record_review(
                inbox_root,
                args.event_id,
                args.decision,
                args.reviewer,
                args.rationale,
                args.reusable,
                reviewed_at=args.reviewed_at,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) if args.json else f"人工复核：{result['status']}｜{args.event_id}")
            return 0
        if args.command == "review-status":
            result = validate_reviews(inbox_root)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["valid"] else 1
        result = validate_inbox(inbox_root)
        review_result = validate_reviews(inbox_root)
        result.update({"review_count": review_result["review_count"], "review_issue_count": review_result["issue_count"], "review_issues": review_result["issues"]})
        if review_result["issues"]:
            result["valid"] = False
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["valid"] else 1
    except (ReviewError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
