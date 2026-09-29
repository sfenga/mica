from pathlib import Path

from mica.domain import Agent, AssigneeType, Issue, Task
from mica.runtime.providers import PiProvider


def test_pi_provider_streams_text_and_reuses_session(tmp_path: Path):
    executable = tmp_path / "pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'message_update', "
        "'assistantMessageEvent': {'delta': 'done'}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    session = tmp_path / "session.jsonl"
    agent = Agent(
        id="pi-agent",
        name="Pi Agent",
        runtime_id="local",
        provider="pi",
        provider_config={"executable": str(executable)},
    )
    issue = Issue(
        title="Inspect service",
        body="Find the failure",
        workspace_id="local",
        assignee_type=AssigneeType.AGENT,
        assignee_id=agent.id,
    )
    task = Task(
        issue_id=issue.id,
        agent_id=agent.id,
        runtime_id="local",
        trigger_type="issue.created",
        trigger_id=issue.id,
    )
    events = []

    result = PiProvider().run(
        agent,
        issue,
        task,
        lambda event_type, payload: events.append((event_type, payload)),
        workdir=str(tmp_path),
        resume_session_id=str(session),
    )

    assert result.output == "done"
    assert result.session_id == str(session)
    assert events == [("agent.text", {"text": "done"})]
    assert session.exists()
