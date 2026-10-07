"""A turn worker may use the route's workspace, but cannot refile its session."""

import ast
import inspect
from pathlib import Path

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming


def test_streaming_turn_uses_bound_workspace_after_first_message(tmp_path):
    bound = tmp_path / "bound"
    stale = tmp_path / "stale"
    bound.mkdir()
    stale.mkdir()
    session = models.Session(
        session_id="bound-stream", workspace=str(bound),
        messages=[{"role": "user", "content": "first turn"}],
    )
    assert streaming._turn_workspace_cwd_for_session(session, str(stale)) == str(bound)
    assert session.workspace == str(bound)


def test_streaming_turn_normalizes_first_binding_and_remote_path(tmp_path, monkeypatch):
    bound = tmp_path / "bound"
    bound.mkdir()
    session = models.Session(session_id="first-stream", workspace=str(bound))
    assert streaming._turn_workspace_cwd_for_session(session, str(bound) + "/.") == str(bound)
    assert session.workspace == str(bound)

    # The resolver must receive the session profile, which owns remote path rules.
    seen = []
    monkeypatch.setattr(streaming, "_resolve_path", lambda path, profile=None: (
        seen.append((path, profile)) or Path(path)
    ))
    session.profile = "remote-profile"
    assert streaming._turn_workspace_cwd_for_session(session, "/other") == str(bound)
    assert seen == [(str(bound), "remote-profile")]


@pytest.mark.parametrize("same_path", [False, True])
def test_gateway_error_settlement_keeps_bound_workspace(tmp_path, monkeypatch, same_path):
    bound = tmp_path / "bound"
    stale = tmp_path / "stale"
    bound.mkdir()
    stale.mkdir()
    session = models.Session(
        session_id="gateway-bound", workspace=str(bound),
        messages=[{"role": "user", "content": "first turn"}],
        context_messages=[],
    )
    session.active_stream_id = "gateway-stream"
    saved = []
    monkeypatch.setattr(session, "save", lambda: saved.append(session.workspace))
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "_stream_writeback_is_current", lambda *_: True)
    monkeypatch.setattr(streaming, "_snapshot_and_append_partial_on_error", lambda *_: None)

    gateway_chat._settle_gateway_terminal_error(
        session.session_id, session.active_stream_id,
        str(bound if same_path else stale),
        "test-model", "test-provider", "gateway failed",
    )
    assert saved == [str(bound)]
    assert session.workspace == str(bound)


def test_turn_workers_have_no_late_session_workspace_writer():
    """Cover the success writer as well as Gateway's terminal error writer."""
    for worker in (
        streaming._run_agent_streaming,
        gateway_chat._run_gateway_chat_streaming,
        gateway_chat._settle_gateway_terminal_error,
    ):
        tree = ast.parse(inspect.getsource(worker))
        writes = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Attribute) and target.attr == "workspace"
        ]
        assert writes == [], worker.__name__
