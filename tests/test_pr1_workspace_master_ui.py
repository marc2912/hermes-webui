"""Client wire and interaction guards for workspace-owned projects."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_new_chat_only_forwards_explicit_project():
    js = source("static/sessions.js")
    fn = js[js.index("async function newSession("):js.index("// Forward a pre-session toolset", js.index("async function newSession("))]
    assert "reqBody.project_id=options.project_id" in fn
    assert "reqBody.project_id=_activeProject" not in fn
    assert "const inheritWs=(options&&options.workspace)||switchWs||sessionWs||(S._profileDefaultWorkspace||null)" in fn


def test_switch_boundary_and_server_project_refresh():
    js = source("static/panels.js")
    fn = js[js.index("async function switchToWorkspace("):js.index("// ── Profile panel", js.index("async function switchToWorkspace("))]
    assert "_newChatOnWorkspaceSwitch" not in fn
    assert "S.messages.length>0" in fn
    assert "S.session.project_id=updated.session.project_id" in fn
    assert "S.session.project_source=updated.session.project_source" in fn


def test_add_form_posts_project_controls_and_setting_removed():
    js = source("static/panels.js")
    assert "project_name:projectName" in js
    assert "link_project:linkProject" in js
    assert "color:projectColor" in js
    assert "settingsNewChatOnWorkspaceSwitch" not in js
    assert "settingsNewChatOnWorkspaceSwitch" not in source("static/index.html")
    assert '"new_chat_on_workspace_switch"' not in source("api/config.py")
