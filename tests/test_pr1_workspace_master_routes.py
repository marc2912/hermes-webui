"""Route regressions for PR 1 workspace/project ownership."""

import pytest
import threading
import json
from unittest.mock import MagicMock
from urllib.parse import urlparse

from api import routes
from api import workspace as workspace_api
from api import profiles
from api import models


def _capture(monkeypatch):
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: (payload, status))
    monkeypatch.setattr(routes, "bad", lambda _handler, message, status=400: ({"error": message}, status))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")


def _project_handler_request(monkeypatch, path, body, profile="default"):
    """Drive the project POST handler with captured replies and no network listener."""
    _capture(monkeypatch)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: profile)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_kw: True)
    handler = MagicMock()
    handler.command = "POST"
    handler.headers = {}
    return routes.handle_post(handler, urlparse(path))


def test_project_rename_foreign_profile_returns_404_without_write(monkeypatch, tmp_path):
    """Operator decision (MCP delete fix): the route delegates ownership to storage."""
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([
        {"project_id": "foreign", "name": "Other", "profile": "other", "parent_id": None},
    ]))
    before = projects_file.read_bytes()
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)

    payload, status = _project_handler_request(
        monkeypatch, "/api/projects/rename", {"project_id": "foreign", "name": "Changed"},
    )
    assert status == 404 and payload["error"] == "Project not found"
    assert projects_file.read_bytes() == before


def test_project_delete_foreign_profile_returns_404_without_write(monkeypatch, tmp_path):
    """Operator decision (MCP delete fix): foreign delete preserves every file link."""
    projects_file = tmp_path / "projects.json"
    workspaces_file = tmp_path / "workspaces.json"
    projects_file.write_text(json.dumps([
        {"project_id": "parent", "name": "Parent", "profile": "other", "parent_id": None},
        {"project_id": "foreign", "name": "Other", "profile": "other", "parent_id": "parent"},
        {"project_id": "child", "name": "Child", "profile": "other", "parent_id": "foreign"},
    ]))
    workspaces_file.write_text(json.dumps([
        {"workspace_id": "linked", "path": str(tmp_path), "name": "Linked", "project_id": "foreign"},
    ]))
    projects_before = projects_file.read_bytes()
    workspaces_before = workspaces_file.read_bytes()
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(workspace_api, "_workspaces_file_for_profile", lambda profile=None: workspaces_file)

    payload, status = _project_handler_request(
        monkeypatch, "/api/projects/delete", {"project_id": "foreign"},
    )
    assert status == 404 and payload["error"] == "Project not found"
    assert projects_file.read_bytes() == projects_before
    assert workspaces_file.read_bytes() == workspaces_before
    assert next(row for row in json.loads(projects_file.read_text()) if row["project_id"] == "child")["parent_id"] == "foreign"
    assert json.loads(workspaces_file.read_text())[0]["project_id"] == "foreign"


def test_project_create_stamps_active_profile_and_null_parent(monkeypatch, tmp_path):
    """Operator decision (MCP delete fix): route and storage agree on new rows."""
    projects_file = tmp_path / "projects.json"
    projects_file.write_text("[]")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)

    payload, status = _project_handler_request(
        monkeypatch, "/api/projects/create", {"name": "Local"},
    )
    assert status == 200 and payload["ok"] is True
    stored = json.loads(projects_file.read_text())
    assert len(stored) == 1
    assert stored[0]["profile"] == "default"
    assert stored[0]["parent_id"] is None
    assert stored[0] == payload["project"]


