#!/usr/bin/env python3
"""automation_runner.py — deterministic reliability runner (stage 0).

Shared by the daily/weekly/monthly zsh wrappers. Python stdlib only.

Key properties:
  * Steps are argv arrays (never interpolated shell strings).
  * Each step records category, timestamps, duration, exit code, status,
    attempts, and a bounded output excerpt.
  * Only steps explicitly marked retry_safe are retried; append-style
    event writes (append_only=True) are never retried.
  * A lock file prevents overlapping runs of the same mode. Lock contention
    is never silent: a machine-readable record (status "locked", exit 3) is
    atomically written to lock-<mode>-latest.json without touching the last
    real status file.
  * --dry-run records the plan in a caller-selected state directory and
    executes nothing.
  * Status is written atomically (tmp file + os.replace) after every step.
  * Every step runs under a timeout (CLI default, per-step override) so a
    silent process cannot hang forever; timeouts are recorded explicitly
    (timed_out: true, failed status) and follow normal exit semantics.
  * Each real run also writes one immutable final JSON under
    history/<mode>/ so run history survives across the observation window.

Exit semantics (also embedded in the JSON status as "exit_policy"):
  0  ok, or degraded (only optional index/delivery steps failed)
  1  a required local step failed
  2  usage error
  3  lock contention (another run of this mode is active)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = ("compute", "write", "index", "delivery")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3

OUTPUT_EXCERPT_LIMIT = 2000
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_STEP_TIMEOUT = 3600.0  # seconds; per-step override via Step.timeout

EXIT_POLICY = (
    "required local step failure => nonzero exit (1); "
    "optional index/delivery step failure => status 'degraded' but exit 0, "
    "so an external notification/index failure cannot erase local success; "
    "lock contention => exit 3; usage error => exit 2; dry-run => exit 0."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def bounded_excerpt(text: str, limit: int = OUTPUT_EXCERPT_LIMIT) -> str:
    """Keep only the tail of captured output, bounded to `limit` chars."""
    if len(text) <= limit:
        return text
    return "...[truncated]..." + text[-limit:]


@dataclass(frozen=True)
class Step:
    """One automation step. argv is an array, never a shell string."""

    name: str
    category: str
    argv: tuple
    required: bool = True
    retry_safe: bool = False
    append_only: bool = False
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    timeout: float = None  # seconds; None => use the runner/CLI default

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise ValueError(f"step {self.name!r}: unknown category {self.category!r}")
        if self.append_only and self.retry_safe:
            raise ValueError(
                f"step {self.name!r}: append-style event writes are never retried"
            )
        if not self.argv or any(not isinstance(a, str) for a in self.argv):
            raise ValueError(f"step {self.name!r}: argv must be a non-empty string array")
        if self.max_attempts < 1:
            raise ValueError(f"step {self.name!r}: max_attempts must be >= 1")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"step {self.name!r}: timeout must be > 0 seconds")

    def plan_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "argv": list(self.argv),
            "required": self.required,
            "retry_safe": self.retry_safe,
            "append_only": self.append_only,
            "max_attempts": self.max_attempts if self.retry_safe else 1,
            "timeout": self.timeout,
        }


@dataclass(frozen=True)
class Plan:
    mode: str
    steps: tuple


class RunLock:
    """Exclusive lock file with stale-pid detection (POSIX)."""

    def __init__(self, path):
        self.path = Path(path)
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    self.path.unlink(missing_ok=True)
                    continue
                return False
            with os.fdopen(fd, "w") as f:
                f.write(f"pid={os.getpid()}\ncreated={utc_now()}\n")
            self.acquired = True
            return True
        return False

    def _is_stale(self) -> bool:
        try:
            text = self.path.read_text()
            pid = int(text.split("pid=")[1].splitlines()[0])
        except (OSError, ValueError, IndexError):
            return False  # unreadable lock: treat as held, never steal
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def release(self):
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"lock held: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def write_json_atomic(path, data) -> None:
    """Write JSON atomically: tmp file in the same directory + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _execute_step(step: Step, record: dict, retry_delay: float,
                  default_timeout: float) -> None:
    started = time.monotonic()
    record["status"] = "running"
    record["started_at"] = utc_now()
    timeout = step.timeout if step.timeout is not None else default_timeout
    limit = step.max_attempts if step.retry_safe and not step.append_only else 1
    attempt = 0
    while True:
        attempt += 1
        record["timed_out"] = False
        try:
            proc = subprocess.run(
                list(step.argv), capture_output=True, text=True, timeout=timeout
            )
            record["exit_code"] = proc.returncode
            record["output_excerpt"] = bounded_excerpt(
                (proc.stdout or "") + (proc.stderr or "")
            )
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            # A silent/hung process is killed by subprocess; record the
            # timeout explicitly and apply normal exit semantics below.
            record["exit_code"] = None
            record["timed_out"] = True
            partial = ""
            for chunk in (exc.stdout, exc.stderr):
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", "replace")
                partial += chunk or ""
            note = f"timed out after {timeout:g}s"
            record["output_excerpt"] = bounded_excerpt(
                note + (": " + partial if partial.strip() else "")
            )
            ok = False
        except (OSError, subprocess.SubprocessError) as exc:
            record["exit_code"] = None
            record["output_excerpt"] = bounded_excerpt(f"spawn error: {exc!r}")
            ok = False
        record["attempts"] = attempt
        if ok:
            record["status"] = "ok"
            break
        record["status"] = "failed"
        if attempt < limit:
            time.sleep(retry_delay)
            continue
        break
    record["ended_at"] = utc_now()
    record["duration_sec"] = round(time.monotonic() - started, 3)


