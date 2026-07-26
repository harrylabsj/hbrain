import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_growth as kg


class KnowledgeGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wiki = Path(self.temp.name) / "wiki"
        for layer in kg.LAYERS:
            (self.wiki / layer).mkdir(parents=True)
        (self.wiki / "_meta" / "knowledge-events" / "knowledge-misses").mkdir(parents=True)
        (self.wiki / "_meta" / "writeback-inbox").mkdir(parents=True)
        self._page("concepts/第二大脑.md", "第二大脑", "2026-01-01")
        self._page("concepts/AI Agent.md", "AI Agent", "2026-01-01")

    def tearDown(self):
        self.temp.cleanup()

    def _page(self, relative, title, created, updated=None, status="active"):
        path = self.wiki / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntitle: {title}\ncreated: {created}\nupdated: {updated or created}\n"
            f"status: {status}\n---\n\n# {title}\n\n{title} 的核心知识。\n",
            encoding="utf-8",
        )
        return path

    def _args(self, **changes):
        evidence = Path(self.temp.name) / "evidence.txt"
        evidence.write_text("evidence", encoding="utf-8")
        values = {
            "wiki_root": self.wiki,
            "date": "2026-07-25",
            "title": "统一 Agent 认知运行时",
            "question": "多个 Agent 如何共享第二大脑？",
            "summary": "多个 Agent 共享事实域，不各自维护长期真相。",
            "body": None,
            "source": [str(evidence)],
            "source_kind": "local-evidence",
            "target_layer": "queries",
            "confidence": 0.9,
            "risk": "low",
            "link": ["concepts/第二大脑", "concepts/AI Agent"],
            "agent": "test-agent",
            "judgment_changed": "false",
            "auto_promote": True,
            "index": False,
            "gbrain_bin": "gbrain",
            "gbrain_source": "hbrain",
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def test_safe_capture_creates_candidate_and_never_anchor_event(self):
        result = kg.capture(self._args())
        self.assertEqual(result["status"], "canonical-candidate")
        page = self.wiki / result["path"]
        self.assertTrue(page.is_file())
        self.assertIn("status: learning-candidate", page.read_text(encoding="utf-8"))
        self.assertFalse((self.wiki / "_meta" / "anchor-events").exists())
        ledger = self.wiki / "_meta" / "knowledge-learning" / "2026-07.jsonl"
        self.assertEqual(len(kg.read_jsonl(ledger)), 1)

    def test_high_risk_and_weak_source_fall_back_to_proposal(self):
        result = kg.capture(
            self._args(
                title="高风险判断",
                risk="high",
                confidence=0.5,
                source_kind="agent-synthesis",
                source=["agent-only"],
                link=[],
            )
        )
        self.assertEqual(result["status"], "proposal")
        self.assertIn("_meta/writeback-inbox/", result["path"])
        self.assertGreaterEqual(len(result["promotion_blockers"]), 3)

    def test_capture_is_idempotent(self):
        first = kg.capture(self._args())
        second = kg.capture(self._args())
        self.assertTrue(first["recorded"])
        self.assertTrue(second["deduplicated"])
        ledger = self.wiki / "_meta" / "knowledge-learning" / "2026-07.jsonl"
        self.assertEqual(len(kg.read_jsonl(ledger)), 1)

    @mock.patch.object(kg, "index_candidate")
    def test_index_imports_only_created_candidate(self, index_candidate):
        index_candidate.return_value = {"status": "ok"}
        result = kg.capture(self._args(index=True))
        self.assertEqual(result["index"]["status"], "ok")
        index_candidate.assert_called_once()

    def test_existing_page_is_never_overwritten(self):
        target = self._page("queries/统一-Agent-认知运行时.md", "已有页", "2026-01-01")
        before = target.read_text(encoding="utf-8")
        result = kg.capture(self._args())
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertIn("禁止自动覆盖", " ".join(result["promotion_blockers"]))

    def test_daily_report_combines_all_sources(self):
        learned = kg.capture(self._args())
        self._page("concepts/手工新增.md", "手工新增", "2026-07-25")
        self._page("practices/昨日更新.md", "昨日更新", "2026-01-01", "2026-07-25")
        hit = self.wiki / "_meta" / "knowledge-events" / "2026-07.jsonl"
        hit.write_text(json.dumps({"action": "knowledge-hit", "date": "2026-07-25", "slug": "concepts/第二大脑"}, ensure_ascii=False) + "\n", encoding="utf-8")
        miss = self.wiki / "_meta" / "knowledge-events" / "knowledge-misses" / "2026-07.jsonl"
        miss.write_text(json.dumps({"date": "2026-07-25", "query": "缺失问题", "source": "test"}, ensure_ascii=False) + "\n", encoding="utf-8")
        text, payload = kg.build_report(self.wiki, dt.date(2026, 7, 25), dt.date(2026, 7, 26))
        self.assertEqual(payload["new_canonical_pages"], 2)
        self.assertEqual(payload["auto_learning_candidates"], 1)
        self.assertEqual(payload["updated_pages"], 1)
        self.assertEqual(payload["knowledge_hits"], 1)
        self.assertEqual(payload["knowledge_misses"], 1)
        self.assertIn(learned["title"], text)
        self.assertIn("手工新增", text)
        self.assertIn("缺失问题", text)


if __name__ == "__main__":
    unittest.main()
