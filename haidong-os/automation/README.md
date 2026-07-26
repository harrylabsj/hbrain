# IMPLEMENTATION — automation and five-domain runtime

Local second-brain automation: a deterministic reliability runner (stage 0) and a
three-conversation manual pilot template with a read-only validator (stage 1).
Dependencies: Python 3 stdlib + zsh only.

## Files

| File | Purpose |
|---|---|
| `runner/automation_runner.py` | Stage-0 runner: step records, retries, lock, dry-run, atomic JSON status, exit policy. |
| `scripts/automation-daily.zsh` | Daily wrapper — delegates to the runner with absolute, env-configurable paths. |
| `scripts/automation-weekly.zsh` | Weekly wrapper (same shape). |
| `scripts/automation-monthly.zsh` | Monthly wrapper (same shape). |
| `scripts/gbrain-daily-sync.sh` | Legacy wrapper being replaced (kept for reference only). |
| `scripts/hbrain-weekly-evolution.sh` | Legacy wrapper being replaced (kept for reference only). |
| `pilot/pilot_template.json` | Stage-1 template: three empty slots, all `planned`; no invented people/quotes/outcomes. |
| `pilot/validate_pilot.py` | Read-only validator: reports missing fields and illegal state transitions. |
| `tests/test_automation_runner.py` | Runner tests (stdlib `unittest`). |
| `tests/test_pilot_validator.py` | Pilot template/validator tests. |
| `knowledge_growth.py` | In-use gap capture, guarded auto-learning, learning ledger, and previous-day report. |
| `tests/test_knowledge_growth.py` | Auto-learning gate, dedupe, no-overwrite, and daily-report tests. |
| `fact_ledger.py` | Append-only fact events, proposals, correction, query, daily projection, and validation. |
| `project_registry.py` | Evidence-backed project current state, proposals, approval gate, rendering, and context. |
| `project_change_compiler.py` | Stage-4 proposal-only compiler: verified dated facts advance only low-impact project evidence pointers; never applies changes. |
| `logseq_event_compiler.py` | Explicit `compile:: yes` single-journal dry-run candidates; no formal writes. |
| `five_domain_runtime.py` | Zero-preload domain classification, bounded context packets, and receipt inbox. |
| `tests/test_five_domain_runtime.py` | Zero-preload, bounds, privacy, symlink, concurrency, and receipt tests. |
| `five_domain_daily.py` | Stage-4 minimal slice: derived five-domain daily report (facts/projects/knowledge/experience/evidence), proposal-only. |
| `tests/test_five_domain_daily.py` | Five-domain summary, date filtering, caps, bad-JSON issues, redaction, no-write, and symlink tests. |
| `experience_review.py` | Stage-4 slice 2: compiles receipt `experience_candidate` entries into an append-only review inbox (`compile`/`validate` only; deterministic ids, within-batch and cross-month dedup, fail-closed on bad JSON or secret-like candidates; never copies action/result/query, never writes CASS). |
| `tests/test_experience_review.py` | Date filter, no action/result leak, idempotency, concurrency, cross-month dedup, bad-JSON/secret fail-closed, symlink refusal, dry-run, and validate tests. |

## Five-domain stage 3

`five_domain_runtime.py classify` performs deterministic domain routing without reading any domain. `context` reads only a caller-selected project and up to five related facts by default. Knowledge and CASS are never called unless the caller explicitly adds `--include knowledge` or `--include experience`; the latter also requires an explicit real workspace path.

Context packets are stdout-only, carry `zero_preload: true`, cap projects/facts/knowledge/experience at 1/5/5/5, and apply a roughly 12k-token character budget. Secret-like values are redacted before output.

`receipt --receipt-file` validates and idempotently appends a completion receipt under `haidong-os/receipts/inbox/`. Receipt files are protected by a global inbox lock, cross-month deduplication, and no-follow writes. Every receipt remains `auto_promote: false` and does not update any of the five domains.

## Stage 0 design

Steps are defined as argv arrays (`Step.argv: tuple`) and executed via
`subprocess.run(list(argv))` — no shell interpolation anywhere. Each step record
contains category (`compute`/`write`/`index`/`delivery`), `started_at`/`ended_at`
(UTC ISO-8601), `duration_sec`, `exit_code`, `status`, `attempts`, `timed_out`,
and a bounded output excerpt (tail, ≤ 2000 chars).

- **Retries**: only steps with `retry_safe=True` are retried (up to `max_attempts`).
  `append_only=True` steps can never be marked retry-safe (enforced in `Step.__post_init__`)
  and always run exactly once. `Step.__post_init__` also rejects `max_attempts < 1`
  and non-positive timeouts.
