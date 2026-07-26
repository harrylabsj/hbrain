import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "fact_ledger.py"
SPEC = importlib.util.spec_from_file_location("fact_ledger", MODULE_PATH)
fact_ledger = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(fact_ledger)


def event(**overrides):
    value = {
        "schema_version": 1,
        "occurred_at": "2026-07-25T09:00:00+08:00",
        "recorded_at": "2026-07-25T10:00:00+08:00",
        "subject": {"type": "project", "id": "hbrain"},
        "event_type": "milestone_completed",
        "summary": "Fact Ledger test event",
        "source_ref": "artifact:test-suite",
        "actor": "unittest",
        "confidence": 1.0,
        "verification": "verified",
        "privacy": "private",
        "project_id": "hbrain",
        "supersedes": None,
        "refs": {"knowledge": [], "cass": [], "artifacts": ["artifact:test-suite"]},
    }
    value.update(overrides)
    return value


class FactLedgerTests(unittest.TestCase):
    def test_append_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, first_written = fact_ledger.append_event(root, event())
            second, second_written = fact_ledger.append_event(root, event())
            self.assertTrue(first_written)
            self.assertFalse(second_written)
            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(len((root / "events" / "2026-07.jsonl").read_text().splitlines()), 1)

    def test_append_rejects_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = event()
            payload.pop("source_ref")
            with self.assertRaises(fact_ledger.LedgerError):
                fact_ledger.append_event(Path(tmp), payload)

    def test_proposal_accepts_partial_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal, written = fact_ledger.propose_event(
                root,
                {"summary": "Needs verification", "source_ref": "conversation:test"},
                recorded_at="2026-07-26T08:00:00+08:00",
            )
            self.assertTrue(written)
            self.assertEqual(proposal["status"], "proposed")
            self.assertTrue((root / "inbox" / "2026-07-26.jsonl").is_file())

    def test_correction_appends_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, _ = fact_ledger.append_event(root, event(summary="Old value"))
            correction_payload = event(
                occurred_at="2026-07-25T11:00:00+08:00",
                recorded_at="2026-07-25T11:01:00+08:00",
                summary="Correct value",
                source_ref="artifact:correction",
            )
            correction, written = fact_ledger.correct_event(root, original["event_id"], correction_payload)
            self.assertTrue(written)
            self.assertEqual(correction["supersedes"], original["event_id"])
            stored = list(fact_ledger.iter_formal_events(root))
            self.assertEqual(len(stored), 2)
            self.assertEqual(stored[0]["summary"], "Old value")

    def test_correction_requires_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fact_ledger.LedgerError):
                fact_ledger.correct_event(Path(tmp), "fact_missing", event())

    def test_query_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fact_ledger.append_event(root, event())
            fact_ledger.append_event(
                root,
                event(
                    subject={"type": "project", "id": "other"},
                    project_id="other",
                    summary="Other event",
                ),
            )
            rows = fact_ledger.query_events(
                root,
                date_from="2026-07-25",
                date_to="2026-07-25",
                project_id="hbrain",
                subject="project:hbrain",
                verification="verified",
            )
            self.assertEqual(len(rows), 1)

    def test_daily_projection_is_rebuildable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fact_ledger.append_event(root, event())
            fact_ledger.propose_event(
                root,
                {"summary": "Review this", "source_ref": "conversation:test"},
                recorded_at="2026-07-25T12:00:00+08:00",
            )
            path, counts = fact_ledger.daily_projection(root, "2026-07-25")
            first = path.read_text()
            path2, counts2 = fact_ledger.daily_projection(root, "2026-07-25")
            self.assertEqual(first, path2.read_text())
            self.assertEqual(counts, counts2)
            self.assertEqual(counts, {"events": 1, "proposals": 1})

    def test_validate_detects_invalid_json_duplicate_and_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, _ = fact_ledger.append_event(root, event())
            path = root / "events" / "2026-07.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(original) + "\n")
                handle.write("{bad json\n")
            missing = event(summary="Missing target", supersedes="fact_not_found")
            missing["event_id"] = fact_ledger.normalized_identity(missing)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(missing) + "\n")
            result = fact_ledger.validate_ledger(root)
            self.assertFalse(result["valid"])
            issues = "\n".join(item["issue"] for item in result["issues"])
            self.assertIn("duplicate event_id", issues)
            self.assertIn("invalid_json", issues)
            self.assertIn("missing supersedes target", issues)

    def test_validate_reports_non_object_json_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, _ = fact_ledger.append_event(root, event())
            path = root / "events" / "2026-07.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write("[]\n")
            correction = event(
                occurred_at="2026-07-25T11:00:00+08:00",
                recorded_at="2026-07-25T11:01:00+08:00",
                summary="Correction after malformed row",
                supersedes=original["event_id"],
            )
            correction["event_id"] = fact_ledger.normalized_identity(correction)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(correction) + "\n")
            result = fact_ledger.validate_ledger(root)
            self.assertFalse(result["valid"])
            self.assertIn("event is not a JSON object", "\n".join(item["issue"] for item in result["issues"]))

    def test_concurrent_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def write(index):
                return fact_ledger.append_event(root, event(summary=f"Event {index}"))[1]

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(write, range(24)))
            self.assertEqual(sum(results), 24)
            lines = (root / "events" / "2026-07.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 24)
            for line in lines:
                json.loads(line)

    def test_cli_validate_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(fact_ledger.main(["--facts-root", str(root), "validate"]), 0)
            path = root / "events" / "2026-07.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("bad\n")
            self.assertEqual(fact_ledger.main(["--facts-root", str(root), "validate"]), 1)


if __name__ == "__main__":
    unittest.main()
