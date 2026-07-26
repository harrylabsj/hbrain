#!/usr/bin/env python3
"""Deterministic helpers for the Hbrain cognitive loop."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_WIKI_ROOT = Path("/Users/jianghaidong/hbrain/llm-wiki")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
ANCHOR_ROLE_STATUSES = ("candidate", "active", "merge-candidate", "retired")
LIFECYCLE_STATUSES = ("candidate", "active", "hot", "merge-candidate", "retired")
ANCHOR_SCAN_DIRS = ("concepts", "entities", "practices", "queries")
ANCHOR_BUDGET = 30
PROVENANCE_VALUES = ("人脑原创", "对话合成", "AI生成")
WHITELIST_AUTOFIXES = ("frontmatter-status-enum", "frontmatter-null-date", "jsonl-format-report")
OPEN_WRITEBACK_STATUSES = {"", "proposed", "pending", "accepted"}
GOVERNANCE_AUDIT_SKIP_FILES = {"log.md", "第二大脑体系改进清单.md"}
GOVERNANCE_AUDIT_SKIP_PREFIXES = (
    "_meta/anchor-governance/",
    "_meta/automation-runs/",
    "_meta/health-check-",
    "_meta/log-archive",
    "_meta/maintenance-repair-",
)
RECALL_RESPONSE_BEGIN = "<!-- BEGIN_RECALL_RESPONSE -->"
RECALL_RESPONSE_END = "<!-- END_RECALL_RESPONSE -->"
RECALL_RATING_LABELS = {"✅": "full", "⚠️": "partial", "❌": "diverged"}
RECALL_ALIGNED_MATCHES = {"aligned", "full"}
REVIEW_INTERVAL_DAYS = (1, 3, 7, 14, 30)
MORNING_RECALL_RECENT_EXCLUDE_DAYS = 7
GRADUATE_RECENT_ALIGNED = 3
HUMAN_EVENT_ACTIONS = {"used", "recall-check"}
HUMAN_EVENT_SOURCES = ("anchor-morning-recall", "router-miss", "human-nomination")
# agent-chat 来源：AI 把某页当材料取用/沉淀写页，不入人脑面账本。
# 注意：hbrain-recall-validation / hbrain-router* 是自动化的人脑面事件
# （recall-check 训练测量、router 需求信号），不属于此集合。
NON_HUMAN_EVENT_SOURCES = {
    "codex",
    "codex-chat",
    "claude-chat",
    "claude-code",
    "claude-code-chat",
}
TRAINING_EVENT_ACTIONS = {"recall-check", "morning-recall"}


def is_human_facing_event(event: dict) -> bool:
    """统一判据：是否计入 weight/hot 派生与人脑面账本。
    训练/测量动作（recall-check/morning-recall）永远算人脑面；其余动作
    （used/closed-loop/maintenance/… 及未来任何 agent 动作）只要 source 属
    agent-chat 即非人脑面——属自动化记录，应进 _meta/automation-runs/ 而非
    anchor-events 账本，且不灌 weight/hot。"""
    action = str(event.get("action") or "").strip()
    source = str(event.get("source") or "").strip()
    if action in TRAINING_EVENT_ACTIONS:
        return True
    if source in NON_HUMAN_EVENT_SOURCES:
        return False
    return True


CAPSULE_BACK_FIELD_LABELS = {
    "one_sentence": "一句话",
    "minimal_model": "最小模型",
    "why_true": "为什么成立",
    "key_distinction": "关键区分",
}
CAPSULE_FIELD_ALIASES = {
    "trigger": ("TRIGGER", "Trigger", "触发", "触发器", "召回线索"),
    "one_sentence": ("一句话", "一句话理解", "一句话总结", "一句话框架"),
    "minimal_model": ("最小模型", "我的大脑里必须记住的最小模型"),
    "why_true": ("为什么成立", "为什么它成立"),
    "key_distinction": ("关键区分", "关键区别", "X不是Y"),
}


def parse_date(value: str | None) -> dt.date:
    if value:
        return dt.date.fromisoformat(value)
    return today_local()


def today_local() -> dt.date:
    return dt.datetime.now(LOCAL_TZ).date()


def parse_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def parse_optional_date(value: object) -> dt.date | None:
    if value in {None, "", "null", "None"}:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def month_path(wiki_root: Path, day: dt.date) -> Path:
    return wiki_root / "_meta" / "anchor-events" / f"{day:%Y-%m}.jsonl"


def ensure_dirs(wiki_root: Path) -> None:
    anchor_dir = wiki_root / "_meta" / "anchor-events"
    inbox_dir = wiki_root / "_meta" / "writeback-inbox"
    governance_dir = wiki_root / "_meta" / "anchor-governance"
    anchors_meta_dir = wiki_root / "_meta" / "anchors"
    automation_dir = wiki_root / "_meta" / "automation-runs"
    judgment_dir = wiki_root / "_meta" / "judgment-log"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    (anchor_dir / "router-misses").mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (governance_dir / "weekly").mkdir(parents=True, exist_ok=True)
    (governance_dir / "monthly").mkdir(parents=True, exist_ok=True)
    (governance_dir / "events").mkdir(parents=True, exist_ok=True)
    (governance_dir / "frontmatter").mkdir(parents=True, exist_ok=True)
    (governance_dir / "mechanisms").mkdir(parents=True, exist_ok=True)
    (governance_dir / "recall-checks").mkdir(parents=True, exist_ok=True)
    (governance_dir / "router").mkdir(parents=True, exist_ok=True)
    (governance_dir / "governance").mkdir(parents=True, exist_ok=True)
    anchors_meta_dir.mkdir(parents=True, exist_ok=True)
    automation_dir.mkdir(parents=True, exist_ok=True)
    judgment_dir.mkdir(parents=True, exist_ok=True)

    miss_readme = anchor_dir / "router-misses" / "README.md"
    if not miss_readme.exists():
        miss_readme.write_text(
            "# Router Misses\n\n"
            "Append-only JSONL log of routed questions that matched no anchor "
            "(`route --record` with zero matches). Monthly governance clusters these "
            "records by shared theme; clusters with >=3 misses become `candidate` "
            "anchor proposals in the monthly merge/retire review. Proposals only — "
            "creating a new anchor still requires human confirmation and the B2 budget "
            "(canonical anchor: active <= 30).\n\n"
            "Schema:\n\n"
            "```json\n"
            "{\"date\":\"YYYY-MM-DD\",\"query\":\"...\",\"source\":\"hbrain-router\"}\n"
            "```\n",
            encoding="utf-8",
        )

    anchor_readme = anchor_dir / "README.md"
    if not anchor_readme.exists():
        anchor_readme.write_text(
            "# Anchor Events\n\n"
            "Append-only JSONL event log for cognitive-anchor usage. "
            "Daily and weekly jobs aggregate these records before updating dashboards.\n\n"
            "Schema:\n\n"
            "```json\n"
            "{\"date\":\"YYYY-MM-DD\",\"anchor\":\"认知锚点\",\"source\":\"codex-chat\","
            "\"action\":\"used\",\"weight_delta\":1,\"summary\":\"short reason\"}\n"
            "```\n",
            encoding="utf-8",
        )

    inbox_readme = inbox_dir / "README.md"
    if not inbox_readme.exists():
        inbox_readme.write_text(
            "# Writeback Inbox\n\n"
            "Human-review queue for durable second-brain writeback proposals. "
            "Conversation closeouts should create proposal files here instead of directly "
            "rewriting concepts, queries, practices, or core anchors.\n\n"
            "Suggested status values: `proposed`, `accepted`, `done`, `rejected`, `superseded`.\n",
            encoding="utf-8",
        )

    governance_readme = governance_dir / "README.md"
    if not governance_readme.exists():
        governance_readme.write_text(
            "# Anchor Governance\n\n"
            "Lifecycle and governance reports for cognitive anchors.\n\n"
            "Allowed lifecycle states:\n\n"
            "- `candidate`: worth tracking, but not yet stable enough to become a core route.\n"
            "- `active`: stable anchor that can be used by the first brain and agents.\n"
            "- `hot`: derived from recent anchor-events and shown in _meta/anchors/index.md; not a durable frontmatter role.\n"
            "- `merge-candidate`: likely overlaps with another anchor; monthly review only.\n"
            "- `retired`: no longer a useful entry point; kept for history and redirects.\n\n"
            "Weekly jobs may identify thin, dormant, duplicate-like, and output-ready anchors. "
            "Monthly jobs may propose merge or retirement actions once per calendar month. "
            "Neither job should directly merge, delete, or retire anchor files.\n",
            encoding="utf-8",
        )

    judgment_readme = judgment_dir / "README.md"
    if not judgment_readme.exists():
        judgment_readme.write_text(
            "# Judgment Log\n\n"
            "Important judgments and predictions with stance, confidence, and review dates. "
            "Use this to test whether the second brain improves judgment rather than only storing notes.\n\n"
            "JSONL schema:\n\n"
            "```json\n"
            "{\"date\":\"YYYY-MM-DD\",\"topic\":\"...\",\"stance\":\"...\","
            "\"confidence\":0.7,\"review_date\":\"YYYY-MM-DD\",\"anchors\":[\"第二大脑\"]}\n"
            "```\n",
            encoding="utf-8",
        )


def slugify(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|#%{}^~\[\]`]+", "-", value).strip(" .-")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:80] or "writeback-proposal"


def write_proposal(
    wiki_root: Path,
    day: dt.date,
    title: str,
    target_layer: str,
    body: str,
    anchors: list[str],
    provenance: str = "对话合成",
    judgment_changed: str = "待人工判别",
) -> Path:
    ensure_dirs(wiki_root)
    path = wiki_root / "_meta" / "writeback-inbox" / f"{day.isoformat()}-{slugify(title)}.md"
    anchor_lookup = {anchor.title: anchor.wiki_link for anchor in iter_anchor_pages(wiki_root)}
    anchor_lines = "\n".join(f"- {anchor_lookup.get(anchor, f'[[{anchor}]]')}" for anchor in anchors)
    text = (
        "---\n"
        f"title: {title}\n"
        f"created: {day.isoformat()}\n"
        "type: writeback-proposal\n"
        "status: proposed\n"
        f"target_layer: {target_layer}\n"
        f"provenance: {provenance if provenance in PROVENANCE_VALUES else '对话合成'}\n"
        f"judgment_changed: {judgment_changed}\n"
        "tags: [second-brain, writeback, cognitive-loop]\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Anchors\n\n"
        f"{anchor_lines or '- none'}\n\n"
        "## Proposal\n\n"
        f"{body.strip()}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def normalize_writeback_status(value: object) -> str:
    # Some inbox files carry human notes after the status value, e.g.
    # `status: accepted  # 决定1已执行...`; count by the canonical token.
    return str(value or "").split("#", 1)[0].strip().strip("\"'").lower()


def open_writeback_proposals(wiki_root: Path) -> list[Path]:
    inbox_dir = wiki_root / "_meta" / "writeback-inbox"
    proposals = sorted(inbox_dir.glob("*.md")) if inbox_dir.exists() else []
    open_items: list[Path] = []
    for proposal in proposals:
        if proposal.name == "README.md":
            continue
        try:
            frontmatter, _ = parse_frontmatter(proposal.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
        status = normalize_writeback_status(frontmatter.get("status", ""))
        if status in OPEN_WRITEBACK_STATUSES:
            open_items.append(proposal)
    return open_items


def iter_events(wiki_root: Path, since: dt.date) -> list[dict]:
    anchor_dir = wiki_root / "_meta" / "anchor-events"
    if not anchor_dir.exists():
        return []
    events: list[dict] = []
    for path in sorted(anchor_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    event_date = dt.date.fromisoformat(str(event.get("date", "1900-01-01")))
                except (json.JSONDecodeError, ValueError):
                    continue
                if event_date >= since:
                    events.append(event)
    return events


def iter_events_between(wiki_root: Path, since: dt.date, end: dt.date) -> list[dict]:
    events = []
    for event in iter_events(wiki_root, since):
        event_date = parse_optional_date(event.get("date"))
        if event_date and since <= event_date <= end:
            events.append(event)
    return events


def router_miss_dir(wiki_root: Path) -> Path:
    return wiki_root / "_meta" / "anchor-events" / "router-misses"


def append_router_miss(wiki_root: Path, day: dt.date, query: str, source: str) -> Path:
    miss_dir = router_miss_dir(wiki_root)
    miss_dir.mkdir(parents=True, exist_ok=True)
    path = miss_dir / f"{day:%Y-%m}.jsonl"
    record = {"date": day.isoformat(), "query": query, "source": source}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_router_misses(wiki_root: Path, since: dt.date, end: dt.date) -> list[dict]:
    miss_dir = router_miss_dir(wiki_root)
    if not miss_dir.exists():
        return []
    records: list[dict] = []
    for path in sorted(miss_dir.glob("*.jsonl")):
        rows, _errors = safe_read_jsonl(path)
        for row in rows:
            day = parse_optional_date(row.get("date"))
            if day and since <= day <= end and str(row.get("query") or "").strip():
                records.append(row)
    return records


def read_knowledge_misses(wiki_root: Path, since: dt.date, end: dt.date) -> list[dict]:
    miss_dir = wiki_root / "_meta" / "knowledge-events" / "knowledge-misses"
    if not miss_dir.exists():
        return []
    records: list[dict] = []
    for path in sorted(miss_dir.glob("*.jsonl")):
        rows, _errors = safe_read_jsonl(path)
        for row in rows:
            day = parse_optional_date(row.get("date"))
            if day and since <= day <= end and str(row.get("query") or "").strip():
                records.append(row)
    return records


def cluster_router_misses(
    records: list[dict], min_cluster: int = 3, limit: int = 5
) -> list[tuple[str, int, list[str]]]:
    """Group miss queries by shared salient unit; a cluster needs >= min_cluster miss records."""
    unit_hits: dict[str, list[int]] = {}
    queries: list[str] = []
    for index, row in enumerate(records):
        query = str(row.get("query") or "").strip()
        queries.append(query)
        for unit in route_units(query):
            unit_hits.setdefault(unit, []).append(index)
    ranked = sorted(
        ((unit, hits) for unit, hits in unit_hits.items() if len(hits) >= min_cluster),
        key=lambda item: (-len(item[1]), -len(item[0]), item[0]),
    )
    clusters: list[tuple[str, int, list[str]]] = []
    covered: list[set[int]] = []
    for unit, hits in ranked:
        hit_set = set(hits)
        if any(hit_set <= seen for seen in covered):
            continue
        examples: list[str] = []
        for i in hits:
            if queries[i] not in examples:
                examples.append(queries[i])
            if len(examples) >= 3:
                break
        clusters.append((unit, len(hits), examples))
        covered.append(hit_set)
        if len(clusters) >= limit:
            break
    return clusters


def append_events(wiki_root: Path, day: dt.date, events: list[dict]) -> Path:
    ensure_dirs(wiki_root)
    path = month_path(wiki_root, day)
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def safe_read_jsonl(path: Path) -> tuple[list[dict], list[tuple[int, str]]]:
    rows: list[dict] = []
    errors: list[tuple[int, str]] = []
    if not path.exists():
        return rows, errors
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                errors.append((line_no, str(exc)))
    return rows, errors


def validate_human_event_source(source: str, action: str) -> None:
    # 统一判据：训练动作放行；agent-chat 的非训练事件（used/closed-loop/maintenance/…）一律拒。
    if is_human_facing_event({"action": action, "source": source}):
        return
    allowed = ", ".join(HUMAN_EVENT_SOURCES)
    raise SystemExit(
        f"refused anchor-event action={action!r} source={source!r}; "
        f"agent-chat 非训练事件不入人脑面账本（应进 _meta/automation-runs/）。"
        f"人脑面 source: {allowed}"
    )


def non_human_event_source_items(wiki_root: Path) -> list[tuple[Path, int, dict, str]]:
    event_dir = wiki_root / "_meta" / "anchor-events"
    if not event_dir.exists():
        return []
    items: list[tuple[Path, int, dict, str]] = []
    for path in sorted(event_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 统一判据：人脑面事件保留；agent-chat 非训练事件（used/closed-loop/
                # maintenance/…）列入清洗清单。
                if is_human_facing_event(event):
                    continue
                action = str(event.get("action") or "").strip()
                reason = f"agent-source-{action or 'unknown'}"
                items.append((path, line_no, event, reason))
    return items


def write_event_source_cleanup_report(wiki_root: Path, day: dt.date) -> Path:
    ensure_dirs(wiki_root)
    items = non_human_event_source_items(wiki_root)
    path = (
        wiki_root
        / "_meta"
        / "anchor-governance"
        / "events"
        / f"{day.isoformat()}-anchor-event-source-cleanup-proposal.md"
    )
    lines = [
        "---",
        f"title: Anchor Event Source Cleanup Proposal {day.isoformat()}",
        f"created: {day.isoformat()}",
        "type: automation-report",
        "status: proposed",
        "tags: [second-brain, anchor-events, cleanup]",
        "---",
        "",
        f"# Anchor Event Source Cleanup Proposal {day.isoformat()}",
        "",
        "- mode: proposal-only",
        "- deleted_events: 0",
        f"- allowed_human_event_sources: {', '.join(HUMAN_EVENT_SOURCES)}",
        f"- non_whitelist_human_events: {len(items)}",
        "",
        "## Proposed Review List",
        "",
    ]
    if items:
        for event_path, line_no, event, reason in items:
            rel = event_path.relative_to(wiki_root)
            summary = str(event.get("summary") or "").replace("\n", " ")[:160]
            lines.append(
                f"- {rel}:{line_no}: reason={reason}; date={event.get('date') or '-'}; "
                f"anchor={event.get('anchor') or '-'}; action={event.get('action') or '-'}; "
                f"source={event.get('source') or '-'}; summary={summary or '-'}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Constraint",
            "",
            "- 本报告只列清单，不删除、不重写任何 anchor-event。",
            "- 是否回溯剔除由东哥确认后另行执行。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    data: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith(("  - ", "- ")) and current_key and current_list is not None:
            current_list.append(line.split("-", 1)[1].strip().strip("\"'"))
            continue
        current_key = None
        current_list = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            current_list = []
            data[key] = current_list
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
        elif value.lower() in {"null", "none", "~"}:
            data[key] = None
        else:
            data[key] = value.strip("\"'")
    return data, body


def update_frontmatter_text(text: str, updates: dict[str, object]) -> str:
    if not updates:
        return text
    if not text.startswith("---\n"):
        lines = ["---"]
        for key, value in updates.items():
            lines.append(f"{key}: {format_frontmatter_value(value)}")
        lines.append("---")
        return "\n".join(lines) + "\n\n" + text

    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("frontmatter opening marker exists without closing marker")

    raw = text[4:end]
    body = text[end + 5 :]
    pending = dict(updates)
    output_lines: list[str] = []
    for line in raw.splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key = line.split(":", 1)[0].strip()
            if key in pending:
                output_lines.append(f"{key}: {format_frontmatter_value(pending.pop(key))}")
                continue
        output_lines.append(line)
    for key, value in pending.items():
        output_lines.append(f"{key}: {format_frontmatter_value(value)}")
    return "---\n" + "\n".join(output_lines) + "\n---\n" + body


def format_frontmatter_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [text]


def normalize_query(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


ROUTE_STOP_UNITS = {
    "一个",
    "这个",
    "那个",
    "什么",
    "为什么",
    "怎么",
    "如何",
    "可以",
    "应该",
    "需要",
    "是否",
    "因为",
    "所以",
    "但是",
    "我的",
    "我们",
}


def route_units(value: str) -> set[str]:
    units: set[str] = set()
    for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", normalize_query(value)):
        if re.fullmatch(r"[a-z0-9_]+", token):
            if len(token) >= 2 and token not in ROUTE_STOP_UNITS:
                units.add(token)
            continue
        if len(token) >= 2 and token not in ROUTE_STOP_UNITS:
            units.add(token)
        for size in (2, 3):
            for index in range(0, max(len(token) - size + 1, 0)):
                unit = token[index : index + size]
                if unit not in ROUTE_STOP_UNITS:
                    units.add(unit)
    return units


def route_overlap(query_units: set[str], value: str) -> list[str]:
    overlap = query_units & route_units(value)
    return sorted(overlap, key=lambda item: (-len(item), item))


def extract_wikilinks(text: str) -> list[str]:
    links = []
    for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
        target = raw.split("|", 1)[0].strip()
        if target:
            links.append(target)
    return links


@dataclass(frozen=True)
class AnchorPage:
    title: str
    path: Path
    status: str
    weight: int
    last_active: dt.date | None
    created: dt.date | None
    body: str
    triggers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    core_questions: tuple[str, ...] = ()

    @property
    def relative_path(self) -> str:
        try:
            return self.path.relative_to(DEFAULT_WIKI_ROOT).with_suffix("").as_posix()
        except ValueError:
            parts = self.path.parts
            if "llm-wiki" in parts:
                index = parts.index("llm-wiki")
                return Path(*parts[index + 1 :]).with_suffix("").as_posix()
            return self.path.with_suffix("").as_posix()

    @property
    def wiki_link(self) -> str:
        return f"[[{self.relative_path}]]"

    @property
    def content_chars(self) -> int:
        return len(self.body.strip())

    @property
    def link_count(self) -> int:
        return len(re.findall(r"\[\[[^\]]+\]\]", self.body))

    @property
    def has_output_link(self) -> bool:
        return bool(re.search(r"输出|公众号|小红书|文章|发布|queries?/|practice|行动", self.body, re.I))

    @property
    def outbound_links(self) -> list[str]:
        return extract_wikilinks(self.body)


@dataclass(frozen=True)
class AnchorCapsule:
    trigger: str
    one_sentence: str
    minimal_model: str
    why_true: str
    key_distinction: str

    @property
    def has_back(self) -> bool:
        return bool(self.one_sentence and self.minimal_model and self.why_true and self.key_distinction)


@dataclass(frozen=True)
class CapsuleLintIssue:
    anchor: AnchorPage
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class AnchorReviewState:
    anchor: AnchorPage
    last_review: dt.date | None
    next_review: dt.date
    interval_days: int
    aligned_streak: int
    recent_results: tuple[bool, ...]
    due: bool


@dataclass(frozen=True)
class AnchorGovernance:
    anchor: AnchorPage
    recent_events: int
    recent_weight_delta: int
    all_events: int
    suggested_status: str

    @property
    def activity_score(self) -> int:
        return self.recent_events + max(self.anchor.weight, 0)

    @property
    def thin_score(self) -> int:
        score = 0
        if self.anchor.content_chars < 1800:
            score += 1
        if self.anchor.link_count < 5:
            score += 1
        if "## 什么时候调用它" not in self.anchor.body:
            score += 1
        if "## 连接到第二大脑" not in self.anchor.body:
            score += 1
        return score

    @property
    def is_thin(self) -> bool:
        return self.thin_score >= 2


def normalize_anchor_role(value: object) -> str:
    role = str(value or "candidate").strip()
    if role == "hot":
        return "active"
    if role not in ANCHOR_ROLE_STATUSES:
        return "candidate"
    return role


def anchor_from_page(path: Path, frontmatter: dict[str, object], body: str, role: str) -> AnchorPage:
    return AnchorPage(
        title=str(frontmatter.get("title") or path.stem),
        path=path,
        status=role,
        weight=parse_int(frontmatter.get("weight")),
        last_active=parse_optional_date(frontmatter.get("last_active")),
        created=parse_optional_date(frontmatter.get("created")),
        body=body,
        triggers=tuple(as_string_list(frontmatter.get("triggers"))),
        aliases=tuple(as_string_list(frontmatter.get("aliases"))),
        core_questions=tuple(as_string_list(frontmatter.get("core_questions")) or as_string_list(frontmatter.get("core_question"))),
    )


def iter_canonical_anchor_pages(wiki_root: Path) -> list[AnchorPage]:
    anchors: list[AnchorPage] = []
    for folder in ANCHOR_SCAN_DIRS:
        root = wiki_root / folder
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            if "anchor" not in frontmatter:
                continue
            anchors.append(anchor_from_page(path, frontmatter, body, normalize_anchor_role(frontmatter.get("anchor"))))
    return anchors


def iter_anchor_pages(wiki_root: Path) -> list[AnchorPage]:
    return sorted(iter_canonical_anchor_pages(wiki_root), key=lambda anchor: anchor.relative_path)


def build_anchor_dashboard(wiki_root: Path, end: dt.date | None = None) -> Path:
    ensure_dirs(wiki_root)
    end = end or today_local()
    events = iter_events_between(wiki_root, dt.date(1900, 1, 1), end)
    stats_by_anchor = aggregate_event_stats(events)
    recall_counts: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        if event.get("action") == "recall-check":
            recall_counts[str(event.get("anchor", ""))][str(event.get("match", "unknown"))] += 1
    path = wiki_root / "_meta" / "anchors" / "index.md"
    migration_map = wiki_root / "_meta" / "anchors" / "migration-map.md"
    migrated_paths: set[str] = set()
    if migration_map.exists():
        for line in migration_map.read_text(encoding="utf-8").splitlines():
            for match in re.findall(r"`llm-wiki/([^`]+)`", line):
                migrated_paths.add(match.removesuffix(".md"))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"title: 认知锚点看板",
        f"created: {end.isoformat()}",
        f"updated: {end.isoformat()}",
        "type: index",
        "tags: [meta, second-brain, anchor, dashboard]",
        "status: generated",
        "---",
        "",
        "# 认知锚点看板",
        "",
        "> 自动生成：canonical 页面提供 `anchor` 角色字段；`derived_weight`、`derived_last_active`、`derived_hot_score` 来自 anchor-events。",
        "",
        "| title | canonical_path | anchor | derived_weight | derived_last_active | derived_hot_score | core_questions | triggers_count | recall_full | recall_partial | recall_diverged | migration_status |",
        "|---|---|---|---:|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for anchor in iter_anchor_pages(wiki_root):
        stats = stats_by_anchor.get(anchor.title)
        full = recall_counts[anchor.title]["full"]
        partial = recall_counts[anchor.title]["partial"]
        diverged = recall_counts[anchor.title]["diverged"]
        hot_score = stats.events if stats else 0
        last_active = stats.last_active.isoformat() if stats and stats.last_active else "-"
        derived_weight = stats.weight_delta if stats else 0
        migration_status = "migrated" if anchor.relative_path in migrated_paths else "native"
        lines.append(
            f"| {anchor.wiki_link} | `{anchor.relative_path}.md` | {anchor.status} | {derived_weight} | {last_active} | "
            f"{hot_score} | {', '.join(anchor.core_questions) or '-'} | {len(anchor.triggers)} | {full} | {partial} | {diverged} | {migration_status} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_anchor_governance(
    wiki_root: Path,
    end: dt.date,
    days: int,
    hot_events: int,
    hot_weight: int,
) -> list[AnchorGovernance]:
    since = end - dt.timedelta(days=days - 1)
    # 治理的 hot/weight 判断同样只看人脑面事件，agent-chat 自动化事件不计。
    recent_events = [e for e in iter_events_between(wiki_root, since, end) if is_human_facing_event(e)]
    all_events = [e for e in iter_events(wiki_root, dt.date(1900, 1, 1)) if is_human_facing_event(e)]
    recent_counts = Counter(str(event.get("anchor", "")) for event in recent_events)
    all_counts = Counter(str(event.get("anchor", "")) for event in all_events)
    recent_deltas = defaultdict(int)
    for event in recent_events:
        anchor = str(event.get("anchor", ""))
        recent_deltas[anchor] += parse_int(event.get("weight_delta"))

    rows: list[AnchorGovernance] = []
    for anchor in iter_anchor_pages(wiki_root):
        recent_count = recent_counts[anchor.title]
        suggested = anchor.status
        derived_weight = recent_deltas[anchor.title]
        if anchor.status not in {"merge-candidate", "retired"}:
            if recent_count >= hot_events or derived_weight >= hot_weight:
                suggested = "hot"
            elif anchor.status == "active":
                suggested = "active"
            elif derived_weight == 0 and recent_count == 0:
                suggested = "candidate"
            else:
                suggested = "active"
        rows.append(
            AnchorGovernance(
                anchor=anchor,
                recent_events=recent_count,
                recent_weight_delta=recent_deltas[anchor.title],
                all_events=all_counts[anchor.title],
                suggested_status=suggested,
            )
        )
    return rows


def is_dormant(anchor: AnchorPage, end: dt.date, dormant_days: int, recent_events: int) -> bool:
    if anchor.status in {"retired", "merge-candidate"} or recent_events:
        return False
    reference = anchor.last_active or anchor.created
    return bool(reference and (end - reference).days >= dormant_days)


def similar_anchor_pairs(anchors: list[AnchorPage], min_ratio: float) -> list[tuple[AnchorPage, AnchorPage, float]]:
    pairs: list[tuple[AnchorPage, AnchorPage, float]] = []
    for index, left in enumerate(anchors):
        if left.status == "retired":
            continue
        for right in anchors[index + 1 :]:
            if right.status == "retired":
                continue
            left_title = left.title.lower()
            right_title = right.title.lower()
            ratio = SequenceMatcher(None, left_title, right_title).ratio()
            containment = (
                min(len(left_title), len(right_title)) >= 2
                and (left_title in right_title or right_title in left_title)
            )
            if ratio >= min_ratio or containment:
                pairs.append((left, right, ratio))
    return sorted(pairs, key=lambda item: item[2], reverse=True)


def format_anchor_item(row: AnchorGovernance, reason: str) -> str:
    anchor = row.anchor
    last_active = anchor.last_active.isoformat() if anchor.last_active else "-"
    return (
        f"- {anchor.wiki_link}：{reason}；status={anchor.status} -> suggested={row.suggested_status}；"
        f"events={row.recent_events}；weight={anchor.weight}；links={anchor.link_count}；"
        f"chars={anchor.content_chars}；last_active={last_active}"
    )


def lifecycle_counts(rows: list[AnchorGovernance], suggested: bool = False) -> Counter:
    if suggested:
        return Counter(row.suggested_status for row in rows)
    return Counter(row.anchor.status for row in rows)


@dataclass(frozen=True)
class AnchorEventStats:
    anchor: str
    events: int
    weight_delta: int
    first_active: dt.date | None
    last_active: dt.date | None


@dataclass(frozen=True)
class FrontmatterChange:
    anchor: AnchorPage
    updates: dict[str, object]
    old_values: dict[str, object]


def aggregate_event_stats(events: list[dict]) -> dict[str, AnchorEventStats]:
    counts = Counter()
    deltas = defaultdict(int)
    first_dates: dict[str, dt.date] = {}
    last_dates: dict[str, dt.date] = {}
    for event in events:
        # 只有人脑面事件计入 weight/hot 派生；agent-chat 自动化事件不灌权重。
        if not is_human_facing_event(event):
            continue
        anchor = str(event.get("anchor", "")).strip()
        if not anchor:
            continue
        event_date = parse_optional_date(event.get("date"))
        counts[anchor] += 1
        deltas[anchor] += parse_int(event.get("weight_delta"))
        if event_date:
            if anchor not in first_dates or event_date < first_dates[anchor]:
                first_dates[anchor] = event_date
            if anchor not in last_dates or event_date > last_dates[anchor]:
                last_dates[anchor] = event_date
    return {
        anchor: AnchorEventStats(
            anchor=anchor,
            events=counts[anchor],
            weight_delta=deltas[anchor],
            first_active=first_dates.get(anchor),
            last_active=last_dates.get(anchor),
        )
        for anchor in counts
    }


def recall_event_aligned(event: dict) -> bool:
    match = str(event.get("match") or "").strip().lower()
    rating = normalize_recall_rating(str(event.get("manual_rating") or ""))
    return match in RECALL_ALIGNED_MATCHES or rating == "✅"


def anchor_recall_events(events: list[dict], anchor_title: str) -> list[dict]:
    return [
        event
        for event in events
        if event.get("anchor") == anchor_title and event.get("action") == "recall-check" and parse_optional_date(event.get("date"))
    ]


def review_interval_for_streak(aligned_streak: int) -> int:
    if aligned_streak <= 0:
        return REVIEW_INTERVAL_DAYS[0]
    return REVIEW_INTERVAL_DAYS[min(aligned_streak - 1, len(REVIEW_INTERVAL_DAYS) - 1)]


def build_anchor_review_states(wiki_root: Path, day: dt.date) -> dict[str, AnchorReviewState]:
    events = iter_events_between(wiki_root, dt.date(1900, 1, 1), day)
    states: dict[str, AnchorReviewState] = {}
    for anchor in iter_anchor_pages(wiki_root):
        recall_events = sorted(
            anchor_recall_events(events, anchor.title),
            key=lambda event: parse_optional_date(event.get("date")) or dt.date(1900, 1, 1),
        )
        aligned_streak = 0
        interval_days = REVIEW_INTERVAL_DAYS[0]
        last_review: dt.date | None = None
        recent_results: list[bool] = []
        for event in recall_events:
            event_date = parse_optional_date(event.get("date"))
            if not event_date:
                continue
            aligned = recall_event_aligned(event)
            recent_results.append(aligned)
            aligned_streak = aligned_streak + 1 if aligned else 0
            interval_days = review_interval_for_streak(aligned_streak)
            last_review = event_date
        next_review = (last_review + dt.timedelta(days=interval_days)) if last_review else day
        states[anchor.title] = AnchorReviewState(
            anchor=anchor,
            last_review=last_review,
            next_review=next_review,
            interval_days=interval_days,
            aligned_streak=aligned_streak,
            recent_results=tuple(recent_results[-GRADUATE_RECENT_ALIGNED:]),
            due=next_review <= day,
        )
    return states


def review_state_payload(wiki_root: Path, day: dt.date) -> dict:
    states = build_anchor_review_states(wiki_root, day)
    rows = []
    for state in sorted(states.values(), key=lambda item: (item.anchor.status, item.next_review, item.anchor.title)):
        rows.append(
            {
                "anchor": state.anchor.title,
                "anchor_path": state.anchor.relative_path + ".md",
                "anchor_status": state.anchor.status,
                "last_review": state.last_review.isoformat() if state.last_review else None,
                "next_review": state.next_review.isoformat(),
                "interval_days": state.interval_days,
                "aligned_streak": state.aligned_streak,
                "recent_aligned": list(state.recent_results),
                "due": state.due,
                "graduate_candidate": is_graduate_candidate(state),
            }
        )
    return {
        "date": day.isoformat(),
        "intervals": list(REVIEW_INTERVAL_DAYS),
        "graduate_recent_aligned": GRADUATE_RECENT_ALIGNED,
        "anchors": rows,
    }


def is_graduate_candidate(state: AnchorReviewState, recent_n: int = GRADUATE_RECENT_ALIGNED) -> bool:
    return (
        state.anchor.status == "active"
        and state.interval_days >= REVIEW_INTERVAL_DAYS[-1]
        and len(state.recent_results) >= recent_n
        and all(state.recent_results[-recent_n:])
    )


def write_anchor_review_state(wiki_root: Path, day: dt.date) -> tuple[Path, Path]:
    ensure_dirs(wiki_root)
    payload = review_state_payload(wiki_root, day)
    json_path = wiki_root / "_meta" / "anchors" / "review-state.json"
    md_path = wiki_root / "_meta" / "anchors" / "review-state.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "---",
        "title: Anchor Review State",
        f"updated: {day.isoformat()}",
        "type: index",
        "status: generated",
        "tags: [meta, second-brain, anchor, spaced-repetition]",
        "---",
        "",
        "# Anchor Review State",
        "",
        "> 自动生成：复习间隔完全派生自 anchor-events 中的 recall-check 历史；不要手工编辑。",
        "",
        "| anchor | status | interval_days | aligned_streak | last_review | next_review | due | graduate_candidate |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for row in payload["anchors"]:
        lines.append(
            f"| [[{row['anchor_path'].removesuffix('.md')}]] | {row['anchor_status']} | "
            f"{row['interval_days']} | {row['aligned_streak']} | {row['last_review'] or '-'} | "
            f"{row['next_review']} | {row['due']} | {row['graduate_candidate']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def write_graduate_candidate_report(wiki_root: Path, day: dt.date) -> Path:
    ensure_dirs(wiki_root)
    states = build_anchor_review_states(wiki_root, day)
    candidates = [state for state in states.values() if is_graduate_candidate(state)]
    path = (
        wiki_root
        / "_meta"
        / "anchor-governance"
        / "monthly"
        / f"{day.isoformat()}-graduate-candidate-proposals.md"
    )
    lines = [
        "---",
        f"title: Anchor Graduate Candidate Proposals {day.isoformat()}",
        f"created: {day.isoformat()}",
        "type: governance-review",
        "status: proposed",
        "tags: [second-brain, anchor-governance, graduate-candidate]",
        "---",
        "",
        f"# Anchor Graduate Candidate Proposals {day.isoformat()}",
        "",
        "- 本文件只生成 graduate-candidate 提案，不自动修改任何 anchor frontmatter。",
        f"- graduation_rule: interval_days >= {REVIEW_INTERVAL_DAYS[-1]} and recent {GRADUATE_RECENT_ALIGNED} recall-check events all aligned",
        "",
        "## Graduate Candidates",
        "",
    ]
    if candidates:
        for state in sorted(candidates, key=lambda item: item.anchor.title):
            lines.append(
                f"- {state.anchor.wiki_link}: graduate-candidate; interval_days={state.interval_days}; "
                f"aligned_streak={state.aligned_streak}; last_review={state.last_review or '-'}; "
                f"next_review={state.next_review}"
            )
    else:
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def event_summary_payload(wiki_root: Path, end: dt.date, days: int) -> dict:
    since = end - dt.timedelta(days=days - 1)
    events = iter_events_between(wiki_root, since, end)
    stats = aggregate_event_stats(events)
    open_proposals = [p.name for p in open_writeback_proposals(wiki_root)]
    anchors = [
        {
            "anchor": item.anchor,
            "events": item.events,
            "weight_delta": item.weight_delta,
            "first_active": item.first_active.isoformat() if item.first_active else None,
            "last_active": item.last_active.isoformat() if item.last_active else None,
        }
        for item in sorted(stats.values(), key=lambda value: (-value.events, value.anchor))
    ]
    return {
        "start": since.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "events": len(events),
        "anchors": anchors,
        "open_writeback_proposals": len(open_proposals),
        "latest_writeback_proposals": open_proposals[-5:],
    }


def write_event_summary_report(wiki_root: Path, end: dt.date, days: int) -> tuple[Path, Path]:
    ensure_dirs(wiki_root)
    payload = event_summary_payload(wiki_root, end, days)
    report_dir = wiki_root / "_meta" / "anchor-governance" / "events"
    stem = f"{end.isoformat()}-{days}d-anchor-events-summary"
    json_path = report_dir / f"{stem}.json"
    md_path = report_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "---",
        f"title: Anchor Events Summary {end.isoformat()} {days}d",
        f"created: {end.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, anchor-events, summary]",
        "---",
        "",
        f"# Anchor Events Summary {end.isoformat()} ({days}d)",
        "",
        f"窗口：{payload['start']} 至 {payload['end']}。",
        "",
        f"- events: {payload['events']}",
        f"- open_writeback_proposals: {payload['open_writeback_proposals']}",
        "",
        "## Anchors",
        "",
    ]
    if payload["anchors"]:
        for row in payload["anchors"]:
            lines.append(
                f"- {row['anchor']}: events={row['events']}, weight_delta={row['weight_delta']}, "
                f"last_active={row['last_active'] or '-'}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Latest Proposals", ""])
    lines.extend([f"- {name}" for name in payload["latest_writeback_proposals"]] or ["- none"])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def desired_status_from_stats(
    current_status: str,
    stats: AnchorEventStats | None,
    hot_weight: int,
    mark_candidates: bool,
) -> str:
    if current_status in {"merge-candidate", "retired"}:
        return current_status
    if stats and stats.weight_delta >= hot_weight:
        return "hot"
    if stats and stats.weight_delta > 0:
        return "active"
    if mark_candidates and current_status == "active":
        return "candidate"
    return current_status


def build_frontmatter_changes(
    wiki_root: Path,
    end: dt.date,
    hot_weight: int,
    mark_candidates: bool,
) -> list[FrontmatterChange]:
    # Role-based anchors keep derived metrics out of page frontmatter.
    # `weight`, `last_active`, and `hot` are written to _meta/anchors/index.md.
    return []


def write_frontmatter_sync_report(
    wiki_root: Path,
    end: dt.date,
    changes: list[FrontmatterChange],
    applied: bool,
) -> Path:
    ensure_dirs(wiki_root)
    mode = "apply" if applied else "dry-run"
    path = wiki_root / "_meta" / "anchor-governance" / "frontmatter" / f"{end.isoformat()}-frontmatter-sync-{mode}.md"
    lines = [
        "---",
        f"title: Anchor Frontmatter Sync {end.isoformat()}",
        f"created: {end.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, anchor-governance, frontmatter]",
        "---",
        "",
        f"# Anchor Frontmatter Sync {end.isoformat()}",
        "",
        f"- mode: {mode}",
        f"- changes: {len(changes)}",
        "- derived metrics: _meta/anchors/index.md",
        "",
        "## Changes",
        "",
    ]
    if changes:
        for change in changes:
            parts = []
            for key, value in change.updates.items():
                if key == "updated":
                    continue
                parts.append(f"{key}: {change.old_values.get(key)} -> {value}")
            lines.append(f"- {change.anchor.wiki_link}: " + "; ".join(parts))
    else:
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def apply_frontmatter_changes(changes: list[FrontmatterChange]) -> None:
    for change in changes:
        path = change.anchor.path
        text = path.read_text(encoding="utf-8")
        path.write_text(update_frontmatter_text(text, change.updates), encoding="utf-8")


def active_anchor_count(anchors: list[AnchorPage]) -> int:
    return sum(1 for anchor in anchors if anchor.status == "active")


def route_query(wiki_root: Path, query: str, limit: int = 3) -> list[tuple[AnchorPage, int, list[str]]]:
    query_norm = normalize_query(query)
    query_units = route_units(query)
    matches: list[tuple[AnchorPage, int, list[str]]] = []
    for anchor in iter_anchor_pages(wiki_root):
        if anchor.status not in {"active", "retired"}:
            continue
        score = 0
        reasons: list[str] = []
        title = normalize_query(anchor.title)
        if title and title in query_norm:
            score += 8
            reasons.append("title")
        for alias in anchor.aliases:
            alias_norm = normalize_query(alias)
            if alias_norm and alias_norm in query_norm:
                score += 6
                reasons.append(f"alias:{alias}")
            else:
                overlap = route_overlap(query_units, alias)
                if overlap:
                    score += min(3, len(overlap))
                    reasons.append(f"alias-overlap:{alias}:{'/'.join(overlap[:3])}")
        for trigger in anchor.triggers:
            trigger_norm = normalize_query(trigger)
            if trigger_norm and trigger_norm in query_norm:
                score += 5
                reasons.append(f"trigger:{trigger}")
            else:
                overlap = route_overlap(query_units, trigger)
                if len(overlap) >= 2:
                    score += min(4, len(overlap))
                    reasons.append(f"trigger-overlap:{trigger}:{'/'.join(overlap[:3])}")
        # Lightweight fallback: exact character-token overlap for short Chinese/English labels.
        for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", anchor.title):
            if token.lower() in query_norm:
                score += 2
                reasons.append(f"token:{token}")
        if score:
            matches.append((anchor, score, reasons))
    return sorted(matches, key=lambda item: (-item[1], item[0].title))[:limit]


# --- Knowledge routing -------------------------------------------------------
# route targets second-brain KNOWLEDGE points (semantic), never cognitive anchors.
# Anchors are the human-recall training layer (morning-recall / recall-check); a
# query that matches no anchor is NOT a failure. We semantic-route via gbrain
# against the knowledge pages and scores the hit pages (+1 usage = "hot knowledge").
# A miss (no relevant knowledge -> new territory) is the CALLER's judgment via the
# --miss flag, NOT an absolute score cutoff: gbrain hybrid scores are RRF-fused and
# saturate high (~0.9+) even for off-topic queries, so no threshold separates a real
# hit from noise. Only .jsonl ledgers are written (gbrain does not index .jsonl), so
# routing can never re-pollute the semantic index.

ROUTE_EXCLUDE_PREFIXES = (
    "_meta/anchor-governance/",
    "_meta/anchor-events/",
    "_meta/automation-runs/",
    "_meta/unresolved-wikilink-stubs/",
    "_meta/log-archive",
    "_meta/knowledge/",
    "_meta/knowledge-events/",
    "_meta/knowledge-growth-reports/",
    "第二大脑/维护日志/",
    "第二大脑/进化日志/",
    "第二大脑/审计报告/",
)
ROUTE_ALLOWED_PREFIXES = (
    "concepts/",
    "entities/",
    "practices/",
    "queries/",
    "comparisons/",
)
ROUTE_EXCLUDE_SUBSTRINGS = (
    "/_archive/",
    "原稿备份",
    ".bak-",
)
_GBRAIN_HIT_RE = re.compile(r"^\[(-?\d+(?:\.\d+)?)\]\s+(\S+)\s+--")


def route_excluded(slug: str) -> bool:
    slug = slug.removeprefix("llm-wiki/").strip("/")
    normalized = f"/{slug}"
    return not any(slug.startswith(prefix) for prefix in ROUTE_ALLOWED_PREFIXES) or any(
        slug.startswith(prefix) for prefix in ROUTE_EXCLUDE_PREFIXES
    ) or any(
        marker in normalized for marker in ROUTE_EXCLUDE_SUBSTRINGS
    )


def parse_gbrain_route_hits(stdout: str, limit: int) -> list[tuple[str, float, str]]:
    hits: list[tuple[str, float, str]] = []
    for line in stdout.splitlines():
        match = _GBRAIN_HIT_RE.match(line.strip())
        if not match:
            continue
        slug = match.group(2).removeprefix("llm-wiki/")
        if route_excluded(slug):
            continue
        score = float(match.group(1))
        excerpt = line.split("--", 1)[1].strip()[:120] if "--" in line else ""
        hits.append((slug, score, excerpt))
    hits.sort(key=lambda item: -item[1])
    return hits[:limit]


def first_relevant_excerpt(body: str, overlap: set[str]) -> str:
    for line in body.splitlines():
        clean = line.strip().strip("#>- ")
        if not clean:
            continue
        if any(unit in clean for unit in overlap):
            return clean[:120]
    return body.strip().replace("\n", " ")[:120]


def route_knowledge_local(query: str, wiki_root: Path, limit: int = 5) -> list[tuple[str, float, str]]:
    """Small local fallback for route closeout when the Gbrain CLI cannot run.

    It is intentionally conservative and lexical: the goal is to keep the
    closeout gate usable and avoid false backend misses, not to replace Gbrain's
    semantic recall.
    """
    query_units = route_units(query)
    if not query_units:
        return []
    hits: list[tuple[str, float, str]] = []
    for folder in ("concepts", "entities", "practices", "queries", "comparisons"):
        root = wiki_root / folder
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                slug = path.relative_to(wiki_root).with_suffix("").as_posix()
            except ValueError:
                continue
            if route_excluded(slug):
                continue
            try:
                frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            title = str(frontmatter.get("title") or path.stem)
            aliases = " ".join(as_string_list(frontmatter.get("aliases")))
            triggers = " ".join(as_string_list(frontmatter.get("triggers")))
            haystack = "\n".join([slug, title, aliases, triggers, body[:5000]])
            hay_units = route_units(haystack)
            overlap = query_units & hay_units
            if not overlap:
                continue
            score = sum(max(len(unit), 1) for unit in overlap)
            if title and (title in query or query in title):
                score += 20
            if aliases and any(alias in query for alias in as_string_list(frontmatter.get("aliases"))):
                score += 10
            normalized = round(score / max(len(query_units), 1), 4)
            hits.append((slug, normalized, first_relevant_excerpt(body, overlap)))
    hits.sort(key=lambda item: (-item[1], item[0]))
    return hits[:limit]


def route_knowledge(
    query: str,
    limit: int = 5,
    source: str = "hbrain",
    wiki_root: Path = DEFAULT_WIKI_ROOT,
) -> list[tuple[str, float, str]]:
    """Route a query to second-brain knowledge points.

    Hybrid `gbrain query` is preferred when healthy; keyword `gbrain search` is
    the stable Hbrain recall contract. If the Gbrain CLI fails inside this
    Python process, fall back to a local lexical wiki scan so `--record` does
    not turn backend failures into false misses.
    """
    commands = [
        ("query", ["gbrain", "query", query, "--source-id", source, "--limit", str(limit + 4)]),
        ("search", ["gbrain", "search", query, "--source", source, "--limit", str(limit + 4)]),
    ]
    saw_success = False
    for _label, command in commands:
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        saw_success = True
        hits = parse_gbrain_route_hits(proc.stdout, limit)
        if hits:
            return hits
    local_hits = route_knowledge_local(query, wiki_root, limit)
    if local_hits:
        return local_hits
    if saw_success:
        return []
    return []


def knowledge_events_path(wiki_root: Path, day: dt.date) -> Path:
    return wiki_root / "_meta" / "knowledge-events" / f"{day:%Y-%m}.jsonl"


def append_knowledge_events(wiki_root: Path, day: dt.date, events: list[dict]) -> Path:
    path = knowledge_events_path(wiki_root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def append_knowledge_miss(wiki_root: Path, day: dt.date, query: str, source: str) -> Path:
    miss_dir = wiki_root / "_meta" / "knowledge-events" / "knowledge-misses"
    miss_dir.mkdir(parents=True, exist_ok=True)
    path = miss_dir / f"{day:%Y-%m}.jsonl"
    record = {"date": day.isoformat(), "query": query, "source": source}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def write_router_report(
    wiki_root: Path,
    day: dt.date,
    query: str,
    matches: list[tuple[AnchorPage, int, list[str]]],
    recorded: bool,
) -> Path:
    ensure_dirs(wiki_root)
    path = wiki_root / "_meta" / "anchor-governance" / "router" / f"{day.isoformat()}-{slugify(query)}.md"
    lines = [
        "---",
        f"title: Anchor Router {day.isoformat()}",
        f"created: {day.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, anchor-router]",
        "---",
        "",
        f"# Anchor Router {day.isoformat()}",
        "",
        f"- query: {query}",
        f"- recorded: {recorded}",
        "",
        "## Matches",
        "",
    ]
    lines.extend(
        [
            f"- {anchor.wiki_link}: score={score}; reasons={', '.join(reasons)}"
            for anchor, score, reasons in matches
        ]
        or ["- none"]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def normalize_capsule_heading(value: str) -> str:
    return re.sub(r"[\s:：()（）【】\[\]#*`\"'“”‘’<>《》\-—_]+", "", value.lower())


def iter_markdown_sections(body: str) -> list[tuple[int, str, str]]:
    matches = list(re.finditer(r"^(#{2,6})\s*(.+?)\s*$", body, flags=re.M))
    sections: list[tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        end = len(body)
        for later in matches[index + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        sections.append((level, title, body[match.end() : end].strip()))
    return sections


def heading_matches(title: str, aliases: tuple[str, ...]) -> bool:
    normalized = normalize_capsule_heading(title)
    for alias in aliases:
        alias_normalized = normalize_capsule_heading(alias)
        if normalized == alias_normalized or normalized.startswith(alias_normalized):
            return True
    return False


def extract_capsule_section(anchor: AnchorPage, field: str) -> str:
    aliases = CAPSULE_FIELD_ALIASES[field]
    for _level, title, content in iter_markdown_sections(anchor.body):
        if heading_matches(title, aliases):
            return content.strip()
    return ""


def extract_anchor_capsule(anchor: AnchorPage) -> AnchorCapsule:
    trigger = extract_capsule_section(anchor, "trigger")
    if not trigger and anchor.triggers:
        trigger = "\n".join(f"- {item}" for item in anchor.triggers)
    if not trigger:
        trigger = anchor.title
    return AnchorCapsule(
        trigger=trigger,
        one_sentence=extract_capsule_section(anchor, "one_sentence"),
        minimal_model=extract_capsule_section(anchor, "minimal_model"),
        why_true=extract_capsule_section(anchor, "why_true"),
        key_distinction=extract_capsule_section(anchor, "key_distinction"),
    )


def capsule_minimal_model_chunks(text: str) -> int:
    count = 0
    in_code = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", line):
            continue
        if re.match(r"^[-*+]\s+", line) or re.match(r"^\d+[.)、]\s+", line):
            count += 1
            continue
        if line.startswith("|") and line.endswith("|"):
            count += 1
            continue
        count += 1
    return count


def lint_capsules(wiki_root: Path) -> list[CapsuleLintIssue]:
    issues: list[CapsuleLintIssue] = []
    for anchor in iter_anchor_pages(wiki_root):
        capsule = extract_anchor_capsule(anchor)
        missing = [
            label
            for field, label in CAPSULE_BACK_FIELD_LABELS.items()
            if not str(getattr(capsule, field)).strip()
        ]
        if missing:
            issues.append(
                CapsuleLintIssue(
                    anchor=anchor,
                    code="capsule-incomplete",
                    severity="info",
                    message="missing " + " / ".join(missing),
                )
            )
        chunks = capsule_minimal_model_chunks(capsule.minimal_model)
        if capsule.minimal_model and chunks > 5:
            issues.append(
                CapsuleLintIssue(
                    anchor=anchor,
                    code="capsule-too-long",
                    severity="warning",
                    message=f"minimal_model_chunks={chunks}; 建议压缩或拆分为 2-3 个锚点",
                )
            )
    return issues


def capsule_lint_payload(wiki_root: Path) -> dict:
    issues = lint_capsules(wiki_root)
    counts = Counter(issue.code for issue in issues)
    return {
        "anchors_scanned": len(iter_anchor_pages(wiki_root)),
        "issues": len(issues),
        "counts": dict(sorted(counts.items())),
        "items": [
            {
                "anchor": issue.anchor.title,
                "anchor_path": issue.anchor.relative_path + ".md",
                "code": issue.code,
                "severity": issue.severity,
                "message": issue.message,
            }
            for issue in issues
        ],
    }


def capsule_missing_fields(capsule: AnchorCapsule) -> list[str]:
    return [
        label
        for field, label in CAPSULE_BACK_FIELD_LABELS.items()
        if not str(getattr(capsule, field)).strip()
    ]


def format_capsule_back(capsule: AnchorCapsule, include_missing: bool = False) -> str:
    missing = set(capsule_missing_fields(capsule))
    lines: list[str] = []
    fields = [
        ("one_sentence", "一句话"),
        ("minimal_model", "最小模型"),
        ("why_true", "为什么成立"),
        ("key_distinction", "关键区分"),
    ]
    for field, label in fields:
        value = str(getattr(capsule, field)).strip()
        if not value:
            if include_missing:
                lines.extend([f"### {label}", "", "capsule-missing", ""])
            continue
        lines.extend([f"### {label}", "", value, ""])
    if include_missing and missing:
        lines.extend(["### 胶囊状态", "", f"capsule-missing: 缺少 {' / '.join(capsule_missing_fields(capsule))}", ""])
    return "\n".join(lines).strip()


def capsule_status(capsule: AnchorCapsule) -> str:
    return "complete" if capsule.has_back else "capsule-missing"


def extract_anchor_model(anchor: AnchorPage) -> str:
    capsule = extract_anchor_capsule(anchor)
    if not capsule.has_back:
        return ""
    return format_capsule_back(capsule)


def recall_match(anchor: AnchorPage, response: str) -> tuple[str, float]:
    expected = normalize_query(extract_anchor_model(anchor))
    actual = normalize_query(response)
    expected_terms = route_units(expected)
    actual_terms = route_units(actual)
    if not expected_terms:
        return "capsule-missing", 0.0
    if not actual_terms:
        return "diverged", 0.0
    overlap = len(expected_terms & actual_terms) / max(min(len(expected_terms), len(actual_terms)), 1)
    if overlap >= 0.45:
        return "full", overlap
    if overlap >= 0.15:
        return "partial", overlap
    return "diverged", overlap


def write_recall_check_report(
    wiki_root: Path,
    day: dt.date,
    anchor: AnchorPage,
    response: str,
    match: str,
    score: float,
) -> Path:
    ensure_dirs(wiki_root)
    path = wiki_root / "_meta" / "anchor-governance" / "recall-checks" / f"{day.isoformat()}-{slugify(anchor.title)}.md"
    capsule = extract_anchor_capsule(anchor)
    comparison = format_capsule_back(capsule, include_missing=True)
    lines = [
        "---",
        f"title: Recall Check {anchor.title} {day.isoformat()}",
        f"created: {day.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, recall-check]",
        "---",
        "",
        f"# Recall Check {anchor.title} {day.isoformat()}",
        "",
        f"- anchor: {anchor.wiki_link}",
        f"- anchor_path: {anchor.relative_path}.md",
        f"- match: {match}",
        f"- score: {score:.2f}",
        "- comparison_scope: capsule-back-four-fields",
        f"- capsule_status: {capsule_status(capsule)}",
        "",
        "## User Response",
        "",
        response.strip() or "- none",
        "",
        "## Capsule Back Compared",
        "",
        comparison or "capsule-missing",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def morning_recall_dir(wiki_root: Path) -> Path:
    return wiki_root / "锚点晨读"


def morning_recall_path(wiki_root: Path, day: dt.date) -> Path:
    return morning_recall_dir(wiki_root) / f"{day.isoformat()}-晨读.md"


def recent_recall_failed(wiki_root: Path, day: dt.date, anchor_title: str) -> bool:
    yesterday = day - dt.timedelta(days=1)
    for event in iter_events_between(wiki_root, yesterday, yesterday):
        if event.get("anchor") != anchor_title or event.get("action") != "recall-check":
            continue
        if event.get("match") == "diverged" or event.get("manual_rating") == "❌":
            return True
    return False


def recall_selection_score(wiki_root: Path, day: dt.date, anchor: AnchorPage) -> int:
    stats = aggregate_event_stats(iter_events_between(wiki_root, dt.date(1900, 1, 1), day)).get(anchor.title)
    reference = (stats.last_active if stats else None) or anchor.last_active or anchor.created or day
    days_inactive = max((day - reference).days, 0)
    derived_weight = (stats.weight_delta if stats else anchor.weight) or 1
    base = max(derived_weight, 1) * (days_inactive + 1)
    if recent_recall_failed(wiki_root, day, anchor.title):
        base *= 2
    return base


def due_active_review_states(wiki_root: Path, day: dt.date) -> list[AnchorReviewState]:
    states = build_anchor_review_states(wiki_root, day)
    return [
        state
        for state in states.values()
        if state.anchor.status == "active" and state.due
    ]


def select_morning_recall_anchor(wiki_root: Path, day: dt.date) -> tuple[AnchorPage, int, AnchorReviewState]:
    due_states = due_active_review_states(wiki_root, day)
    candidates = [
        (state.anchor, recall_selection_score(wiki_root, day, state.anchor), state)
        for state in due_states
    ]
    if not candidates:
        raise SystemExit("no due active anchors for morning recall")
    recent_titles = recent_morning_recall_anchors(wiki_root, day)
    fresh_candidates = [item for item in candidates if item[0].title not in recent_titles]
    ranked_candidates = fresh_candidates or candidates
    return sorted(ranked_candidates, key=lambda item: (-item[1], -item[0].weight, item[0].title))[0]


def format_morning_answer_section(anchor: AnchorPage) -> str:
    capsule = extract_anchor_capsule(anchor)
    if not capsule.has_back:
        return (
            "### 胶囊状态\n\n"
            f"capsule-missing: 缺少 {' / '.join(capsule_missing_fields(capsule))}\n\n"
            "不抽取全文；请先补齐锚点胶囊背面四栏。"
        )
    return "### 胶囊背面四栏\n\n" + format_capsule_back(capsule)


def morning_recall_sections(anchor: AnchorPage) -> tuple[str, str, str]:
    capsule = extract_anchor_capsule(anchor)
    trigger_section = "## TRIGGER\n\n" + (capsule.trigger.strip() or anchor.title)
    answer_section = "## ANSWER\n\n" + f"### {anchor.title}\n\n" + format_morning_answer_section(anchor)
    return capsule_status(capsule), trigger_section, answer_section


def write_morning_recall_file(wiki_root: Path, day: dt.date, anchor: AnchorPage, score: int, force: bool) -> Path:
    path = morning_recall_path(wiki_root, day)
    if path.exists() and not force:
        return path
    morning_recall_dir(wiki_root).mkdir(parents=True, exist_ok=True)
    capsule = extract_anchor_capsule(anchor)
    status = capsule_status(capsule)
    text = "\n".join(
        [
            "---",
            f"title: 锚点晨读 {day.isoformat()}",
            f"created: {day.isoformat()}",
            "type: anchor-morning-recall",
            f"status: {'pending' if status == 'complete' else status}",
            f"anchor: {anchor.title}",
            f"selection_score: {score}",
            f"capsule_status: {status}",
            "tags: [second-brain, anchor-recall]",
            "---",
            "",
            f"# 锚点晨读 · {day.isoformat()}",
            "",
            "## TRIGGER",
            "",
            capsule.trigger.strip() or anchor.title,
            "",
            "## 回忆区",
            "",
            "先不要看 ANSWER。请用自己的话写出这个锚点的「一句话 / 最小模型 / 为什么成立 / 关键区分」。",
            "",
            "### 我的复述",
            "",
            RECALL_RESPONSE_BEGIN,
            "",
            RECALL_RESPONSE_END,
            "",
            "### 自评分",
            "",
            "- [ ] ✅ 清晰记住",
            "- [ ] ⚠️ 部分记住/模糊",
            "- [ ] ❌ 不记得",
            "",
            "--- 先复述，再下翻 ---",
            "",
            "## ANSWER",
            "",
            f"### {anchor.title}",
            "",
            format_morning_answer_section(anchor),
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")
    return path


def existing_morning_recall_anchor(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    frontmatter, _ = parse_frontmatter(text)
    anchor = str(frontmatter.get("anchor") or "").strip()
    if anchor:
        return anchor
    direct = re.search(r"今日锚点[：:]\s*(.+)", text)
    if direct:
        return direct.group(1).strip()
    legacy = re.search(r"^##\s*🔷\s*锚点\s*\d+[：:]\s*(.+)$", text, re.M)
    return legacy.group(1).strip() if legacy else ""


def parse_morning_recall_date(path: Path) -> dt.date | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})-晨读\.md$", path.name)
    if not match:
        return None
    return parse_optional_date(match.group(1))


def recent_morning_recall_anchors(wiki_root: Path, day: dt.date, days: int = MORNING_RECALL_RECENT_EXCLUDE_DAYS) -> set[str]:
    start = day - dt.timedelta(days=days)
    anchors: set[str] = set()
    root = morning_recall_dir(wiki_root)
    if not root.exists():
        return anchors
    for path in root.glob("*-晨读.md"):
        file_date = parse_morning_recall_date(path)
        if not file_date or file_date < start or file_date >= day:
            continue
        anchor = existing_morning_recall_anchor(path)
        if anchor:
            anchors.add(anchor)
    return anchors


def find_morning_recall_file(wiki_root: Path, day: dt.date, latest_before_today: bool) -> Path | None:
    if not latest_before_today:
        path = morning_recall_path(wiki_root, day)
        return path if path.exists() else None
    candidates: list[tuple[dt.date, Path]] = []
    root = morning_recall_dir(wiki_root)
    if not root.exists():
        return None
    for path in root.glob("*-晨读.md"):
        file_date = parse_morning_recall_date(path)
        if file_date and file_date < day:
            candidates.append((file_date, path))
    return sorted(candidates, key=lambda item: item[0])[-1][1] if candidates else None


def extract_recall_response(text: str) -> str:
    if RECALL_RESPONSE_BEGIN in text and RECALL_RESPONSE_END in text:
        start = text.index(RECALL_RESPONSE_BEGIN) + len(RECALL_RESPONSE_BEGIN)
        end = text.index(RECALL_RESPONSE_END, start)
        return text[start:end].strip()
    match = re.search(r"### 我的复述\s*(?P<body>.*?)(?:\n### |\n--- 先复述，再下翻 ---|\Z)", text, re.S)
    return match.group("body").strip() if match else ""


def normalize_recall_rating(value: str) -> str:
    if value.startswith("✅"):
        return "✅"
    if value.startswith("⚠"):
        return "⚠️"
    if value.startswith("❌"):
        return "❌"
    return ""


def extract_manual_rating(text: str) -> str:
    for line in text.splitlines():
        checkbox = re.search(r"-\s*\[[xX✓]\]\s*(✅|⚠️?|❌)", line)
        if checkbox:
            return normalize_recall_rating(checkbox.group(1))
    direct = re.search(r"自评分[：:]\s*(✅|⚠️?|❌)", text)
    return normalize_recall_rating(direct.group(1)) if direct else ""


def match_from_manual_rating(rating: str) -> tuple[str, float]:
    normalized = normalize_recall_rating(rating)
    if normalized == "✅":
        return "full", 1.0
    if normalized == "⚠️":
        return "partial", 0.5
    return "diverged", 0.0


def append_recent_call_record_text(text: str, line: str) -> str:
    heading = "## 最近调用记录"
    start = text.find(heading)
    if start == -1:
        return text.rstrip() + "\n\n" + heading + "\n\n" + line + "\n"
    content_start = text.find("\n", start + len(heading))
    if content_start == -1:
        return text.rstrip() + "\n\n" + line + "\n"
    next_heading = text.find("\n## ", content_start + 1)
    prefix = text[: content_start + 1]
    section = text[content_start + 1 : next_heading if next_heading != -1 else len(text)]
    suffix = text[next_heading:] if next_heading != -1 else ""
    if "- 暂无。" in section:
        section = section.replace("- 暂无。", line, 1)
    else:
        section = section.rstrip() + "\n" + line + "\n"
    return prefix + section + suffix


def update_anchor_after_recall(anchor: AnchorPage, day: dt.date, rating: str, match: str, score: float) -> None:
    # Derived metrics live in anchor-events and _meta/anchors/index.md.
    return


def update_morning_recall_status(path: Path, day: dt.date, match: str, score: float, rating: str) -> None:
    text = path.read_text(encoding="utf-8")
    updates: dict[str, object] = {
        "status": "reviewed",
        "reviewed": day.isoformat(),
        "match": match,
        "score": round(score, 4),
    }
    normalized_rating = normalize_recall_rating(rating)
    if normalized_rating:
        updates["manual_rating"] = normalized_rating
    path.write_text(update_frontmatter_text(text, updates), encoding="utf-8")


def provenance_counts(wiki_root: Path) -> Counter:
    counts = Counter()
    for folder in ("concepts", "queries", "practices"):
        root = wiki_root / folder
        if not root.exists():
            continue
        for path in root.glob("*.md"):
            frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if frontmatter.get("type") in {"automation-report", "automation-run"}:
                continue
            provenance = frontmatter.get("provenance")
            counts[str(provenance) if provenance else "missing"] += 1
    return counts


def low_connection_pages(wiki_root: Path, min_links: int = 2) -> list[tuple[Path, int]]:
    pages: list[tuple[Path, int]] = []
    for folder in ("concepts", "queries", "practices"):
        root = wiki_root / folder
        if not root.exists():
            continue
        for path in root.glob("*.md"):
            _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            count = len(extract_wikilinks(body))
            if count < min_links:
                pages.append((path, count))
    return sorted(pages, key=lambda item: (item[1], str(item[0])))


def serendipity_candidates(rows: list[AnchorGovernance], limit: int = 3) -> list[str]:
    candidates: list[str] = []
    seen = set()
    for row in sorted(rows, key=lambda item: (-item.activity_score, item.anchor.title)):
        for link in row.anchor.outbound_links:
            if link in seen:
                continue
            seen.add(link)
            candidates.append(f"- {row.anchor.wiki_link} -> [[{link}]]")
            if len(candidates) >= limit:
                return candidates
    return candidates


def malformed_event_files(wiki_root: Path) -> list[str]:
    issues: list[str] = []
    event_dir = wiki_root / "_meta" / "anchor-events"
    if not event_dir.exists():
        return issues
    for path in sorted(event_dir.glob("*.jsonl")):
        _, errors = safe_read_jsonl(path)
        for line_no, error in errors:
            issues.append(f"{path.relative_to(wiki_root)}:{line_no}: {error}")
    return issues


def invalid_anchor_statuses(wiki_root: Path) -> list[AnchorPage]:
    # iter_anchor_pages normalizes invalid statuses, so inspect files directly.
    invalid = []
    for path in sorted(p for folder in ANCHOR_SCAN_DIRS for p in (wiki_root / folder).glob("*.md")):
        frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        role_value = frontmatter.get("anchor") if "anchor" in frontmatter else frontmatter.get("status")
        if frontmatter.get("type") != "anchor" and "anchor" not in frontmatter:
            continue
        status = str(role_value or "")
        if status == "hot":
            continue
        if status not in ANCHOR_ROLE_STATUSES:
            invalid.append(
                AnchorPage(
                    title=str(frontmatter.get("title") or path.stem),
                    path=path,
                    status=status or "missing",
                    weight=parse_int(frontmatter.get("weight")),
                    last_active=parse_optional_date(frontmatter.get("last_active")),
                    created=parse_optional_date(frontmatter.get("created")),
                    body=body,
                    triggers=tuple(as_string_list(frontmatter.get("triggers"))),
                    aliases=tuple(as_string_list(frontmatter.get("aliases"))),
        core_questions=tuple(as_string_list(frontmatter.get("core_questions")) or as_string_list(frontmatter.get("core_question"))),
                )
            )
    return invalid


def judgment_log_path(wiki_root: Path) -> Path:
    return wiki_root / "_meta" / "judgment-log" / "judgments.jsonl"


def append_judgment(
    wiki_root: Path,
    day: dt.date,
    topic: str,
    stance: str,
    confidence: float,
    review_date: dt.date,
    anchors: list[str],
) -> Path:
    ensure_dirs(wiki_root)
    path = judgment_log_path(wiki_root)
    row = {
        "date": day.isoformat(),
        "topic": topic,
        "stance": stance,
        "confidence": confidence,
        "review_date": review_date.isoformat(),
        "anchors": anchors,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def due_judgments(wiki_root: Path, day: dt.date) -> list[dict]:
    rows, _ = safe_read_jsonl(judgment_log_path(wiki_root))
    due = []
    for row in rows:
        review_date = parse_optional_date(row.get("review_date"))
        if review_date and review_date <= day:
            due.append(row)
    return due


def closed_loop_count(events: list[dict]) -> int:
    return sum(1 for event in events if event.get("action") in {"closed-loop", "output", "action", "recall-check"})


def iter_markdown_files(wiki_root: Path) -> list[Path]:
    ignored_parts = {".git", ".obsidian"}
    files = []
    for path in wiki_root.rglob("*.md"):
        if any(part in ignored_parts for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def page_target_index(wiki_root: Path, pages: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    for path in pages:
        rel = path.relative_to(wiki_root).with_suffix("").as_posix()
        index[rel].append(path)
        index[path.stem].append(path)
        index[f"llm-wiki/{rel}"].append(path)
    return index


def normalized_link_target(target: str) -> str:
    target = target.split("#", 1)[0].strip().removesuffix(".md")
    if target.startswith("/"):
        target = target.lstrip("/")
    return target


def local_inbound_counts(wiki_root: Path) -> dict[Path, int]:
    pages = iter_markdown_files(wiki_root)
    target_index = page_target_index(wiki_root, pages)
    inbound = {path: 0 for path in pages}
    for source in pages:
        _, body = parse_frontmatter(source.read_text(encoding="utf-8"))
        for raw_target in extract_wikilinks(body):
            target = normalized_link_target(raw_target)
            targets = target_index.get(target) or target_index.get(Path(target).stem, [])
            for path in targets:
                if path != source:
                    inbound[path] = inbound.get(path, 0) + 1
    return inbound


def local_orphan_summary(wiki_root: Path) -> dict[str, list[tuple[Path, int]]]:
    inbound = local_inbound_counts(wiki_root)
    summary: dict[str, list[tuple[Path, int]]] = {}
    for folder in ("concepts", "queries", "practices", "entities"):
        root = wiki_root / folder
        rows = []
        if root.exists():
            for path in sorted(root.glob("*.md")):
                count = inbound.get(path, 0)
                if count == 0:
                    rows.append((path, count))
        summary[folder] = rows
    return summary


def anchor_freeze_violations(wiki_root: Path, freeze_start: dt.date) -> list[AnchorPage]:
    events = iter_events(wiki_root, freeze_start)
    event_counts = Counter(str(event.get("anchor", "")) for event in events)
    violations = []
    for anchor in iter_anchor_pages(wiki_root):
        if anchor.created and anchor.created >= freeze_start and event_counts[anchor.title] == 0:
            violations.append(anchor)
    return violations


def find_text_patterns(wiki_root: Path, patterns: dict[str, str], limit: int = 50) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {key: [] for key in patterns}
    compiled = {key: re.compile(pattern, re.I) for key, pattern in patterns.items()}
    for path in iter_markdown_files(wiki_root):
        rel_path = path.relative_to(wiki_root).as_posix()
        if should_skip_governance_lint(rel_path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(lines, start=1):
            for key, pattern in compiled.items():
                if len(results[key]) >= limit:
                    continue
                if pattern.search(line):
                    rel = path.relative_to(wiki_root)
                    results[key].append(f"{rel}:{line_no}: {line.strip()[:180]}")
    return results


def should_skip_governance_lint(rel_path: str) -> bool:
    return rel_path in GOVERNANCE_AUDIT_SKIP_FILES or rel_path.startswith(GOVERNANCE_AUDIT_SKIP_PREFIXES)


def find_text_pattern(wiki_root: Path, pattern: str, limit: int = 50, flags: int = 0) -> list[str]:
    results: list[str] = []
    compiled = re.compile(pattern, flags)
    for path in iter_markdown_files(wiki_root):
        rel_path = path.relative_to(wiki_root).as_posix()
        if should_skip_governance_lint(rel_path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if compiled.search(line):
                rel = path.relative_to(wiki_root)
                results.append(f"{rel}:{line_no}: {line.strip()[:180]}")
                if len(results) >= limit:
                    return results
    return results


def anchor_count_mismatches(wiki_root: Path, actual_count: int, limit: int = 50) -> list[str]:
    mismatches = []
    pattern = re.compile(r"(?P<num>\d{1,2})\s*个锚点(?:页面|页)?|锚点(?:数|数量)[：:\s]*(?P<num2>\d{1,2})")
    for path in iter_markdown_files(wiki_root):
        rel_path = path.relative_to(wiki_root).as_posix()
        if should_skip_governance_lint(rel_path):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_no, line in enumerate(lines, start=1):
            if "锚点" not in line:
                continue
            for match in pattern.finditer(line):
                raw = match.group("num") or match.group("num2")
                number = int(raw)
                if number < 10:
                    continue
                if number != actual_count and number != ANCHOR_BUDGET:
                    rel = path.relative_to(wiki_root)
                    mismatches.append(f"{rel}:{line_no}: mentioned={number}, actual={actual_count}: {line.strip()[:180]}")
                    if len(mismatches) >= limit:
                        return mismatches
    return mismatches


def model_private_format_mentions(wiki_root: Path, limit: int = 50) -> list[str]:
    patterns = {
        "private_model_format": r"(claude artifact|anthropic-only|openai assistant id|chatgpt export|cursor rule|copilot-specific)",
    }
    return find_text_patterns(wiki_root, patterns, limit)["private_model_format"]


def write_governance_audit_report(
    wiki_root: Path,
    day: dt.date,
    freeze_start: dt.date,
) -> Path:
    ensure_dirs(wiki_root)
    anchors = iter_anchor_pages(wiki_root)
    actual_anchor_count = len(anchors)
    freeze_violations = anchor_freeze_violations(wiki_root, freeze_start)
    orphan_summary = local_orphan_summary(wiki_root)
    text_hits = {
        "uppercase_hbrain_path": find_text_pattern(wiki_root, r"(/Users/jianghaidong/Hbrain|~/Hbrain)", flags=0),
        "legacy_todolist_path": find_text_pattern(wiki_root, r"openclawhaidong/Todolist", flags=0),
    }
    count_mismatches = anchor_count_mismatches(wiki_root, actual_anchor_count)
    private_format = model_private_format_mentions(wiki_root)
    capsule_payload = capsule_lint_payload(wiki_root)

    path = wiki_root / "_meta" / "anchor-governance" / "governance" / f"{day.isoformat()}-governance-audit.md"
    lines = [
        "---",
        f"title: Governance Audit {day.isoformat()}",
        f"created: {day.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, governance, audit]",
        "---",
        "",
        f"# Governance Audit {day.isoformat()}",
        "",
        "本报告只读本地 markdown 与 anchor-events，不调用 Gbrain，不修改召回层。",
        "",
        "## C1 Anchor Freeze",
        "",
        f"- freeze_start: {freeze_start.isoformat()}",
        f"- actual_anchor_count: {actual_anchor_count}",
        f"- new_anchor_without_event_evidence_info: {len(freeze_violations)}",
    ]
    lines.extend([f"- {anchor.wiki_link}: created={anchor.created}" for anchor in freeze_violations] or ["- none"])
    lines.extend(["", "## C2 Orphan Scope", ""])
    priority_total = 0
    for folder in ("concepts", "queries", "practices"):
        rows = orphan_summary.get(folder, [])
        priority_total += len(rows)
        lines.append(f"- {folder}_orphans: {len(rows)}")
    lines.append(f"- priority_orphans_total: {priority_total}")
    lines.append(f"- entities_orphans_separate: {len(orphan_summary.get('entities', []))}")
    lines.extend(["", "### Priority Orphan Samples", ""])
    samples = []
    for folder in ("concepts", "queries", "practices"):
        samples.extend(orphan_summary.get(folder, [])[:8])
    lines.extend([f"- {path.relative_to(wiki_root)}" for path, _ in samples[:24]] or ["- none"])
    lines.extend(["", "## C3 Consistency Lint", ""])
    lines.append("- canonical_hbrain_path: /Users/jianghaidong/hbrain")
    lines.append(f"- uppercase_hbrain_path_mentions: {len(text_hits['uppercase_hbrain_path'])}")
    lines.extend([f"- {item}" for item in text_hits["uppercase_hbrain_path"][:20]] or ["- none"])
    lines.append("")
    lines.append(f"- legacy_todolist_path_mentions: {len(text_hits['legacy_todolist_path'])}")
    lines.extend([f"- {item}" for item in text_hits["legacy_todolist_path"][:20]] or ["- none"])
    lines.append("")
    lines.append(f"- anchor_count_mismatches: {len(count_mismatches)}")
    lines.extend([f"- {item}" for item in count_mismatches[:20]] or ["- none"])
    lines.extend(["", "## C4 Model Agnostic", ""])
    lines.append("- canonical_storage: markdown + YAML frontmatter + JSONL event logs")
    lines.append("- agent_layer: model-replaceable; Claude/Codex/Gbrain/CASS should be event sources or tools, not storage formats")
    lines.append(f"- private_model_format_mentions: {len(private_format)}")
    lines.extend([f"- {item}" for item in private_format[:20]] or ["- none"])
    lines.extend(["", "## C5 Capsule Lint Info", ""])
    lines.append("- status: info")
    lines.append(f"- anchors_scanned: {capsule_payload['anchors_scanned']}")
    lines.append(f"- capsule_issues: {capsule_payload['issues']}")
    for code, count in capsule_payload["counts"].items():
        lines.append(f"- {code}: {count}")
    lines.extend(["", "### Capsule Samples", ""])
    lines.extend(
        [
            f"- {item['anchor_path']}: {item['code']} ({item['severity']}) {item['message']}"
            for item in capsule_payload["items"][:20]
        ]
        or ["- none"]
    )
    lines.extend(["", "## Status", ""])
    lines.append("- c1_status: info")
    lines.append("- c2_status: scoped; priority orphans are concepts/queries/canonical anchors, entities are separate")
    lines.append(f"- c3_status: {'needs-review' if text_hits['uppercase_hbrain_path'] or text_hits['legacy_todolist_path'] or count_mismatches else 'ok'}")
    lines.append(f"- c4_status: {'needs-review' if private_format else 'ok'}")
    lines.append("- c5_status: info")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_mechanism_audit_report(wiki_root: Path, day: dt.date, apply: bool = False) -> Path:
    ensure_dirs(wiki_root)
    anchors = iter_anchor_pages(wiki_root)
    active_count = active_anchor_count(anchors)
    event_issues = malformed_event_files(wiki_root)
    low_links = low_connection_pages(wiki_root)
    provenance = provenance_counts(wiki_root)
    due = due_judgments(wiki_root, day)
    invalid_status = invalid_anchor_statuses(wiki_root)

    if apply:
        for anchor in invalid_status:
            text = anchor.path.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(text)
            role_field = "anchor" if "anchor" in frontmatter else "status"
            anchor.path.write_text(
                update_frontmatter_text(text, {role_field: "candidate", "updated": day.isoformat()}),
                encoding="utf-8",
            )

    path = wiki_root / "_meta" / "anchor-governance" / "mechanisms" / f"{day.isoformat()}-mechanism-audit.md"
    lines = [
        "---",
        f"title: Mechanism Audit {day.isoformat()}",
        f"created: {day.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, mechanism-audit]",
        "---",
        "",
        f"# Mechanism Audit {day.isoformat()}",
        "",
        "## B2 Anchor Budget",
        "",
        f"- active_anchor_budget: {active_count}/{ANCHOR_BUDGET}",
        f"- status: {'ok' if active_count <= ANCHOR_BUDGET else 'over-budget'}",
        "",
        "## B4 Blockers And Whitelist Autofix",
        "",
        f"- whitelist_autofixes: {', '.join(WHITELIST_AUTOFIXES)}",
        f"- invalid_anchor_statuses: {len(invalid_status)}",
        f"- malformed_event_lines: {len(event_issues)}",
        f"- mode: {'apply' if apply else 'dry-run'}",
        "",
        "## B5 Provenance",
        "",
        "- excluded_types: automation-report, automation-run",
    ]
    for key, value in sorted(provenance.items()):
        lines.append(f"- {key}: {value}")
    if not provenance:
        lines.append("- none")
    lines.extend(["", "## B7 Connection Density", ""])
    lines.append(f"- pages_below_2_links: {len(low_links)}")
    lines.extend([f"- {path.relative_to(wiki_root)}: links={count}" for path, count in low_links[:20]] or ["- none"])
    lines.extend(["", "## B8 Judgment Log", ""])
    rows, judgment_errors = safe_read_jsonl(judgment_log_path(wiki_root))
    lines.append(f"- judgments: {len(rows)}")
    lines.append(f"- due_now: {len(due)}")
    lines.append(f"- jsonl_errors: {len(judgment_errors)}")
    lines.extend(["", "## Issues", ""])
    lines.extend([f"- {issue}" for issue in event_issues[:20]] or ["- none"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_weekly_governance_report(
    wiki_root: Path,
    end: dt.date,
    days: int,
    rows: list[AnchorGovernance],
    dormant_days: int,
    similar_pairs: list[tuple[AnchorPage, AnchorPage, float]],
) -> Path:
    ensure_dirs(wiki_root)
    iso_year, iso_week, _ = end.isocalendar()
    since = end - dt.timedelta(days=days - 1)
    path = wiki_root / "_meta" / "anchor-governance" / "weekly" / f"{iso_year}-W{iso_week:02d}-anchor-governance.md"

    high_freq_thin = [
        row
        for row in rows
        if row.is_thin and (row.recent_events >= 2 or (row.recent_events >= 1 and row.anchor.weight >= 1))
    ]
    dormant = [row for row in rows if is_dormant(row.anchor, end, dormant_days, row.recent_events)]
    output_ready = [
        row
        for row in rows
        if row.anchor.status != "retired" and row.activity_score >= 1 and not row.anchor.has_output_link
    ]
    hot_candidates = [row for row in rows if row.suggested_status == "hot" and row.anchor.status != "hot"]
    current_counts = lifecycle_counts(rows)
    suggested_counts = lifecycle_counts(rows, suggested=True)
    active_count = current_counts["active"] + current_counts["hot"]
    weekly_events = iter_events_between(wiki_root, since, end)
    weekly_closed_loops = closed_loop_count(weekly_events)
    provenance = provenance_counts(wiki_root)
    serendipity = serendipity_candidates(rows)

    lines = [
        "---",
        f"title: Anchor Governance Weekly {iso_year}-W{iso_week:02d}",
        f"created: {end.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, anchor-governance, weekly]",
        "---",
        "",
        f"# Anchor Governance Weekly {iso_year}-W{iso_week:02d}",
        "",
        f"窗口：{since.isoformat()} 至 {end.isoformat()}。",
        "",
        "## 结论",
        "",
        "- 本周只做识别和提案，不自动合并、退休或删除锚点。",
        "- `candidate / active / hot` 可以作为轻量维护建议；`merge-candidate / retired` 只进入月度处理。",
        "- 月度合并/退休报告每个自然月只生成一次，避免频繁扰动系统结构。",
        "",
        "## 生命周期看板",
        "",
        f"- anchor budget: {active_count}/{ANCHOR_BUDGET}",
        f"- closed-loop count: {weekly_closed_loops}",
        "",
        "| status | current | suggested |",
        "|---|---:|---:|",
    ]
    for status in LIFECYCLE_STATUSES:
        lines.append(f"| {status} | {current_counts[status]} | {suggested_counts[status]} |")

    lines.extend(["", "## 高频但薄", ""])
    lines.extend([format_anchor_item(row, "近期被调用但页面结构或连接偏薄") for row in high_freq_thin] or ["- none"])
    lines.extend(["", "## 长期沉睡", ""])
    lines.extend([format_anchor_item(row, f"超过 {dormant_days} 天没有活动") for row in dormant] or ["- none"])
    lines.extend(["", "## 重复相近", ""])
    lines.extend(
        [
            f"- {left.wiki_link} <-> {right.wiki_link}：title_similarity={ratio:.2f}；只作为月度合并候选，不自动处理"
            for left, right, ratio in similar_pairs[:10]
        ]
        or ["- none"]
    )
    lines.extend(["", "## 可输出锚点", ""])
    lines.extend([format_anchor_item(row, "有活动但缺少明显输出/行动连接") for row in output_ready[:10]] or ["- none"])
    lines.extend(["", "## Serendipity", ""])
    lines.extend(serendipity or ["- none"])
    lines.extend(["", "## Provenance", ""])
    lines.append("- excluded_types: automation-report, automation-run")
    if provenance:
        for key, value in sorted(provenance.items()):
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- none")
    lines.extend(["", "## Hot 候选", ""])
    lines.extend([format_anchor_item(row, "近期活动或累计权重达到 hot 阈值") for row in hot_candidates] or ["- none"])
    lines.extend(
        [
            "",
            "## 建议",
            "",
            "- 本周优先补强 `高频但薄` 中最多 1-3 个锚点。",
            "- 本周优先把 `可输出锚点` 中最多 1-3 个转成 query、practice 或文章草稿。",
            "- `重复相近` 和 `长期沉睡` 不在周任务中处理，只进入月度合并/退休 review。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_monthly_governance_report(
    wiki_root: Path,
    end: dt.date,
    rows: list[AnchorGovernance],
    dormant_days: int,
    similar_pairs: list[tuple[AnchorPage, AnchorPage, float]],
    force: bool,
    miss_total: int = 0,
    miss_clusters: list[tuple[str, int, list[str]]] | None = None,
) -> tuple[Path, bool]:
    ensure_dirs(wiki_root)
    path = wiki_root / "_meta" / "anchor-governance" / "monthly" / f"{end:%Y-%m}-merge-retire-review.md"
    if path.exists() and not force:
        return path, False
    dormant = [row for row in rows if is_dormant(row.anchor, end, dormant_days, row.recent_events)]
    lines = [
        "---",
        f"title: Anchor Merge Retire Review {end:%Y-%m}",
        f"created: {end.isoformat()}",
        "type: governance-review",
        "tags: [second-brain, anchor-governance, monthly]",
        "---",
        "",
        f"# Anchor Merge Retire Review {end:%Y-%m}",
        "",
        "## 处理闸门",
        "",
        "- 本文件是本月唯一一次合并/退休处理入口。",
        "- 本文件只生成候选，不直接合并、重命名、删除或退休锚点。",
        "- 真正修改 canonical anchor 页面前必须由东哥确认；旧 links 层已从 active wiki 移除，回滚只查 migration-map 或 git 历史。",
        "",
        "## 合并候选",
        "",
    ]
    lines.extend(
        [
            f"- {left.wiki_link} <-> {right.wiki_link}：title_similarity={ratio:.2f}；建议人工判断是否保留主锚点、别名和跳转说明"
            for left, right, ratio in similar_pairs[:20]
        ]
        or ["- none"]
    )
    lines.extend(["", "## 退休候选", ""])
    lines.extend([format_anchor_item(row, f"超过 {dormant_days} 天未使用，建议人工确认是否退休") for row in dormant] or ["- none"])
    lines.extend(["", "## 新领域候选 — 建议新建知识点（concept/query/practice），不是锚点候选", ""])
    lines.append(f"- 本期知识 miss 记录：{miss_total} 条（来源 `_meta/knowledge-events/knowledge-misses/`）")
    if miss_clusters:
        lines.extend(
            f"- 主题「{unit}」：{count} 次未命中；例：{'；'.join(examples)}。"
            "建议人工判断是否新建知识点（concept/query/practice）；"
            "这不是 `candidate` 锚点候选，也不作为 C1 锚点训练证据。"
            for unit, count, examples in miss_clusters
        )
    else:
        lines.append("- 未达到聚类阈值（同主题 ≥3 次未命中），本月无新领域知识点候选。")
    lines.extend(
        [
            "",
            "## 本月动作上限",
            "",
            "- 最多合并 1-3 组锚点。",
            "- 最多退休 1-3 个锚点。",
            "- 最多新增 1-3 个 candidate 锚点提案。",
            "- 如果候选不够确定，本月宁可不处理。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, True


def cmd_route(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    # Route targets second-brain KNOWLEDGE points (semantic), never cognitive anchors.
    if args.record_slug:
        if not args.record:
            raise SystemExit("--record-slug requires --record")
        hits = []
        seen: set[str] = set()
        wiki_root_resolved = args.wiki_root.resolve()
        for raw_slug in args.record_slug:
            slug = raw_slug.removeprefix("llm-wiki/").strip("/")
            page_path = (args.wiki_root / f"{slug}.md").resolve()
            if slug in seen:
                continue
            if route_excluded(slug):
                raise SystemExit(f"refused non-knowledge or excluded route slug: {slug}")
            if not page_path.is_relative_to(wiki_root_resolved) or not page_path.is_file():
                raise SystemExit(f"route slug does not resolve to a knowledge page: {slug}")
            hits.append((slug, 1.0, "explicitly accepted from prior dry run"))
            seen.add(slug)
    else:
        try:
            hits = route_knowledge(args.query, args.limit, wiki_root=args.wiki_root)
        except RuntimeError as exc:
            raise SystemExit(f"route backend failed; no miss recorded: {exc}")
    # Miss = caller's --miss judgment (hits don't cover the query => new territory),
    # or genuinely zero hits. RRF scores can't auto-detect this (see note above).
    is_miss = args.miss or not hits
    ledger = None
    miss_logged = False
    if args.record:
        if is_miss:
            append_knowledge_miss(args.wiki_root, day, args.query, args.source)
            miss_logged = True
        else:
            events = [
                {
                    "date": day.isoformat(),
                    "slug": slug,
                    "action": "knowledge-hit",
                    "score": round(score, 4),
                    "weight_delta": 1,
                    "query": args.query,
                    "source": args.source,
                }
                for slug, score, _excerpt in hits
            ]
            ledger = append_knowledge_events(args.wiki_root, day, events)
    payload = {
        "query": args.query,
        "recorded": args.record,
        "miss": is_miss,
        "miss_logged": miss_logged,
        "dry_run": not args.record,
        "ledger": str(ledger) if ledger else None,
        "hits": [
            {"slug": slug, "score": round(score, 4), "excerpt": excerpt}
            for slug, score, excerpt in hits
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_recall_check(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    anchors = {anchor.title: anchor for anchor in iter_anchor_pages(args.wiki_root)}
    if args.anchor not in anchors:
        raise SystemExit(f"unknown anchor: {args.anchor}")
    anchor = anchors[args.anchor]
    validate_human_event_source(args.source, "recall-check")
    match, score = recall_match(anchor, args.response)
    report = write_recall_check_report(args.wiki_root, day, anchor, args.response, match, score)
    append_events(
        args.wiki_root,
        day,
        [
            {
                "date": day.isoformat(),
                "anchor": anchor.title,
                "anchor_path": anchor.relative_path + ".md",
                "source": args.source,
                "action": "recall-check",
                "weight_delta": 0,
                "summary": f"recall-check match={match} score={score:.2f}",
                "match": match,
                "score": round(score, 4),
            }
        ],
    )
    print(f"created recall check {report}")
    print(f"match={match} score={score:.2f}")


def cmd_morning_recall(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    target_path = morning_recall_path(args.wiki_root, day)
    anchors = {anchor.title: anchor for anchor in iter_anchor_pages(args.wiki_root)}
    if target_path.exists() and not args.force:
        existing_anchor = existing_morning_recall_anchor(target_path)
        existing_page = anchors.get(existing_anchor)
        review_state = build_anchor_review_states(args.wiki_root, day).get(existing_anchor)
        payload = {
            "date": day.isoformat(),
            "anchor": existing_anchor or None,
            "selection_score": None,
            "path": str(target_path),
            "dry_run": args.dry_run,
            "created": False,
            "existing_file": True,
        }
        if existing_page:
            status, trigger_section, answer_section = morning_recall_sections(existing_page)
            payload["capsule_status"] = status
            payload["trigger_section"] = trigger_section
            payload["answer_section"] = answer_section
        if review_state:
            payload["review_interval_days"] = review_state.interval_days
            payload["next_review_date"] = review_state.next_review.isoformat()
            payload["review_due"] = review_state.due
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if existing_anchor:
            print(f"今日锚点：{existing_anchor}")
        return
    anchor, score, review_state = select_morning_recall_anchor(args.wiki_root, day)
    status, trigger_section, answer_section = morning_recall_sections(anchor)
    due_candidates = [
        {
            "anchor": state.anchor.title,
            "next_review": state.next_review.isoformat(),
            "interval_days": state.interval_days,
            "aligned_streak": state.aligned_streak,
        }
        for state in sorted(due_active_review_states(args.wiki_root, day), key=lambda item: (item.next_review, item.anchor.title))
    ]
    payload = {
        "date": day.isoformat(),
        "anchor": anchor.title,
        "selection_score": score,
        "review_interval_days": review_state.interval_days,
        "next_review_date": review_state.next_review.isoformat(),
        "review_due": review_state.due,
        "due_candidates": due_candidates,
        "capsule_status": status,
        "trigger_section": trigger_section,
        "answer_section": answer_section,
        "path": str(target_path),
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    path = write_morning_recall_file(args.wiki_root, day, anchor, score, args.force)
    review_md, review_json = write_anchor_review_state(args.wiki_root, day)
    graduate_report = write_graduate_candidate_report(args.wiki_root, day)
    payload["path"] = str(path)
    payload["review_state"] = str(review_md)
    payload["review_state_json"] = str(review_json)
    payload["graduate_candidate_report"] = str(graduate_report)
    payload["created"] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"今日锚点：{anchor.title}")


def cmd_morning_recall_review(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    anchors = {anchor.title: anchor for anchor in iter_anchor_pages(args.wiki_root)}
    path: Path | None = None
    file_text = ""
    file_frontmatter: dict[str, object] = {}
    anchor_title = args.anchor
    response = args.response or ""
    rating = normalize_recall_rating(args.rating or "")

    if not anchor_title:
        path = find_morning_recall_file(args.wiki_root, day, args.latest_before_today)
        if not path:
            print(json.dumps({"status": "no-file", "date": day.isoformat()}, ensure_ascii=False, indent=2))
            return
        file_text = path.read_text(encoding="utf-8")
        file_frontmatter, _ = parse_frontmatter(file_text)
        if file_frontmatter.get("status") == "reviewed" and not args.force:
            print(
                json.dumps(
                    {"status": "already-reviewed", "path": str(path), "anchor": file_frontmatter.get("anchor")},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        anchor_title = str(file_frontmatter.get("anchor") or "").strip() or existing_morning_recall_anchor(path)
        if not response:
            response = extract_recall_response(file_text)
        if not rating:
            rating = extract_manual_rating(file_text)

    if not anchor_title or anchor_title not in anchors:
        raise SystemExit(f"unknown anchor: {anchor_title or '-'}")
    anchor = anchors[anchor_title]

    if not response.strip() and not rating:
        print(
            json.dumps(
                {
                    "status": "pending",
                    "anchor": anchor.title,
                    "path": str(path) if path else None,
                    "message": "no recall response or manual rating found",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    # 自评分(manual rating)优先于字面 auto-score：人脑判断说了算，字面分仅作参考。
    auto_match, auto_score = (recall_match(anchor, response) if response.strip() else (None, None))
    if rating:
        match, score = match_from_manual_rating(rating)
        if not response.strip():
            response = f"Manual rating only: {rating}"
    elif response.strip():
        match, score = auto_match, auto_score
    else:
        match, score = match_from_manual_rating(rating)
        response = f"Manual rating only: {rating}"

    should_update_anchor = match in {"full", "partial"} or rating in {"✅", "⚠️"}
    validate_human_event_source(args.source, "recall-check")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry-run",
                    "anchor": anchor.title,
                    "match": match,
                    "score": round(score, 4),
                    "manual_rating": rating or None,
                    "anchor_frontmatter_would_update": False,
                    "path": str(path) if path else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    report = write_recall_check_report(args.wiki_root, day, anchor, response, match, score)
    event = {
        "date": day.isoformat(),
        "anchor": anchor.title,
        "anchor_path": anchor.relative_path + ".md",
        "source": args.source,
        "action": "recall-check",
        "weight_delta": 0,
        "summary": f"morning-recall match={match} score={score:.2f}",
        "match": match,
        "score": round(score, 4),
        "report": str(report),
    }
    if rating:
        event["manual_rating"] = rating
        if auto_match is not None:
            event["auto_match"] = auto_match
            event["auto_score"] = round(auto_score, 4)
    if path:
        event["morning_file"] = str(path)
    append_events(args.wiki_root, day, [event])

    if should_update_anchor and args.apply_frontmatter:
        update_anchor_after_recall(anchor, day, rating, match, score)
    if path:
        update_morning_recall_status(path, day, match, score, rating)

    print(
        json.dumps(
            {
                "status": "reviewed",
                "anchor": anchor.title,
                "match": match,
                "score": round(score, 4),
                "manual_rating": rating or None,
                "anchor_frontmatter_updated": False,
                "report": str(report),
                "path": str(path) if path else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_mechanism_audit(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    report = write_mechanism_audit_report(args.wiki_root, day, args.apply)
    print(f"created mechanism audit {report}")


def cmd_governance_audit(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    freeze_start = parse_date(args.freeze_start)
    report = write_governance_audit_report(args.wiki_root, day, freeze_start)
    print(f"created governance audit {report}")


def cmd_judgment_add(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    review_date = parse_date(args.review_date)
    path = append_judgment(
        args.wiki_root,
        day,
        args.topic,
        args.stance,
        args.confidence,
        review_date,
        args.anchor or [],
    )
    print(f"recorded judgment in {path}")


def cmd_judgment_due(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    due = due_judgments(args.wiki_root, day)
    print(json.dumps({"date": day.isoformat(), "due": due}, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_event_summary(args: argparse.Namespace) -> None:
    end = parse_date(args.date)
    if args.write_report:
        md_path, json_path = write_event_summary_report(args.wiki_root, end, args.days)
        print(f"created event summary {md_path}")
        print(f"created event summary json {json_path}")
        return

    payload = event_summary_payload(args.wiki_root, end, args.days)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"# Anchor Events Summary ({payload['start']} to {payload['end']})")
    print()
    print(f"- events: {payload['events']}")
    print(f"- open_writeback_proposals: {payload['open_writeback_proposals']}")
    print()
    print("## Anchors")
    for row in payload["anchors"]:
        print(f"- {row['anchor']}: events={row['events']}, weight_delta={row['weight_delta']}")
    if not payload["anchors"]:
        print("- none")


def cmd_sync_frontmatter(args: argparse.Namespace) -> None:
    end = parse_date(args.date)
    changes = build_frontmatter_changes(args.wiki_root, end, args.hot_weight, args.mark_candidates)
    if args.apply:
        apply_frontmatter_changes(changes)
    report = write_frontmatter_sync_report(args.wiki_root, end, changes, args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"{mode} {len(changes)} frontmatter change(s)")
    print(f"created frontmatter sync report {report}")


def cmd_automation_run(args: argparse.Namespace) -> None:
    end = parse_date(args.date)
    ensure_dirs(args.wiki_root)
    days = 1 if args.mode == "daily" else 7
    generated: list[Path] = []

    event_md, event_json = write_event_summary_report(args.wiki_root, end, days)
    generated.extend([event_md, event_json])

    changes = build_frontmatter_changes(args.wiki_root, end, args.hot_weight, args.mark_candidates)
    if args.apply_frontmatter:
        apply_frontmatter_changes(changes)
    frontmatter_report = write_frontmatter_sync_report(args.wiki_root, end, changes, args.apply_frontmatter)
    generated.append(frontmatter_report)
    generated.append(build_anchor_dashboard(args.wiki_root, end))

    if args.mode == "weekly":
        rows = build_anchor_governance(args.wiki_root, end, 7, args.hot_events, args.hot_weight)
        pairs = similar_anchor_pairs([row.anchor for row in rows], args.similarity)
        generated.append(write_weekly_governance_report(args.wiki_root, end, 7, rows, args.dormant_days, pairs))

    report_path = args.wiki_root / "_meta" / "automation-runs" / f"{end.isoformat()}-second-brain-{args.mode}.md"
    lines = [
        "---",
        f"title: 第二大脑自动化运行 {args.mode} {end.isoformat()}",
        f"created: {end.isoformat()}",
        "type: automation-report",
        "tags: [second-brain, automation, hbrain-loop]",
        "---",
        "",
        f"# 第二大脑自动化运行 {args.mode} {end.isoformat()}",
        "",
        "## 内部脚本",
        "",
        f"- event summary: {event_md.relative_to(args.wiki_root)}",
        f"- event summary json: {event_json.relative_to(args.wiki_root)}",
        f"- frontmatter sync: {frontmatter_report.relative_to(args.wiki_root)}",
        f"- frontmatter mode: {'apply' if args.apply_frontmatter else 'dry-run'}",
        f"- frontmatter changes: {len(changes)}",
    ]
    if args.mode == "weekly":
        lines.append(f"- weekly governance: {generated[-1].relative_to(args.wiki_root)}")
    lines.extend(
        [
            "",
            "## Hermes 编排职责",
            "",
            "- 调用本脚本。",
            "- 运行 Gbrain sync / health / recall 验证。",
            "- 运行 CASS `cm context`，把 agent 工作经验沉淀到报告或后续 writeback proposal。",
            "- 不在 prompt 中手写权重、last_active 或生命周期状态。",
            "",
            "## 外部验证命令",
            "",
            "```bash",
            "gbrain sync --source hbrain --repo /Users/jianghaidong/hbrain/llm-wiki --no-pull --yes",
            "gbrain health",
            "gbrain search \"第二大脑最高指导思想\" --limit 5",
            f"cm context \"第二大脑{args.mode}自动化 frontmatter anchor-events agent经验\" --json --limit 5 --history 5",
            "```",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"created automation run report {report_path}")
    for path in generated:
        print(f"generated {path}")


def cmd_ensure(args: argparse.Namespace) -> None:
    ensure_dirs(args.wiki_root)
    print(f"ensured {args.wiki_root / '_meta' / 'anchor-events'}")
    print(f"ensured {args.wiki_root / '_meta' / 'writeback-inbox'}")
    print(f"ensured {args.wiki_root / '_meta' / 'anchor-governance'}")


def cmd_record(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    ensure_dirs(args.wiki_root)
    validate_human_event_source(args.source, args.action)
    path = month_path(args.wiki_root, day)
    events = []
    for anchor in args.anchor:
        events.append(
            {
                "date": day.isoformat(),
                "anchor": anchor,
                "source": args.source,
                "action": args.action,
                "weight_delta": args.weight_delta,
                "summary": args.summary,
            }
        )
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"recorded {len(events)} event(s) in {path}")

    if args.proposal_title:
        body = args.proposal_body or ""
        if args.proposal_body_file:
            body = Path(args.proposal_body_file).read_text(encoding="utf-8")
        proposal = write_proposal(
            args.wiki_root,
            day,
            args.proposal_title,
            args.target_layer,
            body or args.summary,
            args.anchor,
            args.provenance,
            args.judgment_changed,
        )
        print(f"created proposal {proposal}")


def cmd_event_source_cleanup(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    report = write_event_source_cleanup_report(args.wiki_root, day)
    count = len(non_human_event_source_items(args.wiki_root))
    print(f"created event source cleanup proposal {report}")
    print(f"non_whitelist_human_events={count}")


def cmd_lint_capsule(args: argparse.Namespace) -> None:
    payload = capsule_lint_payload(args.wiki_root)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"anchors_scanned={payload['anchors_scanned']}")
    print(f"capsule_issues={payload['issues']}")
    for code, count in payload["counts"].items():
        print(f"{code}={count}")
    for item in payload["items"]:
        print(
            f"{item['anchor_path']}: {item['code']} "
            f"({item['severity']}) {item['message']}"
        )


def cmd_anchors_review_state(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    payload = review_state_payload(args.wiki_root, day)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    md_path, json_path = write_anchor_review_state(args.wiki_root, day)
    graduate_report = write_graduate_candidate_report(args.wiki_root, day)
    print(f"generated anchor review state {md_path}")
    print(f"generated anchor review state json {json_path}")
    print(f"generated graduate candidate report {graduate_report}")


def cmd_summarize(args: argparse.Namespace) -> None:
    end = parse_date(args.date)
    since = end - dt.timedelta(days=args.days - 1)
    # 与 weight 派生口径一致：cognitive-loop 摘要只统计人脑面事件，
    # agent-chat 自动化事件归 _meta/automation-runs/。
    events = [e for e in iter_events_between(args.wiki_root, since, end) if is_human_facing_event(e)]
    counts = Counter()
    deltas = defaultdict(int)
    for event in events:
        anchor = event.get("anchor", "unknown")
        counts[anchor] += 1
        deltas[anchor] += parse_int(event.get("weight_delta"))

    open_proposals = open_writeback_proposals(args.wiki_root)

    print(f"# Hbrain Cognitive Loop Summary ({since.isoformat()} to {end.isoformat()})")
    print()
    print(f"- events: {len(events)}")
    print(f"- open_writeback_proposals: {len(open_proposals)}")
    print()
    print("## Anchors")
    if counts:
        for anchor, count in counts.most_common():
            print(f"- {anchor}: events={count}, weight_delta={deltas[anchor]}")
    else:
        print("- none")
    print()
    print("## Latest Proposals")
    for path in open_proposals[-5:]:
        print(f"- {path.name}")
    if not open_proposals:
        print("- none")


def cmd_govern_weekly(args: argparse.Namespace) -> None:
    end = parse_date(args.date)
    rows = build_anchor_governance(args.wiki_root, end, args.days, args.hot_events, args.hot_weight)
    pairs = similar_anchor_pairs([row.anchor for row in rows], args.similarity)
    path = write_weekly_governance_report(args.wiki_root, end, args.days, rows, args.dormant_days, pairs)
    print(f"created weekly governance report {path}")


def cmd_govern_monthly(args: argparse.Namespace) -> None:
    end = parse_date(args.date)
    rows = build_anchor_governance(args.wiki_root, end, args.days, args.hot_events, args.hot_weight)
    pairs = similar_anchor_pairs([row.anchor for row in rows], args.similarity)
    miss_records = read_knowledge_misses(args.wiki_root, end - dt.timedelta(days=args.days), end)
    miss_clusters = cluster_router_misses(miss_records, args.miss_cluster_min)
    path, created = write_monthly_governance_report(
        args.wiki_root, end, rows, args.dormant_days, pairs, args.force, len(miss_records), miss_clusters
    )
    if created:
        print(f"created monthly merge/retire review {path}")
    else:
        print(f"monthly merge/retire review already exists {path}")


def cmd_knowledge_dashboard(args: argparse.Namespace) -> None:
    day = parse_date(args.date)
    events_dir = args.wiki_root / "_meta" / "knowledge-events"
    slug_data: dict[str, dict] = {}
    total_events = 0
    for jsonl_path in sorted(events_dir.glob("*.jsonl")):
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("action") != "knowledge-hit":
                    continue
                slug = event.get("slug", "")
                if not slug or route_excluded(slug):
                    continue
                total_events += 1
                event_date = dt.date.fromisoformat(event["date"])
                age_days = (day - event_date).days
                decay = 0.5 ** (age_days / 14)
                if slug not in slug_data:
                    slug_data[slug] = {"hits": 0, "hot_score": 0.0, "last_seen": event_date}
                slug_data[slug]["hits"] += 1
                slug_data[slug]["hot_score"] += decay
                if event_date > slug_data[slug]["last_seen"]:
                    slug_data[slug]["last_seen"] = event_date
    rows = sorted(slug_data.items(), key=lambda x: x[1]["hot_score"], reverse=True)
    out_dir = args.wiki_root / "_meta" / "knowledge"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "hot-knowledge.md"
    lines = [
        "---",
        f"date: {day.isoformat()}",
        "type: automation-report",
        "---",
        "",
        f"# 热知识看板 {day.isoformat()}",
        "",
        f"- 生成日期：{day.isoformat()}",
        f"- 总命中事件数：{total_events}",
        f"- 涉及知识点数：{len(rows)}",
        "",
        "| slug | hits | hot_score | last_seen |",
        "|------|------|-----------|-----------|",
    ]
    for slug, data in rows:
        lines.append(f"| {slug} | {data['hits']} | {data['hot_score']:.4f} | {data['last_seen'].isoformat()} |")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"total events: {total_events}, unique slugs: {len(rows)}")


def cmd_knowledge_candidates(args: argparse.Namespace) -> None:
    """Generate anchor promotion candidates from knowledge-hit events.

    Replaces the old graduate-candidate signal (which was based on anchor recall intervals).
    This command only produces a candidate list — it never auto-modifies any anchor frontmatter.
    Human decides whether to promote.
    """
    day = parse_date(args.date)
    events_dir = args.wiki_root / "_meta" / "knowledge-events"

    # Aggregate knowledge-hit events: slug -> {distinct_days: set, hits: int, hot_score: float, last_seen: date}
    slug_dates: dict[str, set[dt.date]] = {}
    slug_hits: dict[str, int] = {}
    slug_hot: dict[str, float] = {}
    slug_last: dict[str, dt.date] = {}

    for jsonl_path in sorted(events_dir.glob("*.jsonl")):
        # Only top-level .jsonl (hits), skip knowledge-misses/ subdirectory
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("action") != "knowledge-hit":
                    continue
                slug = event.get("slug", "")
                if not slug or route_excluded(slug):
                    continue
                try:
                    event_date = dt.date.fromisoformat(event["date"])
                except (KeyError, ValueError):
                    continue
                age_days = (day - event_date).days
                decay = 0.5 ** (age_days / 14)

                if slug not in slug_dates:
                    slug_dates[slug] = set()
                    slug_hits[slug] = 0
                    slug_hot[slug] = 0.0
                    slug_last[slug] = event_date
                slug_dates[slug].add(event_date)
                slug_hits[slug] += 1
                slug_hot[slug] += decay
                if event_date > slug_last[slug]:
                    slug_last[slug] = event_date

    # Current anchor relative_paths (for exclusion)
    anchor_paths: set[str] = {
        page.relative_path for page in iter_anchor_pages(args.wiki_root)
    }

    # Filter candidates: distinct_days >= 3 AND not already an anchor
    candidates: list[tuple[str, int, int, float, dt.date]] = []
    for slug in slug_dates:
        distinct_days = len(slug_dates[slug])
        if distinct_days >= 3 and slug not in anchor_paths:
            candidates.append((
                slug,
                distinct_days,
                slug_hits[slug],
                slug_hot[slug],
                slug_last[slug],
            ))

    # Sort by distinct_days desc, then hot_score desc
    candidates.sort(key=lambda x: (-x[1], -x[3]))

    out_dir = args.wiki_root / "_meta" / "knowledge"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "promotion-candidates.md"

    lines = [
        "---",
        f"date: {day.isoformat()}",
        "type: automation-report",
        "---",
        "",
        "# 锚点晋升候选信号",
        "",
        "> 本报告替代旧 graduate-candidate（原基于锚点 recall 间隔）的晋升信号。",
        "> 只生成候选清单，绝不自动修改任何 anchor frontmatter，升不升由人定。",
        "",
        f"- 生成日期：{day.isoformat()}",
        f"- 规则：knowledge-hit 命中 slug 在 >=3 个不同日期被查询撞到（持续回访）且该 slug 非当前认知锚点",
        f"- 候选数：{len(candidates)}",
        "",
    ]

    if not candidates:
        lines.append("- none")
    else:
        lines.append("| slug | distinct_days | hits | hot_score | last_seen |")
        lines.append("|------|--------------|------|-----------|-----------|")
        for slug, distinct_days, hits, hot_score, last_seen in candidates:
            lines.append(
                f"| {slug} | {distinct_days} | {hits} | {hot_score:.4f} | {last_seen.isoformat()} |"
            )

    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"candidates: {len(candidates)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hbrain cognitive loop helper")
    parser.add_argument("--wiki-root", type=Path, default=DEFAULT_WIKI_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    ensure_parser = sub.add_parser("ensure", help="create event, inbox, and governance folders")
    ensure_parser.set_defaults(func=cmd_ensure)

    anchors_index = sub.add_parser("anchors-index", help="generate _meta/anchors/index.md from canonical anchors and events")
    anchors_index.add_argument("--date")
    anchors_index.set_defaults(func=lambda args: print(f"generated {build_anchor_dashboard(args.wiki_root, parse_date(args.date))}"))

    anchors_review_state = sub.add_parser(
        "anchors-review-state",
        help="generate derived spaced-repetition state from recall-check events",
    )
    anchors_review_state.add_argument("--date")
    anchors_review_state.add_argument("--json", action="store_true")
    anchors_review_state.set_defaults(func=cmd_anchors_review_state)

    record = sub.add_parser("record", help="append anchor events and optional proposal")
    record.add_argument("--anchor", action="append", required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--date")
    record.add_argument("--source", default="codex-chat")
    record.add_argument("--action", default="used")
    record.add_argument("--weight-delta", type=int, default=1)
    record.add_argument("--proposal-title")
    record.add_argument("--proposal-body")
    record.add_argument("--proposal-body-file")
    record.add_argument("--provenance", choices=PROVENANCE_VALUES, default="对话合成")
    record.add_argument("--judgment-changed", default="待人工判别")
    record.add_argument("--target-layer", default="queries")
    record.set_defaults(func=cmd_record)

    event_source_cleanup = sub.add_parser(
        "event-source-cleanup",
        help="write a proposal-only cleanup list for non-human anchor-event sources",
    )
    event_source_cleanup.add_argument("--date")
    event_source_cleanup.set_defaults(func=cmd_event_source_cleanup)

    lint_capsule = sub.add_parser("lint-capsule", help="lint anchor capsule completeness and minimal-model length")
    lint_capsule.add_argument("--json", action="store_true")
    lint_capsule.set_defaults(func=cmd_lint_capsule)

    summarize = sub.add_parser("summarize", help="summarize recent anchor events")
    summarize.add_argument("--days", type=int, default=7)
    summarize.add_argument("--date")
    summarize.set_defaults(func=cmd_summarize)

    route = sub.add_parser("route", help="semantic-route a question to second-brain knowledge points (not anchors)")
    route.add_argument("--query", required=True)
    route.add_argument("--limit", type=int, default=3)
    route.add_argument("--record", action="store_true")
    route.add_argument(
        "--record-slug",
        action="append",
        help="record an exact knowledge slug accepted from a prior dry run; repeat for multiple hits",
    )
    route.add_argument("--miss", action="store_true", help="caller judges the hits off-point: log a knowledge-miss (new-territory candidate) instead of scoring hits")
    route.add_argument("--write-report", action="store_true", help="(deprecated, inert) the router no longer writes markdown reports")
    route.add_argument("--date")
    route.add_argument("--source", default="hbrain-router")
    route.set_defaults(func=cmd_route)

    recall_check = sub.add_parser("recall-check", help="record a human recall check for one anchor")
    recall_check.add_argument("--anchor", required=True)
    recall_check.add_argument("--response", required=True)
    recall_check.add_argument("--date")
    recall_check.add_argument("--source", default="anchor-morning-recall")
    recall_check.set_defaults(func=cmd_recall_check)

    morning_recall = sub.add_parser("morning-recall", help="create the daily one-anchor recall file")
    morning_recall.add_argument("--date")
    morning_recall.add_argument("--force", action="store_true", help="overwrite the date's recall file")
    morning_recall.add_argument("--dry-run", action="store_true")
    morning_recall.set_defaults(func=cmd_morning_recall)

    morning_review = sub.add_parser("morning-recall-review", help="review a morning recall response and append recall-check event")
    morning_review.add_argument("--date")
    morning_review.add_argument("--latest-before-today", action="store_true")
    morning_review.add_argument("--anchor")
    morning_review.add_argument("--response")
    morning_review.add_argument("--rating", choices=tuple(RECALL_RATING_LABELS))
    morning_review.add_argument("--source", default="anchor-morning-recall")
    morning_review.add_argument("--apply-frontmatter", action="store_true")
    morning_review.add_argument("--force", action="store_true")
    morning_review.add_argument("--dry-run", action="store_true")
    morning_review.set_defaults(func=cmd_morning_recall_review)

    mechanism_audit = sub.add_parser("mechanism-audit", help="audit B-layer mechanisms")
    mechanism_audit.add_argument("--date")
    mechanism_audit.add_argument("--apply", action="store_true")
    mechanism_audit.set_defaults(func=cmd_mechanism_audit)

    governance_audit = sub.add_parser("governance-audit", help="audit C-layer governance without touching Gbrain")
    governance_audit.add_argument("--date")
    governance_audit.add_argument("--freeze-start", default="2026-06-10")
    governance_audit.set_defaults(func=cmd_governance_audit)

    judgment_add = sub.add_parser("judgment-add", help="append an item to the judgment log")
    judgment_add.add_argument("--topic", required=True)
    judgment_add.add_argument("--stance", required=True)
    judgment_add.add_argument("--confidence", type=float, required=True)
    judgment_add.add_argument("--review-date", required=True)
    judgment_add.add_argument("--anchor", action="append")
    judgment_add.add_argument("--date")
    judgment_add.set_defaults(func=cmd_judgment_add)

    judgment_due = sub.add_parser("judgment-due", help="list judgment log entries due for review")
    judgment_due.add_argument("--date")
    judgment_due.set_defaults(func=cmd_judgment_due)

    event_summary = sub.add_parser("event-summary", help="summarize anchor events to stdout or reports")
    event_summary.add_argument("--days", type=int, default=7)
    event_summary.add_argument("--date")
    event_summary.add_argument("--json", action="store_true")
    event_summary.add_argument("--write-report", action="store_true")
    event_summary.set_defaults(func=cmd_event_summary)

    sync_frontmatter = sub.add_parser("sync-frontmatter", help="recompute anchor frontmatter from event logs")
    sync_frontmatter.add_argument("--date")
    sync_frontmatter.add_argument("--hot-weight", type=int, default=5)
    sync_frontmatter.add_argument("--mark-candidates", action="store_true")
    sync_frontmatter.add_argument("--apply", action="store_true")
    sync_frontmatter.set_defaults(func=cmd_sync_frontmatter)

    automation_run = sub.add_parser("automation-run", help="run internal second-brain automation scripts")
    automation_run.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    automation_run.add_argument("--date")
    automation_run.add_argument("--apply-frontmatter", action="store_true")
    automation_run.add_argument("--mark-candidates", action="store_true")
    automation_run.add_argument("--dormant-days", type=int, default=90)
    automation_run.add_argument("--hot-events", type=int, default=3)
    automation_run.add_argument("--hot-weight", type=int, default=5)
    automation_run.add_argument("--similarity", type=float, default=0.72)
    automation_run.set_defaults(func=cmd_automation_run)

    govern_weekly = sub.add_parser("govern-weekly", help="write weekly anchor lifecycle governance report")
    govern_weekly.add_argument("--days", type=int, default=7)
    govern_weekly.add_argument("--date")
    govern_weekly.add_argument("--dormant-days", type=int, default=90)
    govern_weekly.add_argument("--hot-events", type=int, default=3)
    govern_weekly.add_argument("--hot-weight", type=int, default=5)
    govern_weekly.add_argument("--similarity", type=float, default=0.72)
    govern_weekly.set_defaults(func=cmd_govern_weekly)

    govern_monthly = sub.add_parser("govern-monthly", help="write one monthly merge/retire review")
    govern_monthly.add_argument("--days", type=int, default=31)
    govern_monthly.add_argument("--date")
    govern_monthly.add_argument("--dormant-days", type=int, default=90)
    govern_monthly.add_argument("--hot-events", type=int, default=3)
    govern_monthly.add_argument("--hot-weight", type=int, default=5)
    govern_monthly.add_argument("--similarity", type=float, default=0.72)
    govern_monthly.add_argument("--miss-cluster-min", type=int, default=3)
    govern_monthly.add_argument("--force", action="store_true")
    govern_monthly.set_defaults(func=cmd_govern_monthly)

    knowledge_dashboard = sub.add_parser("knowledge-dashboard", help="generate hot-knowledge dashboard from knowledge-hit events")
    knowledge_dashboard.add_argument("--date")
    knowledge_dashboard.set_defaults(func=cmd_knowledge_dashboard)

    knowledge_candidates = sub.add_parser("knowledge-candidates", help="generate anchor promotion candidates from knowledge-hit events")
    knowledge_candidates.add_argument("--date")
    knowledge_candidates.set_defaults(func=cmd_knowledge_candidates)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