- **Per-step timeout**: every step runs under a timeout so a silent process cannot
  hang forever. The CLI default is `--timeout` (3600 s); a step may override it via
  `Step.timeout`. A timeout is recorded explicitly (`timed_out: true`, `exit_code:
  null`, status `failed`, bounded excerpt including partial output) and follows the
  normal required/optional exit semantics — retry-safe steps retry after a timeout.
- **Fail-fast on required steps**: if a required local step fails, later steps are
  recorded as `skipped` — but every completed step's record (and any files it wrote)
  is preserved in the status JSON, which is rewritten atomically after each step.
- **Lock**: exclusive create (`O_CREAT|O_EXCL`) lock file per mode in the state dir,
  with stale-pid detection. An unreadable lock is treated as held (never stolen).
  Lock contention is never silent: on a failed acquire the CLI atomically writes a
  small machine-readable record (`status: locked`, `exit_code: 3`, mode, exit policy,
  detection timestamp) to `lock-<mode>-latest.json` — without overwriting the last
  real run's `status-<mode>.json`.
- **Dry-run**: `--dry-run` requires an explicit `--state-dir`; it writes the full plan
  (including argv arrays) to `status-<mode>.json` in that directory and executes nothing.
- **Atomic status**: `status-<mode>.json` is written via tmp-file + `os.replace` in the
  same directory after every step transition. `status_file` is persisted in the JSON
  from the very first write.
- **Run history**: each real run additionally writes one immutable final JSON to
  `history/<mode>/<UTC-stamp>-<run-id>.json`, so the four-week observation window
  keeps full run history. Both the latest status and the history JSON record the
  `history_file` path. Dry-runs write no history.

Mode plans (`build_mode_plan`) map the **real** production command interfaces:

- daily: `automation-run --mode daily --apply-frontmatter` (write),
  then the two five-domain stage-4 steps in fixed order:
  `experience_review.py --receipts-root <receipts> --inbox-root <review-inbox> compile` (write),
  `five_domain_daily.py --wiki-root <wiki> --facts-root <facts> --projects-root <projects> --receipts-root <receipts>` (write).
  All three are required local steps; the runner CLI exposes
  `--experience-review`, `--five-domain-daily`, `--facts-root`, `--projects-root`,
  `--receipts-root`, `--experience-inbox-root` (local absolute defaults,
  same-named environment variables, and `automation-daily.zsh` overrides all
  supported), so the daily plan is 6 steps total (3 required local + 3 optional index)
- weekly: `automation-run --mode weekly --apply-frontmatter` (write),
  `knowledge-dashboard` (write), `knowledge-candidates` (write),
  `governance-audit` (write), `summarize --days 7` (compute)
- monthly: `govern-monthly` (write), `governance-audit` (write) — there is **no**
  `automation-run --mode monthly`, and `governance-monthly` does not exist
- index (appended to every mode, all optional + retry-safe):
  `gbrain sync --source hbrain --repo <wiki-root> --no-pull --yes --skip-failed --no-embed`,
  `gbrain embed --stale` (no `--repo`), `gbrain health` (no `--repo`)

### Exit semantics

| Exit | Meaning |
|---|---|
| 0 | `ok`, `degraded` (only optional index/delivery steps failed), or dry-run |
| 1 | a required local step failed (`status: failed`) |
| 2 | usage error (e.g. `--dry-run` without `--state-dir`) |
| 3 | lock contention — another run of this mode is active |

The policy is embedded verbatim in every status file under the `exit_policy` key.
Rationale: an external index/delivery failure must never erase evidence of local
success, so those failures yield `status: degraded` with exit 0.

## Stage 1 design

`pilot_template.json` holds exactly three episodes, all `state: planned` with every
content field empty. Allowed flow is `planned -> sent -> replied -> closed`; the
validator enforces per-state required fields (response fields such as
`original_words`/`actual_result`/`surprise`/`judgment_change` must be non-empty before
`closed` is legal), checks `state_history` for skipped transitions, and enforces the
separation rule: closing may set `knowledge_routing_recommendation` but
`anchor_events` (and any cognitive-anchor keys) must remain empty — anchor training is
logged by a separate, human-driven process, and closing an episode never creates an
anchor event. In `state_history`, the initial `planned` template entry may keep an
empty `at` (no invented timestamps); every entry beyond it records a real transition
and must carry a non-empty `at` timestamp. The validator opens the file read-only and
never writes. Exit 0 = valid, 1 = issues found, 2 = unreadable input.

