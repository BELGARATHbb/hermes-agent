from __future__ import annotations

import copy

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_verdicts import VerdictValidationError


def test_post_completion_lifecycle_rung_can_append_without_reopening_history(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="post-completion-rung-red", assignee="reviewer")
        assert kb.claim_task(conn, task_id) is not None
        issuer_run = kb.latest_run(conn, task_id).id
        assert kb.complete_task(
            conn,
            task_id,
            summary="initial local receipt",
            state_events=[{
                "state": "local",
                "value": True,
                "occurred_at": 1_787_581_000,
                "receipt_id": "candidate-1245d6c7",
                "issued_by_run": issuer_run,
                "manifest_id": "manifest-initial",
            }],
        ) is True
        before_task = kb.get_task(conn, task_id)
        before_run = kb.latest_run(conn, task_id)

        # This models the only exposed write path on the immutable candidate.
        # A later pushed/reviewed/deployed receipt must append without reopening
        # or rewriting the already-done task/run, but the candidate rejects it.
        kb.complete_task(
            conn,
            task_id,
            summary="later pushed receipt",
            state_events=[{
                "state": "pushed",
                "value": True,
                "occurred_at": 1_787_581_100,
                "receipt_id": "push-receipt",
                "issued_by_run": issuer_run,
                "manifest_id": "manifest-later",
            }],
        )

        assert kb.get_task(conn, task_id) == before_task
        assert kb.latest_run(conn, task_id) == before_run
        assert [event["state"] for event in kb.list_state_events(conn, task_id)] == ["local", "pushed"]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    connection = kb.connect()
    yield connection
    connection.close()


def _event(state: str, run_id: int, at: int, receipt: str) -> dict:
    return {
        "state": state,
        "value": True,
        "occurred_at": at,
        "receipt_id": receipt,
        "issued_by_run": run_id,
        "manifest_id": f"manifest-{receipt}",
    }


def _tables(conn) -> dict:
    return {
        name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
        for name in ("tasks", "task_runs", "task_events", "task_run_state_events")
    }


def _completed_subject(conn) -> tuple[str, int]:
    task_id = kb.create_task(conn, title="subject", assignee="reviewer")
    assert kb.claim_task(conn, task_id) is not None
    run_id = kb.latest_run(conn, task_id).id
    assert kb.complete_task(
        conn,
        task_id,
        summary="complete",
        state_events=[_event("local", run_id, 100, "local-receipt")],
    )
    return task_id, run_id


def _active_child_issuer(conn, subject_id: str, *, assignee: str = "reviewer") -> tuple[str, int]:
    issuer_task = kb.create_task(
        conn,
        title="evidence issuer",
        assignee=assignee,
        parents=[subject_id],
    )
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, issuer_task) is not None
    return issuer_task, kb.latest_run(conn, issuer_task).id


def test_authorized_descendant_run_appends_independent_terminal_rungs_only(conn):
    subject_id, _subject_run = _completed_subject(conn)
    issuer_task, issuer_run = _active_child_issuer(conn, subject_id)
    before = _tables(conn)
    events = [
        _event(state, issuer_run, 200 + index, f"receipt-{state}")
        for index, state in enumerate(
            ("pushed", "reviewed", "deployed", "use_verified", "runtime_healthy")
        )
    ]

    inserted = kb.append_task_state_events(
        conn,
        subject_id,
        events,
        issuer_run_id=issuer_run,
        issuer_task_id=issuer_task,
        issuer_profile="reviewer",
    )

    assert inserted == 5
    after = _tables(conn)
    assert after["tasks"] == before["tasks"]
    assert after["task_runs"] == before["task_runs"]
    assert after["task_events"] == before["task_events"]
    assert after["task_run_state_events"][:1] == before["task_run_state_events"]
    readback = kb.list_state_events(conn, subject_id)
    assert [event["state"] for event in readback] == [
        "local", "pushed", "reviewed", "deployed", "use_verified", "runtime_healthy",
    ]
    assert all(event["issued_by_run"] == issuer_run for event in readback[1:])
    assert all(event["manifest_id"].startswith("manifest-receipt-") for event in readback[1:])
    assert "released" not in {event["state"] for event in readback}
    assert "merged" not in {event["state"] for event in readback}


