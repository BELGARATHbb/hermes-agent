"""Active-session status must reflect live sessions across every profile."""

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from hermes_state import SessionDB


SUMMARY = [{"role": "user", "content": "compressed handoff"}]


def _fingerprint(path: Path) -> tuple[int, int, int, str, tuple[str, ...]]:
    info = path.stat()
    sidecars = tuple(sorted(item.name for item in path.parent.glob(f"{path.name}*")))
    return (
        info.st_size,
        info.st_mtime_ns,
        stat.S_IMODE(info.st_mode),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        sidecars,
    )


def _seed_session(db_path: Path, session_id: str, *, ended: bool = False) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=db_path)
    try:
        db.create_session(session_id=session_id, source="cli")
        if ended:
            db.end_session(session_id, "completed")
    finally:
        db.close()


def _profile_targets(monkeypatch, homes: dict[str, Path]) -> None:
    import hermes_state
    from hermes_cli import profiles

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", homes["default"] / "state.db")
    monkeypatch.setattr(
        profiles,
        "profiles_to_serve",
        lambda multiplex=True: list(homes.items()),
    )


def test_status_counts_live_kanban_sessions_across_profiles(tmp_path, monkeypatch):
    """Two live profile workers must not collapse to the dashboard profile only."""
    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    developer = root / "profiles" / "developer"
    editor = root / "profiles" / "editor"
    monkeypatch.setenv("HERMES_HOME", str(root))
    _profile_targets(
        monkeypatch,
        {"default": root, "developer": developer, "editor": editor},
    )

    _seed_session(developer / "state.db", "kanban-t_35b304c6")
    _seed_session(editor / "state.db", "kanban-t_d483ecad")
    _seed_session(root / "state.db", "historical-terminal", ended=True)

    assert web_server._count_status_active_sessions() == 2


def test_status_removes_worker_after_its_session_ends(tmp_path, monkeypatch):
    """A terminal worker disappears from the next status read, without a restart."""
    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    developer = root / "profiles" / "developer"
    monkeypatch.setenv("HERMES_HOME", str(root))
    _profile_targets(monkeypatch, {"default": root, "developer": developer})

    db_path = developer / "state.db"
    _seed_session(db_path, "kanban-t_35b304c6")
    assert web_server._count_status_active_sessions() == 1

    db = SessionDB(db_path=db_path)
    try:
        db.end_session("kanban-t_35b304c6", "completed")
    finally:
        db.close()

    assert web_server._count_status_active_sessions() == 0


def test_status_count_is_not_truncated_to_fifty_sessions(tmp_path, monkeypatch):
    """The status number is an exact count, not a projection of one list page."""
    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    _profile_targets(monkeypatch, {"default": root})

    db_path = root / "state.db"
    for index in range(55):
        _seed_session(db_path, f"worker-{index}")

    assert web_server._count_status_active_sessions() == 55


