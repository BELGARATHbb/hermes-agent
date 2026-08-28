"""Typed Kanban verdict and release-state ledger contracts.

The persisted contract is intentionally small, append-only, and value-safe.
Historical prose remains readable, but it is never promoted to typed evidence.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any, Iterable, Optional

VERDICT_SUBJECTS = frozenset(
    {"product", "model_class", "design", "process", "authority", "evidence", "release"}
)
VERDICT_VALUES = frozenset({"pass", "fail", "blocked", "unverified", "not_applicable"})
STATE_NAMES = frozenset(
    {
        "local", "tested", "committed", "pushed", "reviewed", "merged",
        "released", "deployed", "use_verified", "runtime_healthy",
    }
)
STATE_VALUES = frozenset({True, False, "unknown", "not_applicable"})
PRIMARY_OUTCOME_SUBJECTS = ("product", "model_class", "design", "release")
ACCEPTANCE_SUBJECTS = frozenset({"product", "design", "release"})

VERDICT_FIELDS = frozenset(
    {
        "id", "verdict_subject", "subject_id", "subject_version", "subject_hash",
        "criterion", "scope_rung", "verdict_kind", "verdict", "defect_class",
        "owner", "process_verdict", "authority_verdict", "issued_by_run",
        "evidence_manifest_id", "effective", "effective_at", "supersedes",
        "contradicts", "corrected_by", "verified_by",
    }
)
VERDICT_REQUIRED = frozenset(
    {
        "id", "verdict_subject", "subject_id", "subject_version", "subject_hash",
        "criterion", "scope_rung", "verdict_kind", "verdict", "defect_class",
        "owner", "issued_by_run", "evidence_manifest_id", "effective",
        "effective_at", "supersedes", "contradicts", "corrected_by", "verified_by",
    }
)
STATE_FIELDS = frozenset(
    {"state", "value", "occurred_at", "receipt_id", "issued_by_run", "manifest_id"}
)
STATE_REQUIRED = frozenset(
    {"state", "value", "occurred_at", "receipt_id", "issued_by_run", "manifest_id"}
)


class VerdictValidationError(ValueError):
    """Raised before any DB mutation when a ledger payload is invalid."""


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerdictValidationError(f"{field} must be a non-empty string")
    if len(value) > 512:
        raise VerdictValidationError(f"{field} exceeds 512 characters")
    return value


def _evidence_identifier(value: Any, field: str) -> str:
    value = _nonempty_string(value, field)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}", value):
        raise VerdictValidationError(
            f"{field} must be an opaque identifier without whitespace or credential values"
        )
    if re.search(r"(?i)(?:api[_-]?key|token|secret|password|passwd)[:=]", value):
        raise VerdictValidationError(f"{field} must not contain credential-shaped data")
    from agent.redact import redact_sensitive_text
    if redact_sensitive_text(value, force=True) != value:
        raise VerdictValidationError(f"{field} must not contain sensitive data")
    return value


def _nullable_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VerdictValidationError(f"{field} must be a positive integer")
    return value


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VerdictValidationError(f"{field} must be a non-negative integer timestamp")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise VerdictValidationError(f"{field} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        item = _nonempty_string(item, field)
        if item in seen:
            raise VerdictValidationError(f"{field} must not contain duplicate ids")
        seen.add(item)
        result.append(item)
    return result


def normalize_verdicts(records: Optional[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Validate and deep-copy a typed verdict array without mutating callers."""
    if records is None:
        return []
    if not isinstance(records, (list, tuple)):
        raise VerdictValidationError("verdicts must be an array")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise VerdictValidationError(f"verdicts[{index}] must be an object")
        extra = set(raw) - VERDICT_FIELDS
        if extra:
            raise VerdictValidationError(
                f"verdicts[{index}] has unexpected field(s): {', '.join(sorted(extra))}"
            )
        missing = VERDICT_REQUIRED - set(raw)
        if missing:
            raise VerdictValidationError(
                f"verdicts[{index}] missing required field(s): {', '.join(sorted(missing))}"
            )
        item = copy.deepcopy(raw)
        item["id"] = _nonempty_string(item["id"], "id")
        if item["id"] in ids:
            raise VerdictValidationError(f"duplicate verdict id: {item['id']}")
        ids.add(item["id"])
        if item["verdict_subject"] not in VERDICT_SUBJECTS:
            raise VerdictValidationError("verdict_subject is not recognized")
        item["subject_id"] = _nonempty_string(item["subject_id"], "subject_id")
        item["subject_version"] = _nonempty_string(item["subject_version"], "subject_version")
        item["subject_hash"] = _nonempty_string(item["subject_hash"], "subject_hash")
        item["criterion"] = _nonempty_string(item["criterion"], "criterion")
        item["scope_rung"] = _nonempty_string(item["scope_rung"], "scope_rung")
        item["verdict_kind"] = _nonempty_string(item["verdict_kind"], "verdict_kind")
        if item["verdict"] not in VERDICT_VALUES:
            raise VerdictValidationError("verdict is not recognized")
        item["defect_class"] = _nullable_string(item["defect_class"], "defect_class")
        item["owner"] = _nonempty_string(item["owner"], "owner")
        for optional_verdict in ("process_verdict", "authority_verdict"):
            if optional_verdict in item and item[optional_verdict] is not None:
                if item[optional_verdict] not in VERDICT_VALUES:
                    raise VerdictValidationError(f"{optional_verdict} is not recognized")
        item["issued_by_run"] = _positive_int(item["issued_by_run"], "issued_by_run")
        item["evidence_manifest_id"] = _nonempty_string(
            item["evidence_manifest_id"], "evidence_manifest_id"
        )
        if not isinstance(item["effective"], bool):
            raise VerdictValidationError("effective must be a boolean")
        item["effective_at"] = _timestamp(item["effective_at"], "effective_at")
        for relation in ("supersedes", "contradicts", "corrected_by", "verified_by"):
            item[relation] = _string_list(item[relation], relation)
        normalized.append(item)
    return normalized


