#!/usr/bin/env python3
"""validate_pilot.py — read-only validator for the manual pilot file.

Checks structure, per-state required fields, legal state transitions, and
the separation between knowledge writeback, action writeback, and human
cognitive-anchor training (closing may recommend knowledge routing but must
never log an anchor event). Never modifies the file it validates.

Exit 0 = valid, 1 = issues found, 2 = unreadable/unparseable input.
"""

from __future__ import annotations

import json
import sys

SCHEMA = "manual-pilot/v1"
STATE_FLOW = ["planned", "sent", "replied", "closed"]

ALL_FIELDS = [
    "object", "send_time", "question", "prior_judgment", "expected_result",
    "original_words", "actual_result", "surprise", "judgment_change",
    "knowledge_writeback", "action_writeback", "next_step", "review_date",
]

# Fields that must be non-empty once an episode reaches a given state.
REQUIRED_BY_STATE = {
    "planned": [],
    "sent": ["object", "send_time", "question", "prior_judgment", "expected_result"],
    "replied": ["object", "send_time", "question", "prior_judgment", "expected_result",
                "original_words", "actual_result", "surprise", "judgment_change"],
    "closed": list(ALL_FIELDS),
}

# Keys that would blur the writeback/anchor separation if present and non-empty.
FORBIDDEN_NONEMPTY_KEYS = ["anchor_events", "cognitive_anchor", "anchor_training"]


def _required_up_to(state: str) -> list:
    idx = STATE_FLOW.index(state)
    required = []
    for s in STATE_FLOW[: idx + 1]:
        for f in REQUIRED_BY_STATE[s]:
            if f not in required:
                required.append(f)
    return required


def validate(data: dict) -> list:
    """Return a list of human-readable issue strings (empty = valid)."""
    issues = []
    if not isinstance(data, dict):
        return ["top level must be a JSON object"]
    if data.get("schema") != SCHEMA:
        issues.append(f"schema must be {SCHEMA!r}, got {data.get('schema')!r}")
    episodes = data.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        issues.append("'episodes' must be a non-empty array")
        return issues

    for ep in episodes:
        eid = ep.get("id", "<missing id>") if isinstance(ep, dict) else "<not an object>"
        if not isinstance(ep, dict):
            issues.append(f"{eid}: episode must be an object")
            continue

        state = ep.get("state")
        if state not in STATE_FLOW:
            issues.append(f"{eid}: invalid state {state!r} (allowed: {STATE_FLOW})")
            continue

        # State history must follow planned -> sent -> replied -> closed with
        # no skipping (e.g. planned -> closed without sent/replied is illegal).
        history = ep.get("state_history")
        if not isinstance(history, list) or not history:
            issues.append(f"{eid}: state_history must be a non-empty array")
        else:
            seq = [h.get("state") for h in history if isinstance(h, dict)]
            if seq[0] != "planned":
                issues.append(f"{eid}: state_history must start at 'planned', got {seq[0]!r}")
            positions = []
            for s in seq:
                if s not in STATE_FLOW:
                    issues.append(f"{eid}: state_history contains invalid state {s!r}")
                else:
                    positions.append(STATE_FLOW.index(s))
            for prev, cur in zip(positions, positions[1:]):
                if cur != prev and cur != prev + 1:
                    issues.append(
                        f"{eid}: illegal transition "
                        f"{STATE_FLOW[prev]!r} -> {STATE_FLOW[cur]!r} (no skipping)"
                    )
            if positions and positions[-1] != STATE_FLOW.index(state):
                issues.append(
                    f"{eid}: current state {state!r} does not match state_history tail"
                )
            if state == "closed" and ("sent" not in seq or "replied" not in seq):
                issues.append(
                    f"{eid}: cannot be 'closed' without passing through 'sent' and 'replied'"
                )
            # The initial planned template entry may keep an empty 'at' — no
            # invented timestamps. Every entry beyond it records a real
            # transition and must carry a non-empty 'at' timestamp.
            for idx, h in enumerate(history[1:], start=1):
                if isinstance(h, dict) and not str(h.get("at") or "").strip():
                    issues.append(
                        f"{eid}: state_history entry {idx} ({h.get('state')!r}) "
                        f"must have a non-empty 'at' timestamp (only the initial "
                        f"planned template entry may leave it empty)"
                    )

        # Field presence (keys must exist) and per-state non-emptiness.
        for f in ALL_FIELDS:
            if f not in ep:
                issues.append(f"{eid}: missing field key {f!r}")
        for f in _required_up_to(state):
            if f in ep and not str(ep.get(f) or "").strip():
                issues.append(f"{eid}: field {f!r} is required and empty in state {state!r}")

        # Closing may recommend knowledge routing, but must never log an
        # anchor event; anchor training stays separate from both writebacks.
        for key in FORBIDDEN_NONEMPTY_KEYS:
            if ep.get(key):
                issues.append(
                    f"{eid}: {key!r} must remain empty — anchor training is logged "
                    f"separately and never by closing an episode"
                )

    return issues


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: validate_pilot.py <pilot.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[0], "r", encoding="utf-8") as f:  # read-only
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read/parse {argv[0]}: {exc}", file=sys.stderr)
        return 2

    issues = validate(data)
    if issues:
        print(f"INVALID: {len(issues)} issue(s)")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print(f"OK: {len(data['episodes'])} episode(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
