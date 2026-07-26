import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "five_domain_runtime.py"
SPEC = importlib.util.spec_from_file_location("five_domain_runtime", MODULE_PATH)
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runtime)


def make_project(root: Path, project_id="hbrain"):
    projects = root / "projects"
    facts = root / "facts"
    payload = {
        "schema_version": 1,
        "project_id": project_id,
        "name": "Hbrain",
        "status": "active",
        "priority": "P1",
        "phase": "five-domain-stage-3",
        "owner": "jianghaidong",
        "objective": "Test runtime",
        "next_action": "Validate packets",
        "blocked_by": [],
        "repositories": [],
        "knowledge_refs": [],
        "cass_scope": "workspace:hbrain",
        "last_fact_id": None,
        "last_reviewed_at": "2026-07-26",
        "privacy": "private",
    }
    runtime.project_registry.init_project(
        projects, facts, payload, {"fact_id": None, "decision_ref": "decision:test"}
    )
    return projects, facts


def receipt_payload(packet_id="packet_aaaaaaaaaaaaaaaaaaaaaaaa"):
    return {
        "packet_id": packet_id,
        "completed_at": "2026-07-26T12:00:00+08:00",
        "agent": "codex-app",
        "project_id": "hbrain",
        "action": "Implemented runtime",
        "result": "Tests passed",
        "evidence": [{"type": "test", "ref": "test:test_runtime"}],
        "knowledge_gap": [],
        "experience_candidate": [],
        "privacy": "private",
    }


