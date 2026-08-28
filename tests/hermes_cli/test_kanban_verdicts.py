from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_verdicts import (
    STATE_NAMES,
    VerdictValidationError,
    classify_verdicts,
    normalize_state_events,
    normalize_verdicts,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    connection = kb.connect()
    yield connection
    connection.close()


def _verdict(
    record_id: str,
    subject: str,
    verdict: str,
    *,
    subject_hash: str = "sha256:abc",
    criterion: str = "acceptance",
    effective: bool = True,
    supersedes: list[str] | None = None,
) -> dict:
    return {
        "id": record_id,
        "verdict_subject": subject,
        "subject_id": "subject-1",
        "subject_version": "v1",
        "subject_hash": subject_hash,
        "criterion": criterion,
        "scope_rung": "task",
        "verdict_kind": "acceptance",
        "verdict": verdict,
        "defect_class": None,
        "owner": "reviewer",
        "issued_by_run": 41,
        "evidence_manifest_id": "manifest-1",
        "effective": effective,
        "effective_at": 1_787_500_000,
        "supersedes": supersedes or [],
        "contradicts": [],
        "corrected_by": [],
        "verified_by": [],
    }


def _terminal_states(run_id: int, *, unresolved: str | None = None) -> list[dict]:
    events = []
    for offset, state in enumerate(sorted(STATE_NAMES), start=1):
        events.append({
            "state": state,
            "value": "unknown" if state == unresolved else True,
            "occurred_at": 1_787_600_000 + offset,
            "receipt_id": f"receipt-{state}",
            "issued_by_run": run_id,
            "manifest_id": "manifest-1",
        })
    return events


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_verdict_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_accepted_negative_model_verdict_is_not_promoted_by_process_pass():
    records = [
        _verdict("model-fail", "model_class", "fail"),
        _verdict("process-pass", "process", "pass"),
    ]
    classified = classify_verdicts(records, legacy_text="HUMAN_VERIFY_PASS")
    assert classified["source"] == "typed"
    assert classified["outcome"] == "fail"
    assert classified["subjects"]["model_class"] == "fail"
    assert classified["subjects"]["process"] == "pass"


def test_current_typed_pass_ignores_historical_fail_prose():
    classified = classify_verdicts(
        [_verdict("current-pass", "product", "pass")],
        legacy_text="Current PASS. Historical FAIL was corrected.",
    )
    assert classified["source"] == "typed"
    assert classified["outcome"] == "pass"


def test_contradictory_same_hash_effective_reviews_are_conflicted():
    records = [
        _verdict("review-a", "product", "pass"),
        _verdict("review-b", "product", "fail"),
    ]
    classified = classify_verdicts(records)
    assert classified["source"] == "typed_conflict"
    assert classified["outcome"] == "conflicted"
    assert classified["conflicts"] == [["review-a", "review-b"]]


def test_process_only_failure_does_not_become_product_failure():
    classified = classify_verdicts([_verdict("protocol-fail", "process", "fail")])
    assert classified["outcome"] == "unverified"
    assert classified["subjects"] == {"process": "fail"}


def test_supersession_removes_old_failure_from_effective_outcome():
    records = [
        _verdict("old-fail", "product", "fail"),
        _verdict("new-pass", "product", "pass", supersedes=["old-fail"]),
    ]
    classified = classify_verdicts(records)
    assert classified["outcome"] == "pass"
    assert classified["effective_record_ids"] == ["new-pass"]


def test_unknown_and_not_applicable_states_remain_distinct():
    events = normalize_state_events([
        {
            "state": "deployed",
            "value": "unknown",
            "occurred_at": 1_787_500_001,
            "receipt_id": "receipt-1",
            "issued_by_run": 41,
            "manifest_id": "manifest-1",
        },
        {
            "state": "runtime_healthy",
            "value": "not_applicable",
            "occurred_at": 1_787_500_002,
            "receipt_id": "receipt-2",
            "issued_by_run": 41,
            "manifest_id": "manifest-2",
        },
    ])
    assert [event["value"] for event in events] == ["unknown", "not_applicable"]


def test_json_decoded_not_applicable_state_is_accepted_by_value():
    value = json.loads('{"value":"not_applicable"}')["value"]

    events = normalize_state_events([{
        "state": "deployed",
        "value": value,
        "occurred_at": 1_787_500_002,
        "receipt_id": "receipt-json",
        "issued_by_run": 41,
        "manifest_id": "manifest-json",
    }])

    assert events[0]["value"] == "not_applicable"


def test_integer_state_values_do_not_alias_booleans():
    event = {
        "state": "deployed",
        "value": 1,
        "occurred_at": 1_787_500_002,
        "receipt_id": "receipt-int",
        "issued_by_run": 41,
        "manifest_id": "manifest-int",
    }

    with pytest.raises(VerdictValidationError, match="state value is not recognized"):
        normalize_state_events([event])


def test_normalization_is_private_shape_and_does_not_mutate_input():
    raw = [_verdict("v1", "product", "pass")]
    before = copy.deepcopy(raw)
    normalized = normalize_verdicts(raw)
    assert raw == before
    assert normalized == raw
    with pytest.raises(VerdictValidationError, match="unexpected field"):
        normalize_verdicts([{**raw[0], "api_token": "secret"}])


def test_published_json_schema_covers_both_typed_ledgers():
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "kanban-verdict-ledger.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(schema["required"]) == {"verdicts", "state_events"}
    assert set(schema["$defs"]["verdict"]["properties"]["verdict_subject"]["enum"]) == {
        "product", "model_class", "design", "process", "authority", "evidence", "release",
    }


def test_invalid_verdict_rolls_back_without_terminalizing_task(conn):
    task_id = kb.create_task(conn, title="typed", assignee="worker")
    kb.claim_task(conn, task_id)
    with pytest.raises(VerdictValidationError):
        kb.complete_task(
            conn,
            task_id,
            summary="must not land",
            verdicts=[{**_verdict("bad", "product", "pass"), "verdict": "maybe"}],
        )
    assert kb.get_task(conn, task_id).status == "running"
    assert kb.latest_run(conn, task_id).ended_at is None


def test_legacy_rows_are_not_rewritten_and_are_labeled_heuristic(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
            status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
            created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
            claim_lock TEXT, claim_expires INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            profile TEXT, step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
            claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
            outcome TEXT, summary TEXT, metadata TEXT, error TEXT
        );
        INSERT INTO tasks (id, title, status, created_at) VALUES ('legacy', 'old', 'done', 1);
        INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, summary)
        VALUES ('legacy', 'done', 1, 2, 'completed', 'HUMAN_VERIFY_PASS historical text');
    """)
    conn.commit()
    before = conn.execute("SELECT summary, metadata FROM task_runs WHERE task_id='legacy'").fetchone()
    conn.close()

    kb.init_db(db_path)
    with kb.connect(db_path) as migrated:
        after = migrated.execute("SELECT summary, metadata, verdicts FROM task_runs WHERE task_id='legacy'").fetchone()
        run = kb.latest_run(migrated, "legacy")
        assert before == (after["summary"], after["metadata"])
        assert after["verdicts"] is None
        assert run.verdicts == []
        classified = kb.classify_task_outcome(migrated, "legacy")
        assert classified["source"] == "legacy_heuristic"


def test_completion_persists_typed_verdicts_and_timestamped_state_events(conn):
    task_id = kb.create_task(conn, title="typed", assignee="worker")
    kb.claim_task(conn, task_id)
    active_run_id = kb.latest_run(conn, task_id).id
    verdicts = [_verdict("product-pass", "product", "pass")]
    verdicts[0]["issued_by_run"] = active_run_id
    state_events = [{
        "state": "tested",
        "value": True,
        "occurred_at": 1_787_500_003,
        "receipt_id": "pytest-receipt",
        "issued_by_run": active_run_id,
        "manifest_id": "pytest-manifest",
    }]
    assert kb.complete_task(
        conn,
        task_id,
        summary="typed pass",
        verdicts=verdicts,
        state_events=state_events,
    )
    run = kb.latest_run(conn, task_id)
    assert run.verdicts == verdicts
    assert kb.list_state_events(conn, task_id) == [{
        **state_events[0],
        "id": 1,
        "task_id": task_id,
        "run_id": run.id,
    }]
    assert kb.classify_task_outcome(conn, task_id)["outcome"] == "pass"


def test_untyped_new_completion_gets_schema_valid_process_unverified_record(conn):
    task_id = kb.create_task(conn, title="legacy caller", assignee="worker")
    kb.claim_task(conn, task_id)
    assert kb.complete_task(conn, task_id, summary="plain prose")
    run = kb.latest_run(conn, task_id)
    assert len(run.verdicts) == 1
    assert run.verdicts[0]["verdict_subject"] == "process"
    assert run.verdicts[0]["verdict"] == "unverified"
    assert run.verdicts[0]["issued_by_run"] == run.id


def test_typed_v1_policy_rejects_unproved_completion_without_mutation(conn):
    kb.write_board_metadata(None, completion_policy="typed_v1")
    task_id = kb.create_task(conn, title="must prove done", assignee="worker")
    kb.claim_task(conn, task_id)
    run = kb.latest_run(conn, task_id)

    with pytest.raises(
        VerdictValidationError,
        match="requires at least one acceptance verdict",
    ):
        kb.complete_task(conn, task_id, summary="trust me")

    assert kb.get_task(conn, task_id).status == "running"
    assert kb.latest_run(conn, task_id).id == run.id
    assert kb.latest_run(conn, task_id).ended_at is None


def test_typed_v1_policy_rejects_unknown_release_disposition(conn):
    kb.write_board_metadata(None, completion_policy="typed_v1")
    task_id = kb.create_task(conn, title="still not deployed", assignee="worker")
    kb.claim_task(conn, task_id)
    run_id = kb.latest_run(conn, task_id).id
    verdict = _verdict("accepted", "product", "pass")
    verdict["issued_by_run"] = run_id

    with pytest.raises(
        VerdictValidationError,
        match="unresolved release disposition.*deployed",
    ):
        kb.complete_task(
            conn,
            task_id,
            summary="deployment is unknown",
            verdicts=[verdict],
            state_events=_terminal_states(run_id, unresolved="deployed"),
        )

    assert kb.get_task(conn, task_id).status == "running"
    assert kb.list_state_events(conn, task_id) == []


def test_typed_v1_policy_closes_only_with_bound_terminal_evidence(conn):
    kb.write_board_metadata(None, completion_policy="typed_v1")
    task_id = kb.create_task(conn, title="fully proven", assignee="worker")
    kb.claim_task(conn, task_id)
    run_id = kb.latest_run(conn, task_id).id
    verdict = _verdict("accepted", "product", "pass")
    verdict["issued_by_run"] = run_id

    assert kb.complete_task(
        conn,
        task_id,
        summary="all applicable rungs are proven",
        verdicts=[verdict],
        state_events=_terminal_states(run_id),
        expected_run_id=run_id,
    )

    assert kb.get_task(conn, task_id).status == "done"
    assert len(kb.list_state_events(conn, task_id)) == len(STATE_NAMES)


def test_typed_v1_worker_context_explains_exact_terminal_contract(conn):
    kb.write_board_metadata(None, completion_policy="typed_v1")
    task_id = kb.create_task(conn, title="explain proof", assignee="worker")
    kb.claim_task(conn, task_id)
    run_id = kb.latest_run(conn, task_id).id

    context = kb.build_worker_context(conn, task_id)

    assert "fail-closed `typed_v1` completion" in context
    assert f"Current issuer run: `{run_id}`" in context
    assert "local, tested, committed, pushed, reviewed, merged" in context
    assert "Each value must be `true` or `not_applicable`" in context


def test_dashboard_api_returns_ui_ready_trust_outcome_and_ledgers(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="api", assignee="worker")
        kb.claim_task(conn, task_id)
        active_run_id = kb.latest_run(conn, task_id).id
        verdict = _verdict("api-pass", "product", "pass")
        verdict["issued_by_run"] = active_run_id
        kb.complete_task(
            conn,
            task_id,
            summary="Historical FAIL, current typed PASS",
            verdicts=[verdict],
            state_events=[{
                "state": "committed",
                "value": True,
                "occurred_at": 1_787_500_004,
                "receipt_id": "git-sha",
                "issued_by_run": active_run_id,
                "manifest_id": "git-manifest",
            }],
        )
    module = _load_plugin_module()
    body = module.get_task(
        task_id,
        board=None,
        run_state_type=None,
        run_state_name=None,
    )
    assert body["trust_outcome"]["source"] == "typed"
    assert body["trust_outcome"]["outcome"] == "pass"
    assert body["runs"][-1]["verdicts"][0]["id"] == "api-pass"
    assert body["state_events"][0]["state"] == "committed"
