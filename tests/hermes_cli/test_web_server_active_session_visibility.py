"""Active-session status must reflect live sessions across every profile."""

from pathlib import Path

from hermes_state import SessionDB


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

    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["active_sessions"] == 2
    body = response.text
    assert "private-worker-session" not in body
