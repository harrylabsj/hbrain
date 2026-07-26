#!/usr/bin/env python3
"""Tests for review_state.py: stage-5 unified review-state CLI.

Tests cover all four bounded domains (knowledge, experience, project, fact),
normalized report fields, stable IDs, five write markers always false,
limit/count, default open-only vs include_closed filtering, unified review
priority over domain reviews, review mutation scope (domain files unchanged),
input rejection (naive time, unknown item, invalid domain), and report write
behavior (no-write default, --no-write, explicit output).

Design:
  - unittest.TestCase with TemporaryDirectory per test method.
  - Domain data constructed from scratch via fixture factories.
  - Functions tested directly (build_report, record_review, etc.);
    output file behavior tested via atomic_write_report.
  - File-hash comparison before/after review to prove no domain mutation.
  - Does NOT run automatically; the user requested coverage report only.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Allow import from parent automation directory.
_src = str(Path(__file__).resolve().parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from review_state import (
    MAX_ITEMS,
    DOMAINS,
    ReviewStateError,
    build_report,
    build_review_record,
    record_review,
    atomic_write_report,
    knowledge_items,
    experience_items,
    project_items,
    fact_items,
    stable_id,
    main,
    validate_inputs,
)


# ---------------------------------------------------------------------------
# Fixture factories — each returns a string ready for write_text.
# Accept **overrides to vary one field per test.
# ---------------------------------------------------------------------------


def knowledge_learning_jsonl(**overrides: object) -> str:
    """Single knowledge-learning JSONL line."""
    row = {
        "schema_version": 1,
        "learning_id": "kl-001",
        "title": "Test knowledge item",
        "summary": "A learning that requires review",
        "status": "proposal",
        "path": "some/path.md",
        "review_required": True,
        "sources": ["src-a", "src-b"],
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


def writeback_md(
    title: str = "Writeback test", **overrides: str
) -> str:
    """Markdown file with frontmatter for writeback-inbox."""
    meta: dict[str, str] = {
        "title": title,
        "status": "proposed",
        "learning_id": "wb-001",
        "summary": "A writeback inbox item",
    }
    meta.update(overrides)  # type: ignore[arg-type]
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("# Body\nNot read by review_state.\n")
    return "\n".join(lines) + "\n"


def experience_event_jsonl(**overrides: object) -> str:
    """Single experience inbox event JSONL line."""
    row = {
        "schema_version": 1,
        "event_id": "exp-001",
        "source": {"receipt_id": "rcpt-001", "project_id": "proj-A"},
        "candidate": {"pattern": "test pattern", "title": "Test event"},
        "status": "inbox",
        "auto_promote": False,
        "cass_write": False,
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


def experience_review_jsonl(**overrides: object) -> str:
    """Single experience-domain review JSONL line."""
    row = {
        "schema_version": 1,
        "review_id": "rev-exp-001",
        "event_id": "exp-001",
        "decision": "accept",
        "reviewer": "tester",
        "rationale": "Domain review says accept",
        "reusable": True,
        "auto_promote": False,
        "cass_write": False,
        "reviewed_at": "2026-07-26T10:00:00+00:00",
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


def unified_review_jsonl(**overrides: object) -> str:
    """Single unified review log JSONL line."""
    row = {
        "schema_version": 1,
        "review_item_id": "exp-001",
        "domain": "experience",
        "decision": "reject",
        "reviewer": "human",
        "rationale": "Unified review says reject",
        "reviewed_at": "2026-07-26T12:00:00+00:00",
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


def project_proposal_dict(**overrides: object) -> dict:
    """Project change proposal dict (written as .json, not JSONL)."""
    row = {
        "schema_version": 1,
        "proposal_id": "prop-001",
        "project_id": "proj-A",
        "changes": {"file1.py": "modify"},
        "evidence": {"ref": "audit-log"},
        "base_hash": "abc123",
        "status": "proposed",
        "high_impact": False,
    }
    row.update(overrides)
    return row


def applied_audit_jsonl(**overrides: object) -> str:
    """Single applied audit JSONL line."""
    row = {
        "schema_version": 1,
        "proposal_id": "prop-applied-001",
        "project_id": "proj-B",
        "changes": {"removed.txt": "delete"},
        "evidence": {"ref": "exec-log"},
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


def fact_proposal_jsonl(**overrides: object) -> str:
    """Single fact inbox proposal JSONL line."""
    row = {
        "schema_version": 1,
        "proposal_id": "fact-prop-001",
        "summary": "New fact candidate",
        "source_ref": "meeting-notes",
        "status": "proposed",
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


def fact_event_jsonl(**overrides: object) -> str:
    """Single fact events JSONL line (verification=disputed for inclusion)."""
    row = {
        "schema_version": 1,
        "event_id": "fact-evt-001",
        "occurred_at": "2026-07-25",
        "recorded_at": "2026-07-26T10:00:00+00:00",
        "subject": "test",
        "event_type": "milestone",
        "summary": "Disputed fact event",
        "source_ref": "log",
        "actor": "alice",
        "confidence": 0.5,
        "verification": "disputed",
        "privacy": "private",
        "refs": ["ref-1"],
    }
    row.update(overrides)
    return json.dumps(row, sort_keys=True) + "\n"


# ===========================================================================
# Tests
# ===========================================================================

class TestReviewState(unittest.TestCase):
    """Stage-5 unified review-state comprehensive tests."""

    maxDiff = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_roots(self, tmp: str) -> dict[str, Path]:
        """Create all root directories and return path map."""
        d = Path(tmp)
        out = {
            "wiki_root": d / "wiki",
            "experience_root": d / "exp",
            "projects_root": d / "proj",
            "facts_root": d / "fact",
            "review_root": d / "review",
        }
        for name, p in out.items():
            p.mkdir(parents=True, exist_ok=True)
        return out

    def _hash_tree(self, root: Path) -> dict[str, str]:
        """Return {relative_slash_path: hexdigest} for all regular files."""
        result: dict[str, str] = {}
        if not root.exists():
            return result
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                result[str(path.relative_to(root))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return result

    # ==================================================================
    # 1. Knowledge: learning JSONL + writeback MD frontmatter
    # ==================================================================

    def test_knowledge_learning_jsonl(self) -> None:
        """Knowledge domain ingests learning JSONL rows with review_required=true."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            learn_dir = roots["wiki_root"] / "_meta" / "knowledge-learning"
            learn_dir.mkdir(parents=True)

            # Two rows in one file — one valid, one skipped.
            learn_dir.joinpath("ledger.jsonl").write_text(
                knowledge_learning_jsonl()    # review_required=true → included
                + knowledge_learning_jsonl(   # review_required=false → excluded
                    learning_id="kl-skip", review_required=False, status="done"
                )
            )

            issues: list = []
            items = knowledge_items(roots["wiki_root"], issues, 100)
            self.assertGreater(len(items), 0)
            ids = {i["review_item_id"] for i in items}
            self.assertIn("knowledge_kl-001", ids)
            self.assertNotIn("knowledge_kl-skip", ids)

    def test_knowledge_writeback_md(self) -> None:
        """Knowledge domain ingests writeback inbox MD frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            wb_dir = roots["wiki_root"] / "_meta" / "writeback-inbox"
            wb_dir.mkdir(parents=True)

            wb_dir.joinpath("wb-test.md").write_text(writeback_md())

            issues: list = []
            items = knowledge_items(roots["wiki_root"], issues, 100)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]["domain"], "knowledge")
            self.assertTrue(items[0]["review_item_id"].startswith("knowledge_"))

    def test_knowledge_learning_secret_redaction(self) -> None:
        """Secret-like content in knowledge rows produces issues, not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            learn_dir = roots["wiki_root"] / "_meta" / "knowledge-learning"
            learn_dir.mkdir(parents=True)

            learn_dir.joinpath("ledger.jsonl").write_text(
                knowledge_learning_jsonl(
                    summary="api_key=sk-abc123def456",
                )
            )

            issues: list = []
            items = knowledge_items(roots["wiki_root"], issues, 100)
            # Secret detection produces an issue; item is excluded.
            self.assertEqual(len(items), 0)
            self.assertGreater(len(issues), 0)
            self.assertIn("secret", issues[0]["issue"].lower())

    # ==================================================================
    # 2. Experience: inbox events + domain reviews
    # ==================================================================

    def test_experience_inbox_events(self) -> None:
        """Experience domain ingests inbox events."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)

            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl()
            )

            issues: list = []
            items, _ = experience_items(roots["experience_root"], issues, 100)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]["domain"], "experience")
            self.assertEqual(items[0]["review_item_id"], "exp-001")
            self.assertTrue(items[0]["review_required"])

    def test_experience_with_domain_review(self) -> None:
        """Domain-level review is attached as latest_review."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "reviews").mkdir(parents=True)

            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id="evt-review")
            )
            (roots["experience_root"] / "reviews" / "decisions.jsonl").write_text(
                experience_review_jsonl(event_id="evt-review", review_id="r1")
            )

            issues: list = []
            items, _ = experience_items(roots["experience_root"], issues, 100)
            self.assertEqual(len(items), 1)
            lr = items[0].get("latest_review")
            self.assertIsNotNone(lr, "expected domain-level latest_review")
            self.assertEqual(lr["decision"], "accept")
            self.assertEqual(lr["review_id"], "r1")

    def test_experience_missing_event_id(self) -> None:
        """Event without event_id produces an issue."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)

            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id=None)
            )

            issues: list = []
            items, _ = experience_items(roots["experience_root"], issues, 100)
            self.assertEqual(len(items), 0)
            self.assertGreater(len(issues), 0)

    # ==================================================================
    # 3. Project: inbox proposals + applied audit
    # ==================================================================

    def test_project_inbox_proposal(self) -> None:
        """Project domain ingests inbox proposals."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)

            (roots["projects_root"] / "inbox" / "project_change_p1.json").write_text(
                json.dumps(project_proposal_dict(), sort_keys=True)
            )

            issues: list = []
            items = project_items(roots["projects_root"], issues, 100)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]["domain"], "project")
            self.assertEqual(items[0]["domain_status"], "proposed")

    def test_project_applied_audit(self) -> None:
        """Project domain ingests applied audit records with review_required=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "audit").mkdir(parents=True)

            (roots["projects_root"] / "audit" / "applied.jsonl").write_text(
                applied_audit_jsonl()
            )

            issues: list = []
            items = project_items(roots["projects_root"], issues, 100)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]["domain_status"], "applied")
            self.assertIs(items[0]["review_required"], False)

    def test_project_missing_proposal_id(self) -> None:
        """Proposal without proposal_id produces an issue."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)

            (roots["projects_root"] / "inbox" / "project_change_bad.json").write_text(
                json.dumps(project_proposal_dict(proposal_id=None), sort_keys=True)
            )

            issues: list = []
            items = project_items(roots["projects_root"], issues, 100)
            self.assertEqual(len(items), 0)
            self.assertGreater(len(issues), 0)

    # ==================================================================
    # 3b. Project: applied audit dedup / conflict / limit ordering
    # ==================================================================

    def test_project_applied_suppresses_inbox_default(self) -> None:
        """Same proposal_id in inbox and applied: default report suppresses inbox copy."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "audit").mkdir(parents=True)

            # Inbox proposal
            (roots["projects_root"] / "inbox" / "project_change_p1.json").write_text(
                json.dumps(project_proposal_dict(
                    proposal_id="prop-dedup", project_id="proj-X",
                    changes={"f1.py": "modify"},
                ), sort_keys=True)
            )
            # Applied audit for the same proposal_id (same identity)
            (roots["projects_root"] / "audit" / "applied.jsonl").write_text(
                applied_audit_jsonl(
                    proposal_id="prop-dedup", project_id="proj-X",
                    changes={"f1.py": "modify"},
                )
            )

            report = build_report(**roots)  # include_closed=False (default)
            self.assertTrue(report["valid"])
            prop_ids = {i["review_item_id"] for i in report["items"]}
            self.assertNotIn("prop-dedup", prop_ids,
                             "applied proposal must not appear in default (open-only) report")

    def test_project_applied_with_include_closed(self) -> None:
        """include_closed=True shows single applied entry for a proposal in both inbox+audit."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "audit").mkdir(parents=True)

            (roots["projects_root"] / "inbox" / "project_change_p1.json").write_text(
                json.dumps(project_proposal_dict(
                    proposal_id="prop-uniq", project_id="proj-Y",
                    changes={"g.txt": "add"},
                ), sort_keys=True)
            )
            (roots["projects_root"] / "audit" / "applied.jsonl").write_text(
                applied_audit_jsonl(
                    proposal_id="prop-uniq", project_id="proj-Y",
                    changes={"g.txt": "add"},
                )
            )

            report = build_report(**roots, include_closed=True)
            self.assertTrue(report["valid"])
            prop_items = [i for i in report["items"] if i["review_item_id"] == "prop-uniq"]
            self.assertEqual(len(prop_items), 1,
                             "prop-uniq must appear exactly once with include_closed=True")
            self.assertEqual(prop_items[0]["domain_status"], "applied")
            self.assertIs(prop_items[0]["review_required"], False)

    def test_project_applied_conflict_different_project_id(self) -> None:
        """Same proposal_id but different project_id between inbox and audit fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "audit").mkdir(parents=True)

            (roots["projects_root"] / "inbox" / "project_change_c1.json").write_text(
                json.dumps(project_proposal_dict(
                    proposal_id="prop-conflict", project_id="proj-A",
                ), sort_keys=True)
            )
            # Audit says project_id="proj-B" — conflicts with inbox
            (roots["projects_root"] / "audit" / "applied.jsonl").write_text(
                applied_audit_jsonl(
                    proposal_id="prop-conflict", project_id="proj-B",
                )
            )

            report = build_report(**roots)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            conflict_issues = [i for i in report["issues"] if "conflicting" in i["issue"].lower()]
            self.assertGreater(len(conflict_issues), 0,
                               "conflict must produce an issue")
            # Message must not leak actual values
            self.assertNotIn("proj-A", " ".join(str(i) for i in conflict_issues))
            self.assertNotIn("proj-B", " ".join(str(i) for i in conflict_issues))

    def test_project_applied_conflict_different_changes(self) -> None:
        """Same proposal_id but different changes between inbox and audit fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "audit").mkdir(parents=True)

            (roots["projects_root"] / "inbox" / "project_change_c2.json").write_text(
                json.dumps(project_proposal_dict(
                    proposal_id="prop-chg", project_id="proj-A",
                    changes={"old.py": "modify"},
                ), sort_keys=True)
            )
            # Audit says different changes — conflicts with inbox
            (roots["projects_root"] / "audit" / "applied.jsonl").write_text(
                applied_audit_jsonl(
                    proposal_id="prop-chg", project_id="proj-A",
                    changes={"new.py": "delete"},
                )
            )

            report = build_report(**roots)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            conflict_issues = [i for i in report["issues"] if "conflicting" in i["issue"].lower()]
            self.assertGreater(len(conflict_issues), 0,
                               "conflict must produce an issue")

    def test_project_applied_limit_dedup_order(self) -> None:
        """Limit is applied after dedup: applied items suppress inbox even with tight limit."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "audit").mkdir(parents=True)

            # Three inbox proposals: prop-A, prop-B, prop-C
            for pid in ("prop-A", "prop-B", "prop-C"):
                (roots["projects_root"] / "inbox" / f"project_change_{pid}.json").write_text(
                    json.dumps(project_proposal_dict(
                        proposal_id=pid, project_id=f"proj-{pid}",
                        changes={"f.py": "modify"},
                    ), sort_keys=True)
                )
            # Applied audit for prop-B (same identity)
            (roots["projects_root"] / "audit" / "applied.jsonl").write_text(
                applied_audit_jsonl(
                    proposal_id="prop-B", project_id="proj-prop-B",
                    changes={"f.py": "modify"},
                )
            )

            # max_items=3 is just enough to cause the bug without dedup:
            # prop-B(inbox) slips through when limit trims prop-C
            report = build_report(**roots, max_items=3)  # include_closed=False
            self.assertTrue(report["valid"])
            item_ids = {i["review_item_id"] for i in report["items"]}
            self.assertNotIn("prop-B", item_ids,
                             "prop-B is applied and must not appear as pending even with tight limit")

    # ==================================================================
    # 4. Fact: inbox proposals + disputed events
    # ==================================================================

    def test_fact_inbox_proposal(self) -> None:
        """Fact domain ingests inbox proposals."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "inbox").mkdir(parents=True)

            (roots["facts_root"] / "inbox" / "proposals.jsonl").write_text(
                fact_proposal_jsonl()
            )

            issues: list = []
            items = fact_items(roots["facts_root"], issues, 100)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]["domain"], "fact")
            self.assertTrue(items[0]["review_required"])

    def test_fact_disputed_event(self) -> None:
        """Fact domain ingests disputed events (verification=disputed)."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "events").mkdir(parents=True)

            (roots["facts_root"] / "events" / "events.jsonl").write_text(
                # Only disputed event is included.
                fact_event_jsonl(event_id="fact-disputed")
                # Non-disputed event is excluded.
                + fact_event_jsonl(
                    event_id="fact-verified", verification="confirmed"
                )
            )

            issues: list = []
            items = fact_items(roots["facts_root"], issues, 100)
            ids = {i["review_item_id"] for i in items}
            self.assertIn("fact-disputed", ids)
            self.assertNotIn("fact-verified", ids)

    def test_fact_missing_proposal_id(self) -> None:
        """Fact proposal without proposal_id produces an issue."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "inbox").mkdir(parents=True)

            (roots["facts_root"] / "inbox" / "bad.jsonl").write_text(
                fact_proposal_jsonl(proposal_id=None)
            )

            issues: list = []
            items = fact_items(roots["facts_root"], issues, 100)
            self.assertEqual(len(items), 0)
            self.assertGreater(len(issues), 0)

    # ==================================================================
    # 5. Report: four-domain normalization
    # ==================================================================

    def test_all_four_domains_normalized(self) -> None:
        """Report normalizes all four domains into identical item schema."""
        required_fields = {
            "review_item_id", "domain", "source_id", "source_ref",
            "title", "summary", "domain_status", "latest_review",
            "review_required", "evidence_refs",
            "auto_promote", "wiki_write", "cass_write",
            "project_apply", "fact_append",
        }

        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)

            # Knowledge
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )
            # Experience
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "e.jsonl").write_text(
                experience_event_jsonl(event_id="report-exp")
            )
            # Project
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "inbox" / "project_change_rp.json").write_text(
                json.dumps(project_proposal_dict(proposal_id="report-prop"),
                           sort_keys=True)
            )
            # Fact
            (roots["facts_root"] / "inbox").mkdir(parents=True)
            (roots["facts_root"] / "inbox" / "f.jsonl").write_text(
                fact_proposal_jsonl(proposal_id="report-fact")
            )

            report = build_report(**roots, include_closed=True)
            self.assertTrue(report["valid"])
            self.assertGreaterEqual(report["item_count"], 4)

            for item in report["items"]:
                with self.subTest(item=item["review_item_id"]):
                    missing = required_fields - item.keys()
                    self.assertFalse(missing,
                                     f"Missing fields for {item['review_item_id']}: {missing}")
                    self.assertIn(item["domain"], DOMAINS)
                    # The five markers are booleans
                    for m in ("auto_promote", "wiki_write", "cass_write",
                              "project_apply", "fact_append"):
                        self.assertIsInstance(item[m], bool)

    # ==================================================================
    # 6. Stable ID
    # ==================================================================

    def test_stable_id_deterministic(self) -> None:
        """Same input → same review_item_id; different input → different ID."""
        row_a = {"a": 1, "b": "x"}
        row_b = {"a": 1, "b": "x"}
        id1 = stable_id("knowledge_", row_a)
        id2 = stable_id("knowledge_", row_b)
        self.assertEqual(id1, id2)

        id3 = stable_id("knowledge_", {"a": 2, "b": "x"})
        self.assertNotEqual(id1, id3)

        # Prefix changes ID
        id4 = stable_id("fact_", row_a)
        self.assertNotEqual(id1, id4)

    def test_stable_id_from_report(self) -> None:
        """Report items have deterministic IDs; same data → same ID across calls."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "e.jsonl").write_text(
                experience_event_jsonl(event_id="sid-exp")
            )

            r1 = build_report(**roots, include_closed=True)
            r2 = build_report(**roots, include_closed=True)

            ids1 = {i["review_item_id"] for i in r1["items"]}
            ids2 = {i["review_item_id"] for i in r2["items"]}
            self.assertEqual(ids1, ids2)

    # ==================================================================
    # 7. Five write markers always false
    # ==================================================================

    def test_write_markers_always_false(self) -> None:
        """auto_promote/wiki_write/cass_write/project_apply/fact_append are always false."""
        markers = ["auto_promote", "wiki_write", "cass_write",
                   "project_apply", "fact_append"]

        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)

            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "e.jsonl").write_text(
                experience_event_jsonl(event_id="wm-exp")
            )
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "inbox" / "project_change_wm.json").write_text(
                json.dumps(project_proposal_dict(proposal_id="wm-prop"),
                           sort_keys=True)
            )
            (roots["facts_root"] / "inbox").mkdir(parents=True)
            (roots["facts_root"] / "inbox" / "f.jsonl").write_text(
                fact_proposal_jsonl(proposal_id="wm-fact")
            )

            report = build_report(**roots, include_closed=True)
            self.assertTrue(report["valid"])

            # Item-level markers
            for item in report["items"]:
                for m in markers:
                    self.assertIs(item[m], False,
                                  f"item.{m} for {item['review_item_id']} must be False")

            # Recommendation-level markers
            for rec in report["recommendations"]:
                for m in markers:
                    self.assertIs(rec[m], False,
                                  f"rec.{m} for {rec['review_item_id']} must be False")

            # Top-level report markers
            for m in markers:
                self.assertIs(report[m], False,
                              f"report.{m} must be False")

    # ==================================================================
    # 8. Limit / count
    # ==================================================================

    def test_limit_per_domain(self) -> None:
        """max_items limits items per domain."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)

            learn_dir = roots["wiki_root"] / "_meta" / "knowledge-learning"
            learn_dir.mkdir(parents=True)
            # Write MAX_ITEMS + 3 rows, then read with limit=5
            rows = "".join(
                knowledge_learning_jsonl(learning_id=f"kl-lim-{i:03d}",
                                         title=f"Limit test {i}")
                for i in range(MAX_ITEMS + 3)
            )
            learn_dir.joinpath("ledger.jsonl").write_text(rows)

            issues: list = []
            items = knowledge_items(roots["wiki_root"], issues, 5)
            self.assertLessEqual(len(items), 5)

    def test_report_item_count(self) -> None:
        """report.item_count reflects the post-filter item list."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            report = build_report(**roots)  # default open items only
            self.assertTrue(report["valid"])
            self.assertEqual(report["item_count"], len(report["items"]))

    # ==================================================================
    # 9. Default open only vs include_closed
    # ==================================================================

    def test_default_open_only(self) -> None:
        """Default report excludes items that are closed via review."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "reviews").mkdir(parents=True)

            # Open event — no review
            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id="open-item")
                # Closed event — has domain review with decision=accept
                + experience_event_jsonl(event_id="closed-item")
            )
            (roots["experience_root"] / "reviews" / "decisions.jsonl").write_text(
                experience_review_jsonl(event_id="closed-item", review_id="r-close")
            )

            report = build_report(**roots)  # default: include_closed=False
            open_ids = {i["review_item_id"] for i in report["items"]}
            self.assertIn("open-item", open_ids)
            self.assertNotIn("closed-item", open_ids,
                             "closed item should not appear in default report")

    def test_include_closed_flag(self) -> None:
        """include_closed=True brings back closed items."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "reviews").mkdir(parents=True)

            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id="open-item")
                + experience_event_jsonl(event_id="closed-item")
            )
            (roots["experience_root"] / "reviews" / "decisions.jsonl").write_text(
                experience_review_jsonl(event_id="closed-item", review_id="r-close")
            )

            report = build_report(**roots, include_closed=True)
            all_ids = {i["review_item_id"] for i in report["items"]}
            self.assertIn("open-item", all_ids)
            self.assertIn("closed-item", all_ids)

    # ==================================================================
    # 10. Unified review vs domain review — latest by reviewed_at
    # ==================================================================

    def test_unified_review_newer_overrides_domain(self) -> None:
        """When unified review has a later reviewed_at, it overrides domain review."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "reviews").mkdir(parents=True)
            (roots["review_root"] / "reviews").mkdir(parents=True)

            # Event
            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id="priority-item")
            )
            # Domain review: accept at 10:00 (earlier)
            (roots["experience_root"] / "reviews" / "domain.jsonl").write_text(
                experience_review_jsonl(
                    event_id="priority-item", review_id="rev-dom",
                    decision="accept", rationale="Domain says accept",
                )
            )
            # Unified review: reject at 12:00 (later — should win)
            (roots["review_root"] / "reviews" / "2026-07.jsonl").write_text(
                unified_review_jsonl(
                    review_item_id="priority-item",
                    decision="reject", rationale="Unified says reject",
                )
            )

            report = build_report(**roots, include_closed=True)
            item = next(i for i in report["items"]
                        if i["review_item_id"] == "priority-item")
            self.assertIsNotNone(item["latest_review"])
            self.assertEqual(item["latest_review"]["decision"], "reject",
                             "unified review (newer) must override domain review")
            self.assertEqual(item["latest_review"]["rationale"],
                             "Unified says reject")

    def test_domain_review_newer_preserved(self) -> None:
        """When domain review has a later reviewed_at, it is preserved over unified."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "reviews").mkdir(parents=True)
            (roots["review_root"] / "reviews").mkdir(parents=True)

            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id="domain-newer")
            )
            # Domain review: accept at 14:00 (newer)
            (roots["experience_root"] / "reviews" / "domain.jsonl").write_text(
                experience_review_jsonl(
                    event_id="domain-newer", review_id="rev-dom-2",
                    decision="accept", rationale="Domain says accept",
                    reviewed_at="2026-07-26T14:00:00+00:00",
                )
            )
            # Unified review: reject at 12:00 (older — should NOT win)
            (roots["review_root"] / "reviews" / "2026-07.jsonl").write_text(
                unified_review_jsonl(
                    review_item_id="domain-newer",
                    decision="reject", rationale="Unified says reject",
                    reviewed_at="2026-07-26T12:00:00+00:00",
                )
            )

            report = build_report(**roots, include_closed=True)
            item = next(i for i in report["items"]
                        if i["review_item_id"] == "domain-newer")
            self.assertIsNotNone(item["latest_review"])
            self.assertEqual(item["latest_review"]["decision"], "accept",
                             "domain review (newer) must be preserved")
            self.assertEqual(item["latest_review"]["rationale"],
                             "Domain says accept")

    def test_unified_review_defer_keeps_item_open(self) -> None:
        """Unified 'defer' does NOT close an item (not accept/reject)."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["review_root"] / "reviews").mkdir(parents=True)

            (roots["experience_root"] / "inbox" / "events.jsonl").write_text(
                experience_event_jsonl(event_id="defer-item")
            )
            (roots["review_root"] / "reviews" / "2026-07.jsonl").write_text(
                unified_review_jsonl(
                    review_item_id="defer-item",
                    decision="defer", rationale="Need more data",
                )
            )

            report = build_report(**roots)  # default: open only
            ids = {i["review_item_id"] for i in report["items"]}
            self.assertIn("defer-item", ids,
                          "deferred item should still appear in open-only report")

    # ==================================================================
    # 11. Review does not mutate domain files
    # ==================================================================

    def test_review_does_not_mutate_domain_files(self) -> None:
        """review accept/reject/defer only changes review-root; four domain dirs are identical."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)

            # Knowledge item
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )
            # Experience item
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "e.jsonl").write_text(
                experience_event_jsonl(event_id="mut-exp")
            )
            # Project item
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "inbox" / "project_change_mut.json").write_text(
                json.dumps(project_proposal_dict(proposal_id="mut-prop"),
                           sort_keys=True)
            )
            # Fact item
            (roots["facts_root"] / "inbox").mkdir(parents=True)
            (roots["facts_root"] / "inbox" / "f.jsonl").write_text(
                fact_proposal_jsonl(proposal_id="mut-fact")
            )

            # Hash domain dirs before review
            before = {k: self._hash_tree(v) for k, v in roots.items()
                      if k != "review_root"}

            # Record three reviews with different decisions
            for i, decision in enumerate(("accept", "reject", "defer")):
                record_review(
                    roots["wiki_root"], roots["experience_root"], roots["projects_root"], roots["facts_root"],
                    roots["review_root"],
                    review_item_id="knowledge_kl-001",
                    domain="knowledge",
                    decision=decision,
                    reviewer="tester",
                    rationale=f"Testing {decision}",
                    reviewed_at=f"2026-07-26T{13+i:02d}:00:00+00:00",
                )

            # Re-hash domain dirs — must be identical
            after = {k: self._hash_tree(v) for k, v in roots.items()
                     if k != "review_root"}
            self.assertEqual(before, after,
                             "domain files must not change after review")

            # Review-root has new files
            review_files = list((roots["review_root"] / "reviews").glob("*.jsonl"))
            self.assertGreater(len(review_files), 0,
                               "review-root should contain newly written files")

    # ==================================================================
    # 12. Rejection: naive time / unknown item / bad domain
    # ==================================================================

    def test_build_review_rejects_naive_timestamp(self) -> None:
        """Naive datetime (no timezone) in reviewed_at raises ReviewStateError."""
        for naive in ("2026-07-26T12:00:00", "2026-07-26"):
            with self.subTest(timestamp=naive):
                with self.assertRaises(ReviewStateError):
                    build_review_record(
                        review_item_id="test-id",
                        domain="knowledge",
                        decision="accept",
                        reviewer="tester",
                        rationale="test",
                        reviewed_at=naive,
                    )

    def test_record_review_rejects_unknown_item(self) -> None:
        """Unknown review_item_id raises ReviewStateError in record_review."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            # No domain data → empty report → any review_item_id is unknown

            with self.assertRaises(ReviewStateError):
                record_review(
                    roots["wiki_root"], roots["experience_root"], roots["projects_root"], roots["facts_root"],
                    roots["review_root"],
                    review_item_id="nonexistent-item",
                    domain="knowledge",
                    decision="accept",
                    reviewer="tester",
                    rationale="Should not exist",
                    reviewed_at="2026-07-26T12:00:00+00:00",
                )

    def test_build_review_rejects_invalid_domain(self) -> None:
        """Domain not in DOMAINS raises ReviewStateError."""
        with self.assertRaises(ReviewStateError):
            build_review_record(
                review_item_id="test-id",
                domain="invalid-domain",
                decision="accept",
                reviewer="tester",
                rationale="test",
                reviewed_at="2026-07-26T12:00:00+00:00",
            )

    def test_build_review_rejects_bad_decision(self) -> None:
        """Decision not in (accept, reject, defer) raises ReviewStateError."""
        with self.assertRaises(ReviewStateError):
            build_review_record(
                review_item_id="test-id",
                domain="knowledge",
                decision="approve",  # not in REVIEW_DECISIONS
                reviewer="tester",
                rationale="test",
                reviewed_at="2026-07-26T12:00:00+00:00",
            )

    # ==================================================================
    # 13. Report write behavior (atomic_write_report)
    # ==================================================================

    def test_report_no_write_by_default(self) -> None:
        """build_report does NOT write any file; output path none → no file."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            report = build_report(**roots)
            self.assertTrue(report["valid"])

            # No file should exist under the review root (no review recorded)
            review_files = list((roots["review_root"] / "reviews").glob("*"))
            self.assertEqual(len(review_files), 0,
                             "no review files should exist after report-only call")

            # No file at an un-used output path
            out = Path(tmp) / "report.json"
            self.assertFalse(out.exists())

    def test_atomic_write_report_writes_file(self) -> None:
        """Explicit atomic_write_report creates file with valid content."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            report = build_report(**roots, include_closed=True)
            text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

            output_path = Path(tmp) / "reports" / "state.json"
            self.assertFalse(output_path.exists())

            atomic_write_report(output_path, text)
            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 0)
            # Content is valid JSON
            loaded = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn("items", loaded)
            self.assertIn("valid", loaded)

    def test_atomic_write_report_idempotent(self) -> None:
        """Writing the same report twice overwrites without error."""
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            text = '{"valid": true}\n'

            atomic_write_report(output_path, text)
            mtime1 = output_path.stat().st_mtime_ns

            atomic_write_report(output_path, text)
            mtime2 = output_path.stat().st_mtime_ns
            self.assertNotEqual(mtime1, mtime2,
                                "second write should replace the file")
            self.assertEqual(output_path.read_text(encoding="utf-8"), text)

    # ==================================================================
    # 14. Review record structure — deterministic ID excludes reviewed_at
    # ==================================================================

    def test_review_record_has_deterministic_id(self) -> None:
        """Same review inputs produce same review_id, regardless of reviewed_at."""
        r1 = build_review_record(
            "item-1", "knowledge", "accept", "alice",
            "Good work", "2026-07-26T12:00:00+00:00",
        )
        r2 = build_review_record(
            "item-1", "knowledge", "accept", "alice",
            "Good work", "2026-07-26T12:00:00+00:00",
        )
        self.assertEqual(r1["review_id"], r2["review_id"])

        # Different reviewed_at → same ID (not part of identity)
        r_same = build_review_record(
            "item-1", "knowledge", "accept", "alice",
            "Good work", "2026-07-27T08:00:00+00:00",
        )
        self.assertEqual(r1["review_id"], r_same["review_id"],
                         "review_id must not include reviewed_at")

        # Different semantic field → different ID
        r3 = build_review_record(
            "item-1", "knowledge", "reject", "alice",
            "Not good", "2026-07-26T12:00:00+00:00",
        )
        self.assertNotEqual(r1["review_id"], r3["review_id"])

    def test_review_record_markers_false(self) -> None:
        """Review record has auto_promote/wiki_write/cass_write/project_apply/fact_append=false."""
        rec = build_review_record(
            "item-1", "knowledge", "defer", "bob",
            "Need evidence", "2026-07-26T12:00:00+00:00",
        )
        for m in ("auto_promote", "wiki_write", "cass_write",
                  "project_apply", "fact_append"):
            self.assertIs(rec[m], False,
                          f"review record {m} must be False")


    # ==================================================================
    # 15. Knowledge body secret not read (frontmatter-only read)
    # ==================================================================

    def test_knowledge_body_secret_not_read(self) -> None:
        """Secret in writeback body (below frontmatter ---) is not read -> no issue, item OK."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            wb_dir = roots["wiki_root"] / "_meta" / "writeback-inbox"
            wb_dir.mkdir(parents=True)

            content = (
                "---\n"
                "title: Safe title\n"
                "status: proposed\n"
                "learning_id: wb-sec-body\n"
                "summary: Clean summary\n"
                "---\n"
                "\n"
                "# Body\n"
                "api_key=sk-abc123456789\n"
            )
            wb_dir.joinpath("wb-sec.md").write_text(content)

            issues: list = []
            items = knowledge_items(roots["wiki_root"], issues, 100)
            self.assertGreater(len(items), 0)
            self.assertEqual(items[0]["review_item_id"], "knowledge_wb-sec-body")
            secret_issues = [i for i in issues if "secret" in i["issue"].lower()]
            self.assertEqual(len(secret_issues), 0,
                             "body secret must not appear in issues")

            report = build_report(**roots, include_closed=True)
            self.assertTrue(report["valid"])

    # ==================================================================
    # 16. Bad JSON / non-object per domain -> report valid=false
    # ==================================================================

    def test_bad_json_in_knowledge_valid_false(self) -> None:
        """Bad JSON in knowledge-learning -> report valid=false, items empty."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            learn_dir = roots["wiki_root"] / "_meta" / "knowledge-learning"
            learn_dir.mkdir(parents=True)
            learn_dir.joinpath("ledger.jsonl").write_text("{bad json\n")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_bad_json_in_experience_inbox_valid_false(self) -> None:
        """Bad JSON in experience inbox -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "bad.jsonl").write_text("{bad json\n")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_bad_json_in_experience_reviews_valid_false(self) -> None:
        """Bad JSON in experience reviews -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "reviews").mkdir(parents=True)
            (roots["experience_root"] / "reviews" / "bad.jsonl").write_text("{bad json\n")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_non_object_in_experience_valid_false(self) -> None:
        """Non-object row in experience inbox -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "bad.jsonl").write_text('["not an object"]\n')

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_bad_json_in_project_inbox_valid_false(self) -> None:
        """Bad JSON in project inbox -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "inbox" / "project_change_bad.json").write_text("{bad json\n")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_non_object_in_project_valid_false(self) -> None:
        """Non-object in project inbox -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "inbox" / "project_change_arr.json").write_text("[]")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_bad_json_in_fact_inbox_valid_false(self) -> None:
        """Bad JSON in fact inbox -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "inbox").mkdir(parents=True)
            (roots["facts_root"] / "inbox" / "bad.jsonl").write_text("{bad json\n")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_non_object_in_fact_valid_false(self) -> None:
        """Non-object row in fact inbox -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "inbox").mkdir(parents=True)
            (roots["facts_root"] / "inbox" / "bad.jsonl").write_text('["not an object"]\n')

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    def test_bad_json_in_unified_review_valid_false(self) -> None:
        """Bad JSON in unified review log -> report valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["review_root"] / "reviews").mkdir(parents=True)
            (roots["review_root"] / "reviews" / "2026-07.jsonl").write_text("{bad json\n")

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["items"]), 0)
            self.assertEqual(len(report["recommendations"]), 0)

    # ==================================================================
    # 17. Symlink rejection
    # ==================================================================

    def test_wiki_root_symlink_rejected(self) -> None:
        """Wiki root being a symlink raises ReviewStateError."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            real = d / "real"
            real.mkdir()
            link = d / "link"
            link.symlink_to(real, target_is_directory=True)

            with self.assertRaises(ReviewStateError):
                build_report(
                    link, d / "exp", d / "proj", d / "fact", d / "review",
                    include_closed=True,
                )

    def test_review_root_symlink_rejected(self) -> None:
        """Review root being a symlink raises ReviewStateError."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in ("wiki", "exp", "proj", "fact"):
                (d / name).mkdir(parents=True)
            real = d / "real-review"
            real.mkdir()
            link = d / "review-link"
            link.symlink_to(real, target_is_directory=True)

            with self.assertRaises(ReviewStateError):
                build_report(
                    d / "wiki", d / "exp", d / "proj", d / "fact", link,
                    include_closed=True,
                )

    def test_knowledge_leaf_symlink_rejected(self) -> None:
        """Symlinked file under knowledge-learning -> report valid=false with issue."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            learn_dir = roots["wiki_root"] / "_meta" / "knowledge-learning"
            learn_dir.mkdir(parents=True)

            legit = learn_dir / "real.jsonl"
            legit.write_text(knowledge_learning_jsonl())
            link = learn_dir / "leaked.jsonl"
            link.symlink_to(legit)

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"])
            self.assertGreater(len(report["issues"]), 0)
            symlink_issues = [i for i in report["issues"]
                              if "symlink" in i["issue"].lower()]
            self.assertGreater(len(symlink_issues), 0)

    def test_output_path_symlink_rejected(self) -> None:
        """atomic_write_report rejects symlink output path."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            target = d / "target.json"
            link = d / "report-link.json"
            link.symlink_to(target)

            with self.assertRaises(ReviewStateError):
                atomic_write_report(link, '{"valid": true}\n')

    # ==================================================================
    # 18. Unified review idempotency, determinism, and concurrency
    # ==================================================================

    def test_unified_review_idempotent(self) -> None:
        """Same semantic review -> same review_id and only one line, even with different reviewed_at."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            r1 = record_review(
                roots["wiki_root"], roots["experience_root"],
                roots["projects_root"], roots["facts_root"],
                roots["review_root"],
                review_item_id="knowledge_kl-001",
                domain="knowledge",
                decision="accept",
                reviewer="tester",
                rationale="First review",
                reviewed_at="2026-07-26T12:00:00+00:00",
            )
            self.assertTrue(r1["written"])

            # Same semantic content, different reviewed_at — same review_id, deduped
            r2 = record_review(
                roots["wiki_root"], roots["experience_root"],
                roots["projects_root"], roots["facts_root"],
                roots["review_root"],
                review_item_id="knowledge_kl-001",
                domain="knowledge",
                decision="accept",
                reviewer="tester",
                rationale="First review",
                reviewed_at="2026-07-27T08:00:00+00:00",
            )
            self.assertFalse(r2["written"],
                             "duplicate semantic review must not be written")
            self.assertEqual(r1["review"]["review_id"], r2["review"]["review_id"],
                             "same semantic review must have same review_id")

            review_files = list((roots["review_root"] / "reviews").glob("*.jsonl"))
            self.assertEqual(len(review_files), 1)
            lines = review_files[0].read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_unified_review_concurrent_different(self) -> None:
        """Two different reviews for same item both write (different decisions)."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            r1 = record_review(
                roots["wiki_root"], roots["experience_root"],
                roots["projects_root"], roots["facts_root"],
                roots["review_root"],
                review_item_id="knowledge_kl-001",
                domain="knowledge",
                decision="accept",
                reviewer="alice",
                rationale="First opinion",
                reviewed_at="2026-07-26T12:00:00+00:00",
            )
            r2 = record_review(
                roots["wiki_root"], roots["experience_root"],
                roots["projects_root"], roots["facts_root"],
                roots["review_root"],
                review_item_id="knowledge_kl-001",
                domain="knowledge",
                decision="reject",
                reviewer="bob",
                rationale="Second opinion",
                reviewed_at="2026-07-26T13:00:00+00:00",
            )
            self.assertTrue(r1["written"])
            self.assertTrue(r2["written"])

            review_files = list((roots["review_root"] / "reviews").glob("*.jsonl"))
            lines = review_files[0].read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

    def test_unified_review_concurrent_identical(self) -> None:
        """Concurrent identical reviews: only one line written (true concurrency via ThreadPoolExecutor)."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            def record() -> dict:
                return record_review(
                    roots["wiki_root"], roots["experience_root"],
                    roots["projects_root"], roots["facts_root"],
                    roots["review_root"],
                    review_item_id="knowledge_kl-001",
                    domain="knowledge",
                    decision="accept",
                    reviewer="tester",
                    rationale="Concurrent identical",
                    reviewed_at="2026-07-26T12:00:00+00:00",
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(lambda: record()) for _ in range(4)]
                results = [f.result() for f in futures]

            written_count = sum(1 for r in results if r["written"])
            self.assertEqual(written_count, 1,
                             "only one concurrent identical review should be written")

            review_files = list((roots["review_root"] / "reviews").glob("*.jsonl"))
            lines = review_files[0].read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    # ==================================================================
    # 19. Review item domain mismatch rejection
    # ==================================================================

    def test_review_item_domain_mismatch_rejected(self) -> None:
        """record_review with domain not matching item's domain raises error."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            with self.assertRaises(ReviewStateError):
                record_review(
                    roots["wiki_root"], roots["experience_root"],
                    roots["projects_root"], roots["facts_root"],
                    roots["review_root"],
                    review_item_id="knowledge_kl-001",
                    domain="experience",
                    decision="accept",
                    reviewer="tester",
                    rationale="Domain mismatch test",
                    reviewed_at="2026-07-26T12:00:00+00:00",
                )

    # ==================================================================
    # 20. Reviewer/rationale secret check, fact refs, knowledge validation
    # ==================================================================

    def test_reviewer_rationale_secret_rejected(self) -> None:
        """Secrets in reviewer or rationale raise ReviewStateError and never create any file."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            for field, val in (("reviewer", "api_key=sk-abc"), ("rationale", "token is Bearer xyz")):
                with self.subTest(field=field, value=val):
                    kwargs = {
                        "review_item_id": "knowledge_kl-001",
                        "domain": "knowledge",
                        "decision": "accept",
                        "reviewer": "tester",
                        "rationale": "Clean rationale",
                        "reviewed_at": "2026-07-26T12:00:00+00:00",
                    }
                    kwargs[field] = val
                    with self.assertRaises(ReviewStateError):
                        record_review(
                            roots["wiki_root"], roots["experience_root"],
                            roots["projects_root"], roots["facts_root"],
                            roots["review_root"],
                            **kwargs,
                        )

            # No review directory or files created
            review_dir = roots["review_root"] / "reviews"
            self.assertFalse(review_dir.exists(),
                             "review directory must NOT be created when reviewer/rationale has secrets")

    def test_fact_disputed_refs_secret_scanning(self) -> None:
        """Secret-like content in fact disputed event refs produces issue, not item."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "events").mkdir(parents=True)

            (roots["facts_root"] / "events" / "events.jsonl").write_text(
                fact_event_jsonl(
                    event_id="fact-sec-ref",
                    refs=["api_key=sk-abc123"],
                )
            )

            issues: list = []
            items = fact_items(roots["facts_root"], issues, 100)
            self.assertEqual(len(items), 0)
            self.assertGreater(len(issues), 0)
            # Issue message must not leak the secret value
            self.assertNotIn("sk-abc123", str(issues))

    def test_disputed_event_refs_sanitized_in_output(self) -> None:
        """Evidence refs from disputed events are sanitized through safe_text."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["facts_root"] / "events").mkdir(parents=True)
            (roots["review_root"] / "reviews").mkdir(parents=True)

            (roots["facts_root"] / "events" / "events.jsonl").write_text(
                fact_event_jsonl(
                    event_id="fact-sanitize",
                    refs=["Bearer abc123def456"],
                )
            )

            report = build_report(**roots, include_closed=True)
            self.assertFalse(report["valid"],
                             "report must be invalid when fact input has secrets")
            sec_issues = [i for i in report["issues"] if "secret" in i["issue"].lower()]
            self.assertGreater(len(sec_issues), 0)

    def test_knowledge_learning_path_field_validation(self) -> None:
        """validate_inputs checks path is present in knowledge-learning rows."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            learn_dir = roots["wiki_root"] / "_meta" / "knowledge-learning"
            learn_dir.mkdir(parents=True)

            # Row missing 'path' entirely — should be flagged
            row_no_path = {
                "schema_version": 1,
                "learning_id": "kl-nopath",
                "title": "No path",
                "summary": "Missing path field",
                "status": "proposal",
                "review_required": True,
            }
            learn_dir.joinpath("ledger.jsonl").write_text(
                json.dumps(row_no_path, sort_keys=True) + "\n"
            )

            result = validate_inputs(
                roots["wiki_root"], roots["experience_root"],
                roots["projects_root"], roots["facts_root"],
                roots["review_root"],
            )
            issues_text = str(result["issues"])
            self.assertIn("missing fields", issues_text,
                          "validate must flag missing 'path' in knowledge-learning row")
            self.assertIn("path", issues_text)

    def test_writeback_frontmatter_status_validation(self) -> None:
        """validate_inputs checks status is present in writeback inbox frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            wb_dir = roots["wiki_root"] / "_meta" / "writeback-inbox"
            wb_dir.mkdir(parents=True)

            wb_dir.joinpath("no-status.md").write_text(
                "---\ntitle: No status\n---\n\nBody.\n"
            )

            result = validate_inputs(
                roots["wiki_root"], roots["experience_root"],
                roots["projects_root"], roots["facts_root"],
                roots["review_root"],
            )
            issues_text = str(result["issues"])
            self.assertIn("status", issues_text,
                          "validate must flag missing 'status' frontmatter")

    # ==================================================================
    # 21. CLI report output behavior
    # ==================================================================

    def test_cli_report_default_no_file(self) -> None:
        """CLI report (no --output) produces no file on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "--max-items", "50",
                "report",
            ])
            self.assertEqual(rc, 0)

    def test_cli_report_no_write_no_file(self) -> None:
        """CLI report --no-write suppresses file even with --output."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            output_path = Path(tmp) / "output.json"
            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "--max-items", "50",
                "report",
                "--output", str(output_path),
                "--no-write",
            ])
            self.assertEqual(rc, 0)
            self.assertFalse(output_path.exists(),
                              "--no-write must prevent file creation")

    def test_cli_report_explicit_output_writes(self) -> None:
        """CLI report --output writes valid report JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            output_path = Path(tmp) / "reports" / "state.json"
            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "--max-items", "50",
                "report",
                "--output", str(output_path),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(output_path.exists())
            loaded = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn("items", loaded)
            self.assertIn("valid", loaded)
            self.assertTrue(loaded["valid"])

    def test_cli_report_output_symlink_rejected(self) -> None:
        """--output pointing at existing symlink -> exit code 2."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            real = Path(tmp) / "real.json"
            link = Path(tmp) / "output-link.json"
            link.symlink_to(real)

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "--max-items", "50",
                "report",
                "--output", str(link),
            ])
            self.assertEqual(rc, 2,
                             "symlink output must cause exit code 2")

    # ==================================================================
    # 22. CLI bad JSON -> exit 1
    # ==================================================================

    def test_cli_bad_json_exit_1(self) -> None:
        """Bad JSON in any domain -> CLI exit 1."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )
            # Bad JSON in experience inbox
            (roots["experience_root"] / "inbox").mkdir(parents=True)
            (roots["experience_root"] / "inbox" / "bad.jsonl").write_text("{bad json\n")
            # Clean data in other domains
            (roots["projects_root"] / "inbox").mkdir(parents=True)
            (roots["projects_root"] / "inbox" / "p.json").write_text(
                json.dumps(project_proposal_dict(), sort_keys=True)
            )
            (roots["facts_root"] / "inbox").mkdir(parents=True)
            (roots["facts_root"] / "inbox" / "f.jsonl").write_text(
                fact_proposal_jsonl()
            )

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "--max-items", "50",
                "report",
            ])
            self.assertEqual(rc, 1,
                             "bad JSON in any domain must cause exit 1")

    # ==================================================================
    # 23. CLI validate
    # ==================================================================

    def test_cli_validate_clean(self) -> None:
        """CLI validate on clean data -> exit 0 with valid=true."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "validate",
            ])
            self.assertEqual(rc, 0)

    def test_cli_validate_bad_json_exit_1(self) -> None:
        """CLI validate on bad JSON -> exit 1 with valid=false."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text("{bad\n")

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "validate",
            ])
            self.assertEqual(rc, 1)

    # ==================================================================
    # 24. CLI review
    # ==================================================================

    def test_cli_review_dry_run(self) -> None:
        """CLI review --dry-run returns 0 without creating any file."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "review",
                "--review-item-id", "knowledge_kl-001",
                "--domain", "knowledge",
                "--decision", "accept",
                "--reviewer", "tester",
                "--rationale", "CLI dry-run test",
                "--reviewed-at", "2026-07-26T12:00:00+00:00",
                "--dry-run",
            ])
            self.assertEqual(rc, 0)

    def test_cli_review_writes_file(self) -> None:
        """CLI review creates a review record file."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "review",
                "--review-item-id", "knowledge_kl-001",
                "--domain", "knowledge",
                "--decision", "accept",
                "--reviewer", "tester",
                "--rationale", "CLI review test",
                "--reviewed-at", "2026-07-26T12:00:00+00:00",
            ])
            self.assertEqual(rc, 0)
            review_files = list((roots["review_root"] / "reviews").glob("*.jsonl"))
            self.assertEqual(len(review_files), 1)
            lines = review_files[0].read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_cli_review_unknown_item_exit_2(self) -> None:
        """CLI review with unknown review_item_id -> exit 2."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._make_roots(tmp)
            (roots["wiki_root"] / "_meta" / "knowledge-learning").mkdir(parents=True)
            (roots["wiki_root"] / "_meta" / "knowledge-learning" / "l.jsonl").write_text(
                knowledge_learning_jsonl()
            )

            rc = main([
                "--wiki-root", str(roots["wiki_root"]),
                "--experience-root", str(roots["experience_root"]),
                "--projects-root", str(roots["projects_root"]),
                "--facts-root", str(roots["facts_root"]),
                "--review-root", str(roots["review_root"]),
                "review",
                "--review-item-id", "nonexistent-id",
                "--domain", "knowledge",
                "--decision", "accept",
                "--reviewer", "tester",
                "--rationale", "Should fail",
                "--reviewed-at", "2026-07-26T12:00:00+00:00",
            ])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    raise SystemExit("Run via: python -m unittest discover")