def test_workspace_add_links_project_and_rolls_back_on_save_failure(monkeypatch, tmp_path):
    _capture(monkeypatch)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    workspaces = [{"path": str(tmp_path), "name": "Home", "workspace_id": "home", "project_id": None}]
    projects = []
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: list(workspaces))
    monkeypatch.setattr(routes, "save_workspaces", lambda rows, profile=None: workspaces.__setitem__(slice(None), rows))
    monkeypatch.setattr(routes, "load_projects", lambda: list(projects))
    monkeypatch.setattr(routes, "save_projects", lambda rows: projects.__setitem__(slice(None), rows))
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    payload, status = routes._handle_workspace_add(None, {"path": str(workspace), "name": "Repo", "project_name": "Code"})
    assert status == 200
    assert payload["workspace"]["workspace_id"]
    assert payload["workspace"]["project_id"] == payload["project"]["project_id"]
    assert payload["project"]["name"] == "Code"
    assert projects == [payload["project"]]

    another = tmp_path / "other"
    another.mkdir()
    def fail_save(_rows, profile=None):
        raise OSError("injected workspace write failure")
    monkeypatch.setattr(routes, "save_workspaces", fail_save)
    failed, failed_status = routes._handle_workspace_add(None, {"path": str(another)})
    assert failed_status == 500
    assert len(projects) == 1


def test_workspace_add_opt_out(monkeypatch, tmp_path):
    _capture(monkeypatch)
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    rows = []
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: [])
    monkeypatch.setattr(routes, "save_workspaces", lambda value, profile=None: rows.extend(value))
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    payload, status = routes._handle_workspace_add(None, {"path": str(workspace), "link_project": False})
    assert status == 200
    assert payload["workspace"]["project_id"] is None
    assert payload["workspace"]["workspace_id"]
    assert rows == [payload["workspace"]]


def test_project_delete_restores_workspace_link_when_project_save_fails(monkeypatch, tmp_path):
    """Operator decision (MCP delete fix): file rollback precedes session clearing."""
    workspaces_file = tmp_path / "workspaces.json"
    projects_file = tmp_path / "projects.json"
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    workspaces_file.write_text(json.dumps([
        {"path": str(home), "name": "Home", "workspace_id": "home", "project_id": None},
        {"path": str(repo), "name": "Repo", "workspace_id": "repo", "project_id": "deleted"},
    ]))
    projects_file.write_text(json.dumps([
        {"project_id": "parent", "name": "Parent", "profile": "default", "parent_id": None},
        {"project_id": "deleted", "name": "Deleted", "profile": "default", "parent_id": "parent"},
        {"project_id": "child", "name": "Child", "profile": "default", "parent_id": "deleted"},
    ]))
    monkeypatch.setattr(workspace_api, "_workspaces_file_for_profile", lambda profile=None: workspaces_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    session_calls = []

    def fail_project_save(_rows):
        raise OSError("project save failed")

    with pytest.raises(OSError, match="project save failed"):
        models.delete_project_record(
            "deleted", "default", clear_sessions=lambda pid: session_calls.append(pid),
            save_projects_fn=fail_project_save,
        )

    assert session_calls == []
    assert next(row for row in json.loads(workspaces_file.read_text()) if row["workspace_id"] == "repo")["project_id"] == "deleted"
    projects = json.loads(projects_file.read_text())
    assert {row["project_id"] for row in projects} == {"parent", "deleted", "child"}
    assert next(row for row in projects if row["project_id"] == "child")["parent_id"] == "deleted"

    monkeypatch.setattr(routes, "save_projects", fail_project_save)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"project_id": "deleted"})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_kw: True)
    _capture(monkeypatch)
    handler = MagicMock()
    handler.command = "POST"
    handler.headers = {}
    error, status = routes.handle_post(handler, urlparse("/api/projects/delete"))
    assert status == 500
    assert "Could not delete project" in error["error"]
    assert next(row for row in json.loads(workspaces_file.read_text()) if row["workspace_id"] == "repo")["project_id"] == "deleted"
    assert any(row["project_id"] == "deleted" for row in json.loads(projects_file.read_text()))


def test_workspace_add_reports_migration_persist_failure(monkeypatch, tmp_path):
    _capture(monkeypatch)
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: (_ for _ in ()).throw(OSError("private path detail")))
    payload, status = routes._handle_workspace_add(None, {"path": str(target), "link_project": False})
    assert status == 500
    assert "Could not load workspaces" in payload["error"]


