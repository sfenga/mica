import json
from sessions.zh.s03_claude_runner import parse_stream_line


def line(d):
    return json.dumps(d)


def test_malformed_line_returns_empty():
    events, term = parse_stream_line("not json")
    assert events == [] and term is None


def test_blank_line_returns_empty():
    events, term = parse_stream_line("   ")
    assert events == [] and term is None


def test_system_init_emits_progress():
    events, term = parse_stream_line(line({"type": "system", "subtype": "init", "session_id": "ses-1"}))
    assert term is None
    assert events == [{"kind": "system", "session_id": "ses-1"}]


def test_system_thinking_tokens_dropped():
    events, term = parse_stream_line(line({"type": "system", "subtype": "thinking_tokens",
                                            "estimated_tokens": 12, "estimated_tokens_delta": 3}))
    assert events == [] and term is None


def test_assistant_text_and_tool_use_forwarded():
    ev_line = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "c1", "name": "Bash", "input": {}},
    ]}}
    events, term = parse_stream_line(line(ev_line))
    assert term is None
    assert {"kind": "text", "text": "hello"} in events
    assert {"kind": "tool_use", "tool": "Bash"} in events


def test_user_tool_result_forwarded():
    ev_line = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": [], "is_error": False},
    ]}}
    events, term = parse_stream_line(line(ev_line))
    assert term is None
    assert events == [{"kind": "tool_result", "tool_use_id": "c1", "is_error": False}]


def test_result_success_terminal_done_with_session_id():
    _, term = parse_stream_line(line({"type": "result", "subtype": "success",
                                      "result": "ok", "is_error": False, "session_id": "ses-1"}))
    assert term == {"status": "done", "output": "ok", "is_error": False, "session_id": "ses-1"}


def test_result_error_terminal_failed():
    _, term = parse_stream_line(line({"type": "result", "subtype": "error",
                                      "result": "boom", "is_error": True, "session_id": "ses-2"}))
    assert term["status"] == "failed" and term["is_error"] is True and term["session_id"] == "ses-2"


def test_control_request_ignored():
    events, term = parse_stream_line(line({"type": "control_request", "request": {}}))
    assert events == [] and term is None