def test_active_count_projects_multihop_root_branch_and_reset_tips(tmp_path):
    """Each user-visible conversation counts once at its effective live tip."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="plain", source="cli")

        db.create_session(session_id="compressed-root", source="cli")
        db.publish_compression_child(
            parent_session_id="compressed-root",
            child_session_id="compressed-mid",
            source="cli",
            messages=SUMMARY,
            require_compression_lease=False,
        )
        db.publish_compression_child(
            parent_session_id="compressed-mid",
            child_session_id="compressed-tip",
            source="cli",
            messages=SUMMARY,
            require_compression_lease=False,
        )

        db.create_session(session_id="branch-parent", source="cli")
        db.end_session("branch-parent", "branched")
        branch_config = {"_branched_from": "branch-parent"}
        db.create_session(
            session_id="branch",
            source="cli",
            parent_session_id="branch-parent",
            model_config=branch_config,
        )
        db.publish_compression_child(
            parent_session_id="branch",
            child_session_id="branch-mid",
            source="cli",
            model_config=branch_config,
            messages=SUMMARY,
            require_compression_lease=False,
        )
        db.publish_compression_child(
            parent_session_id="branch-mid",
            child_session_id="branch-tip",
            source="cli",
            model_config=branch_config,
            messages=SUMMARY,
            require_compression_lease=False,
        )

        db.create_session(
            session_id="reset-parent", source="gateway", session_key="dm:1"
        )
        db.end_session("reset-parent", "session_reset")
        reset_config = {"_reset_from": "reset-parent"}
        db.create_session(
            session_id="reset",
            source="gateway",
            parent_session_id="reset-parent",
            session_key="dm:1",
            model_config=reset_config,
        )
        db.publish_compression_child(
            parent_session_id="reset",
            child_session_id="reset-mid",
            source="gateway",
            model_config=reset_config,
            messages=SUMMARY,
            require_compression_lease=False,
        )
        db.publish_compression_child(
            parent_session_id="reset-mid",
            child_session_id="reset-tip",
            source="gateway",
            model_config=reset_config,
            messages=SUMMARY,
            require_compression_lease=False,
        )

        assert db.active_session_count(active_after=0.0) == 4
    finally:
        db.close()


def test_active_count_applies_visibility_and_lifecycle_to_projected_tips(tmp_path):
    """Root metadata cannot keep a terminal/private/stale tip active."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for suffix in ("live", "ended", "hidden", "archived", "stale", "delegate"):
            root = f"root-{suffix}"
            db.create_session(session_id=root, source="cli")
            db.publish_compression_child(
                parent_session_id=root,
                child_session_id=f"tip-{suffix}",
                source="cli",
                messages=SUMMARY,
                require_compression_lease=False,
            )

        db.end_session("tip-ended", "completed")
        with db._lock:
            conn = db._conn
            assert conn is not None
            conn.execute("UPDATE sessions SET hidden = 1 WHERE id = 'tip-hidden'")
            conn.execute("UPDATE sessions SET archived = 1 WHERE id = 'tip-archived'")
            conn.execute("UPDATE sessions SET started_at = 1 WHERE id = 'tip-stale'")
            conn.execute("UPDATE messages SET timestamp = 1 WHERE session_id = 'tip-stale'")
            conn.execute(
                "UPDATE sessions SET model_config = ? WHERE id = 'tip-delegate'",
                (json.dumps({"_delegate_from": "root-delegate"}),),
            )

        assert db.active_session_count(active_after=2.0) == 1
    finally:
        db.close()


def test_status_skips_unreadable_or_invalid_profile_stores_without_mutation(
    tmp_path, monkeypatch
):
    """Status garnish never creates, heals, repairs, or sidecar-touches stores."""
    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    healthy_a = root / "profiles" / "healthy-a"
    healthy_b = root / "profiles" / "healthy-b"
    missing = root / "profiles" / "missing"
    zero = root / "profiles" / "zero"
    stale = root / "profiles" / "stale"
    malformed = root / "profiles" / "malformed"
    inaccessible = root / "profiles" / "inaccessible"
    homes = {
        "default": root,
        "healthy-a": healthy_a,
        "healthy-b": healthy_b,
        "missing": missing,
        "zero": zero,
        "stale": stale,
        "malformed": malformed,
        "inaccessible": inaccessible,
    }
    _profile_targets(monkeypatch, homes)
    _seed_session(healthy_a / "state.db", "worker-a")
    _seed_session(healthy_b / "state.db", "worker-b")

    zero.mkdir(parents=True)
    zero_db = zero / "state.db"
    zero_db.touch()

    stale.mkdir(parents=True)
    stale_db = stale / "state.db"
    with sqlite3.connect(stale_db) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")

    malformed.mkdir(parents=True)
    malformed_db = malformed / "state.db"
    malformed_db.write_bytes(b"not a sqlite database")

    inaccessible.mkdir(parents=True)
    inaccessible_db = inaccessible / "state.db"
    _seed_session(inaccessible_db, "private-worker")
    inaccessible_hash = hashlib.sha256(inaccessible_db.read_bytes()).hexdigest()
    inaccessible_db.chmod(0)

    fixed_ns = 1_700_000_000_000_000_000
    for path in (zero_db, stale_db, malformed_db, inaccessible_db):
        os.utime(path, ns=(fixed_ns, fixed_ns))
    before = {
        path: _fingerprint(path)
        for path in (zero_db, stale_db, malformed_db)
    }
    inaccessible_before = inaccessible_db.stat()
    missing_before = tuple(missing.parent.glob("missing*"))

    try:
        assert web_server._count_status_active_sessions() == 2
        assert {
            path: _fingerprint(path)
            for path in (zero_db, stale_db, malformed_db)
        } == before
        inaccessible_after = inaccessible_db.stat()
        assert inaccessible_after.st_size == inaccessible_before.st_size
        assert inaccessible_after.st_mtime_ns == inaccessible_before.st_mtime_ns
        assert stat.S_IMODE(inaccessible_after.st_mode) == 0
        assert tuple(missing.parent.glob("missing*")) == missing_before
        assert not missing.exists()
    finally:
        inaccessible_db.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert hashlib.sha256(inaccessible_db.read_bytes()).hexdigest() == inaccessible_hash
    assert tuple(sorted(item.name for item in inaccessible.glob("state.db*"))) == (
        "state.db",
    )


