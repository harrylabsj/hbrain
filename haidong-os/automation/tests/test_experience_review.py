"""Tests for the experience-candidate review queue compiler."""

import datetime as dt
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experience_review as er  # noqa: E402

DAY = dt.date(2026, 7, 25)
MONTH = "2026-07"


def make_receipt(receipt_id, *, completed_at="2026-07-25T10:00:00+08:00",
                 project_id="proj-a", candidates=None, **overrides):
    receipt = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "packet_id": "packet_" + "0" * 24,
        "completed_at": completed_at,
        "agent": "claude",
        "project_id": project_id,
        "action": "调试了五域日报的日期过滤问题",  # must never leak
        "result": "修复完成并通过测试",  # must never leak
        "evidence": [{"type": "commit", "ref": "abc123"}],
        "knowledge_gap": [],
        "experience_candidate": candidates if candidates is not None else [
            {"summary": "日期过滤要用 completed_at 的本地日", "kind": "playbook"}
        ],
        "privacy": "private",
        "promotion": {"auto_promote": False, "status": "inbox"},
    }
    receipt.update(overrides)
    return receipt


class ExperienceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.receipts_root = self.dir / "receipts"
        self.inbox_root = self.dir / "review"
        (self.receipts_root / "inbox").mkdir(parents=True)

    # -- helpers ----------------------------------------------------------

    def write_receipts(self, rows, month=MONTH):
        path = self.receipts_root / "inbox" / f"{month}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False))
                handle.write("\n")
        return path

    def compile(self, for_date=DAY, dry_run=False):
        return er.compile_day(self.receipts_root, self.inbox_root, for_date, dry_run=dry_run)

    def all_events(self):
        events = []
        for path in er.iter_event_files(self.inbox_root):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        return events

    def month_file(self, month=MONTH):
        return self.inbox_root / "inbox" / f"{month}.jsonl"

    # -- compile behavior -------------------------------------------------

    def test_same_day_filter_only_completed_at_on_for_date(self):
        self.write_receipts([
            make_receipt("receipt_" + "a" * 24),  # 2026-07-25 ✓
            make_receipt("receipt_" + "b" * 24, completed_at="2026-07-24T23:59:00+08:00"),
            make_receipt("receipt_" + "c" * 24, completed_at="2026-07-26T00:01:00+08:00"),
        ])
        result = self.compile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates_found"], 1)
        self.assertEqual(result["appended"], 1)
        events = self.all_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"]["receipt_id"], "receipt_" + "a" * 24)
        self.assertEqual(events[0]["compiled_for"], DAY.isoformat())

    def test_duplicate_candidate_within_one_receipt_compiles_once(self):
        identical = {"summary": "同一候选重复出现", "kind": "playbook"}
        self.write_receipts([
            make_receipt("receipt_" + "a" * 24, candidates=[identical, identical]),
        ])
        result = self.compile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates_found"], 1)
        self.assertEqual(result["appended"], 1)
        events = self.all_events()
        self.assertEqual(len(events), 1, "one compile must never append a duplicate event_id")
        self.assertTrue(er.validate_inbox(self.inbox_root)["valid"])

    def test_receipts_without_candidates_produce_nothing(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24, candidates=[])])
        result = self.compile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates_found"], 0)
        self.assertEqual(result["appended"], 0)
        self.assertEqual(self.all_events(), [])

    def test_only_for_date_month_file_is_read(self):
        # A June receipt whose completed_at would not match anyway; the June
        # file must not even be opened for a July compile (it is invalid JSON
        # — if it were read, issues would fail the compile).
        (self.receipts_root / "inbox" / "2026-06.jsonl").write_text("{bad json\n")
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        result = self.compile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["issue_count"], 0)

    def test_event_shape_and_no_action_result_leak(self):
        receipt = make_receipt("receipt_" + "a" * 24)
        self.write_receipts([receipt])
        result = self.compile()
        self.assertEqual(result["status"], "ok")
        (event,) = self.all_events()
        self.assertEqual(event["schema_version"], 1)
        self.assertTrue(event["event_id"].startswith("xreview_"))
        self.assertTrue(event["candidate_key"].startswith("xcand_"))
        self.assertEqual(event["status"], "inbox")
        self.assertIs(event["auto_promote"], False)
        self.assertIs(event["cass_write"], False)
        self.assertEqual(event["privacy"], "private")
        self.assertEqual(set(event["source"]),
                         {"receipt_id", "agent", "project_id", "completed_at"})
        # action/result/query must never be copied anywhere in the event.
        blob = json.dumps(event, ensure_ascii=False)
        for leaked in ("action", "result", "query",
                       receipt["action"], receipt["result"]):
            self.assertNotIn(leaked, blob)
        # The candidate itself survives intact.
        self.assertEqual(event["candidate"], receipt["experience_candidate"][0])

    def test_deterministic_idempotent_rerun_appends_nothing(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        first = self.compile()
        bytes_after_first = self.month_file().read_bytes()
        second = self.compile()
        self.assertEqual(first["appended"], 1)
        self.assertEqual(second["appended"], 0)
        self.assertEqual(second["already_present"], 1)
        self.assertEqual(self.month_file().read_bytes(), bytes_after_first,
                         "second run must not change the file at all")

    def test_event_ids_are_deterministic_across_roots(self):
        receipt = make_receipt("receipt_" + "a" * 24)
        self.write_receipts([receipt])
        self.compile()
        (event,) = self.all_events()
        expected = er.build_event(receipt, receipt["experience_candidate"][0], DAY)
        self.assertEqual(event["event_id"], expected["event_id"])
        self.assertEqual(event["candidate_key"], expected["candidate_key"])

    def test_cross_month_dedup(self):
        receipt = make_receipt("receipt_" + "a" * 24)
        self.write_receipts([receipt])
        self.compile()
        (event,) = self.all_events()
        # Simulate the same event already present in another month's file
        # (e.g. after a migration); a fresh compile must dedup against it.
        other_root = self.dir / "review2"
        (other_root / "inbox").mkdir(parents=True)
        (other_root / "inbox" / "2026-06.jsonl").write_text(
            json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
        result = er.compile_day(self.receipts_root, other_root, DAY)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["appended"], 0)
        self.assertEqual(result["already_present"], 1)
        self.assertFalse((other_root / "inbox" / f"{MONTH}.jsonl").exists())

    def test_concurrent_compiles_are_idempotent(self):
        rows = [make_receipt(f"receipt_{i:024d}") for i in range(6)]
        self.write_receipts(rows)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: er.compile_day(self.receipts_root, self.inbox_root, DAY),
                range(8),
            ))
        for result in results:
            self.assertEqual(result["status"], "ok")
        self.assertEqual(sum(r["appended"] for r in results), 6,
                         "exactly one compile wins each event under the lock")
        events = self.all_events()
        self.assertEqual(len(events), 6)
        self.assertEqual(len({e["event_id"] for e in events}), 6)

    def test_bad_receipt_json_fails_closed(self):
        self.write_receipts([
            make_receipt("receipt_" + "a" * 24),
            "{not json",
        ])
        result = self.compile()
        self.assertEqual(result["status"], "issues")
        self.assertEqual(result["appended"], 0)
        self.assertGreaterEqual(result["issue_count"], 1)
        self.assertFalse(self.month_file().exists(),
                         "issues present => nothing may be written")
        rc = er.main(["--receipts-root", str(self.receipts_root),
                      "--inbox-root", str(self.inbox_root),
                      "compile", "--for-date", DAY.isoformat(), "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(self.month_file().exists())

    def test_secret_like_candidate_rejected_fail_closed(self):
        self.write_receipts([
            make_receipt("receipt_" + "a" * 24, candidates=[
                {"summary": "把 api_key = sk-live-123 配到环境变量"}
            ]),
            make_receipt("receipt_" + "b" * 24),  # clean sibling
        ])
        result = self.compile()
        self.assertEqual(result["status"], "issues")
        self.assertEqual(result["appended"], 0)
        self.assertFalse(self.month_file().exists(),
                         "a secret-like candidate blocks the whole write; "
                         "nothing is redacted-into-the-queue either")
        issues = json.dumps(result["issues"], ensure_ascii=False)
        self.assertIn("secret-like", issues)

    def test_symlink_targets_refused(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        # Target month leaf symlink -> refused, link target untouched.
        outside = self.dir / "outside.jsonl"
        outside.write_text("", encoding="utf-8")
        (self.inbox_root / "inbox").mkdir(parents=True)
        (self.inbox_root / "inbox" / f"{MONTH}.jsonl").symlink_to(outside)
        with self.assertRaises(er.ReviewError):
            self.compile()
        self.assertEqual(outside.read_text(encoding="utf-8"), "")
        # Inbox directory symlink -> refused.
        (self.inbox_root / "inbox" / f"{MONTH}.jsonl").unlink()
        real_inbox = self.dir / "real-inbox"
        real_inbox.mkdir()
        (self.inbox_root / "inbox").rmdir()
        (self.inbox_root / "inbox").symlink_to(real_inbox)
        with self.assertRaises(er.ReviewError):
            self.compile()
        self.assertEqual(list(real_inbox.iterdir()), [])
        # Inbox root symlink -> refused.
        root2 = self.dir / "review-root-link"
        root2.symlink_to(self.dir / "review2-target")
        with self.assertRaises(er.ReviewError):
            er.compile_day(self.receipts_root, root2, DAY)
        self.assertFalse((self.dir / "review2-target").exists())
        # CLI maps safety refusals to exit 2.
        rc = er.main(["--receipts-root", str(self.receipts_root),
                      "--inbox-root", str(self.inbox_root),
                      "compile", "--for-date", DAY.isoformat()])
        self.assertEqual(rc, 2)

    def test_dry_run_writes_nothing_and_creates_no_lock(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        result = self.compile(dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["would_append"], 1)
        self.assertEqual(result["appended"], 0)
        self.assertFalse(self.inbox_root.exists(),
                         "dry-run must not create the inbox root, month file, or lock")
        # After a real compile, a dry-run reports the dedup accurately.
        self.compile()
        again = self.compile(dry_run=True)
        self.assertEqual(again["already_present"], 1)
        self.assertEqual(again["would_append"], 0)

    def test_cli_compile_json_and_exit_codes(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        rc = er.main(["--receipts-root", str(self.receipts_root),
                      "--inbox-root", str(self.inbox_root),
                      "compile", "--for-date", DAY.isoformat(), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.all_events()), 1)
        rc = er.main(["--receipts-root", str(self.receipts_root),
                      "--inbox-root", str(self.inbox_root),
                      "compile", "--for-date", "not-a-date"])
        self.assertEqual(rc, 2)

    def test_default_for_date_is_previous_day(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        receipt = make_receipt("receipt_" + "a" * 24,
                               completed_at=f"{yesterday.isoformat()}T09:00:00+08:00")
        self.write_receipts([receipt], month=f"{yesterday:%Y-%m}")
        rc = er.main(["--receipts-root", str(self.receipts_root),
                      "--inbox-root", str(self.inbox_root), "compile"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.all_events()), 1)

    # -- validate -----------------------------------------------------------

    def test_validate_accepts_compiled_inbox(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24),
                             make_receipt("receipt_" + "b" * 24)])
        self.compile()
        result = er.validate_inbox(self.inbox_root)
        self.assertTrue(result["valid"])
        self.assertEqual(result["event_count"], 2)
        rc = er.main(["--inbox-root", str(self.inbox_root), "validate"])
        self.assertEqual(rc, 0)

    def _compile_one_then_corrupt(self, mutate):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        path = self.month_file()
        (event,) = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        mutate(event)
        path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
        result = er.validate_inbox(self.inbox_root)
        self.assertFalse(result["valid"])
        return result

    def test_validate_rejects_tampered_event_id(self):
        result = self._compile_one_then_corrupt(
            lambda e: e.__setitem__("event_id", "xreview_" + "f" * 24))
        self.assertIn("deterministic", json.dumps(result["issues"]))

    def test_validate_rejects_duplicate_event_id(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        with self.month_file().open("a", encoding="utf-8") as handle:
            handle.write(self.month_file().read_text(encoding="utf-8"))
        result = er.validate_inbox(self.inbox_root)
        self.assertFalse(result["valid"])
        self.assertIn("duplicate event_id", json.dumps(result["issues"]))

    def test_validate_rejects_illegal_promotion_fields(self):
        def promote(e):
            e["auto_promote"] = True
            e["promoted_at"] = "2026-07-26"
        result = self._compile_one_then_corrupt(promote)
        issues = json.dumps(result["issues"])
        self.assertIn("auto_promote must be false", issues)
        self.assertIn("illegal promotion fields", issues)

    def test_validate_rejects_status_other_than_inbox(self):
        result = self._compile_one_then_corrupt(
            lambda e: e.__setitem__("status", "applied"))
        self.assertIn("status must remain 'inbox'", json.dumps(result["issues"]))

    def test_validate_rejects_secret_in_candidate(self):
        result = self._compile_one_then_corrupt(
            lambda e: e["candidate"].__setitem__("summary", "password: hunter2"))
        self.assertIn("secret-like", json.dumps(result["issues"]))

    def test_validate_rejects_bad_json_and_leaked_source_fields(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        path = self.month_file()
        (event,) = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        event["source"]["action"] = "leaked"  # compile never copies this
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.write("{broken\n")
        result = er.validate_inbox(self.inbox_root)
        issues = json.dumps(result["issues"])
        self.assertFalse(result["valid"])
        self.assertIn("unexpected fields", issues)
        self.assertIn("invalid_json", issues)
        rc = er.main(["--inbox-root", str(self.inbox_root), "validate"])
        self.assertEqual(rc, 1)

    def test_validate_rejects_cass_write_true(self):
        result = self._compile_one_then_corrupt(
            lambda e: e.__setitem__("cass_write", True))
        self.assertIn("cass_write must be false", json.dumps(result["issues"]))

    def test_never_writes_outside_inbox_root(self):
        # The tool's whole write surface is <inbox-root>/inbox/<month>.jsonl
        # plus its lock file — no CASS, no receipts, nothing else.
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        written = sorted(p.relative_to(self.dir).as_posix()
                         for p in self.dir.rglob("*") if p.is_file())
        for path in written:
            if path.startswith("receipts/"):
                continue  # input fixtures
            self.assertTrue(
                path.startswith("review/inbox/") or path == "review/.experience-review.lock",
                f"unexpected write outside the review inbox: {path}")

    # -- human review state -----------------------------------------------

    def test_human_review_is_separate_append_only_state(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        event = self.all_events()[0]
        result = er.record_review(
            self.inbox_root, event["event_id"], "accept", "jianghaidong",
            "重复出现且可迁移", True, reviewed_at="2026-07-26T08:00:00+00:00")
        self.assertEqual(result["status"], "recorded")
        self.assertTrue((self.inbox_root / "reviews" / "2026-07.jsonl").is_file())
        self.assertTrue(er.validate_reviews(self.inbox_root)["valid"])
        self.assertEqual(self.all_events()[0], event, "candidate event must remain immutable")
        review = json.loads((self.inbox_root / "reviews" / "2026-07.jsonl").read_text())
        self.assertTrue(review["cass_recommendation"])
        self.assertFalse(review["cass_write"])
        self.assertFalse(review["auto_promote"])

    def test_human_review_is_idempotent_and_dry_run_writes_nothing(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        event_id = self.all_events()[0]["event_id"]
        dry = er.record_review(self.inbox_root, event_id, "defer", "owner", "需要更多样本", False, dry_run=True, reviewed_at="2026-07-26T08:00:00+00:00")
        self.assertEqual(dry["status"], "dry_run")
        self.assertFalse((self.inbox_root / "reviews").exists())
        first = er.record_review(self.inbox_root, event_id, "defer", "owner", "需要更多样本", False, reviewed_at="2026-07-26T08:00:00+00:00")
        second = er.record_review(self.inbox_root, event_id, "defer", "owner", "需要更多样本", False, reviewed_at="2026-07-26T09:00:00+00:00")
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(len(list((self.inbox_root / "reviews").glob("*.jsonl"))), 1)

    def test_human_review_requires_existing_candidate_and_valid_decision(self):
        with self.assertRaises(er.ReviewError):
            er.record_review(self.inbox_root, "xreview_" + "a" * 24, "accept", "owner", "reason", True)
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        event_id = self.all_events()[0]["event_id"]
        with self.assertRaises(er.ReviewError):
            er.record_review(self.inbox_root, event_id, "promote", "owner", "reason", True)

    def test_review_validation_rejects_tampering_and_unknown_event(self):
        self.write_receipts([make_receipt("receipt_" + "a" * 24)])
        self.compile()
        event_id = self.all_events()[0]["event_id"]
        er.record_review(self.inbox_root, event_id, "reject", "owner", "not reusable", False, reviewed_at="2026-07-26T08:00:00+00:00")
        path = self.inbox_root / "reviews" / "2026-07.jsonl"
        review = json.loads(path.read_text())
        review["cass_write"] = True
        path.write_text(json.dumps(review) + "\n")
        result = er.validate_reviews(self.inbox_root)
        self.assertFalse(result["valid"])
        self.assertIn("cass_write must be false", json.dumps(result["issues"]))


if __name__ == "__main__":
    unittest.main()