def test_link_project_enforces_one_to_one_and_profile(monkeypatch, tmp_path):
    _capture(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rows = [
        {"workspace_id": "home", "path": str(home), "name": "Home", "project_id": None},
        {"workspace_id": "repo", "path": str(workspace), "name": "Repo", "project_id": None},
    ]
    projects = [
        {"project_id": "local", "name": "Local", "profile": "default"},
        {"project_id": "foreign", "name": "Foreign", "profile": "other"},
    ]
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(workspace_api, "_profile_default_workspace", lambda profile=None: str(home))
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: [dict(w) for w in rows])
    monkeypatch.setattr(routes, "save_workspaces", lambda value, profile=None: rows.__setitem__(slice(None), value))
    monkeypatch.setattr(routes, "load_projects", lambda: [dict(p) for p in projects])
    monkeypatch.setattr(routes, "save_projects", lambda value: projects.__setitem__(slice(None), value))

    denied, code = routes._handle_workspace_link_project(None, {"workspace_id": "home", "project_id": "local"})
    assert code == 400 and denied["code"] == "not_linkable"
    denied, code = routes._handle_workspace_link_project(None, {"workspace_id": "repo", "project_id": "foreign"})
    assert code == 404
    linked, code = routes._handle_workspace_link_project(None, {"workspace_id": "repo", "project_id": "local"})
    assert code == 200 and linked["workspace"]["project_id"] == "local"
    denied, code = routes._handle_workspace_link_project(None, {"workspace_id": "repo", "project_name": "Again"})
    assert code == 409 and denied["code"] == "workspace_already_linked"
    assert len(projects) == 2


def test_project_derivation_requires_exact_path_and_profile(monkeypatch):
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: [
        {"path": "/repo", "project_id": "linked"},
    ])
    monkeypatch.setattr(routes, "load_projects", lambda: [
        {"project_id": "linked", "profile": "alpha"},
    ])
    assert routes._workspace_project_for_path("/repo", "alpha") == "linked"
    assert routes._workspace_project_for_path("/repo/subdir", "alpha") is None
    assert routes._workspace_project_for_path("/repo", "beta") is None


def test_legacy_tagged_session_source_is_explicit():
    class Legacy:
        project_id = "legacy"
        project_source = None

    assert routes._session_project_source(Legacy()) == "explicit"


def test_first_message_workspace_override_rederives_and_freezes_prompt_path(monkeypatch, tmp_path):
    from api.models import Session

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(routes, "_workspace_project_for_path", lambda path, _profile: {
        str(first): "first-project", str(second): "second-project",
    }.get(path))
    session = Session(session_id="first-binding", workspace=str(first),
                      project_id="first-project", project_source="derived")
    routes._bind_empty_session_workspace(session, str(second))
    assert (session.workspace, session.created_workspace) == (str(second), str(second))
    assert (session.project_id, session.project_source) == ("second-project", "derived")

    explicit = Session(session_id="explicit-binding", workspace=str(first),
                       project_id="manual-project", project_source="explicit")
    routes._bind_empty_session_workspace(explicit, str(second))
    assert (explicit.project_id, explicit.project_source) == ("manual-project", "explicit")


def test_missing_implicit_workspace_cannot_rebind_messageful_session(monkeypatch):
    class Session:
        workspace = "/missing"
        profile = "default"
        messages = [{"role": "user", "content": "already sent"}]
        context_messages = []
        pending_user_message = None

    monkeypatch.setattr(routes, "resolve_implicit_workspace_with_recovery",
                        lambda *_args, **_kwargs: ("/fallback", True))
    monkeypatch.setattr(routes, "persist_recovered_workspace_binding",
                        lambda *_args, **_kwargs: pytest.fail("messageful workspace was rewritten"))
    session = Session()
    with pytest.raises(ValueError, match="workspace_missing"):
        routes._resolve_chat_workspace_with_recovery(session, None)
    assert session.workspace == "/missing"


