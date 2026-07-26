#!/usr/bin/env python3
"""Auditable Project Registry for the Haidong five-domain architecture.

Canonical project files use strict JSON stored in ``.yaml`` files so the
implementation remains Python-standard-library-only. See projects/README.md.
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


DEFAULT_PROJECTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/projects")
DEFAULT_FACTS_ROOT = Path("/Users/jianghaidong/hbrain/facts")
SCHEMA_VERSION = 1
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
PROPOSAL_ID_RE = re.compile(r"^project_change_[0-9a-f]{24}$")
HIGH_IMPACT = {"priority", "status", "phase", "objective", "owner", "privacy"}
LOW_IMPACT = {
    "next_action",
    "blocked_by",
    "repositories",
    "knowledge_refs",
    "cass_scope",
    "last_fact_id",
    "last_reviewed_at",
}
MUTABLE_FIELDS = HIGH_IMPACT | LOW_IMPACT
PROJECT_FIELDS = {
    "schema_version",
    "project_id",
    "name",
    "status",
    "priority",
    "phase",
    "owner",
    "objective",
    "next_action",
    "blocked_by",
    "repositories",
    "knowledge_refs",
    "cass_scope",
    "last_fact_id",
    "last_reviewed_at",
    "privacy",
    "state_ref",
}


class RegistryError(Exception):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def validate_project_id(project_id: Any) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise RegistryError("invalid project_id")
    return project_id


def safe_project_path(root: Path, project_id: str) -> Path:
    validate_project_id(project_id)
    root_resolved = root.resolve()
    path = (root / project_id / "project.yaml").resolve()
    if root_resolved not in path.parents:
        raise RegistryError("project path escapes registry root")
    return path


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def locked(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".registry.lock").open("a", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read canonical JSON from {path}: {exc}") from exc


def fact_ids(facts_root: Path) -> set[str]:
    ids: set[str] = set()
    events = facts_root / "events"
    if facts_root.is_symlink() or events.is_symlink():
        raise RegistryError("facts root and events directory must not be symlinks")
    if not events.exists():
        return ids
    for path in sorted(events.glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("event_id"), str):
                ids.add(row["event_id"])
    return ids


def evidence_ref(fact_id: str | None, decision_ref: str | None, facts_root: Path) -> dict[str, str | None]:
    if bool(fact_id) == bool(decision_ref):
        raise RegistryError("provide exactly one of fact_id or decision_ref")
    if fact_id:
        if fact_id not in fact_ids(facts_root):
            raise RegistryError(f"fact_id does not exist: {fact_id}")
        return {"fact_id": fact_id, "decision_ref": None}
    if not isinstance(decision_ref, str) or not decision_ref.strip():
        raise RegistryError("decision_ref must be non-empty")
    return {"fact_id": None, "decision_ref": decision_ref.strip()}


def validate_project(project: Any, facts_root: Path) -> list[str]:
    if not isinstance(project, dict):
        return ["project is not an object"]
    errors: list[str] = []
    missing = sorted(PROJECT_FIELDS - set(project))
    extra = sorted(set(project) - PROJECT_FIELDS)
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if extra:
        errors.append("unsupported fields: " + ", ".join(extra))
    if missing:
        return errors
    if project["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    try:
        validate_project_id(project["project_id"])
    except RegistryError as exc:
        errors.append(str(exc))
    for field in ("name", "status", "priority", "phase", "owner", "objective", "next_action", "privacy"):
        if not isinstance(project[field], str) or not project[field].strip():
            errors.append(f"{field} must be a non-empty string")
    for field in ("blocked_by", "repositories", "knowledge_refs"):
        value = project[field]
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            errors.append(f"{field} must be a list of non-empty strings")
    if project["cass_scope"] is not None and not isinstance(project["cass_scope"], str):
        errors.append("cass_scope must be null or string")
    if project["last_fact_id"] is not None and project["last_fact_id"] not in fact_ids(facts_root):
        errors.append(f"last_fact_id does not exist: {project['last_fact_id']}")
    try:
        dt.date.fromisoformat(project["last_reviewed_at"])
    except (TypeError, ValueError):
        errors.append("last_reviewed_at must be YYYY-MM-DD")
    state_ref = project["state_ref"]
    if not isinstance(state_ref, dict) or set(state_ref) != {"fact_id", "decision_ref"}:
        errors.append("state_ref must contain exactly fact_id and decision_ref")
    elif bool(state_ref.get("fact_id")) == bool(state_ref.get("decision_ref")):
        errors.append("state_ref must contain exactly one evidence reference")
    elif state_ref.get("fact_id") and state_ref["fact_id"] not in fact_ids(facts_root):
        errors.append(f"state_ref fact_id does not exist: {state_ref['fact_id']}")
    return errors


def project_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink() or not PROJECT_ID_RE.fullmatch(child.name):
            continue
        path = child / "project.yaml"
        if path.is_file() and not path.is_symlink() and path.resolve() == safe_project_path(root, child.name):
            paths.append(path)
    return paths


def load_project(root: Path, project_id: str, facts_root: Path) -> dict[str, Any]:
    path = safe_project_path(root, project_id)
    if not path.is_file() or path.is_symlink():
        raise RegistryError(f"unknown project: {project_id}")
    project = load_json(path)
    errors = validate_project(project, facts_root)
    if errors:
        raise RegistryError("; ".join(errors[:8]))
    return project


def render(root: Path, facts_root: Path) -> tuple[Path, Path, int]:
    projects = []
    for path in project_files(root):
        project = load_json(path)
        errors = validate_project(project, facts_root)
        if errors:
            raise RegistryError(f"invalid project {path}: {'; '.join(errors[:5])}")
        projects.append(project)
    projects.sort(key=lambda item: (item["priority"], item["project_id"]))
    registry = {
        "schema_version": SCHEMA_VERSION,
        "project_count": len(projects),
        "projects": [
            {
                key: project[key]
                for key in ("project_id", "name", "status", "priority", "phase", "next_action", "last_fact_id")
            }
            for project in projects
        ],
    }
    lines = ["# Project Registry", "", "> 此页由 `project.yaml` 确定性生成，不是第二份事实源。", ""]
    if projects:
        lines += ["| 项目 | 状态 | 优先级 | 阶段 | 下一动作 | 依据 |", "|---|---|---|---|---|---|"]
        for project in projects:
            source = project["last_fact_id"] or project["state_ref"]["decision_ref"] or project["state_ref"]["fact_id"]
            lines.append(
                f"| `{project['project_id']}` {markdown_cell(project['name'])} | "
                f"{markdown_cell(project['status'])} | {markdown_cell(project['priority'])} | "
                f"{markdown_cell(project['phase'])} | {markdown_cell(project['next_action'])} | "
                f"`{markdown_cell(str(source))}` |"
            )
    else:
        lines.append("- 无项目")
    lines.append("")
    registry_path = root / "registry.yaml"
    index_path = root / "index.md"
    atomic_write(registry_path, pretty_json(registry))
    atomic_write(index_path, "\n".join(lines))
    return registry_path, index_path, len(projects)


def markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def init_project(root: Path, facts_root: Path, payload: dict[str, Any], ref: dict[str, str | None]) -> tuple[dict[str, Any], bool]:
    project = dict(payload)
    project.setdefault("schema_version", SCHEMA_VERSION)
    project.setdefault("blocked_by", [])
    project.setdefault("repositories", [])
    project.setdefault("knowledge_refs", [])
    project.setdefault("cass_scope", None)
    project.setdefault("last_fact_id", ref["fact_id"])
    project["state_ref"] = ref
    errors = validate_project(project, facts_root)
    if errors:
        raise RegistryError("; ".join(errors[:8]))
    path = safe_project_path(root, project["project_id"])
    lock = locked(root)
    try:
        if path.exists():
            existing = load_json(path)
            if existing == project:
                return project, False
            raise RegistryError(f"project already exists: {project['project_id']}")
        atomic_write(path, pretty_json(project))
        render(root, facts_root)
        return project, True
    finally:
        lock.close()


def propose_change(
    root: Path,
    facts_root: Path,
    project_id: str,
    changes: dict[str, Any],
    ref: dict[str, str | None],
) -> tuple[dict[str, Any], bool]:
    current = load_project(root, project_id, facts_root)
    if not isinstance(changes, dict) or not changes:
        raise RegistryError("changes must be a non-empty object")
    unsupported = sorted(set(changes) - MUTABLE_FIELDS)
    if unsupported:
        raise RegistryError("unsupported change fields: " + ", ".join(unsupported))
    if "last_fact_id" in changes and changes["last_fact_id"] not in fact_ids(facts_root):
        raise RegistryError(f"last_fact_id does not exist: {changes['last_fact_id']}")
    preview = dict(current)
    preview.update(changes)
    preview["state_ref"] = ref
    preview_errors = validate_project(preview, facts_root)
    if preview_errors:
        raise RegistryError("invalid proposed state: " + "; ".join(preview_errors[:8]))
    base_hash = hashlib.sha256(canonical_json(current).encode()).hexdigest()
    identity = {
        "project_id": project_id,
        "changes": changes,
        "evidence": ref,
        "base_hash": base_hash,
        "schema_version": SCHEMA_VERSION,
    }
    proposal = {
        **identity,
        "proposal_id": stable_id("project_change_", identity),
        "status": "proposed",
        "high_impact": bool(set(changes) & HIGH_IMPACT),
    }
    path = root / "inbox" / f"{proposal['proposal_id']}.json"
    lock = locked(root)
    try:
        if path.exists():
            if load_json(path) == proposal:
                return proposal, False
            raise RegistryError("proposal id collision")
        atomic_write(path, pretty_json(proposal))
        return proposal, True
    finally:
        lock.close()


def applied_ids(root: Path) -> set[str]:
    path = root / "audit" / "applied.jsonl"
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("proposal_id"), str):
            ids.add(row["proposal_id"])
    return ids


def append_audit(root: Path, row: dict[str, Any]) -> None:
    path = root / "audit" / "applied.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def apply_proposal(
    root: Path,
    facts_root: Path,
    proposal_id: str,
    *,
    human_approved: bool,
    approved_by: str | None = None,
    approval_ref: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if not PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise RegistryError("invalid proposal_id")
    proposal_path = root / "inbox" / f"{proposal_id}.json"
    if not proposal_path.is_file() or proposal_path.is_symlink():
        raise RegistryError(f"unknown proposal: {proposal_id}")
    lock = locked(root)
    try:
        if proposal_id in applied_ids(root):
            return load_project(root, load_json(proposal_path)["project_id"], facts_root), False
        proposal = load_json(proposal_path)
        if proposal.get("schema_version") != SCHEMA_VERSION or proposal.get("status") != "proposed":
            raise RegistryError("invalid proposal schema or status")
        if proposal.get("high_impact") and (
            not human_approved
            or not isinstance(approved_by, str)
            or not approved_by.strip()
            or not isinstance(approval_ref, str)
            or not approval_ref.strip()
        ):
            raise RegistryError(
                "high-impact proposal requires --human-approved, --approved-by, and --approval-ref"
            )
        ref = proposal.get("evidence", {})
        evidence_ref(ref.get("fact_id"), ref.get("decision_ref"), facts_root)
        project = load_project(root, proposal["project_id"], facts_root)
        current_hash = hashlib.sha256(canonical_json(project).encode()).hexdigest()
        if proposal.get("base_hash") != current_hash:
            raise RegistryError("proposal is stale; create a new proposal from the current project state")
        changes = proposal.get("changes")
        if not isinstance(changes, dict) or not changes or set(changes) - MUTABLE_FIELDS:
            raise RegistryError("invalid proposal changes")
        if bool(set(changes) & HIGH_IMPACT) != bool(proposal.get("high_impact")):
            raise RegistryError("proposal impact classification mismatch")
        if "last_fact_id" in changes and changes["last_fact_id"] not in fact_ids(facts_root):
            raise RegistryError(f"last_fact_id does not exist: {changes['last_fact_id']}")
        updated = dict(project)
        updated.update(changes)
        updated["state_ref"] = ref
        errors = validate_project(updated, facts_root)
        if errors:
            raise RegistryError("; ".join(errors[:8]))
        atomic_write(safe_project_path(root, updated["project_id"]), pretty_json(updated))
        append_audit(
            root,
            {
                "proposal_id": proposal_id,
                "project_id": updated["project_id"],
                "evidence": ref,
                "human_approved": human_approved,
                "approved_by": approved_by.strip() if isinstance(approved_by, str) else None,
                "approval_ref": approval_ref.strip() if isinstance(approval_ref, str) else None,
                "changes": changes,
            },
        )
        render(root, facts_root)
        return updated, True
    finally:
        lock.close()


def recent_project_facts(facts_root: Path, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    events = facts_root / "events"
    for path in sorted(events.glob("*.jsonl")) if events.exists() else []:
        if path.is_symlink() or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("project_id") == project_id:
                rows.append(
                    {key: row.get(key) for key in ("event_id", "occurred_at", "event_type", "summary", "source_ref")}
                )
    rows.sort(key=lambda item: (item.get("occurred_at") or "", item.get("event_id") or ""), reverse=True)
    return rows[: max(0, min(limit, 5))]


def validate_registry(root: Path, facts_root: Path) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    projects: list[dict[str, Any]] = []
    for path in project_files(root):
        project = load_json(path)
        errors = validate_project(project, facts_root)
        for error in errors:
            issues.append({"path": str(path), "issue": error})
        if isinstance(project, dict):
            projects.append(project)
    expected_ids = sorted(item.get("project_id") for item in projects if isinstance(item.get("project_id"), str))
    if len(expected_ids) != len(set(expected_ids)):
        issues.append({"path": str(root), "issue": "duplicate project_id"})
    registry_path = root / "registry.yaml"
    if registry_path.exists():
        registry = load_json(registry_path)
        actual_ids = sorted(item.get("project_id") for item in registry.get("projects", []) if isinstance(item, dict))
        if registry.get("schema_version") != SCHEMA_VERSION or actual_ids != expected_ids:
            issues.append({"path": str(registry_path), "issue": "registry projection is stale or invalid"})
    else:
        issues.append({"path": str(registry_path), "issue": "missing registry projection"})
    return {"valid": not issues, "project_count": len(projects), "issue_count": len(issues), "issues": issues[:100]}


def parse_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid {label}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be an object")
    return value


def add_evidence_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fact-id")
    group.add_argument("--decision-ref")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditable Project Registry.")
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--facts-root", type=Path, default=DEFAULT_FACTS_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--project-json", required=True)
    add_evidence_args(init)
    query = commands.add_parser("query")
    query.add_argument("--project-id")
    propose = commands.add_parser("propose")
    propose.add_argument("--project-id", required=True)
    propose.add_argument("--changes-json", required=True)
    add_evidence_args(propose)
    apply_cmd = commands.add_parser("apply")
    apply_cmd.add_argument("--proposal-id", required=True)
    apply_cmd.add_argument("--human-approved", action="store_true")
    apply_cmd.add_argument("--approved-by")
    apply_cmd.add_argument("--approval-ref")
    commands.add_parser("render")
    commands.add_parser("validate")
    context = commands.add_parser("context")
    context.add_argument("--project-id", required=True)
    context.add_argument("--fact-limit", type=int, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            ref = evidence_ref(args.fact_id, args.decision_ref, args.facts_root)
            project, written = init_project(
                args.projects_root, args.facts_root, parse_object(args.project_json, "--project-json"), ref
            )
            result = {"status": "created" if written else "exists", "project": project}
        elif args.command == "query":
            if args.project_id:
                result = {"project": load_project(args.projects_root, args.project_id, args.facts_root)}
            else:
                result = {"projects": [load_json(path) for path in project_files(args.projects_root)]}
        elif args.command == "propose":
            ref = evidence_ref(args.fact_id, args.decision_ref, args.facts_root)
            proposal, written = propose_change(
                args.projects_root,
                args.facts_root,
                args.project_id,
                parse_object(args.changes_json, "--changes-json"),
                ref,
            )
            result = {"status": "proposed" if written else "exists", "proposal": proposal}
        elif args.command == "apply":
            project, written = apply_proposal(
                args.projects_root,
                args.facts_root,
                args.proposal_id,
                human_approved=args.human_approved,
                approved_by=args.approved_by,
                approval_ref=args.approval_ref,
            )
            result = {"status": "applied" if written else "already-applied", "project": project}
        elif args.command == "render":
            registry, index, count = render(args.projects_root, args.facts_root)
            result = {"status": "rendered", "projects": count, "registry": str(registry), "index": str(index)}
        elif args.command == "validate":
            result = validate_registry(args.projects_root, args.facts_root)
            print(pretty_json(result), end="")
            return 0 if result["valid"] else 1
        else:
            project = load_project(args.projects_root, args.project_id, args.facts_root)
            result = {
                "project": project,
                "facts": recent_project_facts(args.facts_root, args.project_id, args.fact_limit),
                "limits": {"projects": 1, "facts": min(max(args.fact_limit, 0), 5)},
            }
        print(pretty_json(result), end="")
        return 0
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