def normalize_state_events(events: Optional[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Validate and deep-copy timestamped state evidence events."""
    if events is None:
        return []
    if not isinstance(events, (list, tuple)):
        raise VerdictValidationError("state_events must be an array")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise VerdictValidationError(f"state_events[{index}] must be an object")
        extra = set(raw) - STATE_FIELDS
        if extra:
            raise VerdictValidationError(
                f"state_events[{index}] has unexpected field(s): {', '.join(sorted(extra))}"
            )
        missing = STATE_REQUIRED - set(raw)
        if missing:
            raise VerdictValidationError(
                f"state_events[{index}] missing required field(s): {', '.join(sorted(missing))}"
            )
        item = copy.deepcopy(raw)
        if item["state"] not in STATE_NAMES:
            raise VerdictValidationError("state is not recognized")
        value = item["value"]
        if not (
            type(value) is bool
            or (type(value) is str and value in {"unknown", "not_applicable"})
        ):
            raise VerdictValidationError("state value is not recognized")
        item["occurred_at"] = _timestamp(item["occurred_at"], "occurred_at")
        item["receipt_id"] = _evidence_identifier(item["receipt_id"], "receipt_id")
        item["issued_by_run"] = _positive_int(item["issued_by_run"], "issued_by_run")
        item["manifest_id"] = _evidence_identifier(item["manifest_id"], "manifest_id")
        normalized.append(item)
    return normalized


def validate_typed_completion_evidence(
    verdicts: Optional[Iterable[dict[str, Any]]],
    state_events: Optional[Iterable[dict[str, Any]]],
) -> dict[str, Any]:
    """Validate the fail-closed ``typed_v1`` terminal evidence contract.

    ``unknown`` and ``False`` remain valid append-only observations, but they
    are not final dispositions. A caller must keep the card open until every
    rung is proven or deliberately marked ``not_applicable``.
    """
    typed_verdicts = normalize_verdicts(verdicts)
    typed_states = normalize_state_events(state_events)
    if not typed_verdicts:
        raise VerdictValidationError(
            "typed completion requires at least one acceptance verdict"
        )

    classified = classify_verdicts(typed_verdicts)
    acceptance = {
        subject: value
        for subject, value in classified["subjects"].items()
        if subject in ACCEPTANCE_SUBJECTS
    }
    if not acceptance:
        raise VerdictValidationError(
            "typed completion requires an effective product, design, or release verdict"
        )
    rejected = {
        subject: value
        for subject, value in acceptance.items()
        if value not in {"pass", "not_applicable"}
    }
    if rejected:
        details = ", ".join(
            f"{subject}={value}" for subject, value in sorted(rejected.items())
        )
        raise VerdictValidationError(
            f"typed completion acceptance is not terminal-pass: {details}"
        )

    states = [event["state"] for event in typed_states]
    duplicates = sorted({state for state in states if states.count(state) > 1})
    if duplicates:
        raise VerdictValidationError(
            f"typed completion has duplicate state disposition(s): {', '.join(duplicates)}"
        )
    missing = sorted(STATE_NAMES - set(states))
    if missing:
        raise VerdictValidationError(
            f"typed completion is missing release disposition(s): {', '.join(missing)}"
        )
    unresolved = sorted(
        event["state"]
        for event in typed_states
        if event["value"] not in {True, "not_applicable"}
    )
    if unresolved:
        raise VerdictValidationError(
            f"typed completion has unresolved release disposition(s): {', '.join(unresolved)}"
        )

    manifest_ids = {record["evidence_manifest_id"] for record in typed_verdicts}
    unbound = sorted(
        event["state"]
        for event in typed_states
        if event["manifest_id"] not in manifest_ids
    )
    if unbound:
        raise VerdictValidationError(
            "typed completion state evidence is not bound to a verdict manifest for: "
            + ", ".join(unbound)
        )
    return classified


def default_terminal_verdict(
    *, task_id: str, run_id: int, profile: Optional[str], outcome: str, occurred_at: int
) -> dict[str, Any]:
    """Create a non-claiming typed process record for legacy completion callers."""
    negative = outcome in {"blocked", "crashed", "timed_out", "spawn_failed", "gave_up"}
    value = "blocked" if outcome == "blocked" else ("fail" if negative else "unverified")
    return {
        "id": f"run-{run_id}-process",
        "verdict_subject": "process",
        "subject_id": task_id,
        "subject_version": f"run:{run_id}",
        "subject_hash": f"run:{run_id}",
        "criterion": "terminal lifecycle receipt",
        "scope_rung": "run",
        "verdict_kind": "lifecycle",
        "verdict": value,
        "defect_class": "worker_process" if negative else None,
        "owner": profile or "unassigned",
        "issued_by_run": run_id,
        "evidence_manifest_id": f"run:{run_id}",
        "effective": True,
        "effective_at": occurred_at,
        "supersedes": [],
        "contradicts": [],
        "corrected_by": [],
        "verified_by": [],
    }


def _collapse_values(values: set[str]) -> str:
    if len(values) > 1:
        return "conflicted"
    return next(iter(values))


def _overall_outcome(subjects: dict[str, str]) -> str:
    values = [subjects[s] for s in PRIMARY_OUTCOME_SUBJECTS if s in subjects]
    if not values:
        return "unverified"
    if "conflicted" in values:
        return "conflicted"
    for value in ("fail", "blocked", "unverified"):
        if value in values:
            return value
    if all(value == "not_applicable" for value in values):
        return "not_applicable"
    return "pass"


def _legacy_heuristic(text: str) -> str:
    upper = text.upper()
    has_pass = bool(re.search(r"(?:^|\W)(?:PASS|HUMAN_VERIFY_PASS|REVIEW_PASS)(?:\W|$)", upper))
    has_fail = bool(re.search(r"(?:^|\W)(?:FAIL|FAILED|REVIEW_FAIL)(?:\W|$)", upper))
    if has_pass and has_fail:
        return "conflicted"
    if has_fail:
        return "fail"
    if has_pass:
        return "pass"
    return "unverified"


def classify_verdicts(
    records: Optional[Iterable[dict[str, Any]]], *, legacy_text: str = ""
) -> dict[str, Any]:
    """Resolve effective typed records; fall back to explicitly heuristic prose."""
    typed = normalize_verdicts(records)
    if not typed:
        return {
            "source": "legacy_heuristic",
            "trust": "heuristic_only",
            "outcome": _legacy_heuristic(legacy_text),
            "subjects": {},
            "effective_record_ids": [],
            "conflicts": [],
        }

    superseded = {
        target
        for record in typed
        if record["effective"]
        for target in record["supersedes"]
    }
    effective = [
        record for record in typed if record["effective"] and record["id"] not in superseded
    ]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in effective:
        key = (
            record["verdict_subject"], record["subject_id"],
            record["subject_hash"], record["criterion"],
        )
        groups[key].append(record)

    conflicts: list[list[str]] = []
    for group in groups.values():
        if len({record["verdict"] for record in group}) > 1:
            conflicts.append(sorted(record["id"] for record in group))
    conflicts.sort()

    values_by_subject: dict[str, set[str]] = defaultdict(set)
    for record in effective:
        values_by_subject[record["verdict_subject"]].add(record["verdict"])
    subjects = {
        subject: _collapse_values(values)
        for subject, values in sorted(values_by_subject.items())
    }
    return {
        "source": "typed_conflict" if conflicts else "typed",
        "trust": "conflicted" if conflicts else "typed_effective",
        "outcome": _overall_outcome(subjects),
        "subjects": subjects,
        "effective_record_ids": [record["id"] for record in effective],
        "conflicts": conflicts,
    }
