from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from jsonschema import Draft202012Validator

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        subject_id = kb.create_task(conn, title="subject", assignee="developer")
        assert kb.claim_task(conn, subject_id) is not None
        subject_run = kb.latest_run(conn, subject_id)
        assert subject_run is not None
        assert kb.complete_task(
            conn,
            subject_id,
            summary="done",
            state_events=[{
                "state": "local",
                "value": True,
                "occurred_at": 100,
                "receipt_id": "local-receipt",
                "issued_by_run": subject_run.id,
                "manifest_id": "local-manifest",
            }],
        )
        issuer_task = kb.create_task(
            conn,
            title="review evidence",
            assignee="reviewer",
            parents=[subject_id],
        )
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, issuer_task) is not None
        issuer_run = kb.latest_run(conn, issuer_task)
        assert issuer_run is not None
    return home, subject_id, issuer_task, issuer_run.id


def _event(run_id: int, state: str = "reviewed", receipt: str = "review-receipt") -> dict:
    return {
        "state": state,
        "value": True,
        "occurred_at": 200,
        "receipt_id": receipt,
        "issued_by_run": run_id,
        "manifest_id": f"manifest-{receipt}",
    }


def _load_plugin_module():
    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    name = f"kanban_state_append_api_{id(plugin_file)}"
    spec = importlib.util.spec_from_file_location(name, plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_tool_appends_to_completed_ancestor_with_authenticated_run(board, monkeypatch):
    _home, subject_id, issuer_task, issuer_run = board
    monkeypatch.setenv("HERMES_KANBAN_TASK", issuer_task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(issuer_run))
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_is_dispatcher_owned_worker", lambda: True)
    result = json.loads(kanban_tools._handle_append_state({
        "task_id": subject_id,
        "state_events": [_event(issuer_run)],
    }))
    assert result["ok"] is True
    assert result["inserted"] == 1
    with kb.connect() as conn:
        assert [event["state"] for event in kb.list_state_events(conn, subject_id)] == [
            "local", "reviewed",
        ]


def test_worker_tool_rejects_forged_or_unrelated_subject_without_mutation(board, monkeypatch):
    _home, subject_id, issuer_task, issuer_run = board
    monkeypatch.setenv("HERMES_KANBAN_TASK", issuer_task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(issuer_run + 999))
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_is_dispatcher_owned_worker", lambda: True)
    with kb.connect() as conn:
        before = kb.list_state_events(conn, subject_id)
    result = json.loads(kanban_tools._handle_append_state({
        "task_id": subject_id,
        "state_events": [_event(issuer_run + 999)],
    }))
    assert "issuer run" in result["error"]
    with kb.connect() as conn:
        assert kb.list_state_events(conn, subject_id) == before


def test_worker_tool_rejects_cross_board_override_before_mutation(board, monkeypatch):
    _home, subject_id, issuer_task, issuer_run = board
    monkeypatch.setenv("HERMES_KANBAN_TASK", issuer_task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(issuer_run))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    from tools import kanban_tools

    monkeypatch.setattr(kanban_tools, "_is_dispatcher_owned_worker", lambda: True)
    with kb.connect(board="default") as conn:
        before = kb.list_state_events(conn, subject_id)
    result = json.loads(kanban_tools._handle_append_state({
        "task_id": subject_id,
        "board": "other-board",
        "state_events": [_event(issuer_run)],
    }))
    assert "pinned board" in result["error"]
    with kb.connect(board="default") as conn:
        assert kb.list_state_events(conn, subject_id) == before


def test_dashboard_api_handler_appends_and_reads_back_without_inference(board):
    _home, subject_id, issuer_task, issuer_run = board
    module = _load_plugin_module()
    payload = {
        "issuer_run_id": issuer_run,
        "issuer_task_id": issuer_task,
        "issuer_profile": "reviewer",
        "state_events": [_event(issuer_run, state="deployed", receipt="deploy-only")],
    }
    response = module.append_task_state_events(
        subject_id,
        module.AppendStateEventsBody(**payload),
        board=None,
    )
    assert response["inserted"] == 1
    detail = module.get_task(
        subject_id,
        board=None,
        run_state_type=None,
        run_state_name=None,
    )
    states = {event["state"] for event in detail["state_events"]}
    assert "deployed" in states
    assert "released" not in states
    assert "merged" not in states
    assert "reviewed" not in states


def test_dashboard_api_rejects_unauthorized_issuer_before_mutation(board):
    _home, subject_id, issuer_task, issuer_run = board
    module = _load_plugin_module()
    with kb.connect() as conn:
        before = kb.list_state_events(conn, subject_id)
    with pytest.raises(HTTPException) as exc_info:
        module.append_task_state_events(
            subject_id,
            module.AppendStateEventsBody(**{
                "issuer_run_id": issuer_run,
                "issuer_task_id": issuer_task,
                "issuer_profile": "writer",
                "state_events": [_event(issuer_run)],
            }),
            board=None,
        )
    assert exc_info.value.status_code == 400
    with kb.connect() as conn:
        assert kb.list_state_events(conn, subject_id) == before


def test_dashboard_api_rejects_extra_envelope_fields_before_mutation(board):
    _home, subject_id, issuer_task, issuer_run = board
    module = _load_plugin_module()
    with kb.connect() as conn:
        before = kb.list_state_events(conn, subject_id)
    with pytest.raises(ValidationError):
        module.AppendStateEventsBody(**{
                "issuer_run_id": issuer_run,
                "issuer_task_id": issuer_task,
                "issuer_profile": "reviewer",
                "state_events": [_event(issuer_run)],
                "api_token": "must-not-be-accepted",
        })
    with kb.connect() as conn:
        assert kb.list_state_events(conn, subject_id) == before


def test_state_ledger_schema_is_valid_draft_2020_12_and_requires_bindings():
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "kanban-verdict-ledger.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    required = set(schema["$defs"]["stateEvent"]["required"])
    assert {"receipt_id", "manifest_id", "issued_by_run"} <= required

    from tools import kanban_tools

    tool_parameters = kanban_tools.KANBAN_APPEND_STATE_SCHEMA["parameters"]
    Draft202012Validator.check_schema(tool_parameters)
    event_schema = tool_parameters["properties"]["state_events"]["items"]
    assert event_schema["additionalProperties"] is False
    assert set(event_schema["required"]) == {
        "state", "value", "occurred_at", "receipt_id", "issued_by_run", "manifest_id",
    }
