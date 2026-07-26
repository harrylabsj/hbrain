#!/usr/bin/env python3
"""Zero-preload router, context packets, and completion receipts.

The runtime never scans the five domains at startup. Retrieval happens only in
the explicit ``context`` command and knowledge/CASS calls require ``--include``.
Completion receipts are append-only inbox records; they never promote content.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


AUTOMATION_ROOT = Path(__file__).resolve().parent
if str(AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_ROOT))
import project_registry  # noqa: E402


DEFAULT_PROJECTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/projects")
DEFAULT_FACTS_ROOT = Path("/Users/jianghaidong/hbrain/facts")
DEFAULT_RECEIPTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/receipts")
HBRAIN_LOOP = Path("/Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py")
SCHEMA_VERSION = 1
DOMAINS = ("project", "experience", "fact", "artifact", "knowledge")
PRIVACY_LEVELS = ("private", "sensitive", "shareable")
MAX_ITEMS = 5
DEFAULT_CHAR_BUDGET = 48_000
MAX_SUMMARY_CHARS = 500
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
PACKET_ID_RE = re.compile(r"^packet_[0-9a-f]{24}$")
RECEIPT_ID_RE = re.compile(r"^receipt_[0-9a-f]{24}$")
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"\s*(?:::|=|:)\s*\S+|\bBearer\s+[A-Za-z0-9._~+/=-]+"
)

DOMAIN_TERMS = {
    "project": (
        "项目", "阶段", "进度", "下一步", "推进", "阻塞", "优先级", "里程碑",
        "project", "phase", "milestone", "roadmap", "next action", "blocked",
    ),
    "experience": (
        "上次怎么", "以前怎么", "以前", "踩坑", "踩过", "失败模式", "调试经验", "工作流经验", "复盘经验",
        "experience", "playbook", "lessons learned", "failure pattern",
    ),
    "fact": (
        "发生了什么", "昨天", "今天", "何时", "审核状态", "通过了", "发布了", "完成了",
        "fact", "when", "status at", "happened", "released", "passed",
    ),
    "artifact": (
        "文件在哪里", "代码在哪里", "证据在哪里", "截图", "链接", "仓库", "commit", "artifact",
        "file", "repository", "screenshot", "evidence",
    ),
    "knowledge": (
        "是什么", "为什么", "如何理解", "原理", "概念", "方法论", "区别", "知识",
        "what is", "why", "principle", "concept", "difference",
    ),
}
PRECEDENCE = {domain: index for index, domain in enumerate(DOMAINS)}


class RuntimeError(Exception):
    pass


def now_rfc3339() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]


def truncate(value: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sanitize_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        sanitized, count = SECRET_RE.subn("[REDACTED]", value)
        return sanitized, count
    if isinstance(value, list):
        result, total = [], 0
        for item in value:
            clean, count = sanitize_value(item)
            result.append(clean)
            total += count
        return result, total
    if isinstance(value, dict):
        result, total = {}, 0
        for key, item in value.items():
            clean, count = sanitize_value(item)
            result[key] = clean
            total += count
        return result, total
    return value, 0


def classify(query: str, *, explicit_domain: str | None = None, project_id: str | None = None) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise RuntimeError("query must be non-empty")
    if explicit_domain:
        if explicit_domain not in DOMAINS:
            raise RuntimeError(f"domain must be one of {DOMAINS}")
        return {
            "primary_domain": explicit_domain,
            "secondary_domains": [],
            "confidence": 1.0,
            "needs_review": False,
            "method": "explicit",
            "scores": {domain: int(domain == explicit_domain) for domain in DOMAINS},
        }
    lowered = query.casefold()
    scores = {domain: sum(1 for term in terms if term.casefold() in lowered) for domain, terms in DOMAIN_TERMS.items()}
    if project_id:
        scores["project"] += 2
    matched = [domain for domain, score in scores.items() if score > 0]
    if not matched:
        return {
            "primary_domain": None,
            "secondary_domains": [],
            "confidence": 0.0,
            "needs_review": True,
            "method": "undetermined",
            "scores": scores,
        }
    matched.sort(key=lambda domain: (-scores[domain], PRECEDENCE[domain]))
    primary = matched[0]
    top = scores[primary]
    tied = sum(1 for domain in matched if scores[domain] == top)
    confidence = round(max(0.3, min(0.95, 0.55 + 0.15 * top - (0.15 if tied > 1 else 0.0))), 2)
    return {
        "primary_domain": primary,
        "secondary_domains": matched[1:],
        "confidence": confidence,
        "needs_review": tied > 1 or confidence < 0.6,
        "method": "deterministic-keywords",
        "scores": scores,
    }


def run_json_command(command: list[str], *, timeout: int = 20) -> tuple[dict[str, Any] | None, str | None]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if completed.returncode != 0:
        return None, truncate(completed.stderr or completed.stdout or f"exit {completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON output: {exc.msg}"
    return payload if isinstance(payload, dict) else None, None


def knowledge_summaries(query: str) -> tuple[list[dict[str, Any]], str | None]:
    payload, error = run_json_command(
        [sys.executable, str(HBRAIN_LOOP), "route", "--query", query[:4000]]
    )
    if error or payload is None:
        return [], error or "empty knowledge response"
    rows = []
    for hit in payload.get("hits", [])[:MAX_ITEMS]:
        if isinstance(hit, dict):
            rows.append(
                {
                    "slug": truncate(hit.get("slug"), 300),
                    "summary": truncate(hit.get("excerpt")),
                    "score": hit.get("score"),
                }
            )
    return rows, None


def _candidate_lists(payload: dict[str, Any]) -> Iterable[list[Any]]:
    for key in ("rules", "history", "results", "items", "context"):
        value = payload.get(key)
        if isinstance(value, list):
            yield value
        elif isinstance(value, dict):
            yield from _candidate_lists(value)
    data = payload.get("data")
    if isinstance(data, dict):
        yield from _candidate_lists(data)


def experience_summaries(query: str, workspace: Path) -> tuple[list[dict[str, Any]], str | None]:
    if workspace.is_symlink() or not workspace.is_dir():
        return [], "--cass-workspace must be a real directory"
    payload, error = run_json_command(
        [
            "cm", "context", query[:4000], "--workspace", str(workspace.resolve()),
            "--limit", str(MAX_ITEMS), "--history", "1", "--json",
        ]
    )
    if error or payload is None:
        return [], error or "empty CASS response"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for items in _candidate_lists(payload):
        for item in items:
            if len(rows) >= MAX_ITEMS:
                break
            if isinstance(item, str):
                summary, item_id = truncate(item), stable_id("cass_", item)
            elif isinstance(item, dict):
                raw = item.get("summary") or item.get("title") or item.get("text") or item.get("content")
                if not raw:
                    continue
                summary = truncate(raw)
                item_id = truncate(item.get("id") or stable_id("cass_", summary), 300)
            else:
                continue
            if item_id not in seen:
                rows.append({"id": item_id, "summary": summary})
                seen.add(item_id)
    return rows, None


def build_context_packet(
    query: str,
    *,
    project_id: str | None,
    explicit_domain: str | None,
    include: set[str],
    projects_root: Path,
    facts_root: Path,
    cass_workspace: Path | None,
    artifact_refs: list[str],
    char_budget: int,
) -> dict[str, Any]:
    if include - {"knowledge", "experience"}:
        raise RuntimeError("include only supports knowledge and experience")
    if project_id and not PROJECT_ID_RE.fullmatch(project_id):
        raise RuntimeError("invalid project_id")
    classification = classify(query, explicit_domain=explicit_domain, project_id=project_id)
    identity = {
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "project_id": project_id,
        "domain": explicit_domain,
        "include": sorted(include),
        "artifact_refs": sorted(set(artifact_refs))[:MAX_ITEMS],
        "char_budget": max(2048, min(char_budget, DEFAULT_CHAR_BUDGET)),
        "cass_workspace": str(cass_workspace.absolute()) if cass_workspace else None,
    }
    request_summary = {
        "query_sha256": identity["query_sha256"],
        "project_id": project_id,
        "domain": explicit_domain,
        "include": sorted(include),
        "artifact_ref_sha256": [hashlib.sha256(ref.encode()).hexdigest() for ref in identity["artifact_refs"]],
        "char_budget": identity["char_budget"],
        "cass_workspace_sha256": (
            hashlib.sha256(identity["cass_workspace"].encode()).hexdigest()
            if identity["cass_workspace"]
            else None
        ),
    }
    packet: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "packet_id": stable_id("packet_", identity),
        "created_at": now_rfc3339(),
        "zero_preload": True,
        "request": request_summary,
        "classification": classification,
        "project": None,
        "facts": [],
        "knowledge_summaries": [],
        "experience_summaries": [],
        "artifact_refs": identity["artifact_refs"],
        "retrieval": {
            "project": {"requested": bool(project_id), "executed": False, "error": None},
            "facts": {"requested": False, "executed": False, "error": None},
            "knowledge": {"requested": "knowledge" in include, "executed": False, "error": None},
            "experience": {"requested": "experience" in include, "executed": False, "error": None},
        },
        "limits": {"projects": 1, "facts": 5, "knowledge": 5, "experience": 5},
        "budget": {"max_chars": max(2048, min(char_budget, DEFAULT_CHAR_BUDGET)), "truncated": False},
        "privacy": {"visibility": "private", "redacted_values": 0},
    }
    if project_id:
        try:
            packet["project"] = project_registry.load_project(projects_root, project_id, facts_root)
            packet["retrieval"]["project"]["executed"] = True
            packet["facts"] = project_registry.recent_project_facts(facts_root, project_id, MAX_ITEMS)
            packet["retrieval"]["facts"] = {"requested": True, "executed": True, "error": None}
        except project_registry.RegistryError as exc:
            packet["retrieval"]["project"]["error"] = str(exc)
    elif classification["primary_domain"] in {"project", "fact"}:
        packet["retrieval"][classification["primary_domain"]] = {
            "requested": True,
            "executed": False,
            "error": "--project-id is required for scoped retrieval",
        }
    if "knowledge" in include:
        rows, error = knowledge_summaries(query)
        packet["knowledge_summaries"] = rows[:MAX_ITEMS]
        packet["retrieval"]["knowledge"].update({"executed": error is None, "error": error})
    if "experience" in include:
        if cass_workspace is None:
            packet["retrieval"]["experience"]["error"] = "--cass-workspace is required for experience"
        else:
            rows, error = experience_summaries(query, cass_workspace)
            packet["experience_summaries"] = rows[:MAX_ITEMS]
            packet["retrieval"]["experience"].update({"executed": error is None, "error": error})
    for key in ("project", "facts", "knowledge_summaries", "experience_summaries", "artifact_refs"):
        packet[key], count = sanitize_value(packet[key])
        packet["privacy"]["redacted_values"] += count
    enforce_budget(packet)
    return packet


def enforce_budget(packet: dict[str, Any]) -> None:
    budget = packet["budget"]["max_chars"]
    for key in ("experience_summaries", "knowledge_summaries", "facts", "artifact_refs"):
        while packet[key] and len(pretty_json(packet)) > budget:
            packet[key].pop()
            packet["budget"]["truncated"] = True
    if len(pretty_json(packet)) > budget and packet.get("project"):
        project = packet["project"]
        packet["project"] = {
            key: project.get(key)
            for key in ("project_id", "name", "status", "priority", "phase", "next_action", "last_fact_id")
        }
        packet["budget"]["truncated"] = True
    if len(pretty_json(packet)) > budget:
        packet["classification"].pop("scores", None)
        packet["classification"]["secondary_domains"] = []
        packet["budget"]["truncated"] = True
    actual = len(pretty_json(packet))
    packet["budget"]["actual_chars"] = actual
    packet["budget"]["estimated_tokens"] = (actual + 3) // 4


def validate_evidence(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["evidence must be a non-empty list"]
    errors = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or not isinstance(item.get("ref"), str):
            errors.append(f"evidence[{index}] must contain string type and ref")
        elif not item["type"].strip() or not item["ref"].strip():
            errors.append(f"evidence[{index}] type and ref must be non-empty")
        elif SECRET_RE.search(item["ref"]):
            errors.append(f"evidence[{index}].ref contains secret-like content")
    return errors


def validate_summary_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{field} must be a list"]
    if len(value) > MAX_ITEMS:
        return [f"{field} must contain at most {MAX_ITEMS} items"]
    errors = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{field}[{index}] must be an object")
        elif SECRET_RE.search(canonical_json(item)):
            errors.append(f"{field}[{index}] contains secret-like content")
    return errors


def validate_receipt(receipt: Any) -> list[str]:
    if not isinstance(receipt, dict):
        return ["receipt is not an object"]
    errors: list[str] = []
    required = {
        "schema_version", "receipt_id", "packet_id", "completed_at", "agent", "project_id",
        "action", "result", "evidence", "knowledge_gap", "experience_candidate",
        "privacy", "promotion",
    }
    missing = sorted(required - set(receipt))
    if missing:
        return ["missing fields: " + ", ".join(missing)]
    if receipt["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(receipt["receipt_id"], str) or not RECEIPT_ID_RE.fullmatch(receipt["receipt_id"]):
        errors.append("invalid receipt_id")
    if not isinstance(receipt["packet_id"], str) or not PACKET_ID_RE.fullmatch(receipt["packet_id"]):
        errors.append("invalid packet_id")
    try:
        completed_at = dt.datetime.fromisoformat(str(receipt["completed_at"]).replace("Z", "+00:00"))
        if completed_at.tzinfo is None:
            errors.append("completed_at must include timezone")
    except ValueError:
        errors.append("completed_at must be RFC3339")
    for field in ("agent", "project_id", "action", "result"):
        if not isinstance(receipt[field], str) or not receipt[field].strip():
            errors.append(f"{field} must be non-empty")
        elif SECRET_RE.search(receipt[field]):
            errors.append(f"{field} contains secret-like content")
    if isinstance(receipt["project_id"], str) and not PROJECT_ID_RE.fullmatch(receipt["project_id"]):
        errors.append("invalid project_id")
    if receipt["privacy"] not in PRIVACY_LEVELS:
        errors.append(f"privacy must be one of {PRIVACY_LEVELS}")
    errors.extend(validate_evidence(receipt["evidence"]))
    errors.extend(validate_summary_list(receipt["knowledge_gap"], "knowledge_gap"))
    errors.extend(validate_summary_list(receipt["experience_candidate"], "experience_candidate"))
    promotion = receipt["promotion"]
    if not isinstance(promotion, dict) or promotion.get("auto_promote") is not False or promotion.get("status") != "inbox":
        errors.append("promotion must remain auto_promote=false and status=inbox")
    if not errors and receipt["receipt_id"] != receipt_identity(receipt):
        errors.append("receipt_id does not match deterministic identity")
    return errors


def receipt_identity(receipt: dict[str, Any]) -> str:
    identity = {
        key: receipt[key]
        for key in (
            "schema_version", "packet_id", "agent", "project_id", "action", "result", "evidence",
            "knowledge_gap", "experience_candidate", "privacy", "promotion",
        )
        if key in receipt
    }
    return stable_id("receipt_", identity)


def normalize_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    receipt = dict(payload)
    receipt.setdefault("schema_version", SCHEMA_VERSION)
    receipt.setdefault("completed_at", now_rfc3339())
    receipt.setdefault("knowledge_gap", [])
    receipt.setdefault("experience_candidate", [])
    receipt.setdefault("privacy", "private")
    receipt["promotion"] = {"auto_promote": False, "status": "inbox"}
    receipt["receipt_id"] = receipt_identity(receipt)
    errors = validate_receipt(receipt)
    if errors:
        raise RuntimeError("; ".join(errors[:10]))
    return receipt


def receipt_lock(root: Path, *, exclusive: bool):
    if root.exists() and root.is_symlink():
        raise RuntimeError("receipts root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("receipts root must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(root / ".receipts.lock", flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot open receipts lock safely: {exc}") from exc
    handle = os.fdopen(fd, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return handle


def iter_receipt_files(root: Path) -> Iterable[Path]:
    inbox = root / "inbox"
    if not inbox.exists():
        return []
    if inbox.is_symlink():
        raise RuntimeError("receipts inbox must not be a symlink")
    return sorted(path for path in inbox.glob("*.jsonl") if path.is_file() and not path.is_symlink())


def receipt_exists(root: Path, receipt_id: str) -> bool:
    for path in iter_receipt_files(root):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(existing, dict) and existing.get("receipt_id") == receipt_id:
                    return True
    return False


def append_receipt(root: Path, receipt: dict[str, Any]) -> bool:
    month = dt.datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00")).strftime("%Y-%m")
    lock = receipt_lock(root, exclusive=True)
    try:
        if receipt_exists(root, receipt["receipt_id"]):
            return False
        inbox = root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        if inbox.is_symlink():
            raise RuntimeError("receipts inbox must not be a symlink")
        path = inbox / f"{month}.jsonl"
        if path.is_symlink():
            raise RuntimeError("receipt month file must not be a symlink")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(f"cannot open receipt month file safely: {exc}") from exc
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    finally:
        lock.close()


def validate_receipt_inbox(root: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    count = 0
    seen: set[str] = set()
    lock = receipt_lock(root, exclusive=False)
    try:
        for path in iter_receipt_files(root):
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    count += 1
                    try:
                        receipt = json.loads(line)
                    except json.JSONDecodeError as exc:
                        issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
                        continue
                    for error in validate_receipt(receipt):
                        issues.append({"path": str(path), "line": line_number, "issue": error})
                    receipt_id = receipt.get("receipt_id") if isinstance(receipt, dict) else None
                    if receipt_id in seen:
                        issues.append({"path": str(path), "line": line_number, "issue": f"duplicate receipt_id: {receipt_id}"})
                    elif isinstance(receipt_id, str):
                        seen.add(receipt_id)
    finally:
        lock.close()
    return {"valid": not issues, "receipt_count": count, "issue_count": len(issues), "issues": issues[:100]}


def parse_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid {label}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zero-preload five-domain runtime.")
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--facts-root", type=Path, default=DEFAULT_FACTS_ROOT)
    parser.add_argument("--receipts-root", type=Path, default=DEFAULT_RECEIPTS_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    classify_cmd = commands.add_parser("classify")
    classify_cmd.add_argument("--query", required=True)
    classify_cmd.add_argument("--domain", choices=DOMAINS)
    classify_cmd.add_argument("--project-id")
    context = commands.add_parser("context")
    context.add_argument("--query", required=True)
    context.add_argument("--domain", choices=DOMAINS)
    context.add_argument("--project-id")
    context.add_argument("--include", action="append", choices=("knowledge", "experience"), default=[])
    context.add_argument("--cass-workspace", type=Path)
    context.add_argument("--artifact-ref", action="append", default=[])
    context.add_argument("--char-budget", type=int, default=DEFAULT_CHAR_BUDGET)
    receipt = commands.add_parser("receipt")
    receipt.add_argument("--receipt-file", type=Path, required=True)
    commands.add_parser("validate-receipts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "classify":
            result = classify(args.query, explicit_domain=args.domain, project_id=args.project_id)
        elif args.command == "context":
            result = build_context_packet(
                args.query,
                project_id=args.project_id,
                explicit_domain=args.domain,
                include=set(args.include),
                projects_root=args.projects_root,
                facts_root=args.facts_root,
                cass_workspace=args.cass_workspace,
                artifact_refs=args.artifact_ref,
                char_budget=args.char_budget,
            )
        elif args.command == "receipt":
            if args.receipt_file.is_symlink() or not args.receipt_file.is_file():
                raise RuntimeError("--receipt-file must be a real file")
            payload = parse_object(args.receipt_file.read_text(encoding="utf-8"), "--receipt-file")
            receipt = normalize_receipt(payload)
            written = append_receipt(args.receipts_root, receipt)
            result = {"status": "appended" if written else "exists", "receipt": receipt}
        else:
            result = validate_receipt_inbox(args.receipts_root)
            print(pretty_json(result), end="")
            return 0 if result["valid"] else 1
        print(pretty_json(result), end="")
        return 0
    except (RuntimeError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
