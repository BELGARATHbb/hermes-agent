"""Capacity-aware health telemetry for the embedded Kanban dispatcher."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import kanban_watchers
from gateway.kanban_watchers import GatewayKanbanWatchersMixin


class _Runner(GatewayKanbanWatchersMixin):
    def __init__(self) -> None:
        self._running = True
        self._kanban_dispatcher_lock_handle = None

    def _release_kanban_dispatcher_lock(self) -> None:
        self._kanban_dispatcher_lock_handle = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dispatch_results", "spawnable_boards", "expects_warning"),
    [
        pytest.param(
            {"default": SimpleNamespace(
                spawned=[], capacity_limited=True,
                all_work_capacity_limited=True,
                skipped_per_profile_capped=[],
            )},
            {"default"},
            False,
            id="global-cap-saturated",
        ),
        pytest.param(
            {"default": SimpleNamespace(
                spawned=[], capacity_limited=True,
                all_work_capacity_limited=True,
                skipped_per_profile_capped=[("task-1", "default", 5)],
            )},
            {"default"},
            False,
            id="per-profile-cap-saturated",
        ),
        pytest.param(
            {"default": SimpleNamespace(
                spawned=[], capacity_limited=True,
                skipped_per_profile_capped=[("alice-task", "alice", 1)],
            )},
            {"default"},
            True,
            id="mixed-capped-profile-and-broken-profile",
        ),
        pytest.param(
            {"default": SimpleNamespace(
                spawned=[], capacity_limited=False, skipped_per_profile_capped=[],
            )},
            set(),
            False,
            id="genuinely-idle-dispatcher",
        ),
        pytest.param(
            {"default": SimpleNamespace(
                spawned=[], capacity_limited=True,
                capacity_gate_error=True,
                all_work_capacity_limited=False,
                skipped_per_profile_capped=[],
            )},
            {"default"},
            True,
            id="capacity-controller-error-is-unhealthy",
        ),
        pytest.param(
            {"default": SimpleNamespace(
                spawned=[], capacity_limited=True,
                capacity_gate_error=True,
                all_work_capacity_limited=False,
                skipped_per_profile_capped=[],
            )},
            set(),
            True,
            id="controller-error-remains-visible-after-unassigned-refusal",
        ),
        pytest.param(
            {
                "capped": SimpleNamespace(
                    spawned=[], capacity_limited=True,
                    all_work_capacity_limited=True,
                    skipped_per_profile_capped=[("alice-task", "alice", 1)],
                ),
                "broken": SimpleNamespace(
                    spawned=[], capacity_limited=False,
                    skipped_per_profile_capped=[],
                ),
            },
            {"capped", "broken"},
            True,
            id="capped-board-does-not-hide-broken-board",
        ),
    ],
)
async def test_stuck_warning_only_counts_ticks_with_spawn_capacity(
    monkeypatch, caplog, dispatch_results, spawnable_boards, expects_warning,
):
    """Suppress warnings only when all pending work is capacity-limited."""
    from hermes_cli import config as config_module
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "auto_decompose": False,
                "dispatch_interval_seconds": 1,
                "max_in_progress": 5,
                "max_in_progress_per_profile": 5,
            }
        },
    )
    monkeypatch.setattr(
        kanban_watchers, "_acquire_singleton_lock", lambda path: (object(), "held")
    )
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **kwargs: [{"slug": slug} for slug in dispatch_results],
    )
    monkeypatch.setattr(
        kb, "dispatch_once", lambda conn, **kwargs: dispatch_results[kwargs["board"]]
    )
    monkeypatch.setattr(
        kb, "has_spawnable_ready", lambda conn: conn.slug in spawnable_boards
    )
    monkeypatch.setattr(kb, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(
        kb,
        "kanban_db_path",
        lambda slug: SimpleNamespace(
            expanduser=lambda: SimpleNamespace(resolve=lambda: "/tmp/kanban.db"),
            stat=lambda: SimpleNamespace(st_mtime_ns=1, st_size=1),
        ),
    )
    monkeypatch.setattr(kb, "connect", lambda **kwargs: SimpleNamespace(
        slug=kwargs["board"], close=lambda: None,
    ))

    async def _run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(
        kanban_watchers, "_to_thread_process_service", _run_inline,
    )

    runner = _Runner()
    sleeps = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        # One startup sleep, then six complete dispatcher ticks.
        if sleeps >= 7:
            runner._running = False

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", _sleep)

    caplog.set_level("WARNING", logger=kanban_watchers.logger.name)
    await runner._kanban_dispatcher_watcher()

    stuck_warnings = [
        record for record in caplog.records
        if "kanban dispatcher stuck" in record.getMessage()
    ]
    assert len(stuck_warnings) == (1 if expects_warning else 0)
