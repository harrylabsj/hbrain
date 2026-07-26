import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import five_domain_daily as fdd


DAY = date(2026, 7, 25)
GENERATED = date(2026, 7, 26)


def write_jsonl(path: Path, rows: list[dict], raw: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows) + raw
    path.write_text(text, encoding="utf-8")
    return path


class FiveDomainDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.wiki = root / "wiki"
        self.facts = root / "facts"
        self.projects = root / "projects"
        self.receipts = root / "receipts"
        for path in (self.wiki, self.facts, self.projects, self.receipts):
            path.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, day=DAY):
        return fdd.build_report(
            day=day,
            generated=GENERATED,
            wiki_root=self.wiki,
            facts_root=self.facts,
            projects_root=self.projects,
            receipts_root=self.receipts,
        )

    def seed_all_domains(self):
        write_jsonl(
            self.facts / "events" / "2026-07.jsonl",
            [
                {
                    "event_id": "fact_dayone",
                    "occurred_at": "2026-07-25T10:00:00+08:00",
                    "summary": "完成五域日报切片",
                    "source_ref": "test:facts",
                    "verification": "observed",
                },
                {
                    "event_id": "fact_prevday",
                    "occurred_at": "2026-07-24T10:00:00+08:00",
                    "summary": "前一天的事实",
                    "source_ref": "test:old",
                    "verification": "observed",
                },
            ],
        )
        write_jsonl(
            self.facts / "inbox" / "2026-07-25.jsonl",
            [{"proposal_id": "proposal_one", "summary": "待审事实提案", "source_ref": "test:inbox"}],
        )
        write_jsonl(
            self.projects / "audit" / "applied.jsonl",
            [
                {
                    "proposal_id": "project_change_a",
                    "project_id": "hbrain",
                    "evidence": {"fact_id": "fact_dayone", "decision_ref": None},
                    "changes": {"next_action": "写日报"},
                },
                {
                    "proposal_id": "project_change_b",
                    "project_id": "hbrain",
                    "evidence": {"fact_id": "fact_prevday", "decision_ref": None},
                    "changes": {"status": "active"},
                },
                {
                    "proposal_id": "project_change_c",
                    "project_id": "hbrain",
                    "evidence": {"fact_id": None, "decision_ref": "decision:manual"},
                    "changes": {"phase": "p4"},
                },
            ],
        )
        write_jsonl(
            self.wiki / "_meta" / "knowledge-learning" / "2026-07.jsonl",
            [
                {"date": "2026-07-25", "status": "canonical-candidate", "title": "候选知识", "path": "concepts/x.md", "confidence": 0.9},
                {"date": "2026-07-25", "status": "proposal", "title": "待审知识", "path": "_meta/writeback-inbox/x.md"},
                {"date": "2026-07-24", "status": "canonical-candidate", "title": "昨日知识", "path": "concepts/y.md"},
            ],
        )
        write_jsonl(
            self.wiki / "_meta" / "knowledge-events" / "2026-07.jsonl",
            [{"action": "knowledge-hit", "date": "2026-07-25", "slug": "concepts/第二大脑"}],
        )
        write_jsonl(
            self.wiki / "_meta" / "knowledge-events" / "knowledge-misses" / "2026-07.jsonl",
            [{"date": "2026-07-25", "query": "缺失的问题", "source": "test"}],
        )
        write_jsonl(
            self.receipts / "inbox" / "2026-07.jsonl",
            [
                {
                    "receipt_id": "receipt_one",
                    "completed_at": "2026-07-25T18:00:00+08:00",
                    "experience_candidate": [{"summary": "先实现再自审"}],
                    "knowledge_gap": [{"question": "日报放哪里", "why_missing": "无规范"}],
                    "evidence": [{"type": "test", "ref": "test:test_five_domain_daily"}],
                },
                {
                    "receipt_id": "receipt_old",
                    "completed_at": "2026-07-24T18:00:00+08:00",
                    "experience_candidate": [{"summary": "旧经验"}],
                    "knowledge_gap": [],
                    "evidence": [{"type": "test", "ref": "test:old"}],
                },
            ],
        )

    def test_five_domain_summary(self):
        self.seed_all_domains()
        text, payload = self.build()
        self.assertEqual(payload["report_for"], "2026-07-25")
        self.assertTrue(payload["proposal_only"])
        self.assertFalse(payload["auto_promote"])
        domains = payload["domains"]
        self.assertEqual(domains["facts"], {"events": 1, "proposals": 1})
        self.assertEqual(domains["projects"]["changes"], 1)
        self.assertEqual(domains["knowledge"], {"candidates": 1, "proposals": 1, "hits": 1})
        self.assertEqual(domains["experience"]["candidates"], 1)
        self.assertEqual(domains["evidence"]["items"], 2)
        self.assertEqual(domains["knowledge_gaps"], {"receipt_gaps": 1, "misses": 1})
        self.assertEqual(payload["issue_count"], 0)
        for marker in (
            "完成五域日报切片", "待审事实提案", "project_change_a", "候选知识", "待审知识",
            "先实现再自审", "日报放哪里", "缺失的问题", "test:test_five_domain_daily",
            "test:facts", "proposal_only: true", "auto_promote: false", "派生视图",
        ):
            self.assertIn(marker, text)
        self.assertNotIn("前一天的事实", text)
        self.assertNotIn("旧经验", text)

    def test_date_filtering_uses_only_the_day(self):
        write_jsonl(
            self.facts / "events" / "2026-06.jsonl",
            [{"event_id": "fact_june", "occurred_at": "2026-06-25T10:00:00+00:00", "summary": "六月事实", "source_ref": "t", "verification": "observed"}],
        )
        write_jsonl(
            self.facts / "events" / "2026-07.jsonl",
            [{"event_id": "fact_july24", "occurred_at": "2026-07-24T23:00:00+00:00", "summary": "七月24日事实", "source_ref": "t", "verification": "observed"}],
        )
        write_jsonl(
            self.receipts / "inbox" / "2026-07.jsonl",
            [{"receipt_id": "receipt_old", "completed_at": "2026-07-24T10:00:00+00:00", "experience_candidate": ["旧经验"], "knowledge_gap": [], "evidence": []}],
        )
        text, payload = self.build()
        self.assertEqual(payload["domains"]["facts"]["events"], 0)
        self.assertEqual(payload["domains"]["experience"]["candidates"], 0)
        self.assertNotIn("六月事实", text)
        self.assertNotIn("七月24日事实", text)
        self.assertNotIn("旧经验", text)

    def test_each_domain_is_capped_at_twenty(self):
        write_jsonl(
            self.facts / "events" / "2026-07.jsonl",
            [
                {
                    "event_id": f"fact_{index:03d}",
                    "occurred_at": f"2026-07-25T10:{index:02d}:00+08:00",
                    "summary": f"事实 {index}",
                    "source_ref": "test",
                    "verification": "observed",
                }
                for index in range(25)
            ],
        )
        text, payload = self.build()
        self.assertEqual(payload["domains"]["facts"]["events"], 25)
        self.assertEqual(payload["max_items_per_domain"], 20)
        self.assertIn("另有 5 条未展示", text)
        self.assertIn("事实 19", text)
        self.assertNotIn("事实 24", text)

    def test_invalid_json_lines_become_issues_without_crashing(self):
        write_jsonl(
            self.facts / "events" / "2026-07.jsonl",
            [{"event_id": "fact_ok", "occurred_at": "2026-07-25T10:00:00+08:00", "summary": "好行", "source_ref": "t", "verification": "observed"}],
            raw="this is not json\n",
        )
        write_jsonl(self.receipts / "inbox" / "2026-07.jsonl", [], raw="{broken\n")
        text, payload = self.build()
        self.assertEqual(payload["issue_count"], 2)
        issues = "\n".join(item["issue"] for item in payload["issues"])
        self.assertIn("invalid_json", issues)
        self.assertEqual(payload["domains"]["facts"]["events"], 1)
        self.assertIn("好行", text)
        self.assertIn("## 数据问题", text)

    def test_secret_like_content_is_redacted(self):
        write_jsonl(
            self.facts / "events" / "2026-07.jsonl",
            [{"event_id": "fact_secret", "occurred_at": "2026-07-25T10:00:00+08:00", "summary": "泄露 api_key=abc123 的内容", "source_ref": "https://x/?api_key=abc123", "verification": "observed"}],
        )
        text, payload = self.build()
        self.assertNotIn("abc123", text)
        self.assertIn("[REDACTED]", text)
        self.assertGreaterEqual(payload["redacted_values"], 1)

    def run_cli(self, *argv):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = fdd.main(list(argv))
        return code, stdout.getvalue()

    def cli_args(self, output):
        return [
            "--for-date", "2026-07-25",
            "--report-date", "2026-07-26",
            "--wiki-root", str(self.wiki),
            "--facts-root", str(self.facts),
            "--projects-root", str(self.projects),
            "--receipts-root", str(self.receipts),
            "--output", str(output),
            "--json",
        ]

    def test_no_write_leaves_no_file(self):
        self.seed_all_domains()
        output = Path(self.temp.name) / "report.md"
        code, out = self.run_cli(*self.cli_args(output), "--no-write")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["written"])
        self.assertEqual(payload["domains"]["facts"]["events"], 1)
        self.assertFalse(output.exists())

    def test_write_is_atomic_and_marks_proposal_only(self):
        self.seed_all_domains()
        output = Path(self.temp.name) / "nested" / "report.md"
        code, out = self.run_cli(*self.cli_args(output))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["written"])
        text = output.read_text(encoding="utf-8")
        self.assertIn("proposal_only: true", text)
        self.assertIn("auto_promote: false", text)

    def test_output_symlink_is_rejected(self):
        self.seed_all_domains()
        target = Path(self.temp.name) / "target.md"
        target.write_text("safe\n", encoding="utf-8")
        output = Path(self.temp.name) / "report.md"
        output.symlink_to(target)
        code, _ = self.run_cli(*self.cli_args(output))
        self.assertEqual(code, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "safe\n")
        with self.assertRaises(fdd.DailyError):
            fdd.atomic_write_report(output, "x")


if __name__ == "__main__":
    unittest.main()
