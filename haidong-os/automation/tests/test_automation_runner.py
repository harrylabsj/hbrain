"""Tests for the stage-0 reliability runner."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

import automation_runner as ar  # noqa: E402


def make_script(directory, name, body):
    """Write an executable zsh-free python helper script; return its argv."""
    path = Path(directory) / name
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return (sys.executable, str(path))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "state"

    def run_and_load(self, plan, **kwargs):
        kwargs.setdefault("retry_delay", 0)
        kwargs.setdefault("quiet", True)
        status = ar.run_plan(plan, self.state, **kwargs)
        with open(status["status_file"]) as f:
            on_disk = json.load(f)
        return status, on_disk

    def test_required_failure_preserves_completed_results(self):
        marker = self.dir / "local-result.txt"
        ok_step = ar.Step("local-work", "compute",
                          (sys.executable, "-c",
                           f"open({str(marker)!r}, 'w').write('done')"))
        bad_step = ar.Step("bad-required", "compute",
                           (sys.executable, "-c", "import sys; sys.exit(1)"))
        later = ar.Step("later-optional", "index",
                        (sys.executable, "-c", "print('never runs')"),
                        required=False)
        status, disk = self.run_and_load(ar.Plan("daily", (ok_step, bad_step, later)))

        self.assertEqual(status["exit_code"], ar.EXIT_FAILED)
        self.assertEqual(disk["status"], "failed")
        self.assertTrue(marker.exists(), "completed local result must be preserved")
        self.assertEqual(disk["steps"][0]["status"], "ok")
        self.assertEqual(disk["steps"][1]["status"], "failed")
        self.assertEqual(disk["steps"][2]["status"], "skipped")
        self.assertEqual(disk["failed_required_steps"], ["bad-required"])

    def test_optional_network_failure_is_degraded_but_exit_zero(self):
        local = ar.Step("local-work", "write", (sys.executable, "-c", "print('ok')"))
        net = ar.Step("flaky-index", "index",
                      (sys.executable, "-c", "import sys; sys.exit(1)"),
                      required=False, retry_safe=True)
        status, disk = self.run_and_load(ar.Plan("daily", (local, net)))

        self.assertEqual(disk["status"], "degraded")
        self.assertEqual(status["exit_code"], ar.EXIT_OK,
                         "optional index/delivery failure must not erase local success")
        self.assertEqual(disk["steps"][0]["status"], "ok")
        self.assertEqual(disk["steps"][1]["status"], "failed")
        self.assertEqual(disk["failed_optional_steps"], ["flaky-index"])

    def test_retry_only_safe_idempotent_steps(self):
        counter = self.dir / "counter.txt"
        flaky_argv = make_script(self.dir, "flaky.py", f"""