def test_recovery_preserves_project_and_records_deleted_path(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    deleted = str(tmp_path / "deleted")
    recovered = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    session = models.Session(session_id="recovery-pr1", workspace=deleted,
                             project_id="linked", project_source="derived")
    session.save()
    monkeypatch.setattr(routes, "resolve_implicit_workspace_with_recovery",
                        lambda *_args, **_kwargs: (recovered, True))
    assert routes._resolve_chat_workspace_with_recovery(session, None) == recovered
    loaded = models.Session.load("recovery-pr1")
    assert (loaded.project_id, loaded.project_source) == ("linked", "derived")
    assert loaded.workspace_recovered_from == deleted
    assert loaded.workspace == recovered


def test_explicit_switch_stays_immutable_and_remote_missing_is_not_rebound(monkeypatch):
    class Session:
        profile = "remote"
        workspace = "/home/remote/repo"
        messages = [{"role": "user", "content": "sent"}]
        context_messages = []
        pending_user_message = None

    session = Session()
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda *_args, **_kw: "/home/remote/other")
    with pytest.raises(ValueError, match="workspace_immutable"):
        routes._resolve_chat_workspace_with_recovery(session, "/home/remote/other")
    monkeypatch.setattr(routes, "resolve_implicit_workspace_with_recovery",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("remote path inaccessible")))
    with pytest.raises(ValueError, match="remote path inaccessible"):
        routes._resolve_chat_workspace_with_recovery(session, None)


def test_explicit_bound_path_deleted_reports_missing(monkeypatch):
    class Session:
        profile = "default"
        workspace = "/deleted"
        messages = [{"role": "user", "content": "sent"}]
        context_messages = []
        pending_user_message = None

    monkeypatch.setattr(routes, "resolve_trusted_workspace",
                        lambda *_args, **_kw: (_ for _ in ()).throw(ValueError("Path does not exist")))
    monkeypatch.setattr(routes, "resolve_implicit_workspace_with_recovery",
                        lambda *_args, **_kw: ("/home", True))
    with pytest.raises(ValueError, match="workspace_missing"):
        routes._resolve_chat_workspace_with_recovery(Session(), "/deleted")


def test_deleted_folder_wakeup_returns_distinct_error_code(monkeypatch):
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kw: None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: object())
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery",
                        lambda *_args: (_ for _ in ()).throw(ValueError("workspace_missing")))
    result = routes.start_session_turn("sid", "wake up")
    assert result["_status"] == 400
    assert result["code"] == "workspace_missing"
    assert "folder no longer exists" in result["error"]


def test_default_marker_tracks_config_and_home_cannot_link(tmp_path, monkeypatch):
    first, second = (tmp_path / name for name in ("first", "second"))
    first.mkdir()
    second.mkdir()
    rows = [{"path": str(first), "workspace_id": "first", "project_id": None},
            {"path": str(second), "workspace_id": "second", "project_id": None}]
    chosen = {"path": str(first)}
    monkeypatch.setattr(workspace_api, "_profile_default_workspace", lambda profile=None: chosen["path"])
    assert [row["is_default"] for row in routes._mark_default_workspace_rows(rows, "default")] == [True, False]
    chosen["path"] = str(second)
    assert [row["is_default"] for row in routes._mark_default_workspace_rows(rows, "default")] == [False, True]
    assert all("is_default" not in row for row in rows)
    _capture(monkeypatch)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: rows)
    denied, status = routes._handle_workspace_link_project(None, {"workspace_id": "second", "project_id": "p"})
    assert status == 400 and denied["code"] == "not_linkable"


def test_two_tab_new_session_profile_and_foreign_project(monkeypatch, tmp_path):
    from tests.test_issue5420_profile_switch_session_new import _post_session_new

    class NewSession:
        session_id = "two-tabs"
        profile = "work"
        messages = []
        project_id = None
        project_source = None

        def compact(self):
            return {"session_id": self.session_id, "profile": self.profile}

    calls = []
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profile_default_for_new_session", lambda profile: str(tmp_path))
    monkeypatch.setattr(routes, "new_session", lambda **kw: calls.append(kw) or NewSession())
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: [])
    monkeypatch.setattr(routes, "load_projects", lambda: [{"project_id": "foreign", "profile": "default"}])
    accepted = _post_session_new({"profile": "work", "worktree": False}, monkeypatch)
    assert "bad" not in accepted and calls[0]["profile"] == "work"
    rejected = _post_session_new({"profile": "work", "project_id": "foreign", "worktree": False}, monkeypatch)
    assert rejected["bad"][1] == 404
    assert len(calls) == 1


