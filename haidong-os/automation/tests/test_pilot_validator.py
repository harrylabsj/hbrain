"""Tests for the stage-1 manual pilot template and read-only validator."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pilot"))

import validate_pilot as vp  # noqa: E402

TEMPLATE_PATH = ROOT / "pilot" / "pilot_template.json"


def load_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def filled_episode(**overrides):
    ep = {
        "id": "episode-x",
        "state": "planned",
        "state_history": [{"state": "planned", "at": "2026-07-26T00:00:00Z"}],
        "object": "", "send_time": "", "question": "", "prior_judgment": "",
        "expected_result": "", "original_words": "", "actual_result": "",
        "surprise": "", "judgment_change": "", "knowledge_writeback": "",
        "action_writeback": "", "next_step": "", "review_date": "",
        "knowledge_routing_recommendation": "", "anchor_events": [],
    }
    ep.update(overrides)
    return ep


def closed_episode():
    ep = filled_episode(
        state="closed",
        state_history=[
            {"state": "planned", "at": "2026-07-20T00:00:00Z"},
            {"state": "sent", "at": "2026-07-21T09:00:00Z"},
            {"state": "replied", "at": "2026-07-22T10:00:00Z"},
            {"state": "closed", "at": "2026-07-26T12:00:00Z"},
        ],
    )
    for f in vp.ALL_FIELDS:
        ep[f] = f"real value for {f}"
    ep["knowledge_routing_recommendation"] = "route to knowledge/people"
    return ep


def pilot_with(episodes):
    return {"schema": vp.SCHEMA, "state_flow": list(vp.STATE_FLOW),
            "episodes": episodes}


class TemplateTests(unittest.TestCase):
    def test_template_is_valid(self):
        self.assertEqual(vp.validate(load_template()), [])

    def test_template_has_three_empty_planned_slots(self):
        data = load_template()
        self.assertEqual(len(data["episodes"]), 3)
        for ep in data["episodes"]:
            self.assertEqual(ep["state"], "planned")
            for f in vp.ALL_FIELDS:
                self.assertIn(f, ep)
                self.assertEqual(str(ep[f]).strip(), "",
                                 f"template must not invent content for {f!r}")
            self.assertEqual(ep["anchor_events"], [])


class ValidatorTests(unittest.TestCase):
    def test_closed_episode_with_real_fields_is_valid(self):
        self.assertEqual(vp.validate(pilot_with([closed_episode()])), [])

    def test_rejects_skip_planned_to_closed(self):
        ep = closed_episode()
        ep["state_history"] = [
            {"state": "planned", "at": "t0"},
            {"state": "closed", "at": "t1"},
        ]
        issues = vp.validate(pilot_with([ep]))
        self.assertTrue(any("illegal transition" in i for i in issues))
        self.assertTrue(any("without passing through" in i for i in issues))

    def test_rejects_closed_without_response_fields(self):
        ep = filled_episode(
            state="replied",
            state_history=[{"state": "planned", "at": "t0"},
                           {"state": "sent", "at": "t1"},
                           {"state": "replied", "at": "t2"}],
            object="alice", send_time="2026-07-21T09:00:00Z",
            question="q", prior_judgment="p", expected_result="e",
            # original_words / actual_result / surprise / judgment_change empty
        )
        issues = vp.validate(pilot_with([ep]))
        for missing in ("original_words", "actual_result", "surprise",
                        "judgment_change"):
            self.assertTrue(any(missing in i for i in issues),
                            f"expected an issue about {missing}")

    def test_rejects_missing_field_keys(self):
        ep = filled_episode()
        del ep["review_date"]
        issues = vp.validate(pilot_with([ep]))
        self.assertTrue(any("missing field key 'review_date'" in i for i in issues))

    def test_rejects_anchor_events_on_close(self):
        ep = closed_episode()
        ep["anchor_events"] = [{"type": "cognitive-anchor", "at": "t"}]
        issues = vp.validate(pilot_with([ep]))
        self.assertTrue(any("anchor_events" in i for i in issues),
                        "closing must never log an anchor event")

    def test_knowledge_routing_recommendation_allowed_on_close(self):
        ep = closed_episode()
        ep["knowledge_routing_recommendation"] = "route to knowledge/projects"
        self.assertEqual(vp.validate(pilot_with([ep])), [])

    def test_rejects_invalid_state_and_bad_history_start(self):
        ep = filled_episode(state="archived",
                            state_history=[{"state": "sent", "at": "t0"}])
        issues = vp.validate(pilot_with([ep]))
        self.assertTrue(any("invalid state" in i for i in issues))

    def test_state_must_match_history_tail(self):
        ep = filled_episode(
            state="sent",
            state_history=[{"state": "planned", "at": "t0"},
                           {"state": "sent", "at": "t1"},
                           {"state": "replied", "at": "t2"}],
        )
        issues = vp.validate(pilot_with([ep]))
        self.assertTrue(any("does not match state_history tail" in i for i in issues))

    def test_history_entries_beyond_template_require_timestamp(self):
        ep = filled_episode(
            state="sent",
            object="alice", send_time="2026-07-21T09:00:00Z",
            question="q", prior_judgment="p", expected_result="e",
            state_history=[{"state": "planned", "at": ""},
                           {"state": "sent", "at": ""}],
        )
        issues = vp.validate(pilot_with([ep]))
        self.assertTrue(any("non-empty 'at' timestamp" in i for i in issues),
                        "real transitions must record when they happened")

    def test_untouched_planned_template_needs_no_invented_timestamp(self):
        ep = filled_episode(state_history=[{"state": "planned", "at": ""}])
        self.assertEqual(vp.validate(pilot_with([ep])), [])

    def test_validator_is_read_only(self):
        before = TEMPLATE_PATH.read_bytes()
        rc = vp.main([str(TEMPLATE_PATH)])
        self.assertEqual(rc, 0)
        self.assertEqual(TEMPLATE_PATH.read_bytes(), before,
                         "validator must not modify the file it checks")

    def test_cli_reports_issues_and_exit_codes(self):
        import io
        import tempfile
        bad = pilot_with([filled_episode(state="bogus")])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text(json.dumps(bad))
            buf = io.StringIO()
            old = sys.stdout
            try:
                sys.stdout = buf
                rc = vp.main([str(p)])
            finally:
                sys.stdout = old
            self.assertEqual(rc, 1)
            self.assertIn("INVALID", buf.getvalue())
        self.assertEqual(vp.main([str(TEMPLATE_PATH)]), 0)
        self.assertEqual(vp.main(["/nonexistent/pilot.json"]), 2)


if __name__ == "__main__":
    unittest.main()