def test_public_append_rejects_nonterminal_or_retained_subject_run(conn):
    active_task = kb.create_task(conn, title="active subject", assignee="reviewer")
    assert kb.claim_task(conn, active_task) is not None
    active_run = kb.latest_run(conn, active_task).id
    before = _tables(conn)
    with pytest.raises(ValueError, match="terminal"):
        kb.append_task_state_events(
            conn,
            active_task,
            [_event("pushed", active_run, 100, "active-subject")],
            issuer_run_id=active_run,
            issuer_task_id=active_task,
            issuer_profile="reviewer",
        )
    assert _tables(conn) == before

    subject_id, subject_run = _completed_subject(conn)
    before = _tables(conn)
    with pytest.raises(ValueError, match="active claimed run"):
        kb.append_task_state_events(
            conn,
            subject_id,
            [_event("pushed", subject_run, 200, "retained-general")],
            issuer_run_id=subject_run,
            issuer_task_id=subject_id,
            issuer_profile="reviewer",
        )
    assert _tables(conn) == before


def test_terminal_compatibility_rejects_superseded_run_or_unrelated_completion_fields(conn):
    subject_id, first_run = _completed_subject(conn)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (subject_id,))
    conn.commit()
    assert kb.claim_task(conn, subject_id) is not None
    second_run = kb.latest_run(conn, subject_id).id
    assert second_run != first_run
    assert kb.complete_task(conn, subject_id, summary="second completion")

    before = _tables(conn)
    with pytest.raises(ValueError, match="latest completed run"):
        kb.complete_task(
            conn,
            subject_id,
            summary="stale receipt",
            state_events=[_event("pushed", first_run, 200, "stale-run")],
        )
    assert _tables(conn) == before

    with pytest.raises(ValueError, match="only accepts summary and state_events"):
        kb.complete_task(
            conn,
            subject_id,
            result="must not be discarded",
            state_events=[_event("pushed", second_run, 201, "extra-fields")],
        )
    assert _tables(conn) == before


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda ctx: {"issuer_run_id": 999999}, "issuer run"),
        (lambda ctx: {"issuer_task_id": "t_wrong"}, "issuer task"),
        (lambda ctx: {"issuer_profile": "writer"}, "issuer profile"),
    ],
)
def test_append_rejects_mismatched_issuer_before_mutation(conn, mutate, message):
    subject_id, _ = _completed_subject(conn)
    issuer_task, issuer_run = _active_child_issuer(conn, subject_id)
    kwargs = {
        "issuer_run_id": issuer_run,
        "issuer_task_id": issuer_task,
        "issuer_profile": "reviewer",
    }
    kwargs.update(mutate(kwargs))
    before = _tables(conn)
    with pytest.raises(ValueError, match=message):
        kb.append_task_state_events(
            conn,
            subject_id,
            [_event("pushed", issuer_run, 200, "rejected")],
            **kwargs,
        )
    assert _tables(conn) == before


