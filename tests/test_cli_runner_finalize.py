import io
import sessions.zh.s03_claude_runner as m
from sessions.zh.s03_claude_runner import ClaudeCliRunner, Issue, Task


class _FakeProc:
    def __init__(self, stdout_lines, stderr="", returncode=0):
        self.stdout = io.StringIO("".join(stdout_lines))
        self.stderr = io.StringIO(stderr)
        self._rc = returncode
    def wait(self):
        return self._rc


def _patch(monkeypatch, proc, capture=None):
    def fake(cmd, **k):
        if capture is not None:
            capture.append(cmd)
        return proc
    monkeypatch.setattr(m.subprocess, "Popen", fake)


def _issue():
    return Issue(id="i1", workspace_id="ws", title="the-original-task")


def _task():
    return Task(id="t1", issue_id="i1", assignee_type="agent", assignee_id="a1")


def test_no_result_event_marks_failed(monkeypatch):
    _patch(monkeypatch, _FakeProc([
        '{"type":"system","subtype":"init","session_id":"ses-1"}\n',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
    ]))
    res = ClaudeCliRunner().run(_task(), _issue(), [], lambda e: None)
    assert res["status"] == "failed"
    assert res["session_id"] == "ses-1"   # system init 的 session_id 仍捕获
    assert "without result" in res["output"]


def test_result_error_marks_failed(monkeypatch):
    _patch(monkeypatch, _FakeProc([
        '{"type":"result","subtype":"error","result":"boom","is_error":true,"session_id":"ses-2"}\n',
    ]))
    res = ClaudeCliRunner().run(_task(), _issue(), [], lambda e: None)
    assert res["status"] == "failed" and res["output"] == "boom" and res["session_id"] == "ses-2"


def test_result_success_exit_zero_done(monkeypatch):
    captured = []
    _patch(monkeypatch, _FakeProc([
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        '{"type":"result","subtype":"success","result":"hi","is_error":false,"session_id":"ses-3"}\n',
    ]))
    res = ClaudeCliRunner().run(_task(), _issue(), [], captured.append)
    assert res["status"] == "done" and res["output"] == "hi" and res["session_id"] == "ses-3"
    assert any(e.get("kind") == "text" for e in captured)


def test_result_success_but_exit_nonzero_failed(monkeypatch):
    _patch(monkeypatch, _FakeProc([
        '{"type":"result","subtype":"success","result":"ok","is_error":false,"session_id":"ses-4"}\n',
    ], returncode=2))
    res = ClaudeCliRunner().run(_task(), _issue(), [], lambda e: None)
    assert res["status"] == "failed" and res["session_id"] == "ses-4"


def test_cli_not_found(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError(2, "no")
    monkeypatch.setattr(m.subprocess, "Popen", boom)
    res = ClaudeCliRunner(cli="nonexistent-cli-xyz").run(_task(), _issue(), [], lambda e: None)
    assert res["status"] == "failed" and "not found" in res["output"] and res["session_id"] == ""


def test_no_resume_when_none(monkeypatch):
    argv = []
    _patch(monkeypatch, _FakeProc([
        '{"type":"result","subtype":"success","result":"ok","is_error":false,"session_id":"ses-5"}\n',
    ]), capture=argv)
    ClaudeCliRunner().run(_task(), _issue(), [], lambda e: None)
    assert "--resume" not in argv[0]
    assert "--no-session-persistence" not in argv[0]
