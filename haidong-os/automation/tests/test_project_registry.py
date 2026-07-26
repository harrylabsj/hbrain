import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "project_registry.py"
SPEC = importlib.util.spec_from_file_location("project_registry", MODULE_PATH)
registry = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(registry)


def project(project_id="hbrain", **overrides):
    value = {
        "schema_version": 1,
        "project_id": project_id,
        "name": "Hbrain",
        "status": "active",
        "priority": "P1",
        "phase": "five-domain-stage-2",
        "owner": "jianghaidong",
        "objective": "Build an auditable cognitive system",
        "next_action": "Implement Project Registry",
        "blocked_by": [],
        "repositories": ["/repo/hbrain"],
        "knowledge_refs": [],
        "cass_scope": "workspace:hbrain",
        "last_fact_id": None,
        "last_reviewed_at": "2026-07-26",
        "privacy": "private",
    }
    value.update(overrides)
    return value


def write_fact(root: Path, fact_id="fact_aaaaaaaaaaaaaaaaaaaaaaaa"):
    path = root / "events" / "2026-07.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"event_id": fact_id, "project_id": "hbrain"}) + "\n")
    return fact_id


class ProjectRegistryTests(unittest.TestCase):
    def init(self, projects: Path, facts: Path, payload=None):
        return registry.init_project(
            projects,
            facts,
            payload or project(),
            {"fact_id": None, "decision_ref": "decision:test"},
        )

    def test_init_is_idempotent_and_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            _, first = self.init(projects, facts)
            _, second = self.init(projects, facts)
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertTrue((projects / "registry.yaml").is_file())
            self.assertTrue((projects / "index.md").is_file())

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(registry.RegistryError):
                self.init(root / "projects", root / "facts", project("../escape"))

    def test_unknown_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = project(schema_version=2)
            with self.assertRaises(registry.RegistryError):
                self.init(root / "projects", root / "facts", payload)

    def test_missing_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(registry.RegistryError):
                registry.evidence_ref(None, None, Path(tmp))
            with self.assertRaises(registry.RegistryError):
                registry.evidence_ref("fact_missing", None, Path(tmp))

    def test_high_impact_requires_human_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            proposal, _ = registry.propose_change(
                projects,
                facts,
                "hbrain",
                {"priority": "P0"},
                {"fact_id": None, "decision_ref": "decision:priority"},
            )
            with self.assertRaises(registry.RegistryError):
                registry.apply_proposal(projects, facts, proposal["proposal_id"], human_approved=False)
            updated, written = registry.apply_proposal(
                projects,
                facts,
                proposal["proposal_id"],
                human_approved=True,
                approved_by="jianghaidong",
                approval_ref="decision:priority-approved",
            )
            self.assertTrue(written)
            self.assertEqual(updated["priority"], "P0")

    def test_low_impact_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            proposal, _ = registry.propose_change(
                projects,
                facts,
                "hbrain",
                {"next_action": "Run three pilots"},
                {"fact_id": None, "decision_ref": "decision:next"},
            )
            first, first_written = registry.apply_proposal(
                projects, facts, proposal["proposal_id"], human_approved=False
            )
            second, second_written = registry.apply_proposal(
                projects, facts, proposal["proposal_id"], human_approved=False
            )
            self.assertTrue(first_written)
            self.assertFalse(second_written)
            self.assertEqual(first, second)

    def test_stale_proposal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            first, _ = registry.propose_change(
                projects,
                facts,
                "hbrain",
                {"next_action": "First"},
                {"fact_id": None, "decision_ref": "decision:first"},
            )
            stale, _ = registry.propose_change(
                projects,
                facts,
                "hbrain",
                {"blocked_by": ["later"]},
                {"fact_id": None, "decision_ref": "decision:stale"},
            )
            registry.apply_proposal(projects, facts, first["proposal_id"], human_approved=False)
            with self.assertRaises(registry.RegistryError):
                registry.apply_proposal(projects, facts, stale["proposal_id"], human_approved=False)

    def test_invalid_change_is_rejected_before_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            with self.assertRaises(registry.RegistryError):
                registry.propose_change(
                    projects,
                    facts,
                    "hbrain",
                    {"priority": 123},
                    {"fact_id": None, "decision_ref": "decision:bad"},
                )
            self.assertFalse((projects / "inbox").exists())

    def test_last_fact_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            with self.assertRaises(registry.RegistryError):
                registry.propose_change(
                    projects,
                    facts,
                    "hbrain",
                    {"last_fact_id": "fact_missing"},
                    {"fact_id": None, "decision_ref": "decision:test"},
                )
            fact_id = write_fact(facts)
            proposal, _ = registry.propose_change(
                projects,
                facts,
                "hbrain",
                {"last_fact_id": fact_id},
                {"fact_id": fact_id, "decision_ref": None},
            )
            updated, _ = registry.apply_proposal(projects, facts, proposal["proposal_id"], human_approved=False)
            self.assertEqual(updated["last_fact_id"], fact_id)

    def test_context_caps_facts_at_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            path = facts / "events" / "2026-07.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                "".join(
                    json.dumps(
                        {
                            "event_id": f"fact_{index:024x}",
                            "project_id": "hbrain",
                            "occurred_at": f"2026-07-26T12:{index:02d}:00+08:00",
                            "event_type": "test",
                            "summary": str(index),
                            "source_ref": "test",
                        }
                    )
                    + "\n"
                    for index in range(8)
                )
            )
            rows = registry.recent_project_facts(facts, "hbrain", 99)
            self.assertEqual(len(rows), 5)
            self.assertEqual(registry.recent_project_facts(facts, "hbrain", -1), [])

    def test_render_ignores_symlinked_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"
            self.init(projects, facts)
            outside = root / "outside" / "escaped"
            outside.mkdir(parents=True)
            payload = project("escaped", name="Escaped")
            payload["state_ref"] = {"fact_id": None, "decision_ref": "decision:test"}
            (outside / "project.yaml").write_text(json.dumps(payload))
            (projects / "escaped").symlink_to(outside, target_is_directory=True)
            registry.render(projects, facts)
            derived = json.loads((projects / "registry.yaml").read_text())
            self.assertEqual([row["project_id"] for row in derived["projects"]], ["hbrain"])

    def test_concurrent_distinct_initialization_preserves_all_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects, facts = root / "projects", root / "facts"

            def create(index):
                payload = project(
                    f"project-{index}",
                    name=f"Project {index}",
                    repositories=[f"/repo/{index}"],
                )
                return self.init(projects, facts, payload)[1]

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(create, range(12)))
            self.assertEqual(sum(results), 12)
            result = registry.validate_registry(projects, facts)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["project_count"], 12)


if __name__ == "__main__":
    unittest.main()
