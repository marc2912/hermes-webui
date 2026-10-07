"""PR1 storage contracts; all files here are disposable pytest fixtures."""
import json
import threading
from pathlib import Path

from api import models, workspace


def test_workspace_migration_preserves_fields_and_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / 'workspaces.json'
    missing = str(tmp_path / 'missing')
    path.write_text(json.dumps([{'path': missing, 'name': 'Legacy', 'worktree': {'branch': 'x'}, 'future': 7}]))
    monkeypatch.setattr(workspace, '_workspaces_file_for_profile', lambda profile=None: path)
    first = workspace.load_workspaces(profile='default')
    assert first[0]['workspace_id']
    assert first[0]['project_id'] is None
    assert first[0]['worktree'] == {'branch': 'x'}
    assert first[0]['future'] == 7
    persisted = path.read_bytes()
    assert workspace.load_workspaces(profile='default') == first
    assert path.read_bytes() == persisted


def test_default_home_stable_and_unlinked(tmp_path, monkeypatch):
    path = tmp_path / 'workspaces.json'
    monkeypatch.setattr(workspace, '_workspaces_file_for_profile', lambda profile=None: path)
    monkeypatch.setattr(workspace, '_profile_default_workspace', lambda profile=None: str(tmp_path))
    first = workspace.load_workspaces(profile='default')
    assert first[0]['name'] == 'Home'
    assert first[0]['project_id'] is None
    assert workspace.load_workspaces(profile='default') == first


def test_workspace_migration_write_failure_does_not_expose_new_id(tmp_path, monkeypatch):
    path = tmp_path / 'workspaces.json'
    path.write_text(json.dumps([{'path': str(tmp_path / 'repo'), 'name': 'Repo'}]))
    monkeypatch.setattr(workspace, '_workspaces_file_for_profile', lambda profile=None: path)
    monkeypatch.setattr(workspace, 'save_workspaces', lambda *_args, **_kw: (_ for _ in ()).throw(OSError('write denied')))
    import pytest
    with pytest.raises(OSError, match='write denied'):
        workspace.load_workspaces(profile='default')
    assert 'workspace_id' not in json.loads(path.read_text())[0]


def test_default_record_write_failure_does_not_return_random_id(tmp_path, monkeypatch):
    path = tmp_path / 'workspaces.json'
    monkeypatch.setattr(workspace, '_workspaces_file_for_profile', lambda profile=None: path)
    monkeypatch.setattr(workspace, '_GLOBAL_WS_FILE', tmp_path / 'absent-global-workspaces.json')
    monkeypatch.setattr(workspace, '_profile_default_workspace', lambda profile=None: str(tmp_path))
    monkeypatch.setattr(workspace, 'save_workspaces', lambda *_args, **_kw: (_ for _ in ()).throw(OSError('write denied')))
    import pytest
    with pytest.raises(OSError, match='write denied'):
        workspace.load_workspaces(profile='default')
    assert not path.exists()


def test_project_write_serializes_workspace_write(tmp_path, monkeypatch):
    project_file = tmp_path / 'projects.json'
    workspace_file = tmp_path / 'workspaces.json'
    entered_project_write = threading.Event()
    release_project_write = threading.Event()
    workspace_file_lookup = threading.Event()
    second_started = threading.Event()

    class BlockingProjectFile:
        def write_text(self, content, encoding):
            entered_project_write.set()
            assert release_project_write.wait(2)
            return project_file.write_text(content, encoding=encoding)

    monkeypatch.setattr(models, 'PROJECTS_FILE', BlockingProjectFile())

    def workspace_path(_profile=None):
        workspace_file_lookup.set()
        return workspace_file

    monkeypatch.setattr(workspace, '_workspaces_file_for_profile', workspace_path)
    project_thread = threading.Thread(target=lambda: models.save_projects([{'project_id': 'p', 'parent_id': None}]))
    project_thread.start()
    assert entered_project_write.wait(2)

    def write_workspace():
        second_started.set()
        workspace.save_workspaces([{'path': str(tmp_path), 'workspace_id': 'w', 'project_id': None}])

    workspace_thread = threading.Thread(target=write_workspace)
    workspace_thread.start()
    assert second_started.wait(2)
    assert not workspace_file_lookup.wait(0.05)
    release_project_write.set()
    project_thread.join(2)
    workspace_thread.join(2)
    assert not project_thread.is_alive() and not workspace_thread.is_alive()
    assert json.loads(project_file.read_text())[0]['project_id'] == 'p'
    assert json.loads(workspace_file.read_text())[0]['workspace_id'] == 'w'


