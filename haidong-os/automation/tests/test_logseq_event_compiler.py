import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "logseq_event_compiler.py"
SPEC = importlib.util.spec_from_file_location("logseq_event_compiler", MODULE_PATH)
compiler = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(compiler)


class LogseqEventCompilerTests(unittest.TestCase):
    def run_compile(self, journal: Path, output: Path, *extra: str) -> int:
        return compiler.main(
            [
                "--journal",
                str(journal),
                "--allowed-root",
                str(journal.parent),
                "--output-dir",
                str(output),
                *extra,
            ]
        )

    def test_only_explicit_markers_compile_with_line_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026-07-26.md"
            journal.write_text(
                "- ordinary note\n  compile:: no\n- shipped seven skills\n  compile:: yes\n  project-id:: workbuddy-skillops\n",
                encoding="utf-8",
            )
            output = root / "out"
            self.assertEqual(self.run_compile(journal, output), 0)
            rows = [json.loads(line) for line in (output / "candidates.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["summary"], "shipped seven skills")
            self.assertEqual(rows[0]["project_id"], "workbuddy-skillops")
            self.assertTrue(rows[0]["source_ref"].endswith("2026-07-26.md#line:3"))

    def test_no_marker_produces_empty_deterministic_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026_07_26.md"
            journal.write_text("- private journal note\n", encoding="utf-8")
            output = root / "out"
            self.assertEqual(self.run_compile(journal, output), 0)
            first_json = (output / "candidates.jsonl").read_bytes()
            first_report = (output / "report.md").read_bytes()
            self.assertEqual(self.run_compile(journal, output), 0)
            self.assertEqual(first_json, (output / "candidates.jsonl").read_bytes())
            self.assertEqual(first_report, (output / "report.md").read_bytes())

    def test_refuses_symlink_and_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026-07-26.md"
            journal.write_text("- event\n  compile:: yes\n", encoding="utf-8")
            link = root / "linked.md"
            link.symlink_to(journal)
            self.assertEqual(self.run_compile(link, root / "out", "--date", "2026-07-26"), 2)
            self.assertEqual(self.run_compile(root, root / "out", "--date", "2026-07-26"), 2)

    def test_refuses_symlink_component_inside_allowed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            actual.mkdir()
            journal = actual / "2026-07-26.md"
            journal.write_text("- event\n  compile:: yes\n", encoding="utf-8")
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            code = compiler.main(
                [
                    "--journal",
                    str(linked / journal.name),
                    "--allowed-root",
                    str(root),
                    "--output-dir",
                    str(root / "out"),
                ]
            )
            self.assertEqual(code, 2)

    def test_redacts_inline_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026-07-26.md"
            journal.write_text("- rotated api_key: abc123 def,ghi\n  compile:: YES\n", encoding="utf-8")
            output = root / "out"
            self.assertEqual(self.run_compile(journal, output), 0)
            text = (output / "candidates.jsonl").read_text()
            self.assertNotIn("abc123", text)
            self.assertNotIn("def,ghi", text)
            self.assertIn("[REDACTED]", text)

    def test_compile_marker_inside_code_fence_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026-07-26.md"
            journal.write_text("- code sample\n  ```\n  compile:: yes\n  ```\n", encoding="utf-8")
            output = root / "out"
            self.assertEqual(self.run_compile(journal, output), 0)
            self.assertEqual((output / "candidates.jsonl").read_text(), "")

    def test_nested_marker_belongs_to_nested_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026-07-26.md"
            journal.write_text("- parent\n  - child event\n    compile:: yes\n- next\n", encoding="utf-8")
            output = root / "out"
            self.assertEqual(self.run_compile(journal, output), 0)
            row = json.loads((output / "candidates.jsonl").read_text())
            self.assertEqual(row["summary"], "child event")

    def test_concurrent_runs_leave_valid_complete_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "2026-07-26.md"
            journal.write_text("- event\n  compile:: yes\n", encoding="utf-8")
            output = root / "out"
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.run_compile(journal, output), range(16)))
            self.assertEqual(results, [0] * 16)
            self.assertEqual(len((output / "candidates.jsonl").read_text().splitlines()), 1)
            json.loads((output / "candidates.jsonl").read_text())

    def test_output_directory_is_bound_to_one_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "2026-07-25.md"
            second = root / "2026-07-26.md"
            first.write_text("- first\n  compile:: yes\n", encoding="utf-8")
            second.write_text("- second\n  compile:: yes\n", encoding="utf-8")
            output = root / "out"
            self.assertEqual(self.run_compile(first, output), 0)
            self.assertEqual(self.run_compile(second, output), 2)
            self.assertIn("first", (output / "candidates.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
