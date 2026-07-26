#!/usr/bin/env python3
"""Five-domain daily report for the Haidong cognitive system (stage-4 minimal slice).

This is a derived, read-only view over the five domains. It never modifies the
Project Registry, Fact Ledger, canonical wiki pages, or CASS, and it never
promotes anything: every line in the report remains proposal-only with
``auto_promote=false``. Standard library only.

Data sources (only the month/day files for the reported date are read):

- facts:    ``<facts-root>/events/YYYY-MM.jsonl`` (occurred_at on the day) and
            ``<facts-root>/inbox/YYYY-MM-DD.jsonl`` (pending proposals)
- projects: ``<projects-root>/audit/applied.jsonl`` rows whose
            ``evidence.fact_id`` points at a fact that occurred on the day
- knowledge: ``<wiki-root>/_meta/knowledge-learning/YYYY-MM.jsonl`` plus
            knowledge hits under ``_meta/knowledge-events/``
- experience: ``experience_candidate`` of the day's completion receipts
- evidence: receipt ``evidence`` refs and fact ``source_ref`` values
- gaps:     receipt ``knowledge_gap`` plus ``_meta/knowledge-events/knowledge-misses/``
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


AUTOMATION_ROOT = Path(__file__).resolve().parent
DEFAULT_WIKI_ROOT = Path("/Users/jianghaidong/hbrain/llm-wiki")
DEFAULT_FACTS_ROOT = Path("/Users/jianghaidong/hbrain/facts")
DEFAULT_PROJECTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/projects")
DEFAULT_RECEIPTS_ROOT = Path("/Users/jianghaidong/hbrain/haidong-os/receipts")
DEFAULT_OUTPUT_ROOT = AUTOMATION_ROOT.parent / "reports" / "five-domain-daily"

SCHEMA_VERSION = 1
MAX_ITEMS = 20
MAX_LINE_CHARS = 300
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"\s*(?:::|=|:)\s*\S+|\bBearer\s+[A-Za-z0-9._~+/=-]+"
)


class DailyError(Exception):
    pass


def parse_date(value: str | None, default: dt.date | None = None) -> dt.date:
    return dt.date.fromisoformat(value) if value else (default or dt.date.today())


def day_of(value: Any) -> str | None:
    """Tolerant RFC3339 -> YYYY-MM-DD; returns None for unparseable values.

    Aware datetimes are converted to local time first so the reported calendar
    day matches the wall-clock day in the runtime's local zone. Naive datetimes
    keep their existing calendar date (fail-open for legacy data).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None and parsed.tzinfo.utcoffset(parsed) is not None:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def read_jsonl(path: Path, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read a JSONL file tolerantly; invalid JSON lines are counted as issues."""
    rows: list[dict[str, Any]] = []
    if path.is_symlink() or not path.is_file():
        return rows
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        issues.append({"path": str(path), "line": None, "issue": f"unreadable: {exc}"})
        return rows
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append({"path": str(path), "line": line_number, "issue": f"invalid_json: {exc.msg}"})
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def sanitize_text(text: str) -> tuple[str, int]:
    return SECRET_RE.subn("[REDACTED]", text)


def truncate(text: str, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compact(value: Any, limit: int = MAX_LINE_CHARS) -> str:
    if isinstance(value, str):
        return truncate(value, limit)
    try:
        return truncate(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)
    except (TypeError, ValueError):
        return truncate(str(value), limit)


def capped(items: list[Any], limit: int = MAX_ITEMS) -> tuple[list[Any], int]:
    return items[:limit], max(0, len(items) - limit)


def facts_for_day(
    facts_root: Path, day: dt.date, issues: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    month_path = facts_root / "events" / f"{day:%Y-%m}.jsonl"
    events = [
        row
        for row in read_jsonl(month_path, issues)
        if day_of(row.get("occurred_at")) == day.isoformat()
    ]
    events.sort(key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("event_id") or "")))
    proposals = read_jsonl(facts_root / "inbox" / f"{day.isoformat()}.jsonl", issues)
    proposals.sort(key=lambda row: str(row.get("proposal_id") or ""))
    return events, proposals


def project_changes_for_day(
    projects_root: Path, day_fact_ids: set[str], issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for row in read_jsonl(projects_root / "audit" / "applied.jsonl", issues):
        evidence = row.get("evidence")
        if isinstance(evidence, dict) and evidence.get("fact_id") in day_fact_ids:
            rows.append(row)
    rows.sort(key=lambda row: (str(row.get("project_id") or ""), str(row.get("proposal_id") or "")))
    return rows


def knowledge_for_day(wiki_root: Path, day: dt.date, issues: list[dict[str, Any]]) -> dict[str, Any]:
    meta = wiki_root / "_meta"
    learning = [
        row
        for row in read_jsonl(meta / "knowledge-learning" / f"{day:%Y-%m}.jsonl", issues)
        if row.get("date") == day.isoformat()
    ]
    hits = [
        row
        for row in read_jsonl(meta / "knowledge-events" / f"{day:%Y-%m}.jsonl", issues)
        if row.get("date") == day.isoformat() and row.get("action") == "knowledge-hit"
    ]
    misses = [
        row
        for row in read_jsonl(meta / "knowledge-events" / "knowledge-misses" / f"{day:%Y-%m}.jsonl", issues)
        if row.get("date") == day.isoformat()
    ]
    return {
        "candidates": [row for row in learning if row.get("status") == "canonical-candidate"],
        "proposals": [row for row in learning if row.get("status") == "proposal"],
        "hits": hits,
        "misses": misses,
    }


def receipts_for_day(receipts_root: Path, day: dt.date, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    month_path = receipts_root / "inbox" / f"{day:%Y-%m}.jsonl"
    rows = [
        row
        for row in read_jsonl(month_path, issues)
        if day_of(row.get("completed_at")) == day.isoformat()
    ]
    rows.sort(key=lambda row: (str(row.get("completed_at") or ""), str(row.get("receipt_id") or "")))
    return rows


def flatten(receipts: list[dict[str, Any]], field: str) -> list[Any]:
    items: list[Any] = []
    for receipt in receipts:
        value = receipt.get(field)
        if isinstance(value, list):
            items.extend(value)
    return items


def build_report(
    *,
    day: dt.date,
    generated: dt.date,
    wiki_root: Path,
    facts_root: Path,
    projects_root: Path,
    receipts_root: Path,
) -> tuple[str, dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    events, fact_proposals = facts_for_day(facts_root, day, issues)
    day_fact_ids = {row.get("event_id") for row in events if isinstance(row.get("event_id"), str)}
    project_changes = project_changes_for_day(projects_root, day_fact_ids, issues)
    knowledge = knowledge_for_day(wiki_root, day, issues)
    receipts = receipts_for_day(receipts_root, day, issues)
    experience = flatten(receipts, "experience_candidate")
    receipt_gaps = flatten(receipts, "knowledge_gap")
    evidence_items: list[str] = []
    for receipt in receipts:
        for item in receipt.get("evidence") or []:
            if isinstance(item, dict):
                evidence_items.append(f"receipt:{item.get('type', '-')}: {item.get('ref', '-')}")
    for event in events:
        evidence_items.append(f"fact:{event.get('event_id', '-')}: {event.get('source_ref', '-')}")

    redacted = 0

    def line(text: str) -> str:
        nonlocal redacted
        clean, count = sanitize_text(text)
        redacted += count
        return clean

    def section(rows: list[str], truncated_count: int) -> list[str]:
        out = rows or ["- 无。"]
        if truncated_count:
            out.append(f"- …另有 {truncated_count} 条未展示（每域最多展示 {MAX_ITEMS} 条）。")
        return out + [""]

    lines = [
        "---",
        f"title: 海东认知系统五域日报 {day.isoformat()}",
        f"created: {generated.isoformat()}",
        f"report_for: {day.isoformat()}",
        "type: automation-report",
        "status: generated",
        "proposal_only: true",
        "auto_promote: false",
        "tags: [haidong-os, five-domain, daily-report]",
        "---",
        "",
        f"# 海东认知系统五域日报｜{day.isoformat()}",
        "",
        "> 派生视图：本报告不修改 Project Registry、Fact Ledger、Wiki 规范页或 CASS，"
        "不自动晋升任何内容；全部条目保持 proposal-only，auto_promote=false。",
        "",
        "## 概览",
        "",
        "| 域 | 数量 |",
        "|---|---:|",
        f"| 事实（正式 {len(events)} / 待审 {len(fact_proposals)}） | {len(events) + len(fact_proposals)} |",
        f"| 项目变化 | {len(project_changes)} |",
        f"| 知识（候选 {len(knowledge['candidates'])} / 待审 {len(knowledge['proposals'])} / 调用 {len(knowledge['hits'])}） | "
        f"{len(knowledge['candidates']) + len(knowledge['proposals']) + len(knowledge['hits'])} |",
        f"| 经验候选 | {len(experience)} |",
        f"| 证据 | {len(evidence_items)} |",
        f"| 知识缺口 | {len(receipt_gaps) + len(knowledge['misses'])} |",
        "",
        "## 事实",
        "",
    ]
    fact_rows = [
        line(
            f"- `{event.get('event_id', '-')}` {compact(event.get('summary'))}"
            f"（{event.get('verification', '-')}；来源：`{compact(event.get('source_ref'), 120)}`）"
        )
        for event in events
    ] + [
        line(f"- `{row.get('proposal_id', '-')}` {compact(row.get('summary'))}（待审）")
        for row in fact_proposals
    ]
    shown, extra = capped(fact_rows)
    lines += section(shown, extra)

    lines += ["## 项目", ""]
    shown, extra = capped(project_changes)
    lines += section(
        [
            line(
                f"- `{row.get('project_id', '-')}` 变更 {compact(sorted((row.get('changes') or {}).keys()), 120)}"
                f"（提案 `{row.get('proposal_id', '-')}`；依据事实 `{row.get('evidence', {}).get('fact_id', '-')}`）"
            )
            for row in shown
        ],
        extra,
    )

    lines += ["## 知识", ""]
    knowledge_rows = [
        line(f"- 候选：**{compact(row.get('title'), 120)}**（置信度 {row.get('confidence', '-')}；`{row.get('path', '-')}`）")
        for row in knowledge["candidates"]
    ] + [
        line(f"- 待审：**{compact(row.get('title'), 120)}**（`{row.get('path', '-')}`）")
        for row in knowledge["proposals"]
    ]
    shown, extra = capped(knowledge_rows)
    lines += section(shown, extra)
    hit_count = len(knowledge["hits"])
    lines += [f"- 知识调用事件：{hit_count} 次。", ""]
    lines += ["### 知识缺口", ""]
    gap_rows = [
        line(f"- {compact(item)}（来源：receipt knowledge_gap）") for item in receipt_gaps
    ] + [
        line(f"- {compact(row.get('query', '未记录'))}（来源：knowledge-miss/{row.get('source', 'unknown')}）")
        for row in knowledge["misses"]
    ]
    shown, extra = capped(gap_rows)
    lines += section(shown, extra)

    lines += ["## 经验", ""]
    shown, extra = capped(experience)
    lines += section([line(f"- {compact(item)}") for item in shown], extra)

    lines += ["## 证据", ""]
    shown, extra = capped(evidence_items)
    lines += section([line(f"- {item}") for item in shown], extra)

    lines += ["## 数据问题", ""]
    if issues:
        for issue in issues[:MAX_ITEMS]:
            where = f"{issue['path']}:{issue['line']}" if issue.get("line") else str(issue["path"])
            lines.append(f"- `{where}` {issue['issue']}")
        if len(issues) > MAX_ITEMS:
            lines.append(f"- …另有 {len(issues) - MAX_ITEMS} 条未展示。")
    else:
        lines.append("- 无。")
    lines += [
        "",
        "## 纪律",
        "",
        "- 本报告是派生视图，可从 Fact Ledger / Project Registry / receipts / knowledge-learning 重建，不是新的事实源。",
        "- 所有待审项保持 proposal-only；本报告不做自动晋升，不创建人脑 anchor-event，不修改 `weight`、`last_active` 或 `hot`。",
        f"- 秘密样式内容已脱敏 {redacted} 处。",
        "",
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_for": day.isoformat(),
        "generated_on": generated.isoformat(),
        "proposal_only": True,
        "auto_promote": False,
        "domains": {
            "facts": {"events": len(events), "proposals": len(fact_proposals)},
            "projects": {"changes": len(project_changes)},
            "knowledge": {
                "candidates": len(knowledge["candidates"]),
                "proposals": len(knowledge["proposals"]),
                "hits": len(knowledge["hits"]),
            },
            "experience": {"candidates": len(experience)},
            "evidence": {"items": len(evidence_items)},
            "knowledge_gaps": {"receipt_gaps": len(receipt_gaps), "misses": len(knowledge["misses"])},
        },
        "max_items_per_domain": MAX_ITEMS,
        "issue_count": len(issues),
        "issues": issues[:100],
        "redacted_values": redacted,
    }
    return "\n".join(lines), payload


def atomic_write_report(path: Path, text: str) -> None:
    if path.is_symlink():
        raise DailyError(f"output path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DailyError(f"output path must not be a symlink: {path}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Haidong five-domain daily report (derived view, proposal-only).")
    parser.add_argument("--for-date", help="reported day (default: previous local calendar day)")
    parser.add_argument("--report-date", help="generation date (default: today)")
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--facts-root", type=Path, default=DEFAULT_FACTS_ROOT)
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--receipts-root", type=Path, default=DEFAULT_RECEIPTS_ROOT)
    parser.add_argument("--output", help="report path (default: haidong-os/reports/five-domain-daily/<day>-五域日报.md)")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        generated = parse_date(args.report_date)
        day = parse_date(args.for_date, generated - dt.timedelta(days=1))
        wiki_root = args.wiki_root.expanduser().absolute()
        facts_root = args.facts_root.expanduser().absolute()
        projects_root = args.projects_root.expanduser().absolute()
        receipts_root = args.receipts_root.expanduser().absolute()
        for label, root in (
            ("wiki-root", wiki_root),
            ("facts-root", facts_root),
            ("projects-root", projects_root),
            ("receipts-root", receipts_root),
        ):
            if root.is_symlink():
                raise DailyError(f"{label} must not be a symlink: {root}")
        text, payload = build_report(
            day=day,
            generated=generated,
            wiki_root=wiki_root,
            facts_root=facts_root,
            projects_root=projects_root,
            receipts_root=receipts_root,
        )
        output = (
            Path(args.output).expanduser()
            if args.output
            else DEFAULT_OUTPUT_ROOT / f"{day.isoformat()}-五域日报.md"
        )
        written = not args.no_write
        if written:
            atomic_write_report(output, text)
        payload["report_path"] = str(output)
        payload["written"] = written
    except (DailyError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)[:1000]}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    domains = payload["domains"]
    print(f"🧭 五域日报｜{payload['report_for']}")
    print(f"事实：{domains['facts']['events']}（待审 {domains['facts']['proposals']}）；项目变化：{domains['projects']['changes']}")
    print(
        f"知识：候选 {domains['knowledge']['candidates']} / 待审 {domains['knowledge']['proposals']} / 调用 {domains['knowledge']['hits']}；"
        f"经验候选：{domains['experience']['candidates']}"
    )
    print(f"证据：{domains['evidence']['items']}；知识缺口：{domains['knowledge_gaps']['receipt_gaps'] + domains['knowledge_gaps']['misses']}；数据问题：{payload['issue_count']}")
    print(f"报告：{payload['report_path']}{'（未写入）' if not payload['written'] else ''}")
    print("派生视图：proposal-only，auto_promote=false，未修改任何域。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