def run_plan(
    plan: Plan,
    state_dir,
    status_file=None,
    dry_run: bool = False,
    retry_delay: float = 1.0,
    step_timeout: float = DEFAULT_STEP_TIMEOUT,
    quiet: bool = False,
) -> dict:
    """Execute (or, if dry_run, only record) a plan. Returns the status dict
    whose "exit_code" key implements the documented exit policy."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    status_file = Path(status_file) if status_file else state_dir / f"status-{plan.mode}.json"

    status = {
        "schema": "automation-run/v1",
        "run_id": uuid.uuid4().hex,
        "mode": plan.mode,
        "dry_run": dry_run,
        "status": "dry_run" if dry_run else "running",
        "exit_code": EXIT_OK,
        "exit_policy": EXIT_POLICY,
        "state_dir": str(state_dir),
        "status_file": str(status_file),
        "started_at": utc_now(),
        "ended_at": None,
        "steps": [],
    }
    write_json_atomic(status_file, status)

    halted = False
    for step in plan.steps:
        record = step.plan_dict()
        record.update(
            {
                "status": "planned",
                "attempts": 0,
                "started_at": None,
                "ended_at": None,
                "duration_sec": None,
                "exit_code": None,
                "timed_out": False,
                "output_excerpt": "",
            }
        )
        status["steps"].append(record)

        if halted:
            record["status"] = "skipped"
        elif not dry_run:
            _execute_step(step, record, retry_delay, step_timeout)
            if record["status"] == "failed" and step.required:
                halted = True  # fail fast; later steps recorded as skipped

        write_json_atomic(status_file, status)
        if not quiet:
            print(
                f"[{plan.mode}] {record['status']:<8} {step.name} "
                f"({step.category}{'' if step.required else ', optional'}) "
                f"attempts={record['attempts']}"
            )

    failed_required = [
        s["name"] for s in status["steps"] if s["status"] == "failed" and s["required"]
    ]
    failed_optional = [
        s["name"] for s in status["steps"] if s["status"] == "failed" and not s["required"]
    ]
    status["ended_at"] = utc_now()
    if dry_run:
        status["status"] = "dry_run"
        status["exit_code"] = EXIT_OK
    elif failed_required:
        status["status"] = "failed"
        status["exit_code"] = EXIT_FAILED
    elif failed_optional:
        # Local work succeeded; only optional index/delivery failed.
        status["status"] = "degraded"
        status["exit_code"] = EXIT_OK
    else:
        status["status"] = "ok"
        status["exit_code"] = EXIT_OK
    status["failed_required_steps"] = failed_required
    status["failed_optional_steps"] = failed_optional
    # Real runs preserve one immutable final JSON per run under
    # history/<mode>/ so the four-week observation window keeps full run
    # history; status-<mode>.json remains the atomic latest status.
    history_file = None
    if not dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        history_file = (
            state_dir / "history" / plan.mode
            / f"{stamp}-{status['run_id'][:8]}.json"
        )
    status["history_file"] = str(history_file) if history_file else None
    write_json_atomic(status_file, status)
    if history_file is not None:
        write_json_atomic(history_file, status)

    if not quiet:
        print(
            f"run {plan.mode}: {status['status']} (exit {status['exit_code']})"
            + (f"; failed required: {', '.join(failed_required)}" if failed_required else "")
            + (f"; failed optional: {', '.join(failed_optional)}" if failed_optional else "")
        )
        print(f"status file: {status_file}")
        if history_file is not None:
            print(f"history file: {history_file}")
    return status


# --------------------------------------------------------------------------
# Mode definitions, mapped against the real command interfaces:
#   * hbrain_loop.py automation-run supports only --mode daily / --mode weekly
#     (there is no monthly mode).
#   * The monthly command is `hbrain_loop.py govern-monthly`, followed by
#     `governance-audit`. `governance-monthly` does not exist.
#   * gbrain: `sync --source hbrain --repo <wiki-root> --no-pull --yes
#     --skip-failed --no-embed`, `embed --stale` (no --repo), `health`
#     (no --repo).
# The production environment substitutes real absolute paths via CLI flags /
# environment variables.
# --------------------------------------------------------------------------

# Local absolute defaults for the five-domain stage-4 steps (overridable via
# the matching CLI flags or same-named environment variables).
DEFAULT_EXPERIENCE_REVIEW = "/Users/jianghaidong/hbrain/haidong-os/automation/experience_review.py"
DEFAULT_FIVE_DOMAIN_DAILY = "/Users/jianghaidong/hbrain/haidong-os/automation/five_domain_daily.py"
DEFAULT_FACTS_ROOT = "/Users/jianghaidong/hbrain/facts"
DEFAULT_PROJECTS_ROOT = "/Users/jianghaidong/hbrain/haidong-os/projects"
DEFAULT_RECEIPTS_ROOT = "/Users/jianghaidong/hbrain/haidong-os/receipts"
DEFAULT_EXPERIENCE_INBOX_ROOT = "/Users/jianghaidong/hbrain/haidong-os/experience-review"


def _index_steps(gbrain: str, repo: str) -> list:
    return [
        Step(
            name="gbrain-sync",
            category="index",
            argv=(gbrain, "sync", "--source", "hbrain", "--repo", repo,
                  "--no-pull", "--yes", "--skip-failed", "--no-embed"),
            required=False,
            retry_safe=True,
        ),
        Step(
            name="gbrain-embed",
            category="index",
            argv=(gbrain, "embed", "--stale"),
            required=False,
            retry_safe=True,
        ),
        Step(
            name="gbrain-health",
            category="index",
            argv=(gbrain, "health"),
            required=False,
            retry_safe=True,
        ),
    ]


def build_mode_plan(mode: str, python: str, hbrain_loop: str, repo: str, gbrain: str, *,
                    experience_review: str = DEFAULT_EXPERIENCE_REVIEW,
                    five_domain_daily: str = DEFAULT_FIVE_DOMAIN_DAILY,
                    facts_root: str = DEFAULT_FACTS_ROOT,
                    projects_root: str = DEFAULT_PROJECTS_ROOT,
                    receipts_root: str = DEFAULT_RECEIPTS_ROOT,
                    experience_inbox_root: str = DEFAULT_EXPERIENCE_INBOX_ROOT) -> Plan:
    hb = (python, hbrain_loop)
    if mode == "daily":
        local = [
            Step(name="hbrain-daily-maintenance", category="write",
                 argv=(*hb, "automation-run", "--mode", "daily", "--apply-frontmatter")),
            # Five-domain stage 4: experience-review compile, then the daily
            # report. Both are required local steps, in this fixed order.
            Step(name="experience-review-compile", category="write",
                 argv=(python, experience_review,
                       "--receipts-root", receipts_root,
                       "--inbox-root", experience_inbox_root,
                       "compile")),
            Step(name="five-domain-daily-report", category="write",
                 argv=(python, five_domain_daily,
                       "--wiki-root", repo,
                       "--facts-root", facts_root,
                       "--projects-root", projects_root,
                       "--receipts-root", receipts_root)),
        ]
    elif mode == "weekly":
        local = [
            Step(name="hbrain-weekly-maintenance", category="write",
                 argv=(*hb, "automation-run", "--mode", "weekly", "--apply-frontmatter")),
            Step(name="knowledge-dashboard", category="write",
                 argv=(*hb, "knowledge-dashboard")),
            Step(name="knowledge-candidates", category="write",
                 argv=(*hb, "knowledge-candidates")),
            Step(name="governance-audit", category="write",
                 argv=(*hb, "governance-audit")),
            Step(name="weekly-summary", category="compute",
                 argv=(*hb, "summarize", "--days", "7")),
        ]
    elif mode == "monthly":
        # No `automation-run --mode monthly` exists; monthly governance is
        # exactly `govern-monthly` followed by `governance-audit`.
        local = [
            Step(name="hbrain-monthly-governance", category="write",
                 argv=(*hb, "govern-monthly")),
            Step(name="governance-audit", category="write",
                 argv=(*hb, "governance-audit")),
        ]
    else:
        raise ValueError(f"unknown mode: {mode!r}")
    return Plan(mode=mode, steps=tuple(local + _index_steps(gbrain, repo)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("daily", "weekly", "monthly"), required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="record the plan in --state-dir without executing commands")
    parser.add_argument("--state-dir",
                        default=os.environ.get("AUTOMATION_STATE_DIR"),
                        help="run state directory (required for --dry-run; "
                             "default: $AUTOMATION_STATE_DIR or <repo>/.automation/state)")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--hbrain-loop",
                        default=os.environ.get("HBRAIN_LOOP", "/path/to/hbrain_loop.py"))
    parser.add_argument("--repo",
                        default=os.environ.get("SECOND_BRAIN_REPO",
                                               "/path/to/local/second-brain"))
    parser.add_argument("--gbrain", default=os.environ.get("GBRAIN_BIN", "gbrain"))
    parser.add_argument("--experience-review",
                        default=os.environ.get("EXPERIENCE_REVIEW", DEFAULT_EXPERIENCE_REVIEW),
                        help="experience_review.py path (daily stage-4 compile step)")
    parser.add_argument("--five-domain-daily",
                        default=os.environ.get("FIVE_DOMAIN_DAILY", DEFAULT_FIVE_DOMAIN_DAILY),
                        help="five_domain_daily.py path (daily stage-4 report step)")
    parser.add_argument("--facts-root",
                        default=os.environ.get("FACTS_ROOT", DEFAULT_FACTS_ROOT))
    parser.add_argument("--projects-root",
                        default=os.environ.get("PROJECTS_ROOT", DEFAULT_PROJECTS_ROOT))
    parser.add_argument("--receipts-root",
                        default=os.environ.get("RECEIPTS_ROOT", DEFAULT_RECEIPTS_ROOT))
    parser.add_argument("--experience-inbox-root",
                        default=os.environ.get("EXPERIENCE_INBOX_ROOT",
                                               DEFAULT_EXPERIENCE_INBOX_ROOT))
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_STEP_TIMEOUT,
                        help="per-step timeout in seconds (steps may override "
                             "via Step.timeout; default: %(default)s)")
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        print("error: --timeout must be > 0 seconds", file=sys.stderr)
        return EXIT_USAGE

    state_dir = args.state_dir or str(Path(args.repo) / ".automation" / "state")
    if args.dry_run and not args.state_dir:
        print("error: --dry-run requires an explicit caller-selected --state-dir",
              file=sys.stderr)
        return EXIT_USAGE

    plan = build_mode_plan(args.mode, args.python, args.hbrain_loop, args.repo,
                           args.gbrain,
                           experience_review=args.experience_review,
                           five_domain_daily=args.five_domain_daily,
                           facts_root=args.facts_root,
                           projects_root=args.projects_root,
                           receipts_root=args.receipts_root,
                           experience_inbox_root=args.experience_inbox_root)

    if args.dry_run:
        status = run_plan(plan, state_dir, dry_run=True,
                          retry_delay=args.retry_delay, step_timeout=args.timeout)
        return status["exit_code"]

    lock = RunLock(Path(state_dir) / f"run-{args.mode}.lock")
    if not lock.acquire():
        # Lock contention must not be silent: record a small machine-readable
        # status atomically in a separate file, leaving the last real run's
        # status-<mode>.json untouched.
        lock_record = {
            "schema": "automation-run/v1",
            "mode": args.mode,
            "status": "locked",
            "exit_code": EXIT_LOCKED,
            "exit_policy": EXIT_POLICY,
            "state_dir": str(state_dir),
            "lock_file": str(lock.path),
            "detected_at": utc_now(),
        }
        write_json_atomic(Path(state_dir) / f"lock-{args.mode}-latest.json",
                          lock_record)
        print(f"error: another {args.mode} run holds the lock in {state_dir}",
              file=sys.stderr)
        return EXIT_LOCKED
    try:
        status = run_plan(plan, state_dir, retry_delay=args.retry_delay,
                          step_timeout=args.timeout)
    finally:
        lock.release()
    return status["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
