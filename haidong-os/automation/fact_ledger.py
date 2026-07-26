#!/usr/bin/env python3
"""Deterministic, append-only Fact Ledger for the Haidong cognitive system."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


DEFAULT_FACTS_ROOT = Path("/Users/jianghaidong/hbrain/facts")
SCHEMA_VERSION = 1
VERIFICATIONS = {"observed", "asserted", "verified", "disputed", "superseded"}
PRIVACY_LEVELS = {"private", "sensitive", "shareable"}
REF_KEYS = ("knowledge", "cass", "artifacts")
REQUIRED_FIELDS = (
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
)


class LedgerError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise LedgerError("timestamp must include a timezone")
    return value.isoformat(timespec="seconds")


def parse_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{field} must be a non-empty RFC3339 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerError(f"{field} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"{field} must include a timezone")
    return parsed


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_identity(event: dict[str, Any], prefix: str = "fact") -> str:
    payload = {key: value for key, value in event.items() if key not in {"event_id", "recorded_at", "status"}}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def empty_refs() -> dict[str, list[str]]:
    return {key: [] for key in REF_KEYS}


def normalize_refs(value: Any) -> dict[str, list[str]]:
    if value is None:
        return empty_refs()
    if not isinstance(value, dict):
        raise LedgerError("refs must be an object")
    result: dict[str, list[str]] = {}
    for key in REF_KEYS:
        items = value.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise LedgerError(f"refs.{key} must be a list of non-empty strings")
        result[key] = sorted(set(item.strip() for item in items))
    extra = sorted(set(value) - set(REF_KEYS))
    if extra:
        raise LedgerError(f"refs contains unsupported keys: {', '.join(extra)}")
    return result


def normalize_formal_event(payload: dict[str, Any], recorded_at: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LedgerError("event must be a JSON object")
    event = dict(payload)
    event.setdefault("schema_version", SCHEMA_VERSION)
    event.setdefault("recorded_at", recorded_at or format_timestamp(utc_now()))
    event.setdefault("project_id", None)
    event.setdefault("supersedes", None)
    event["refs"] = normalize_refs(event.get("refs"))
    if not event.get("event_id"):
        event["event_id"] = normalized_identity(event)
    errors = validate_formal_event(event)
    if errors:
        raise LedgerError("; ".join(errors[:8]))
    return event


def validate_formal_event(event: Any) -> list[str]:
    if not isinstance(event, dict):
        return ["event is not a JSON object"]
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in event:
            errors.append(f"missing required field: {field}")
    if errors:
        return errors
    if event["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(event["event_id"], str) or not event["event_id"].startswith("fact_"):
        errors.append("event_id must start with fact_")
    for field in ("occurred_at", "recorded_at"):
        try:
            parse_timestamp(event[field], field)
        except LedgerError as exc:
            errors.append(str(exc))
    subject = event["subject"]
    if not isinstance(subject, dict) or set(subject) != {"type", "id"}:
        errors.append("subject must contain exactly type and id")
    elif not all(isinstance(subject[key], str) and subject[key].strip() for key in ("type", "id")):
        errors.append("subject.type and subject.id must be non-empty strings")
    for field in ("event_type", "summary", "source_ref", "actor"):
        if not isinstance(event[field], str) or not event[field].strip():
            errors.append(f"{field} must be a non-empty string")
    confidence = event["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append("confidence must be a number from 0 to 1")
    if event["verification"] not in VERIFICATIONS:
        errors.append(f"verification must be one of {sorted(VERIFICATIONS)}")
    if event["privacy"] not in PRIVACY_LEVELS:
        errors.append(f"privacy must be one of {sorted(PRIVACY_LEVELS)}")
    if event["project_id"] is not None and (
        not isinstance(event["project_id"], str) or not event["project_id"].strip()
    ):
        errors.append("project_id must be null or a non-empty string")
    if event["supersedes"] is not None and (
        not isinstance(event["supersedes"], str) or not event["supersedes"].startswith("fact_")
    ):
        errors.append("supersedes must be null or a fact_ event_id")
    try:
        normalize_refs(event["refs"])
    except LedgerError as exc:
        errors.append(str(exc))
    if not errors and event["event_id"] != normalized_identity(event):
        errors.append("event_id does not match deterministic content identity")
    return errors


def events_path(root: Path, occurred_at: str) -> Path:
    month = parse_timestamp(occurred_at, "occurred_at").strftime("%Y-%m")
    return root / "events" / f"{month}.jsonl"


def inbox_path(root: Path, recorded_at: str) -> Path:
    day = parse_timestamp(recorded_at, "recorded_at").date().isoformat()
    return root / "inbox" / f"{day}.jsonl"


def append_jsonl_locked(path: Path, row: dict[str, Any], id_field: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        for line in handle:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if existing.get(id_field) == row[id_field]:
                return False
        handle.seek(0, os.SEEK_END)
        handle.write(canonical_json(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def iter_jsonl_files(directory: Path) -> Iterable[Path]:
    if directory.is_dir():
        yield from sorted(directory.glob("*.jsonl"))


def iter_formal_events(root: Path) -> Iterable[dict[str, Any]]:
    for path in iter_jsonl_files(root / "events"):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row


def find_event(root: Path, event_id: str) -> dict[str, Any] | None:
    for event in iter_formal_events(root):
        if event.get("event_id") == event_id:
            return event
    return None


def append_event(root: Path, payload: dict[str, Any], recorded_at: str | None = None) -> tuple[dict[str, Any], bool]:
    event = normalize_formal_event(payload, recorded_at=recorded_at)
    if not event["source_ref"].strip():
        raise LedgerError("source_ref is required")
    existing = find_event(root, event["event_id"])
    if existing is not None:
        return existing, False
    written = append_jsonl_locked(events_path(root, event["occurred_at"]), event, "event_id")
    return event, written


def propose_event(root: Path, payload: dict[str, Any], recorded_at: str | None = None) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise LedgerError("proposal must be a JSON object")
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise LedgerError("summary is required")
    if not isinstance(payload.get("source_ref"), str) or not payload["source_ref"].strip():
        raise LedgerError("source_ref is required")
    stamp = recorded_at or format_timestamp(utc_now())
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": stamp,
        "occurred_at": payload.get("occurred_at"),
        "subject": payload.get("subject"),
        "event_type": payload.get("event_type"),
        "summary": payload["summary"].strip(),
        "source_ref": payload["source_ref"].strip(),
        "actor": payload.get("actor", "unknown"),
        "confidence": payload.get("confidence"),
        "verification": payload.get("verification", "asserted"),
        "privacy": payload.get("privacy", "private"),
        "project_id": payload.get("project_id"),
        "refs": payload.get("refs", empty_refs()),
        "status": "proposed",
    }
    proposal["proposal_id"] = normalized_identity(proposal, prefix="proposal")
    written = append_jsonl_locked(inbox_path(root, stamp), proposal, "proposal_id")
    return proposal, written


def correct_event(
    root: Path, target_id: str, payload: dict[str, Any], recorded_at: str | None = None
) -> tuple[dict[str, Any], bool]:
    target = find_event(root, target_id)
    if target is None:
        raise LedgerError(f"correction target not found: {target_id}")
    correction = dict(payload)
    correction["supersedes"] = target_id
    correction.setdefault("event_type", "correction")
    correction.setdefault("subject", target["subject"])
    correction.setdefault("project_id", target["project_id"])
    correction.setdefault("privacy", target["privacy"])
    return append_event(root, correction, recorded_at=recorded_at)


def query_events(
    root: Path,
    date_from: str | None = None,
    date_to: str | None = None,
    project_id: str | None = None,
    subject: str | None = None,
    verification: str | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in iter_formal_events(root):
        day = parse_timestamp(event["occurred_at"], "occurred_at").date().isoformat()
        subject_ref = f"{event['subject']['type']}:{event['subject']['id']}"
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        if project_id and event["project_id"] != project_id:
            continue
        if subject and subject_ref != subject:
            continue
        if verification and event["verification"] != verification:
            continue
        results.append(event)
    return sorted(results, key=lambda item: (item["occurred_at"], item["event_id"]))


def atomic_replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def daily_projection(root: Path, day: str) -> tuple[Path, dict[str, int]]:
    dt.date.fromisoformat(day)
    events = query_events(root, date_from=day, date_to=day)
    proposals: list[dict[str, Any]] = []
    proposal_path = root / "inbox" / f"{day}.jsonl"
    if proposal_path.is_file():
        with proposal_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    proposals.append(row)
    lines = [
        "---",
        f"title: {day} 事实日报",
        f"date: {day}",
        "type: fact-projection",
        "status: generated",
        "---",
        "",
        f"# {day} 事实日报",
        "",
        "> 派生视图，可从 Fact Ledger 重建；不是新的事实源。",
        "",
        f"- 正式事实：{len(events)}",
        f"- 待审提案：{len(proposals)}",
        "",
        "## 正式事实",
        "",
    ]
    if not events:
        lines.append("- 无")
    for event in events:
        lines.append(
            f"- `{event['event_id']}` {event['summary']} "
            f"（{event['verification']}；来源：`{event['source_ref']}`）"
        )
    lines.extend(["", "## 待审提案", ""])
    if not proposals:
        lines.append("- 无")
    for proposal in sorted(proposals, key=lambda item: item["proposal_id"]):
        lines.append(f"- `{proposal['proposal_id']}` {proposal['summary']}（待审）")
    lines.append("")
    path = root / "projections" / "daily" / f"{day}.md"
    atomic_replace(path, "\n".join(lines))
    return path, {"events": len(events), "proposals": len(proposals)}


def validate_ledger(root: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    rows: list[tuple[Path, int, dict[str, Any]]] = []
    seen: dict[str, tuple[Path, int]] = {}
    for path in iter_jsonl_files(root / "events"):
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
                    continue
                rows.append((path, line_number, event))
                for error in validate_formal_event(event):
                    issues.append({"path": str(path), "line": line_number, "issue": error})
                event_id = event.get("event_id") if isinstance(event, dict) else None
                if event_id in seen:
                    issues.append({"path": str(path), "line": line_number, "issue": f"duplicate event_id: {event_id}"})
                elif isinstance(event_id, str):
                    seen[event_id] = (path, line_number)
    ids = set(seen)
    for path, line_number, event in rows:
        if not isinstance(event, dict):
            continue
        target = event.get("supersedes")
        if target and target not in ids:
            issues.append({"path": str(path), "line": line_number, "issue": f"missing supersedes target: {target}"})
        if all(field in event for field in ("occurred_at", "recorded_at")):
            try:
                occurred = parse_timestamp(event["occurred_at"], "occurred_at")
                recorded = parse_timestamp(event["recorded_at"], "recorded_at")
                if occurred > recorded:
                    issues.append({"path": str(path), "line": line_number, "issue": "occurred_at is after recorded_at"})
                if target in seen:
                    target_event = next(
                        (
                            row[2]
                            for row in rows
                            if isinstance(row[2], dict) and row[2].get("event_id") == target
                        ),
                        None,
                    )
                    if target_event and recorded < parse_timestamp(target_event["recorded_at"], "recorded_at"):
                        issues.append({"path": str(path), "line": line_number, "issue": "correction predates target record"})
            except LedgerError:
                pass
    return {"valid": not issues, "event_count": len(rows), "issue_count": len(issues), "issues": issues[:100]}


def load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.event_json:
        try:
            value = json.loads(args.event_json)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"invalid --event-json: {exc.msg}") from exc
    else:
        try:
            value = json.loads(args.event_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"cannot read --event-file: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerError("event payload must be a JSON object")
    return value


def add_payload_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--event-json")
    group.add_argument("--event-file", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts-root", type=Path, default=DEFAULT_FACTS_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("append", "propose"):
        child = subparsers.add_parser(command)
        add_payload_args(child)
    correct = subparsers.add_parser("correct")
    correct.add_argument("--target", required=True)
    add_payload_args(correct)
    query = subparsers.add_parser("query")
    query.add_argument("--date-from")
    query.add_argument("--date-to")
    query.add_argument("--project")
    query.add_argument("--subject", help="type:id")
    query.add_argument("--verification", choices=sorted(VERIFICATIONS))
    daily = subparsers.add_parser("daily")
    daily.add_argument("--date", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    subparsers.add_parser("validate")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.facts_root.expanduser().resolve()
    try:
        if args.command == "append":
            event, written = append_event(root, load_payload(args))
            result = {"status": "appended" if written else "deduplicated", "event": event}
        elif args.command == "propose":
            proposal, written = propose_event(root, load_payload(args))
            result = {"status": "proposed" if written else "deduplicated", "proposal": proposal}
        elif args.command == "correct":
            event, written = correct_event(root, args.target, load_payload(args))
            result = {"status": "corrected" if written else "deduplicated", "event": event}
        elif args.command == "query":
            result = {
                "events": query_events(
                    root,
                    date_from=args.date_from,
                    date_to=args.date_to,
                    project_id=args.project,
                    subject=args.subject,
                    verification=args.verification,
                )
            }
        elif args.command == "daily":
            path, counts = daily_projection(root, args.date)
            result = {"status": "generated", "path": str(path), **counts}
        else:
            result = validate_ledger(root)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["valid"] else 1
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (LedgerError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)[:1000]}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