## Agent auto-learning and daily report

`knowledge_growth.py capture` is the deterministic write gate used after an Agent
finds a real knowledge miss and researches it. It does not browse or call a model.
It creates a retrievable `learning-candidate` only when the Agent explicitly asks
for auto-promotion, risk is low, confidence is at least 0.80, the declared source
type validates, at least two canonical links resolve, and the target page does not
exist. Otherwise it creates a proposal under `_meta/writeback-inbox/`.

Existing canonical pages are never overwritten. Each capture is idempotently
recorded under `_meta/knowledge-learning/YYYY-MM.jsonl`; neither capture nor report
writes anchor-events. Passing `--index` incrementally imports only the newly created
candidate into Gbrain, avoiding a full-repository sync and its embedding cost.

`knowledge_growth.py daily-report` defaults to the previous local calendar day. It
combines learning events, canonical `created`/`updated` metadata, knowledge hits,
knowledge misses, and new writeback proposals. It writes
`_meta/daily-reports/YYYY-MM-DD-第二大脑日报.md` and prints a short delivery digest.

## Five-domain stage 4 (minimal slice)

`five_domain_daily.py` builds the "海东认知系统五域日报" for the previous local
calendar day (`--for-date` to override). It is a derived, read-only view: it reads
only the day's month/day JSONL files — fact events + fact inbox proposals, project
`audit/applied.jsonl` rows whose `evidence.fact_id` points at a same-day fact,
knowledge-learning/knowledge-event/miss ledgers, and receipt `experience_candidate`
/ `knowledge_gap` / `evidence` — and never modifies the Project Registry, Fact
Ledger, canonical wiki pages, or CASS. Each domain section shows at most 20 items
and reports truncation; invalid JSON lines are tolerated and counted under
`issues`; secret-like values are redacted. Output is atomic (tmp + `os.replace`),
a symlink output leaf is refused, and the report is always marked
`proposal_only: true` / `auto_promote: false`. Default output:
`haidong-os/reports/five-domain-daily/YYYY-MM-DD-五域日报.md`; `--no-write` and
`--json` supported.

`project_change_compiler.py compile` reads only the selected day's formal facts
and registered project files. It emits idempotent inbox proposals containing
`last_fact_id` and `last_reviewed_at` only. Proposals are marked
`proposal_only: true`, `auto_promote: false`, and `high_impact: false`.
Use `--no-write` first; `validate` audits the proposal inbox.

## Running the tests

```sh
python3 -m unittest discover -s tests -v
```

No network, credentials, or paths outside a temp directory are touched.

## Production integration steps

1. Copy `runner/` and the three `scripts/automation-*.zsh` wrappers to the target host.
2. Set environment overrides (or edit wrapper defaults) for the real absolute paths:
   `SECOND_BRAIN_REPO`, `HBRAIN_LOOP`, `GBRAIN_BIN`, `AUTOMATION_STATE_DIR`.
   The wrappers never source or print secrets; keep credentials in the environment of
   the scheduling context, not in these files.
3. Verify each mode first with a dry run into a scratch directory, e.g.
   `scripts/automation-weekly.zsh --dry-run --state-dir /tmp/auto-weekly-dryrun`,
   and inspect the recorded plan's argv arrays.
4. Run each wrapper once manually and confirm `status-<mode>.json` reads `ok`
   (or `degraded` with exit 0 if only index steps fail); each run also leaves an
   immutable record under `history/<mode>/` for the four-week observation window.
5. Only then schedule the wrappers (launchd/cron). Monitor the JSON status files;
   alert on `status: failed` (exit 1) and optionally on repeated `degraded`.
   `lock-<mode>-latest.json` with `status: locked` (exit 3) means a scheduled run
   was skipped due to contention — investigate overlapping schedules.
6. Replace the legacy `gbrain-daily-sync.sh` / `hbrain-weekly-evolution.sh` schedules
   with the new wrappers once step 5 is green.

## Claims pending real-world evidence

The following are **not** claimed and must be earned with runtime data:

- Multi-week (e.g. four-week) unattended reliability — no long-horizon runs exist yet;
  the `history/<mode>/` records exist to collect exactly this evidence.
- Any particular embedding coverage or index freshness for `gbrain embed/sync`.
- The stale-lock recovery path has been unit-reasoned but not observed under a real
  crashed-process scenario.
- Production command names/flags now match the real `hbrain_loop.py` / `gbrain`
  interfaces as verified during review, but the wrappers have still not been
  executed against the real tools from this workspace.
- Stage 1 has zero real conversations recorded; all three slots are empty and planned.
