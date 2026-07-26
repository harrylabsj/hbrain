#!/usr/bin/env python3
"""Compile dated formal facts into proposal-only low-impact project changes.

The compiler is intentionally conservative: it only advances evidence pointers
(``last_fact_id`` and ``last_reviewed_at``). It never applies proposals or
invents a project action/status/phase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import project_registry as registry


DEFAULT_FACTS_ROOT = Path("/Users/jianghaidong/hbrain/facts")
DEFAULT_PROJECTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/projects")
LOW_IMPACT = {"last_fact_id", "last_reviewed_at"}


class CompilerError(Exception):
    pass


def parse_date(value: str | None) -> dt.date:
    try:
        return dt.date.fromisoformat(value) if value else dt.date.today() - dt.timedelta(days=1)
    except ValueError as exc:
        raise CompilerError(f"--for-date must be YYYY-MM-DD: {exc}") from exc


def day_of(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.date().isoformat()
    except ValueError:
        return None


def ensure_safe_root(root: Path, *children: str) -> None:
    if root.is_symlink():
        raise CompilerError(f"root must not be a symlink: {root}")
    current = root
    for child in children:
        current = current / child
        if current.is_symlink():
            raise CompilerError(f"path component must not be a symlink: {current}")


def read_events(facts_root: Path, day: dt.date, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ensure_safe_root(facts_root, "events")
    path = facts_root / "events" / f"{day:%Y-%m}.jsonl"
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise CompilerError(f"events file must be a regular file: {path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"path": str(path), "line": number, "issue": f"invalid_json: {exc.msg}"})
            continue
        if isinstance(row, dict) and day_of(row.get("occurred_at")) == day.isoformat():
            rows.append(row)
    rows.sort(key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("event_id") or "")))
    return rows


def projects(root: Path, facts_root: Path) -> dict[str, dict[str, Any]]:
    ensure_safe_root(root)
    result: dict[str, dict[str, Any]] = {}
    for path in registry.project_files(root):
        project = registry.load_project(root, path.parent.name, facts_root)
        result[project["project_id"]] = project
    return result


def candidate(project: dict[str, Any], fact: dict[str, Any], day: dt.date) -> dict[str, Any]:
    project_id = project["project_id"]
    fact_id = fact.get("event_id")
    if not isinstance(fact_id, str) or not fact_id.startswith("fact_"):
        raise CompilerError(f"invalid fact event_id for {project_id}")
    changes = {"last_fact_id": fact_id, "last_reviewed_at": day.isoformat()}
    evidence = {"fact_id": fact_id, "decision_ref": None}
    base_hash = registry.canonical_json(project)
    identity = {
        "project_id": project_id,
        "changes": changes,
        "evidence": evidence,
        "base_hash": __import__("hashlib").sha256(base_hash.encode()).hexdigest(),
        "schema_version": registry.SCHEMA_VERSION,
    }
    return {
        **identity,
        "proposal_id": registry.stable_id("project_change_", identity),
        "status": "proposed",
        "high_impact": False,
        "proposal_only": True,
        "auto_promote": False,
        "source": {"event_type": fact.get("event_type"), "occurred_at": fact.get("occurred_at")},
    }


def compile_changes(
    projects_root: Path, facts_root: Path, day: dt.date, *, no_write: bool = False
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    registered = projects(projects_root, facts_root)
    events = read_events(facts_root, day, issues)
    if issues:
        return {
            "schema_version": 1,
            "command": "compile",
            "for_date": day.isoformat(),
            "proposal_only": True,
            "auto_promote": False,
            "dry_run": no_write,
            "valid": False,
            "candidates": 0,
            "appended": 0,
            "issues": issues,
            "proposals": [],
        }
    latest: dict[str, dict[str, Any]] = {}
    for fact in events:
        project_id = fact.get("project_id")
        if not isinstance(project_id, str) or project_id not in registered:
            continue
        if fact.get("verification") not in {"observed", "verified"}:
            continue
        previous = latest.get(project_id)
        if previous is None or (str(fact.get("occurred_at")), str(fact.get("event_id"))) > (
            str(previous.get("occurred_at")), str(previous.get("event_id"))
        ):
            latest[project_id] = fact
    output: list[dict[str, Any]] = []
    for project_id in sorted(latest):
        project = registered[project_id]
        fact = latest[project_id]
        if project.get("last_fact_id") == fact.get("event_id") and project.get("last_reviewed_at") == day.isoformat():
            continue
        preview = candidate(project, fact, day)
        annotations = {"proposal_only": True, "auto_promote": False, "source": preview["source"]}
        if no_write:
            output.append({**preview, "written": False})
            continue
        existing_path = projects_root / "inbox" / f"{preview['proposal_id']}.json"
        if existing_path.is_file() and not existing_path.is_symlink():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CompilerError(f"existing proposal is corrupted: {existing_path}: {exc}")
            output.append({**existing, "written": False})
            continue
        proposal, written = registry.propose_change(
            projects_root,
            facts_root,
            project_id,
            preview["changes"],
            preview["evidence"],
            annotations=annotations,
        )
        output.append({**proposal, "written": written})
    return {
        "schema_version": 1,
        "command": "compile",
        "for_date": day.isoformat(),
        "proposal_only": True,
        "auto_promote": False,
        "dry_run": no_write,
        "valid": True,
        "candidates": len(output),
        "appended": sum(1 for row in output if row.get("written")),
        "issues": issues,
        "proposals": output,
    }


def validate(root: Path, facts_root: Path) -> dict[str, Any]:
    ensure_safe_root(root, "inbox")
    issues: list[dict[str, Any]] = []
    legacy_ignored = 0
    for path in sorted((root / "inbox").glob("project_change_*.json")) if (root / "inbox").exists() else []:
        if path.is_symlink() or not path.is_file():
            issues.append({"path": str(path), "issue": "proposal path is not a regular file"})
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append({"path": str(path), "issue": str(exc)})
            continue
        # The registry inbox may already contain human-created proposals. They
        # are validated by project_registry; this command audits only proposals
        # explicitly emitted by this compiler.
        if "proposal_only" not in row:
            legacy_ignored += 1
            continue
        if row.get("proposal_only") is not True or row.get("auto_promote") is not False:
            issues.append({"path": str(path), "issue": "safety flags missing or invalid"})
        if row.get("high_impact") is not False or set(row.get("changes", {})) - LOW_IMPACT:
            issues.append({"path": str(path), "issue": "proposal is not low impact"})
        try:
            registry.evidence_ref(row.get("evidence", {}).get("fact_id"), None, facts_root)
        except (AttributeError, TypeError, registry.RegistryError) as exc:
            issues.append({"path": str(path), "issue": f"invalid evidence: {exc}"})
    return {"schema_version": 1, "command": "validate", "valid": not issues, "issue_count": len(issues), "legacy_ignored": legacy_ignored, "issues": issues}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile facts into proposal-only project updates.")
    parser.add_argument("--facts-root", type=Path, default=DEFAULT_FACTS_ROOT)
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--for-date")
    sub = parser.add_subparsers(dest="command", required=True)
    compile_cmd = sub.add_parser("compile")
    compile_cmd.add_argument("--no-write", action="store_true")
    sub.add_parser("validate")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        day = parse_date(args.for_date)
        result = compile_changes(args.projects_root, args.facts_root, day, no_write=getattr(args, "no_write", False)) if args.command == "compile" else validate(args.projects_root, args.facts_root)
        print(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result.get("valid", True) else 1
    except (CompilerError, registry.RegistryError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
