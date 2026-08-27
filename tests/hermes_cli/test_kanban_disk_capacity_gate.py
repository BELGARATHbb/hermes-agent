"""Bateman dashboard disk-capacity gate at the real Kanban spawn boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def capacity_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / ".hermes"
    home.mkdir()
    helper = tmp_path / "hermes-final-review"
    helper_bytes = b"#!/bin/sh\nexit 0\n"
    helper.write_bytes(helper_bytes)
    helper.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(kb._BATEMAN_DISPATCH_CAPACITY_ENV, "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_bateman_capacity_helper_path", lambda: helper)
    monkeypatch.setattr(
        kb,
        "_BATEMAN_FINAL_REVIEW_SHA256",
        hashlib.sha256(helper_bytes).hexdigest(),
    )
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda sample=None: "ok")
    kb.init_db()
    return home, helper


def _result_for_argv(
    argv: list[str],
    status: object = "ok",
    *,
    returncode: int = 0,
    lane: str = "dashboard",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    target = Path(argv[argv.index("--path") + 1]).resolve()
    payload = {"lane": lane, "status": status, "path": str(target)}
    return subprocess.CompletedProcess(argv, returncode, json.dumps(payload), stderr)


def _row(conn, task_id: str):
    return conn.execute(
        "SELECT status, claim_lock, current_run_id, consecutive_failures "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def test_ready_and_review_each_check_capacity_immediately_before_spawn(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    calls: list[tuple[list[str], dict]] = []
    kb.workspaces_root().mkdir(parents=True, exist_ok=True)
    target = kb.workspaces_root().resolve()

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _result_for_argv(argv, "warn")

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    spawned: list[str] = []

    with kb.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="worker")
        review = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 42,
        )

    assert set(spawned) == {ready, review}
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv[0].startswith("/proc/self/fd/")
        assert argv[1:] == [
            "capacity", "--lane", "dashboard", "--path", str(target), "--json",
        ]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == kb._BATEMAN_DISPATCH_CAPACITY_TIMEOUT_SECONDS
        assert kwargs["pass_fds"] == (int(argv[0].rsplit("/", 1)[1]),)
    assert result.capacity_limited is False
    assert result.capacity_gate_error is False
    assert result.capacity_deferred == []


def test_capacity_checks_each_task_target_filesystem(
    capacity_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    first_root = tmp_path / "volume-a"
    second_root = tmp_path / "volume-b"
    first_root.mkdir()
    second_root.mkdir()
    checked: list[Path] = []

    def fake_run(argv, **kwargs):
        checked.append(Path(argv[argv.index("--path") + 1]))
        return _result_for_argv(argv)

    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    with kb.connect() as conn:
        kb.create_task(
            conn, title="first", assignee="worker", workspace_kind="dir",
            workspace_path=str(first_root),
        )
        kb.create_task(
            conn, title="second", assignee="worker", workspace_kind="dir",
            workspace_path=str(second_root),
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)

    assert result.capacity_gate_error is False
    assert checked == [first_root.resolve(), second_root.resolve()]


def test_unmaterialized_worktree_checks_board_default_filesystem(
    capacity_home, monkeypatch, tmp_path,
):
    repo_anchor = tmp_path / "repo-on-target-volume"
    repo_anchor.mkdir()
    monkeypatch.setattr(
        kb,
        "read_board_metadata",
        lambda board=None: {"default_workdir": str(repo_anchor)},
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="worktree", assignee="worker", workspace_kind="worktree",
        )
        task = kb.get_task(conn, task_id)
    assert kb._bateman_task_capacity_target(task) == repo_anchor.resolve()


def test_repo_root_worktree_anchor_checks_eventual_worktrees_filesystem(
    capacity_home, monkeypatch, tmp_path,
):
    repo_root = tmp_path / "repo"
    target_volume = tmp_path / "worktree-volume"
    repo_root.mkdir()
    target_volume.mkdir()
    (repo_root / ".worktrees").symlink_to(target_volume, target_is_directory=True)
    monkeypatch.setattr(kb, "_is_linked_worktree_checkout", lambda path: False)
    monkeypatch.setattr(kb, "_git_toplevel", lambda path: repo_root)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="repo anchor", assignee="worker",
            workspace_kind="worktree", workspace_path=str(repo_root),
        )
        task = kb.get_task(conn, task_id)
    assert kb._bateman_task_capacity_target(task) == target_volume.resolve()


def test_occupied_worktree_checks_sibling_fallback_filesystem(
    capacity_home, monkeypatch, tmp_path,
):
    occupied = tmp_path / "occupied-worktree"
    repo_root = tmp_path / "repo"
    target_volume = tmp_path / "worktree-volume"
    occupied.mkdir()
    repo_root.mkdir()
    target_volume.mkdir()
    (repo_root / ".worktrees").symlink_to(target_volume, target_is_directory=True)
    monkeypatch.setattr(kb, "_is_linked_worktree_checkout", lambda path: True)
    monkeypatch.setattr(kb, "_git_current_branch", lambda path: "wt/someone-else")
    monkeypatch.setattr(kb, "_repo_root_for_worktree_target", lambda path: repo_root)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="occupied", assignee="worker",
            workspace_kind="worktree", workspace_path=str(occupied),
            branch_name="wt/owned-task",
        )
        task = kb.get_task(conn, task_id)
    assert kb._bateman_task_capacity_target(task) == target_volume.resolve()


def test_block_defers_ready_without_claim_run_or_failure(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    calls = 0

    def blocked(argv, **kwargs):
        nonlocal calls
        calls += 1
        return _result_for_argv(
            argv, "block", returncode=75, stderr="disk policy block",
        )

    monkeypatch.setattr(kb.subprocess, "run", blocked)
    spawned: list[str] = []
    with kb.connect() as conn:
        first = kb.create_task(conn, title="one", assignee="worker")
        second = kb.create_task(conn, title="two", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 42,
        )
        for task_id in (first, second):
            row = _row(conn, task_id)
            assert row["status"] == "ready"
            assert row["claim_lock"] is None
            assert row["current_run_id"] is None
            assert row["consecutive_failures"] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
            ).fetchone()[0] == 0

    assert calls == 2
    assert spawned == []
    assert result.capacity_limited is True
    assert result.capacity_gate_error is False
    assert result.all_work_capacity_limited is True
    assert [item[0] for item in result.capacity_deferred] == [first, second]


def test_block_defers_review_without_claim_or_failure(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda argv, **kwargs: _result_for_argv(
            argv, "block", returncode=75, stderr="low disk",
        ),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)
        row = _row(conn, task_id)
        assert row["status"] == "review"
        assert row["claim_lock"] is None
        assert row["current_run_id"] is None
        assert row["consecutive_failures"] == 0
    assert result.capacity_gate_error is False
    assert result.capacity_deferred[0][0] == task_id


def test_capacity_refusal_does_not_persist_default_assignment(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda argv, **kwargs: _result_for_argv(
            argv, "block", returncode=75, stderr="low disk",
        ),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unassigned")
        result = kb.dispatch_once(
            conn,
            default_assignee="worker",
            spawn_fn=lambda *args, **kwargs: 42,
        )
        row = conn.execute(
            "SELECT status, assignee, claim_lock, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assigned_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ).fetchone()[0]
    assert row["status"] == "ready"
    assert row["assignee"] is None
    assert row["claim_lock"] is None
    assert row["current_run_id"] is None
    assert assigned_events == 0
    assert result.auto_assigned_default == []
    assert result.all_work_capacity_limited is True


def test_respawn_guarded_work_prevents_all_capacity_limited_classification(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda argv, **kwargs: _result_for_argv(
            argv, "block", returncode=75, stderr="low disk",
        ),
    )
    with kb.connect() as conn:
        guarded = kb.create_task(
            conn, title="guarded", assignee="worker", priority=10,
        )
        blocked = kb.create_task(conn, title="blocked", assignee="worker")
        monkeypatch.setattr(
            kb,
            "check_respawn_guard",
            lambda _conn, task_id, lane="ready": (
                "recent_success" if task_id == guarded else None
            ),
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)
    assert result.respawn_guarded == [(guarded, "recent_success")]
    assert [item[0] for item in result.capacity_deferred] == [blocked]
    assert result.capacity_limited is True
    assert result.all_work_capacity_limited is False


@pytest.mark.parametrize(
    "failure",
    ["missing", "timeout", "malformed", "wrong-lane", "non-string-status"],
)
def test_helper_failure_modes_fail_closed_as_control_errors(
    capacity_home, all_assignees_spawnable, monkeypatch, failure,
):
    if failure == "missing":
        capacity_home[1].unlink()
    elif failure == "timeout":
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 5))
        monkeypatch.setattr(kb.subprocess, "run", timeout)
    elif failure == "malformed":
        monkeypatch.setattr(
            kb.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "not-json", ""),
        )
    elif failure == "wrong-lane":
        monkeypatch.setattr(
            kb.subprocess,
            "run",
            lambda argv, **kwargs: _result_for_argv(argv, lane="interactive"),
        )
    else:
        monkeypatch.setattr(
            kb.subprocess,
            "run",
            lambda argv, **kwargs: _result_for_argv(argv, status=[]),
        )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=failure, assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)
        assert _row(conn, task_id)["status"] == "ready"
    assert result.capacity_limited is True
    assert result.capacity_gate_error is True
    assert result.all_work_capacity_limited is False
    assert result.capacity_deferred[0][0] == task_id


def test_helper_hash_mismatch_fails_before_execution(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    capacity_home[1].write_bytes(b"#!/bin/sh\necho replaced\n")
    capacity_home[1].chmod(0o700)
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unverified helper must not execute"),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="hash mismatch", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)
        assert _row(conn, task_id)["status"] == "ready"
    assert result.capacity_gate_error is True
    assert result.all_work_capacity_limited is False


def test_dry_run_and_disabled_gate_do_not_execute_helper(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    def unexpected(*args, **kwargs):
        pytest.fail("capacity helper must not run")

    monkeypatch.setattr(kb.subprocess, "run", unexpected)
    with kb.connect() as conn:
        kb.create_task(conn, title="dry", assignee="worker")
        dry = kb.dispatch_once(conn, dry_run=True)
        assert len(dry.spawned) == 1

    monkeypatch.delenv(kb._BATEMAN_DISPATCH_CAPACITY_ENV)
    with kb.connect() as conn:
        live = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)
    assert len(live.spawned) == 1


def test_housekeeping_and_promotion_continue_while_spawn_is_deferred(
    capacity_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda argv, **kwargs: _result_for_argv(
            argv, "block", returncode=75, stderr="low disk",
        ),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="promote", assignee="worker")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 42)
        assert result.promoted == 1
        assert _row(conn, task_id)["status"] == "ready"
        assert _row(conn, task_id)["claim_lock"] is None
    assert result.capacity_gate_error is False
    assert result.capacity_deferred[0][0] == task_id