def test_status_reads_live_wal_exactly_without_touching_source_sidecars(
    tmp_path, monkeypatch
):
    """An active profile's uncheckpointed WAL row is counted observationally."""
    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "developer"
    _profile_targets(monkeypatch, {"default": root, "developer": profile})
    db_path = profile / "state.db"
    db_path.parent.mkdir(parents=True)
    db = SessionDB(db_path=db_path)
    try:
        assert db._conn is not None
        db._conn.execute("PRAGMA wal_autocheckpoint=0")
        db.create_session(session_id="wal-worker", source="cli")
        source_files = (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
        assert all(path.exists() for path in source_files)
        durable_files = source_files[:2]
        before = {path: _fingerprint(path) for path in durable_files}
        sidecar_names = tuple(sorted(path.name for path in db_path.parent.iterdir()))

        assert web_server._count_status_active_sessions() == 1
        assert {path: _fingerprint(path) for path in durable_files} == before
        assert tuple(sorted(path.name for path in db_path.parent.iterdir())) == sidecar_names
    finally:
        db.close()


def test_status_uses_five_minute_activity_window(tmp_path, monkeypatch):
    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    _profile_targets(monkeypatch, {"default": root})
    db_path = root / "state.db"
    _seed_session(db_path, "fresh")
    _seed_session(db_path, "stale")
    db = SessionDB(db_path=db_path)
    try:
        assert db._conn is not None
        db._conn.execute("UPDATE sessions SET started_at = 701 WHERE id = 'fresh'")
        db._conn.execute("UPDATE sessions SET started_at = 699 WHERE id = 'stale'")
    finally:
        db.close()
    monkeypatch.setattr(web_server.time, "time", lambda: 1000.0)

    assert web_server._count_status_active_sessions() == 1


def test_authenticated_status_exposes_only_the_aggregate(tmp_path, monkeypatch):
    """The browser gets the exact count without profile or session identifiers."""
    from starlette.testclient import TestClient

    import hermes_cli.web_server as web_server

    root = tmp_path / ".hermes"
    developer = root / "profiles" / "developer"
    editor = root / "profiles" / "editor"
    monkeypatch.setenv("HERMES_HOME", str(root))
    _profile_targets(
        monkeypatch,
        {"default": root, "developer": developer, "editor": editor},
    )
    _seed_session(developer / "state.db", "private-worker-session-a")
    _seed_session(editor / "state.db", "private-worker-session-b")
    private_values = (
        "private-profile-name",
        "private-task-id",
        "private-transcript",
        "private-prompt",
        "private-title",
        "private-cwd",
        "private-cmdline",
        "private-credential",
        "private-token",
        "private-cookie",
    )
    db = SessionDB(db_path=developer / "state.db")
    try:
        assert db._conn is not None
        db._conn.execute(
            """UPDATE sessions
               SET profile_name = ?, title = ?, cwd = ?, system_prompt = ?,
                   model_config = ?, origin_json = ?
               WHERE id = 'private-worker-session-a'""",
            (
                private_values[0],
                private_values[4],
                private_values[5],
                private_values[3],
                json.dumps(
                    {
                        "task": private_values[1],
                        "cmdline": private_values[6],
                        "credential": private_values[7],
                        "token": private_values[8],
                        "cookie": private_values[9],
                    }
                ),
                json.dumps({"transcript": private_values[2]}),
            ),
        )
    finally:
        db.close()

    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["active_sessions"] == 2
    body = response.text
    assert "private-worker-session" not in body
    assert all(value not in body for value in private_values)