def test_inherited_deleted_workspace_uses_profile_default_not_last(monkeypatch, tmp_path):
    from types import SimpleNamespace

    deleted = str(tmp_path / "deleted")
    home = str(tmp_path / "home")
    last = str(tmp_path / "last-linked")
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kw: SimpleNamespace(workspace=deleted))
    monkeypatch.setattr(routes, "_profile_default_for_new_session", lambda profile: home)
    monkeypatch.setattr(routes, "get_last_workspace", lambda profile=None: last)
    monkeypatch.setattr(routes, "resolve_implicit_workspace_with_recovery",
                        lambda _candidate, fallback, profile=None: (fallback(profile), True))
    body = {"workspace": deleted, "workspace_inherited_from_prev_session": True}
    resolved = routes._resolve_new_session_workspace(body, "previous", profile="work")
    assert resolved == home
    monkeypatch.setattr(routes, "load_workspaces", lambda profile=None: [
        {"path": home, "project_id": None}, {"path": last, "project_id": "last-project"}
    ])
    monkeypatch.setattr(routes, "load_projects", lambda: [{"project_id": "last-project", "profile": "work"}])
    assert routes._workspace_project_for_path(resolved, "work") is None


def test_project_delete_and_workspace_add_serialize_at_write_edge(monkeypatch, tmp_path):
    from api import models

    home, old, added = (tmp_path / name for name in ("home", "old", "added"))
    for path in (home, old, added):
        path.mkdir()
    workspaces_file = tmp_path / "workspaces.json"
    projects_file = tmp_path / "projects.json"
    workspaces_file.write_text(json.dumps([
        {"path": str(home), "name": "Home", "workspace_id": "home", "project_id": None},
        {"path": str(old), "name": "Old", "workspace_id": "old", "project_id": "old-project"},
    ]))
    projects_file.write_text(json.dumps([
        {"project_id": "old-project", "name": "Old", "profile": "default", "parent_id": None}
    ]))
    monkeypatch.setattr(workspace_api, "_workspaces_file_for_profile", lambda profile=None: workspaces_file)
    monkeypatch.setattr(workspace_api, "_profile_default_workspace", lambda profile=None: str(home))
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", tmp_path / "absent-index.json")
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    _capture(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "read_body", lambda _h: {"project_id": "old-project"})
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_kw: True)
    entered = threading.Event()
    release = threading.Event()
    add_started = threading.Event()
    add_finished = threading.Event()
    actual_load_projects = models.load_projects

    def blocked_load_projects():
        if threading.current_thread().name == "project-delete":
            entered.set()
            assert release.wait(2)
        return actual_load_projects()

    monkeypatch.setattr(routes, "load_projects", blocked_load_projects)
    results = {}

    def delete_project():
        handler = MagicMock()
        handler.command = "POST"
        handler.headers = {}
        results["delete"] = routes.handle_post(handler, urlparse("/api/projects/delete"))

    def add_workspace():
        add_started.set()
        results["add"] = routes._handle_workspace_add(None, {"path": str(added), "name": "Added"})
        add_finished.set()

    deletion = threading.Thread(target=delete_project, name="project-delete")
    addition = threading.Thread(target=add_workspace, name="workspace-add")
    deletion.start()
    assert entered.wait(2)
    addition.start()
    assert add_started.wait(2)
    assert not add_finished.wait(0.05)
    release.set()
    deletion.join(2)
    addition.join(2)
    assert not deletion.is_alive() and not addition.is_alive()
    assert results["delete"][1] == 200 and results["add"][1] == 200
    final_workspaces = json.loads(workspaces_file.read_text())
    final_projects = json.loads(projects_file.read_text())
    assert next(row for row in final_workspaces if row["workspace_id"] == "old")["project_id"] is None
    new_link = next(row for row in final_workspaces if row["path"] == str(added))["project_id"]
    assert {row["project_id"] for row in final_projects} == {new_link}
