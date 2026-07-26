import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("project_change_compiler", ROOT / "project_change_compiler.py")
compiler = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(compiler)


def load_registry():
    path = ROOT / "project_registry.py"
    spec = importlib.util.spec_from_file_location("project_registry", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


registry = load_registry()


def project():
    return {
        "schema_version": 1, "project_id": "hbrain", "name": "Hbrain", "status": "active",
        "priority": "P1", "phase": "stage4", "owner": "owner", "objective": "objective",
        "next_action": "observe", "blocked_by": [], "repositories": [], "knowledge_refs": [],
        "cass_scope": "workspace:hbrain", "last_fact_id": None, "last_reviewed_at": "2026-07-24",
        "privacy": "private", "state_ref": {"fact_id": None, "decision_ref": "decision:init"},
    }


def fact(event_id="fact_aaaaaaaaaaaaaaaaaaaaaaaa", project_id="hbrain", verification="verified"):
    return {
        "schema_version": 1, "event_id": event_id, "occurred_at": "2026-07-25T10:00:00+08:00",
        "recorded_at": "2026-07-25T10:01:00+08:00", "subject": {"type": "project", "id": project_id},
        "event_type": "milestone", "summary": "completed a real step", "source_ref": "test",
        "actor": "owner", "confidence": 1, "verification": verification, "privacy": "private",
        "project_id": project_id, "supersedes": None, "refs": {"knowledge": [], "cass": [], "artifacts": []},
    }


class CompilerTests(unittest.TestCase):
    def setup(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        projects, facts = root / "projects", root / "facts"
        registry.init_project(projects, facts, project(), {"fact_id": None, "decision_ref": "decision:init"})
        path = facts / "events" / "2026-07.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(fact(), ensure_ascii=False) + "\n")
        return tmp, projects, facts

    def test_dry_run_does_not_write_and_is_low_impact(self):
        tmp, projects, facts = self.setup()
        with tmp:
            result = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25), no_write=True)
            self.assertEqual(result["candidates"], 1)
            self.assertTrue(result["proposal_only"])
            self.assertEqual(set(result["proposals"][0]["changes"]), {"last_fact_id", "last_reviewed_at"})
            self.assertFalse((projects / "inbox").exists())

    def test_compile_writes_idempotent_proposal(self):
        tmp, projects, facts = self.setup()
        with tmp:
            first = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            second = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(first["appended"], 1)
            self.assertEqual(second["appended"], 0)
            proposal = next((projects / "inbox").glob("*.json"))
            row = json.loads(proposal.read_text())
            self.assertFalse(row["high_impact"])
            self.assertTrue(row["proposal_only"])
            self.assertFalse(row["auto_promote"])

    def test_ignores_unverified_or_unknown_project(self):
        tmp, projects, facts = self.setup()
        with tmp:
            path = facts / "events" / "2026-07.jsonl"
            path.write_text(json.dumps(fact("fact_bbbbbbbbbbbbbbbbbbbbbbbb", verification="asserted")) + "\n" + json.dumps(fact("fact_cccccccccccccccccccccccc", project_id="unknown")) + "\n")
            result = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25), no_write=True)
            self.assertEqual(result["candidates"], 0)

    def test_symlink_root_rejected(self):
        tmp, projects, facts = self.setup()
        with tmp:
            outside = Path(tmp.name) / "outside"
            outside.mkdir()
            linked = Path(tmp.name) / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(compiler.CompilerError):
                compiler.compile_changes(linked, facts, compiler.dt.date(2026, 7, 25), no_write=True)


    def test_bad_fact_json_fails_closed_and_cli_nonzero(self):
        """Bad fact JSON → valid=false, appended=0, CLI exit 1."""
        import contextlib
        import io
        tmp, projects, facts = self.setup()
        with tmp:
            path = facts / "events" / "2026-07.jsonl"
            valid = path.read_text().strip()
            path.write_text(valid + "\n" + "{this is not valid json\n")
            # Programmatic: valid=False, nothing written
            result = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertFalse(result["valid"], "bad JSON must produce valid=false")
            self.assertEqual(result["appended"], 0)
            self.assertGreaterEqual(len(result["issues"]), 1)
            self.assertIn("invalid_json", result["issues"][0]["issue"])
            # CLI: non-zero exit
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = compiler.main([
                    "--facts-root", str(facts),
                    "--projects-root", str(projects),
                    "--for-date", "2026-07-25",
                    "compile",
                ])
            self.assertEqual(rc, 1, "CLI must exit non-zero for bad fact JSON")

    def test_corrupted_existing_proposal_returns_clear_error(self):
        """Corrupt existing proposal file → CompilerError with 'corrupted'."""
        tmp, projects, facts = self.setup()
        with tmp:
            first = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(first["appended"], 1)
            proposal_path = next((projects / "inbox").glob("*.json"))
            proposal_path.write_text("{this is broken")
            with self.assertRaises(compiler.CompilerError) as ctx:
                compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertIn("corrupted", str(ctx.exception))

    def test_existing_proposal_valid_non_object_raises(self):
        """Existing proposal is valid JSON but not an object → CompilerError."""
        tmp, projects, facts = self.setup()
        with tmp:
            first = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(first["appended"], 1)
            proposal_path = next((projects / "inbox").glob("*.json"))
            # Replace with valid JSON array (not a dict)
            proposal_path.write_text("[]")
            with self.assertRaises(compiler.CompilerError) as ctx:
                compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertIn("not an object", str(ctx.exception))

    def test_existing_proposal_tampered_identity_raises(self):
        """Existing proposal with tampered identity/safety flag → CompilerError."""
        tmp, projects, facts = self.setup()
        with tmp:
            first = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(first["appended"], 1)
            proposal_path = next((projects / "inbox").glob("*.json"))
            row = json.loads(proposal_path.read_text())
            # Tamper with safety flag: change high_impact to true
            row["high_impact"] = True
            proposal_path.write_text(json.dumps(row, sort_keys=True, ensure_ascii=False))
            with self.assertRaises(compiler.CompilerError) as ctx:
                compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertIn("identity mismatch", str(ctx.exception))

    def test_existing_proposal_tampered_proposal_id_raises(self):
        """Existing proposal with tampered proposal_id → CompilerError."""
        tmp, projects, facts = self.setup()
        with tmp:
            first = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(first["appended"], 1)
            proposal_path = next((projects / "inbox").glob("*.json"))
            row = json.loads(proposal_path.read_text())
            # Tamper with identity: change project_id
            row["project_id"] = "different-project"
            proposal_path.write_text(json.dumps(row, sort_keys=True, ensure_ascii=False))
            with self.assertRaises(compiler.CompilerError) as ctx:
                compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertIn("identity mismatch", str(ctx.exception))

    def test_normal_idempotency_unaffected_by_tamper_tests(self):
        """Normal second-time compile still idempotent (not affected by tamper tests)."""
        tmp, projects, facts = self.setup()
        with tmp:
            first = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(first["appended"], 1)
            second = compiler.compile_changes(projects, facts, compiler.dt.date(2026, 7, 25))
            self.assertEqual(second["appended"], 0)
            self.assertTrue(second["valid"])


if __name__ == "__main__":
    unittest.main()
