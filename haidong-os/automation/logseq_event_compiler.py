#!/usr/bin/env python3
"""Compile explicitly marked Logseq blocks into deterministic fact proposals.

This tool is intentionally dry-run only. It reads one caller-selected journal,
does not follow links or symlinks, and never writes the formal Fact Ledger.
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
from typing import Any


BLOCK_RE = re.compile(r"^(?P<indent>[ \t]*)-\s+(?P<body>.*)$")
PROPERTY_RE = re.compile(r"^\s*(?P<key>[\w-]+)::\s*(?P<value>.*)\s*$", re.IGNORECASE)
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"(\s*(?:::|=|:)\s*)(.+)$"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
PRIVATE_KEY_RE = re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----")
VALID_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class CompileError(Exception):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def redact(text: str) -> str:
    if PRIVATE_KEY_RE.search(text):
        return "[REDACTED SENSITIVE EVENT]"
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    return SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)


def infer_day(path: Path, explicit: str | None) -> str:
    if explicit:
        candidate = explicit
    else:
        candidate = path.stem.replace("_", "-")
    try:
        return dt.date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise CompileError("--date is required when the journal filename is not YYYY-MM-DD.md") from exc


def safe_journal(path: Path, allowed_root: Path) -> Path:
    if allowed_root.is_symlink() or not allowed_root.is_dir():
        raise CompileError("--allowed-root must be a real directory, not a symlink")
    lexical_root = allowed_root.absolute()
    lexical_path = path.absolute()
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise CompileError("journal is outside --allowed-root") from exc
    cursor = lexical_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CompileError("journal path contains a symlink component")
    if not path.exists() or not path.is_file():
        raise CompileError("--journal must be one existing file")
    if path.suffix.lower() != ".md":
        raise CompileError("--journal must be a Markdown file")
    resolved_root = allowed_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if resolved_root not in resolved_path.parents:
        raise CompileError("journal resolves outside --allowed-root")
    return resolved_path


def block_ranges(lines: list[str]) -> list[tuple[int, int, int, str]]:
    starts: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = BLOCK_RE.match(line)
        if match:
            starts.append((index, len(match.group("indent").expandtabs(4)), match.group("body").strip()))
    ranges: list[tuple[int, int, int, str]] = []
    for position, (start, indent, body) in enumerate(starts):
        end = len(lines)
        for next_start, next_indent, _ in starts[position + 1 :]:
            if next_indent <= indent:
                end = next_start
                break
        ranges.append((start, end, indent, body))
    return ranges


def compile_blocks(
    journal: Path,
    *,
    day: str,
    project_id: str | None,
    privacy: str,
) -> list[dict[str, Any]]:
    lines = journal.read_text(encoding="utf-8").splitlines()
    candidates: list[dict[str, Any]] = []
    for start, end, _indent, body in block_ranges(lines):
        properties: dict[str, str] = {}
        fence: str | None = None
        for line in lines[start + 1 : end]:
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                marker = stripped[:3]
                fence = None if fence == marker else marker
                continue
            # A nested bullet starts a child block. Its properties belong to
            # that child and must never mark the parent for compilation.
            if fence is None and BLOCK_RE.match(line):
                break
            if fence is not None:
                continue
            prop = PROPERTY_RE.match(line)
            if prop:
                properties[prop.group("key").lower()] = prop.group("value").strip()
        if properties.get("compile", "").lower() != "yes":
            continue
        summary = redact(body).strip()
        if not summary:
            summary = "Logseq marked event"
        effective_project = properties.get("project-id") or project_id
        if effective_project and not VALID_PROJECT_ID.fullmatch(effective_project):
            raise CompileError(f"invalid project id at line {start + 1}")
        source_ref = f"{journal}#line:{start + 1}"
        identity = {
            "occurred_at": day,
            "summary": summary,
            "source_ref": source_ref,
            "project_id": effective_project,
            "privacy": privacy,
        }
        candidate = {
            "candidate_id": "proposal_" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24],
            "status": "proposed",
            **identity,
        }
        candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item["source_ref"], item["candidate_id"]))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_report(output_dir: Path, journal: Path, day: str, candidates: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink():
        raise CompileError("output directory symlinks are not allowed")
    lock_path = output_dir / ".compile.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        source_path = output_dir / ".source.json"
        source_record = {"journal": str(journal)}
        if source_path.exists() and load_source(source_path) != source_record:
            raise CompileError("output directory is already bound to a different journal")
        jsonl = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in candidates)
        lines = [
            "# Logseq 事件候选（dry-run）",
            "",
            f"- 日期：{day}",
            f"- 来源：`{journal}`",
            f"- 候选数：{len(candidates)}",
            "- 状态：仅候选，未写入 Fact Ledger",
            "",
        ]
        if candidates:
            lines.extend(f"- `{row['candidate_id']}` {row['summary']}（`{row['source_ref']}`）" for row in candidates)
        else:
            lines.append("- 无显式 `compile:: yes` 事件")
        lines.append("")
        atomic_write(output_dir / "candidates.jsonl", jsonl)
        atomic_write(output_dir / "report.md", "\n".join(lines))
        atomic_write(source_path, json.dumps(source_record, ensure_ascii=False, sort_keys=True) + "\n")


def load_source(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompileError(f"invalid output source binding: {exc}") from exc
    if not isinstance(value, dict):
        raise CompileError("invalid output source binding")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run compiler for explicitly marked Logseq events.")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--allowed-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--date")
    parser.add_argument("--project-id")
    parser.add_argument("--privacy", choices=("private", "sensitive", "shareable"), default="private")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        journal = safe_journal(args.journal, args.allowed_root)
        if args.project_id and not VALID_PROJECT_ID.fullmatch(args.project_id):
            raise CompileError("invalid --project-id")
        day = infer_day(journal, args.date)
        candidates = compile_blocks(
            journal,
            day=day,
            project_id=args.project_id,
            privacy=args.privacy,
        )
        write_report(args.output_dir, journal, day, candidates)
        print(json.dumps({"candidates": len(candidates), "dry_run": True}, sort_keys=True))
        return 0
    except (CompileError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