import pathlib, sys
c = pathlib.Path({str(counter)!r})
n = int(c.read_text()) if c.exists() else 0
c.write_text(str(n + 1))
sys.exit(0 if n >= 2 else 1)  # succeeds on the 3rd attempt
""")
        retryable = ar.Step("retryable", "index", flaky_argv,
                            required=False, retry_safe=True, max_attempts=3)
        plain = ar.Step("plain-fail", "compute",
                        (sys.executable, "-c", "import sys; sys.exit(1)"),
                        required=False)
        status, disk = self.run_and_load(ar.Plan("daily", (retryable, plain)))

        self.assertEqual(disk["steps"][0]["status"], "ok")
        self.assertEqual(disk["steps"][0]["attempts"], 3)
        self.assertEqual(disk["steps"][1]["attempts"], 1,
                         "non-retry_safe step must run exactly once")
        # The later optional non-retry-safe step failed, so the run is
        # degraded (optional failure), not ok; exit stays 0.
        self.assertEqual(disk["status"], "degraded")
        self.assertEqual(disk["failed_optional_steps"], ["plain-fail"])
        self.assertEqual(status["exit_code"], ar.EXIT_OK)

    def test_append_only_writes_never_retried(self):
        append_step = ar.Step("event-append", "write",
                              (sys.executable, "-c", "import sys; sys.exit(1)"),
                              required=False, append_only=True)
        _, disk = self.run_and_load(ar.Plan("daily", (append_step,)))
        self.assertEqual(disk["steps"][0]["attempts"], 1)
        self.assertEqual(disk["steps"][0]["status"], "failed")
        with self.assertRaises(ValueError):
            ar.Step("bad", "write", ("true",), append_only=True, retry_safe=True)

    def test_lock_contention_exits_3(self):
        self.state.mkdir(parents=True)
        held = ar.RunLock(self.state / "run-daily.lock")
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)

        contender = ar.RunLock(self.state / "run-daily.lock")
        self.assertFalse(contender.acquire(), "live lock must block a second run")
        # CLI-level check: same pid is alive, so contention, not staleness.
        rc = ar.main(["--mode", "daily", "--state-dir", str(self.state),
                      "--hbrain-loop", "/nonexistent/hbrain_loop.py"])
        self.assertEqual(rc, ar.EXIT_LOCKED)

    def test_lock_contention_writes_machine_readable_record(self):
        self.state.mkdir(parents=True)
        # A previous successful run's latest status must not be overwritten.
        latest = self.state / "status-daily.json"
        latest.write_text(json.dumps({"status": "ok", "marker": "previous-run"}))
        held = ar.RunLock(self.state / "run-daily.lock")
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)

        rc = ar.main(["--mode", "daily", "--state-dir", str(self.state),
                      "--hbrain-loop", "/nonexistent/hbrain_loop.py"])
        self.assertEqual(rc, ar.EXIT_LOCKED)

        with open(self.state / "lock-daily-latest.json") as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "locked")
        self.assertEqual(rec["exit_code"], ar.EXIT_LOCKED)
        self.assertEqual(rec["mode"], "daily")
        self.assertIn("exit_policy", rec)
        self.assertTrue(str(rec["detected_at"]).strip())
        self.assertIn("run-daily.lock", rec["lock_file"])
        self.assertEqual(json.loads(latest.read_text()),
                         {"status": "ok", "marker": "previous-run"},
                         "lock contention must not touch the last real status")

    def test_dry_run_executes_nothing(self):
        marker = self.dir / "should-not-exist.txt"
        step = ar.Step("would-write", "write",
                       (sys.executable, "-c",
                        f"open({str(marker)!r}, 'w').write('x')"))
        status, disk = self.run_and_load(ar.Plan("daily", (step,)), dry_run=True)

        self.assertFalse(marker.exists(), "dry-run must not execute commands")
        self.assertEqual(disk["status"], "dry_run")
        self.assertEqual(status["exit_code"], ar.EXIT_OK)
        self.assertEqual(disk["steps"][0]["status"], "planned")
        self.assertEqual(disk["steps"][0]["attempts"], 0)
        # Plan (including argv arrays) is recorded in the caller-selected dir.
        self.assertTrue(str(disk["status_file"]).startswith(str(self.state)))

    def test_dry_run_requires_explicit_state_dir(self):
        rc = ar.main(["--mode", "daily", "--dry-run"])
        self.assertEqual(rc, ar.EXIT_USAGE)

    def test_status_file_is_atomic_valid_json(self):
        step = ar.Step("s", "compute", (sys.executable, "-c", "print('hi')"))
        status, disk = self.run_and_load(ar.Plan("daily", (step,)))
        # json.load in run_and_load already proves validity; check no tmp litter.
        leftovers = [p for p in self.state.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])
        for key in ("schema", "run_id", "mode", "status", "exit_code",
                    "exit_policy", "started_at", "ended_at", "steps"):
            self.assertIn(key, disk)
        rec = disk["steps"][0]
        for key in ("category", "started_at", "ended_at", "duration_sec",
                    "exit_code", "status", "attempts", "output_excerpt"):
            self.assertIn(key, rec)

    def test_exit_policy_explicit_in_json(self):
        step = ar.Step("s", "compute", (sys.executable, "-c", "True"))
        _, disk = self.run_and_load(ar.Plan("daily", (step,)))
        self.assertIn("degraded", disk["exit_policy"])
        self.assertIn("exit 0", disk["exit_policy"])
        self.assertEqual(disk["exit_code"], ar.EXIT_OK)

    def test_output_excerpt_is_bounded(self):
        step = ar.Step("noisy", "compute",
                       (sys.executable, "-c", "print('x' * 10000)"))
        _, disk = self.run_and_load(ar.Plan("daily", (step,)))
        self.assertLessEqual(len(disk["steps"][0]["output_excerpt"]),
                             ar.OUTPUT_EXCERPT_LIMIT + 20)

    def test_step_timeout_is_recorded_and_retried(self):
        step = ar.Step("sleeper", "compute",
                       (sys.executable, "-c", "import time; time.sleep(30)"),
                       required=False, retry_safe=True, max_attempts=2)
        status, disk = self.run_and_load(ar.Plan("daily", (step,)),
                                         step_timeout=0.5)

        rec = disk["steps"][0]
        self.assertEqual(rec["status"], "failed")
        self.assertTrue(rec["timed_out"], "timeout must be recorded explicitly")
        self.assertEqual(rec["attempts"], 2, "retry-safe step retries after a timeout")
        self.assertIsNone(rec["exit_code"])
        self.assertIn("timed out", rec["output_excerpt"])
        self.assertLessEqual(len(rec["output_excerpt"]),
                             ar.OUTPUT_EXCERPT_LIMIT + 20)
        self.assertLess(rec["duration_sec"], 30,
                        "a silent process must not hang forever")
        self.assertEqual(disk["status"], "degraded")  # optional step failure
        self.assertEqual(status["exit_code"], ar.EXIT_OK)

    def test_step_validation_rejects_bad_attempts_and_timeout(self):
        with self.assertRaises(ValueError):
            ar.Step("bad", "compute", ("true",), max_attempts=0)
        with self.assertRaises(ValueError):
            ar.Step("bad", "compute", ("true",), timeout=0)
        with self.assertRaises(ValueError):
            ar.Step("bad", "compute", ("true",), timeout=-1.5)
        # Valid boundaries do not raise.
        ar.Step("ok", "compute", ("true",), max_attempts=1, timeout=0.1)

    def test_two_runs_write_two_immutable_history_files(self):
        step = ar.Step("s", "compute", (sys.executable, "-c", "print('hi')"))
        first, _ = self.run_and_load(ar.Plan("daily", (step,)))
        second, disk = self.run_and_load(ar.Plan("daily", (step,)))

        history_dir = self.state / "history" / "daily"
        history_files = sorted(history_dir.glob("*.json"))
        self.assertEqual(len(history_files), 2,
                         "each real run must preserve one history JSON")
        loaded = []
        for path in history_files:
            with open(path) as f:  # each history file is valid JSON
                loaded.append(json.load(f))
        self.assertEqual({h["run_id"] for h in loaded},
                         {first["run_id"], second["run_id"]})
        for h in loaded:
            self.assertEqual(h["status"], "ok")
            self.assertTrue(h["history_file"].endswith(".json"))
            self.assertTrue((self.state / "history" / "daily" /
                             Path(h["history_file"]).name).exists())
        # Latest status points at the most recent history file.
        self.assertEqual(disk["history_file"], second["history_file"])
        self.assertIn("status_file", disk)

    def test_dry_run_writes_no_history(self):
        step = ar.Step("s", "compute", (sys.executable, "-c", "print('hi')"))
        _, disk = self.run_and_load(ar.Plan("daily", (step,)), dry_run=True)
        self.assertIsNone(disk["history_file"])
        self.assertFalse((self.state / "history").exists())

    def test_mode_plans_shape(self):
        for mode, expected_local in (("daily", 4), ("weekly", 5), ("monthly", 2)):
            plan = ar.build_mode_plan(mode, sys.executable, "/x/hbrain_loop.py",
                                      "/x/repo", "gbrain")
            self.assertEqual(len(plan.steps), expected_local + 3)  # +3 index steps
            for s in plan.steps[:expected_local]:
                self.assertTrue(s.required)
            for s in plan.steps[expected_local:]:
                self.assertEqual(s.category, "index")
                self.assertFalse(s.required)
                self.assertTrue(s.retry_safe)
            for s in plan.steps:
                self.assertIsInstance(s.argv, tuple)  # argv arrays, not shell strings

    def test_mode_plans_match_production_commands(self):
        # Daily: maintenance, then stage-4 experience-review compile, then
        # the five-domain daily report — fixed order, all required.
        daily = ar.build_mode_plan("daily", sys.executable, "/x/hbrain_loop.py",
                                   "/x/wiki", "gbrain",
                                   experience_review="/x/experience_review.py",
                                   five_domain_daily="/x/five_domain_daily.py",
                                   project_change_compiler="/x/project_change_compiler.py",
                                   facts_root="/x/facts",
                                   projects_root="/x/projects",
                                   receipts_root="/x/receipts",
                                   experience_inbox_root="/x/review-inbox")
        self.assertEqual(len(daily.steps), 7)  # 4 required local + 3 optional index
        self.assertEqual([s.name for s in daily.steps[:4]], [
            "hbrain-daily-maintenance",
            "experience-review-compile",
            "five-domain-daily-report",
            "project-change-compile",
        ])
        for s in daily.steps[:4]:
            self.assertTrue(s.required)
            self.assertEqual(s.category, "write")
        self.assertEqual(daily.steps[0].argv,
                         (sys.executable, "/x/hbrain_loop.py",
                          "automation-run", "--mode", "daily", "--apply-frontmatter"))
        self.assertEqual(daily.steps[1].argv,
                         (sys.executable, "/x/experience_review.py",
                          "--receipts-root", "/x/receipts",
                          "--inbox-root", "/x/review-inbox",
                          "compile"))
        self.assertEqual(daily.steps[2].argv,
                         (sys.executable, "/x/five_domain_daily.py",
                          "--wiki-root", "/x/wiki",
                          "--facts-root", "/x/facts",
                          "--projects-root", "/x/projects",
                          "--receipts-root", "/x/receipts"))
        self.assertEqual(daily.steps[3].argv,
                         (sys.executable, "/x/project_change_compiler.py",
                          "--facts-root", "/x/facts",
                          "--projects-root", "/x/projects", "compile"))

        # Categories verified against the real command interfaces.
        weekly = ar.build_mode_plan("weekly", sys.executable, "/x/hbrain_loop.py",
                                    "/x/wiki", "gbrain")
        weekly_cats = {s.name: s.category for s in weekly.steps[:5]}
        self.assertEqual(weekly_cats, {
            "hbrain-weekly-maintenance": "write",
            "knowledge-dashboard": "write",
            "knowledge-candidates": "write",
            "governance-audit": "write",
            "weekly-summary": "compute",
        })
        self.assertEqual(weekly.steps[0].argv,
                         (sys.executable, "/x/hbrain_loop.py",
                          "automation-run", "--mode", "weekly", "--apply-frontmatter"))

        # There is no `automation-run --mode monthly`; monthly is exactly
        # `govern-monthly` followed by `governance-audit`.
        monthly = ar.build_mode_plan("monthly", sys.executable, "/x/hbrain_loop.py",
                                     "/x/wiki", "gbrain")
        self.assertEqual(monthly.steps[0].argv,
                         (sys.executable, "/x/hbrain_loop.py", "govern-monthly"))
        self.assertEqual(monthly.steps[1].argv,
                         (sys.executable, "/x/hbrain_loop.py", "governance-audit"))
        self.assertEqual([s.category for s in monthly.steps[:2]],
                         ["write", "write"])
        for s in monthly.steps:
            self.assertNotIn("governance-monthly", s.argv)
            self.assertNotIn("monthly", s.argv)

        # gbrain: sync carries --repo <wiki-root>; embed and health do not.
        idx = weekly.steps[5:]
        self.assertEqual(idx[0].argv,
                         ("gbrain", "sync", "--source", "hbrain", "--repo", "/x/wiki",
                          "--no-pull", "--yes", "--skip-failed", "--no-embed"))
        self.assertEqual(idx[1].argv, ("gbrain", "embed", "--stale"))
        self.assertEqual(idx[2].argv, ("gbrain", "health"))

    def test_cli_dry_run_end_to_end(self):
        rc = ar.main(["--mode", "weekly", "--dry-run", "--state-dir", str(self.state)])
        self.assertEqual(rc, ar.EXIT_OK)
        with open(self.state / "status-weekly.json") as f:
            disk = json.load(f)
        self.assertEqual(disk["status"], "dry_run")
        self.assertEqual(len(disk["steps"]), 8)

    def test_cli_daily_dry_run_records_plan_and_executes_nothing(self):
        rc = ar.main(["--mode", "daily", "--dry-run", "--state-dir", str(self.state),
                      "--hbrain-loop", "/x/hbrain_loop.py",
                      "--repo", "/x/wiki",
                      "--experience-review", "/x/experience_review.py",
                      "--five-domain-daily", "/x/five_domain_daily.py",
                      "--project-change-compiler", "/x/project_change_compiler.py",
                      "--facts-root", "/x/facts",
                      "--projects-root", "/x/projects",
                      "--receipts-root", "/x/receipts",
                      "--experience-inbox-root", "/x/review-inbox"])
        self.assertEqual(rc, ar.EXIT_OK)
        with open(self.state / "status-daily.json") as f:
            disk = json.load(f)
        self.assertEqual(disk["status"], "dry_run")
        self.assertEqual(len(disk["steps"]), 7)  # 4 required local + 3 optional index
        for rec in disk["steps"]:
            self.assertEqual(rec["status"], "planned", "dry-run must execute nothing")
            self.assertEqual(rec["attempts"], 0)
            self.assertIsNone(rec["exit_code"])
        self.assertEqual(disk["steps"][1]["argv"], [
            sys.executable, "/x/experience_review.py",
            "--receipts-root", "/x/receipts",
            "--inbox-root", "/x/review-inbox", "compile"])
        self.assertEqual(disk["steps"][2]["argv"], [
            sys.executable, "/x/five_domain_daily.py",
            "--wiki-root", "/x/wiki",
            "--facts-root", "/x/facts",
            "--projects-root", "/x/projects",
            "--receipts-root", "/x/receipts"])
        self.assertEqual(disk["steps"][3]["argv"], [
            sys.executable, "/x/project_change_compiler.py",
            "--facts-root", "/x/facts",
            "--projects-root", "/x/projects", "compile"])
        self.assertFalse((self.state / "history").exists())


if __name__ == "__main__":
    unittest.main()