class FiveDomainRuntimeTests(unittest.TestCase):
    def test_startup_and_classify_do_not_retrieve(self):
        with mock.patch.object(runtime, "run_json_command") as command:
            result = runtime.classify("这个项目下一步是什么")
            self.assertEqual(result["primary_domain"], "project")
            command.assert_not_called()

    def test_mixed_query_has_one_primary_and_secondary(self):
        result = runtime.classify("这个项目以前踩过什么坑，下一步怎么推进")
        self.assertEqual(result["primary_domain"], "project")
        self.assertIn("experience", result["secondary_domains"])

    def test_ambiguous_query_requires_review(self):
        result = runtime.classify("帮我看看这个")
        self.assertIsNone(result["primary_domain"])
        self.assertTrue(result["needs_review"])

    def test_explicit_domain_wins(self):
        result = runtime.classify("项目下一步", explicit_domain="knowledge")
        self.assertEqual(result["primary_domain"], "knowledge")
        self.assertEqual(result["method"], "explicit")

    def test_default_packet_does_not_call_knowledge_or_cass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = make_project(root)
            with mock.patch.object(runtime, "run_json_command") as command:
                packet = runtime.build_context_packet(
                    "项目下一步", project_id="hbrain", explicit_domain=None, include=set(),
                    projects_root=projects, facts_root=facts, cass_workspace=None,
                    artifact_refs=[], char_budget=48000,
                )
            command.assert_not_called()
            self.assertTrue(packet["zero_preload"])
            self.assertEqual(packet["project"]["project_id"], "hbrain")
            self.assertFalse(packet["retrieval"]["knowledge"]["executed"])
            self.assertFalse(packet["retrieval"]["experience"]["executed"])

    def test_packet_has_one_project_and_at_most_five_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = make_project(root)
            event_path = facts / "events" / "2026-07.jsonl"
            event_path.parent.mkdir(parents=True)
            event_path.write_text(
                "".join(
                    json.dumps(
                        {
                            "event_id": f"fact_{index:024x}", "project_id": "hbrain",
                            "occurred_at": f"2026-07-26T12:{index:02d}:00+08:00",
                            "event_type": "test", "summary": str(index), "source_ref": "test",
                        }
                    ) + "\n"
                    for index in range(8)
                )
            )
            packet = runtime.build_context_packet(
                "项目进度", project_id="hbrain", explicit_domain=None, include=set(),
                projects_root=projects, facts_root=facts, cass_workspace=None,
                artifact_refs=[], char_budget=48000,
            )
            self.assertEqual(packet["project"]["project_id"], "hbrain")
            self.assertEqual(len(packet["facts"]), 5)

    def test_knowledge_only_runs_when_explicitly_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = make_project(root)
            fake = ({"hits": [{"slug": f"concepts/{i}", "excerpt": str(i), "score": 1} for i in range(8)]}, None)
            with mock.patch.object(runtime, "run_json_command", return_value=fake) as command:
                packet = runtime.build_context_packet(
                    "第二大脑是什么", project_id=None, explicit_domain="knowledge", include={"knowledge"},
                    projects_root=projects, facts_root=facts, cass_workspace=None,
                    artifact_refs=[], char_budget=48000,
                )
            command.assert_called_once()
            self.assertTrue(packet["retrieval"]["knowledge"]["executed"])
            self.assertEqual(len(packet["knowledge_summaries"]), 5)

    def test_experience_include_requires_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = make_project(root)
            packet = runtime.build_context_packet(
                "以前怎么处理", project_id="hbrain", explicit_domain="experience", include={"experience"},
                projects_root=projects, facts_root=facts, cass_workspace=None,
                artifact_refs=[], char_budget=48000,
            )
            self.assertFalse(packet["retrieval"]["experience"]["executed"])
            self.assertIn("required", packet["retrieval"]["experience"]["error"])

    def test_packet_id_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = make_project(root)
            kwargs = dict(
                query="项目进度", project_id="hbrain", explicit_domain=None, include=set(),
                projects_root=projects, facts_root=facts, cass_workspace=None,
                artifact_refs=[], char_budget=48000,
            )
            self.assertEqual(
                runtime.build_context_packet(**kwargs)["packet_id"],
                runtime.build_context_packet(**kwargs)["packet_id"],
            )

    def test_packet_redacts_secret_like_artifact_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = make_project(root)
            packet = runtime.build_context_packet(
                "证据在哪里", project_id="hbrain", explicit_domain="artifact", include=set(),
                projects_root=projects, facts_root=facts, cass_workspace=None,
                artifact_refs=["https://x/?api_key=abc123"], char_budget=48000,
            )
            self.assertNotIn("abc123", runtime.pretty_json(packet))
            self.assertEqual(packet["privacy"]["redacted_values"], 1)

    def test_receipt_requires_evidence(self):
        payload = receipt_payload()
        payload["evidence"] = []
        with self.assertRaises(runtime.RuntimeError):
            runtime.normalize_receipt(payload)

    def test_receipt_is_idempotent_and_never_promotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = runtime.normalize_receipt(receipt_payload())
            self.assertTrue(runtime.append_receipt(root, receipt))
            self.assertFalse(runtime.append_receipt(root, receipt))
            self.assertEqual(receipt["promotion"], {"auto_promote": False, "status": "inbox"})
            self.assertEqual(len((root / "inbox" / "2026-07.jsonl").read_text().splitlines()), 1)

    def test_concurrent_receipt_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = runtime.normalize_receipt(receipt_payload())
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: runtime.append_receipt(root, receipt), range(20)))
            self.assertEqual(sum(results), 1)

    def test_receipt_is_idempotent_across_months(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            june = receipt_payload()
            june["completed_at"] = "2026-06-30T23:59:00+00:00"
            july = receipt_payload()
            july["completed_at"] = "2026-07-01T00:01:00+00:00"
            first = runtime.normalize_receipt(june)
            second = runtime.normalize_receipt(july)
            self.assertEqual(first["receipt_id"], second["receipt_id"])
            self.assertTrue(runtime.append_receipt(root, first))
            self.assertFalse(runtime.append_receipt(root, second))
            self.assertFalse((root / "inbox" / "2026-07.jsonl").exists())

    def test_receipt_month_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir(parents=True)
            target = root / "target.txt"
            target.write_text("safe\n")
            (inbox / "2026-07.jsonl").symlink_to(target)
            with self.assertRaises(runtime.RuntimeError):
                runtime.append_receipt(root, runtime.normalize_receipt(receipt_payload()))
            self.assertEqual(target.read_text(), "safe\n")

    def test_receipt_rejects_secret_like_content(self):
        payload = receipt_payload()
        payload["result"] = "password=abc123"
        with self.assertRaises(runtime.RuntimeError):
            runtime.normalize_receipt(payload)

    def test_receipt_rejects_secret_in_evidence_or_candidate(self):
        payload = receipt_payload()
        payload["evidence"] = [{"type": "url", "ref": "https://x/?api_key=abc123"}]
        with self.assertRaises(runtime.RuntimeError):
            runtime.normalize_receipt(payload)
        payload = receipt_payload()
        payload["knowledge_gap"] = [{"question": "password=abc123", "why_missing": "unknown"}]
        with self.assertRaises(runtime.RuntimeError):
            runtime.normalize_receipt(payload)

    def test_naive_completion_timestamp_is_rejected(self):
        payload = receipt_payload()
        payload["completed_at"] = "2026-07-26T12:00:00"
        with self.assertRaises(runtime.RuntimeError):
            runtime.normalize_receipt(payload)

    def test_validate_detects_duplicate_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = runtime.normalize_receipt(receipt_payload())
            runtime.append_receipt(root, receipt)
            path = root / "inbox" / "2026-07.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(receipt) + "\n")
                handle.write("bad\n")
            result = runtime.validate_receipt_inbox(root)
            self.assertFalse(result["valid"])
            issues = "\n".join(item["issue"] for item in result["issues"])
            self.assertIn("duplicate receipt_id", issues)
            self.assertIn("invalid_json", issues)


if __name__ == "__main__":
    unittest.main()
