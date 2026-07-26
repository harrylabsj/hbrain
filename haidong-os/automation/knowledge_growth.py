#!/usr/bin/env python3
"""Source-backed Hbrain auto-learning gate and previous-day report.

The script is deterministic and uses only the Python standard library. Agents do
the research; this gate validates metadata, prevents overwrites, writes either a
retrievable ``learning-candidate`` or a writeback proposal, and records an
append-only learning event. It never writes anchor-events.
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
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_WIKI_ROOT = Path("/Users/jianghaidong/hbrain/llm-wiki")
LAYERS = ("concepts", "entities", "queries", "practices", "comparisons")
TYPE_BY_LAYER = {
    "concepts": "concept",
    "entities": "entity",
    "queries": "query",
    "practices": "practice",
    "comparisons": "comparison",
}
AUTO_SOURCE_KINDS = ("official-primary", "local-evidence", "user-provided")
SOURCE_KINDS = AUTO_SOURCE_KINDS + ("secondary", "agent-synthesis")
MIN_AUTO_CONFIDENCE = 0.80


def parse_date(value: str | None, default: dt.date | None = None) -> dt.date:
    return dt.date.fromisoformat(value) if value else (default or dt.date.today())


def slugify(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|#%{}^~\[\]`]+", "-", value).strip(" .-")
    value = re.sub(r"\s+", "-", value)
    return value[:100] or "auto-learning"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().split(" #", 1)[0].strip("\"'")
    return values, text[end + 5 :]


def first_summary(body: str, limit: int = 180) -> str:
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "---", "```", "|")):
            continue
        line = re.sub(r"^[-*>\s]+", "", line)
        if line:
            return line[:limit]
    return "未提取到摘要"


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


def create_exclusive(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def append_event(path: Path, event: dict) -> tuple[dict, bool]:
    """Append under an exclusive lock; return an existing event on dedupe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        for line in handle:
            try:
                current = json.loads(line)
            except json.JSONDecodeError:
                continue
            if current.get("learning_id") == event["learning_id"]:
                return current, False
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event, True


def normalize_link(value: str) -> str:
    value = value.strip()
    if value.startswith("[[") and value.endswith("]]" ):
        value = value[2:-2]
    value = value.split("|", 1)[0].lstrip("/")
    if value.startswith("llm-wiki/"):
        value = value[len("llm-wiki/") :]
    return value[:-3] if value.endswith(".md") else value


def resolve_link(wiki_root: Path, value: str) -> str | None:
    slug = normalize_link(value)
    if not any(slug.startswith(layer + "/") for layer in LAYERS):
        return None
    path = (wiki_root / f"{slug}.md").resolve()
    try:
        path.relative_to(wiki_root.resolve())
    except ValueError:
        return None
    return slug if path.is_file() else None


def valid_source(source: str, kind: str) -> bool:
    if kind == "official-primary":
        parsed = urlparse(source)
        return parsed.scheme == "https" and bool(parsed.netloc)
    if kind == "local-evidence":
        return Path(source).is_absolute() and Path(source).exists()
    if kind == "user-provided":
        return source.startswith(("conversation:", "user-provided:"))
    return bool(source.strip())