def test_append_rejects_unrelated_or_ended_descendant_issuer(conn):
    subject_id, _ = _completed_subject(conn)
    unrelated = kb.create_task(conn, title="unrelated", assignee="reviewer")
    assert kb.claim_task(conn, unrelated) is not None
    unrelated_run = kb.latest_run(conn, unrelated).id
    before = _tables(conn)
    with pytest.raises(ValueError, match="not authorized"):
        kb.append_task_state_events(
            conn,
            subject_id,
            [_event("pushed", unrelated_run, 200, "unrelated")],
            issuer_run_id=unrelated_run,
            issuer_task_id=unrelated,
            issuer_profile="reviewer",
        )
    assert _tables(conn) == before

    issuer_task, issuer_run = _active_child_issuer(conn, subject_id)
    assert kb.complete_task(conn, issuer_task, summary="ended")
    before = _tables(conn)
    with pytest.raises(ValueError, match="active claimed run"):
        kb.append_task_state_events(
            conn,
            subject_id,
            [_event("reviewed", issuer_run, 201, "ended")],
            issuer_run_id=issuer_run,
            issuer_task_id=issuer_task,
            issuer_profile="reviewer",
        )
    assert _tables(conn) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda event: event.update(receipt_id=""),
        lambda event: event.update(manifest_id=""),
        lambda event: event.update(state="invented"),
        lambda event: event.update(value="yes"),
        lambda event: event.update(api_token="privacy-unsafe"),
        lambda event: event.update(receipt_id="api_token=privacy-unsafe"),
        lambda event: event.update(manifest_id="password:privacy-unsafe"),
        lambda event: event.update(receipt_id="sk-proj-abcdefghijklmnopqrstuvwxyz"),
        lambda event: event.update(manifest_id="ghp_abcdefghijklmnopqrstuvwxyz123456"),
    ],
)
def test_append_rejects_invalid_or_privacy_unsafe_event_before_mutation(conn, change):
    subject_id, _ = _completed_subject(conn)
    issuer_task, issuer_run = _active_child_issuer(conn, subject_id)
    event = _event("pushed", issuer_run, 200, "invalid")
    change(event)
    before = _tables(conn)
    with pytest.raises((ValueError, VerdictValidationError)):
        kb.append_task_state_events(
            conn,
            subject_id,
            [event],
            issuer_run_id=issuer_run,
            issuer_task_id=issuer_task,
            issuer_profile="reviewer",
        )
    assert _tables(conn) == before


def test_append_rejects_receipt_replay_and_chronology_regression(conn):
    subject_id, _ = _completed_subject(conn)
    issuer_task, issuer_run = _active_child_issuer(conn, subject_id)
    kwargs = {
        "issuer_run_id": issuer_run,
        "issuer_task_id": issuer_task,
        "issuer_profile": "reviewer",
    }
    assert kb.append_task_state_events(
        conn, subject_id, [_event("pushed", issuer_run, 200, "once")], **kwargs
    ) == 1
    before = _tables(conn)
    with pytest.raises(ValueError, match="receipt"):
        kb.append_task_state_events(
            conn, subject_id, [_event("reviewed", issuer_run, 201, "once")], **kwargs
        )
    assert _tables(conn) == before
    with pytest.raises(ValueError, match="chronological"):
        kb.append_task_state_events(
            conn, subject_id, [_event("reviewed", issuer_run, 199, "older")], **kwargs
        )
    assert _tables(conn) == before


def test_append_transaction_failure_rolls_back_all_rows(conn, monkeypatch):
    subject_id, _ = _completed_subject(conn)
    issuer_task, issuer_run = _active_child_issuer(conn, subject_id)
    before = _tables(conn)
    original = kb._insert_task_state_event
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected insert failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "_insert_task_state_event", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        kb.append_task_state_events(
            conn,
            subject_id,
            [
                _event("pushed", issuer_run, 200, "tx-one"),
                _event("reviewed", issuer_run, 201, "tx-two"),
            ],
            issuer_run_id=issuer_run,
            issuer_task_id=issuer_task,
            issuer_profile="reviewer",
        )
    assert _tables(conn) == before


def test_completion_path_replay_failure_rolls_back_task_and_run(conn):
    completed_id, completed_run = _completed_subject(conn)
    assert kb.complete_task(
        conn,
        completed_id,
        summary="compat receipt",
        state_events=[_event("pushed", completed_run, 200, "board-wide-replay")],
    )

    task_id = kb.create_task(conn, title="rollback completion", assignee="reviewer")
    assert kb.claim_task(conn, task_id) is not None
    latest = kb.latest_run(conn, task_id)
    assert latest is not None
    run_id = latest.id
    before = _tables(conn)
    with pytest.raises(ValueError, match="receipt replay"):
        kb.complete_task(
            conn,
            task_id,
            summary="must roll back",
            state_events=[_event("tested", run_id, 201, "board-wide-replay")],
        )
    assert _tables(conn) == before