def test_remote_target_path_is_preserved_with_unknown_fields(monkeypatch):
    monkeypatch.setattr(workspace, '_remote_terminal_workspace_candidate',
                        lambda path, profile=None: Path(path) if profile == 'remote' else None)
    record = {'path': '/home/remote/project', 'name': 'remote', 'future': {'enabled': True}}
    cleaned = workspace._clean_workspace_list([record], profile='remote')
    assert cleaned[0]['path'] == record['path']
    assert cleaned[0]['future'] == record['future']


def test_project_parent_migration_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / 'projects.json'
    path.write_text(json.dumps([{'project_id': 'a', 'name': 'A', 'profile': 'default'}]))
    monkeypatch.setattr(models, 'PROJECTS_FILE', path)
    monkeypatch.setattr(models, '_projects_migrated', False)
    assert models.load_projects()[0]['parent_id'] is None
    persisted = path.read_bytes()
    assert models.load_projects()[0]['parent_id'] is None
    assert path.read_bytes() == persisted


def test_parent_migration_does_not_rearm_profile_backfill(tmp_path, monkeypatch):
    path = tmp_path / 'projects.json'
    path.write_text(json.dumps([{'project_id': 'new', 'name': 'New', 'profile': 'work'}]))
    monkeypatch.setattr(models, 'PROJECTS_FILE', path)
    monkeypatch.setattr(models, '_projects_migrated', True)
    monkeypatch.setattr(models, '_backfill_project_profiles_if_needed',
                        lambda _rows: (_ for _ in ()).throw(AssertionError('profile backfill rearmed')))
    assert models.load_projects()[0]['parent_id'] is None
    assert json.loads(path.read_text())[0]['parent_id'] is None


def test_session_source_legacy_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(models, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', tmp_path / '_index.json')
    legacy = models.Session(session_id='legacy', workspace=str(tmp_path), project_id='p')
    assert legacy.project_source == 'explicit'
    legacy.save()
    assert json.loads(legacy.path.read_text())['project_source'] == 'explicit'
    assert models.Session.load('legacy').project_source == 'explicit'
    assert models.Session.load_metadata_only('legacy').project_source == 'explicit'
    assert models.Session.load('legacy').compact()['project_source'] == 'explicit'
    derived = models.Session(session_id='derived', workspace=str(tmp_path), project_id='p', project_source='derived')
    derived.save()
    assert models.Session.load('derived').project_source == 'derived'
    derived.workspace_recovered_from = str(tmp_path / 'deleted')
    derived.save()
    assert models.Session.load('derived').workspace_recovered_from == str(tmp_path / 'deleted')
    assert models.Session.load_metadata_only('derived').workspace_recovered_from == str(tmp_path / 'deleted')
    assert models.Session.load('derived').compact()['workspace_recovered_from'] == str(tmp_path / 'deleted')


def test_new_session_omitted_workspace_uses_profile_default(monkeypatch, tmp_path):
    monkeypatch.setattr(models, '_profile_default_workspace', lambda profile=None: str(tmp_path / profile))
    monkeypatch.setattr(models, 'get_last_workspace', lambda profile=None: str(tmp_path / 'last'))
    monkeypatch.setattr(models, '_profile_default_model_state', lambda profile: ('model', None))
    session = models.new_session(profile='alice')
    assert session.workspace == str(tmp_path / 'alice')
    assert session.project_source is None