def make_learning_id(title: str, question: str, sources: list[str]) -> str:
    value = json.dumps(
        {"title": title.strip(), "question": question.strip(), "sources": sorted(sources)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def index_candidate(path: Path, wiki_root: Path, gbrain_bin: str, source: str) -> dict:
    """Import exactly one candidate into Gbrain; never trigger a full-repo sync."""
    slug = path.relative_to(wiki_root).with_suffix("").as_posix()
    layer = slug.split("/", 1)[0]
    try:
        proc = subprocess.run(
            [
                gbrain_bin, "capture", "--file", str(path), "--slug", slug,
                "--type", TYPE_BY_LAYER.get(layer, "note"),
                "--source", source, "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "output": ((proc.stdout or "") + (proc.stderr or ""))[-1000:],
    }


def candidate_markdown(args: argparse.Namespace, day: dt.date, item_id: str, links: list[str]) -> str:
    body = (args.body or args.summary).strip()
    sources = "\n".join(f"- {item}" for item in args.source)
    connections = "\n".join(f"- [[{item}]]" for item in links)
    return f"""---
title: {args.title}
created: {day.isoformat()}
updated: {day.isoformat()}
type: {TYPE_BY_LAYER[args.target_layer]}
status: learning-candidate
confidence: {args.confidence:.2f}
provenance: agent-auto-learning
learning_id: {item_id}
source_kind: {args.source_kind}
sources: {json.dumps(args.source, ensure_ascii=False)}
tags: [auto-learned, needs-review]
---

# {args.title}

> Agent 自动学习候选：可以参与召回，但重要判断使用前必须核验来源。知识沉淀不等于人脑锚点训练，本页不会自动创建 `anchor:` 或 anchor-event。

## 知识点

{body}

## 触发这个知识缺口的问题

{args.question.strip()}

## 证据

{sources}

## 已有连接

{connections}

## 复核状态

- Agent：{args.agent}
- 风险：{args.risk}
- 置信度：{args.confidence:.2f}
- 待确认：准确性、时效性、是否应合并到已有页面。
"""


def proposal_markdown(
    args: argparse.Namespace, day: dt.date, item_id: str, links: list[str], blockers: list[str]
) -> str:
    body = (args.body or args.summary).strip()
    sources = "\n".join(f"- {item}" for item in args.source) or "- 无"
    connections = "\n".join(f"- [[{item}]]" for item in links) or "- 无"
    reasons = "\n".join(f"- {item}" for item in blockers) or "- 默认人工复核"
    return f"""---
title: {args.title}
created: {day.isoformat()}
type: writeback-proposal
status: proposed
target_layer: {args.target_layer}
provenance: agent-auto-learning
judgment_changed: {json.dumps(args.judgment_changed, ensure_ascii=False)}
learning_id: {item_id}
confidence: {args.confidence:.2f}
source_kind: {args.source_kind}
sources: {json.dumps(args.source, ensure_ascii=False)}
tags: [second-brain, writeback, auto-learning, needs-review]
---

# {args.title}

## 知识缺口

{args.question.strip()}

## 学习结果

{body}

## 来源

{sources}

## 已有连接

{connections}

## 未自动进入规范层的原因

{reasons}

## 复核纪律

复核后才可合并进规范知识；本提案不得自动产生人脑 anchor-event 或 `anchor:`。
"""


def capture(args: argparse.Namespace) -> dict:
    wiki_root = args.wiki_root.resolve()
    day = parse_date(args.date)
    args.source = [item.strip() for item in args.source if item.strip()]
    item_id = make_learning_id(args.title, args.question, args.source)
    ledger = wiki_root / "_meta" / "knowledge-learning" / f"{day:%Y-%m}.jsonl"
    for row in read_jsonl(ledger):
        if row.get("learning_id") == item_id:
            result = {**row, "recorded": False, "deduplicated": True}
            if args.index and row.get("status") == "canonical-candidate" and row.get("path"):
                result["index"] = index_candidate(
                    wiki_root / row["path"], wiki_root, args.gbrain_bin, args.gbrain_source
                )
            return result

    links = list(dict.fromkeys(filter(None, (resolve_link(wiki_root, item) for item in args.link))))
    blockers: list[str] = []
    if args.risk != "low":
        blockers.append(f"风险等级为 {args.risk}")
    if args.confidence < MIN_AUTO_CONFIDENCE:
        blockers.append(f"置信度低于 {MIN_AUTO_CONFIDENCE:.2f}")
    if args.source_kind not in AUTO_SOURCE_KINDS:
        blockers.append(f"来源类型 {args.source_kind} 只允许进入待审区")
    if not args.source or not all(valid_source(item, args.source_kind) for item in args.source):
        blockers.append("来源为空或与来源类型不匹配")
    if len(links) < 2:
        blockers.append("少于两个可解析的既有规范知识连接")

    target = wiki_root / args.target_layer / f"{slugify(args.title)}.md"
    if target.exists():
        blockers.append("同名规范知识页已存在，禁止自动覆盖")

    auto = args.auto_promote and not blockers
    if auto:
        artifact = target
        if not create_exclusive(artifact, candidate_markdown(args, day, item_id, links)):
            blockers.append("并发写入时同名页已经存在")
            auto = False
    if not auto:
        artifact = (
            wiki_root
            / "_meta"
            / "writeback-inbox"
            / f"{day.isoformat()}-auto-learning-{slugify(args.title)}-{item_id[:8]}.md"
        )
        create_exclusive(artifact, proposal_markdown(args, day, item_id, links, blockers))

    event = {
        "action": "knowledge-learned",
        "date": day.isoformat(),
        "learning_id": item_id,
        "title": args.title,
        "question": args.question,
        "summary": args.summary,
        "status": "canonical-candidate" if auto else "proposal",
        "path": str(artifact.relative_to(wiki_root)),
        "target_layer": args.target_layer,
        "sources": args.source,
        "source_kind": args.source_kind,
        "confidence": args.confidence,
        "risk": args.risk,
        "agent": args.agent,
        "links": links,
        "promotion_blockers": blockers,
        "review_required": True,
    }
    if auto and args.index:
        event["index"] = index_candidate(artifact, wiki_root, args.gbrain_bin, args.gbrain_source)
    else:
        event["index"] = {"status": "not-requested" if not args.index else "not-applicable"}
    saved, appended = append_event(ledger, event)
    return {**saved, "recorded": appended, "deduplicated": not appended}


def records_for_day(paths: list[Path], day: dt.date, action: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        for row in read_jsonl(path):
            if row.get("date") == day.isoformat() and (not action or row.get("action") == action):
                rows.append(row)
    return rows


def pages_for_day(wiki_root: Path, day: dt.date) -> tuple[list[dict], list[dict]]:
    created: list[dict] = []
    updated: list[dict] = []
    for layer in LAYERS:
        root = wiki_root / layer
        for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            item = {
                "title": meta.get("title") or path.stem,
                "path": str(path.relative_to(wiki_root)),
                "status": meta.get("status", "canonical"),
                "summary": meta.get("summary") or first_summary(body),
            }
            if meta.get("created") == day.isoformat():
                created.append(item)
            elif meta.get("updated") == day.isoformat():
                updated.append(item)
    return created, updated


def proposals_for_day(wiki_root: Path, day: dt.date) -> list[dict]:
    rows: list[dict] = []
    inbox = wiki_root / "_meta" / "writeback-inbox"
    for path in sorted(inbox.glob("*.md")) if inbox.is_dir() else []:
        if path.name == "README.md":
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get("created") == day.isoformat() and meta.get("status", "").split()[0] in {
            "proposed", "candidate"
        }:
            rows.append({"title": meta.get("title") or path.stem, "path": str(path.relative_to(wiki_root)), "summary": first_summary(body)})
    return rows


def build_report(wiki_root: Path, day: dt.date, generated: dt.date) -> tuple[str, dict]:
    learning = records_for_day(sorted((wiki_root / "_meta" / "knowledge-learning").glob("*.jsonl")), day)
    event_root = wiki_root / "_meta" / "knowledge-events"
    hits = records_for_day(sorted(event_root.glob("*.jsonl")), day, "knowledge-hit")
    misses = records_for_day(sorted((event_root / "knowledge-misses").glob("*.jsonl")), day)
    created, updated = pages_for_day(wiki_root, day)
    proposals = proposals_for_day(wiki_root, day)
    learned_paths = {row.get("path") for row in learning}
    manual_created = [row for row in created if row["path"] not in learned_paths]
    candidates = [row for row in learning if row.get("status") == "canonical-candidate"]
    learned_proposals = [row for row in learning if row.get("status") == "proposal"]
    hit_counts = Counter(row.get("slug") for row in hits if row.get("slug"))
    payload = {
        "report_for": day.isoformat(), "generated_on": generated.isoformat(),
        "new_canonical_pages": len(created), "auto_learning_candidates": len(candidates),
        "auto_learning_proposals": len(learned_proposals), "updated_pages": len(updated),
        "knowledge_hits": len(hits), "knowledge_misses": len(misses),
        "new_proposals": len(proposals),
    }
    lines = [
        "---", f"title: 第二大脑日报 {day.isoformat()}", f"created: {generated.isoformat()}",
        f"report_for: {day.isoformat()}", "type: automation-report", "status: generated",
        "tags: [second-brain, daily-report, knowledge-growth]", "---", "",
        f"# 第二大脑日报｜{day.isoformat()}", "",
        f"> 新增规范知识 {len(created)} 个，其中 Agent 自动学习候选 {len(candidates)} 个；新增待审学习 {len(learned_proposals)} 个；发现知识缺口 {len(misses)} 个。", "",
        "## 概览", "", "| 指标 | 数量 |", "|---|---:|",
        f"| 新增规范知识页 | {len(created)} |", f"| Agent 自动学习候选 | {len(candidates)} |",
        f"| Agent 学习待审项 | {len(learned_proposals)} |", f"| 更新规范知识页 | {len(updated)} |",
        f"| 知识调用事件 | {len(hits)} |", f"| 新知识缺口 | {len(misses)} |",
        f"| 当日新增待审提案 | {len(proposals)} |", "", "## 昨日新增知识点", "",
    ]
    for row in candidates:
        lines += [f"### {row.get('title', '未命名')}", "", f"- 核心：{row.get('summary', '未提供')}", f"- 状态：自动学习候选；置信度 {row.get('confidence', '-')}", f"- 来源：{', '.join(row.get('sources', [])) or '未记录'}", f"- 文件：`{row.get('path', '-')}`", ""]
    for row in manual_created:
        lines += [f"### {row['title']}", "", f"- 核心：{row['summary']}", f"- 状态：{row['status']}", f"- 文件：`{row['path']}`", ""]
    if not candidates and not manual_created:
        lines += ["- 无。", ""]
    lines += ["## Agent 自动学习待审区", ""]
    for row in learned_proposals:
        lines += [f"- **{row.get('title', '未命名')}**：{row.get('summary', '未提供')}", f"  - 原因：{'；'.join(row.get('promotion_blockers', [])) or '待复核'}", f"  - 提案：`{row.get('path', '-')}`"]
    if not learned_proposals:
        lines.append("- 无。")
    lines += ["", "## 昨日更新的知识页", ""] + ([f"- **{row['title']}**：`{row['path']}`" for row in updated] or ["- 无。"])
    lines += ["", "## 新发现的知识缺口", ""] + ([f"- {row.get('query', '未记录')}（来源：{row.get('source', 'unknown')}）" for row in misses] or ["- 无。"])
    lines += ["", "## 昨日被调用的知识", ""] + ([f"- `[[{slug}]]`：{count} 次" for slug, count in hit_counts.most_common(10)] or ["- 无。"])
    lines += ["", "## 今日复核建议", "", f"- 复核自动学习待审项 {len(learned_proposals)} 个、当日新增提案 {len(proposals)} 个。", "- `learning-candidate` 可召回，但重要判断必须打开原页核验来源与置信度。", "- 本日报不创建人脑 anchor-event，也不修改 `weight`、`last_active` 或 `hot`。", ""]
    return "\n".join(lines), payload


def daily_report(args: argparse.Namespace) -> dict:
    generated = parse_date(args.report_date)
    day = parse_date(args.for_date, generated - dt.timedelta(days=1))
    text, payload = build_report(args.wiki_root.resolve(), day, generated)
    output = Path(args.output).expanduser() if args.output else args.wiki_root.resolve() / "_meta" / "daily-reports" / f"{day.isoformat()}-第二大脑日报.md"
    if not args.no_write:
        atomic_replace(output, text)
    return {**payload, "report_path": str(output), "written": not args.no_write}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hbrain automatic knowledge growth")
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--question", required=True); cap.add_argument("--title", required=True)
    cap.add_argument("--summary", required=True)
    body = cap.add_mutually_exclusive_group(); body.add_argument("--body"); body.add_argument("--body-file", type=Path)
    cap.add_argument("--source", action="append", required=True); cap.add_argument("--source-kind", choices=SOURCE_KINDS, required=True)
    cap.add_argument("--target-layer", choices=LAYERS, default="queries"); cap.add_argument("--confidence", type=float, required=True)
    cap.add_argument("--risk", choices=("low", "medium", "high"), default="low"); cap.add_argument("--link", action="append", default=[])
    cap.add_argument("--agent", default="unknown-agent"); cap.add_argument("--judgment-changed", default="待人工判别")
    cap.add_argument("--auto-promote", action="store_true"); cap.add_argument("--date")
    cap.add_argument("--index", action="store_true", help="incrementally import only the new candidate into Gbrain")
    cap.add_argument("--gbrain-bin", default=os.environ.get("GBRAIN_BIN", "gbrain"))
    cap.add_argument("--gbrain-source", default="hbrain")
    report = sub.add_parser("daily-report")
    report.add_argument("--for-date"); report.add_argument("--report-date"); report.add_argument("--output")
    report.add_argument("--no-write", action="store_true"); report.add_argument("--json", action="store_true")
    return parser


def main() -> None:
    parser = build_parser(); args = parser.parse_args()
    if args.command == "capture":
        if not 0 <= args.confidence <= 1:
            parser.error("--confidence must be between 0 and 1")
        if args.body_file:
            args.body = args.body_file.read_text(encoding="utf-8")
        result = capture(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)); return
    result = daily_report(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)); return
    print(f"🧠 第二大脑日报｜{result['report_for']}")
    print(f"新增规范知识：{result['new_canonical_pages']}（自动学习候选 {result['auto_learning_candidates']}）")
    print(f"新增待审学习：{result['auto_learning_proposals']}；更新知识页：{result['updated_pages']}")
    print(f"知识调用：{result['knowledge_hits']}；新缺口：{result['knowledge_misses']}")
    print(f"报告：{result['report_path']}")
    print("候选可召回，但重要判断仍需核验；未产生人脑锚点事件。")


if __name__ == "__main__":
    main()
