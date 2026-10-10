import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mica.api import create_app
from mica.application.chat import ChatIngressCommand
from mica.application.web_chat import WebChatService
from mica.bootstrap import build_memory_container, build_postgres_container
from mica.domain import TaskStatus


@pytest.fixture(params=["memory", "postgres"])
def container(request, monkeypatch, tmp_path):
    monkeypatch.setenv("MICA_EMBEDDED_DAEMON", "0")
    monkeypatch.setenv("MICA_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    if request.param == "postgres":
        if not os.getenv("MICA_TEST_DATABASE_URL"):
            pytest.skip("requires disposable PostgreSQL")
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        dsn = os.environ["MICA_TEST_DATABASE_URL"]
        schema = "web_chat_" + uuid4().hex
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield build_postgres_container(
                make_conninfo(dsn, options=f"-c search_path={schema}"), default_provider="stub"
            )
        finally:
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    else:
        yield build_memory_container()


def create(client, body="你好", **extra):
    return client.post(
        "/api/chat/sessions",
        json={"body": body, "request_id": uuid4().hex, "agent_id": "local-assistant", **extra},
    )


def test_web_chat_lifecycle_and_idempotency(container):
    client = TestClient(create_app(container))
    payload = {"body": "分析一个问题", "agent_id": "local-assistant", "request_id": uuid4().hex}
    response = client.post("/api/chat/sessions", json=payload)
    assert response.status_code == 201, response.text
    result = response.json()
    session_id = result["session"]["id"]
    task_id = result["task"]["id"]
    url = f"/api/chat/sessions/{session_id}"
    repeated = client.post("/api/chat/sessions", json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["task"]["id"] == task_id
    assert client.post("/api/chat/sessions", json={**payload, "body": "changed"}).status_code == 409
    assert client.patch(url, json={"archived": True}).status_code == 409
    assert client.delete(url).status_code == 409
    assert container.daemon.run_once()
    messages = client.get(url + "/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["external_message_id"] is None
    session = client.get(url).json()
    assert session["unread_count"] == 1
    assert client.post(url + "/read", json={"message_id": messages[-1]["id"]}).status_code == 204
    assert client.get(url).json()["unread_count"] == 0
    assert client.get(url, headers={"X-Mica-User": "another-user"}).json()["unread_count"] == 1
    assert client.patch(url, json={"pinned": True, "title": "新标题"}).json()["pinned"] is True
    assert client.patch(url, json={"title": "   "}).status_code == 422
    assert client.patch(url, json={"archived": True}).status_code == 200
    assert (
        client.post(url + "/messages", json={"body": "hi", "request_id": uuid4().hex}).status_code
        == 409
    )
    assert client.patch(url, json={"archived": False}).status_code == 200
    assert client.patch(url, json={"archived": True}).status_code == 200
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert container.tasks.get(task_id) is None
    assert container.events.list(task_id) == []


def test_queue_serial_execution_and_future_prompt_isolation(container):
    service = WebChatService(container)
    first = service.send("FIRST_SENTINEL", uuid4().hex, "local-user", agent_id="local-assistant")
    session_id = first["session"]["id"]
    second = service.send("FUTURE_SENTINEL", uuid4().hex, "local-user", session_id=session_id)
    assert len(service.tasks(session_id)) == 2
    # Stub echoes the prompt, making contamination observable in persisted output.
    assert container.daemon.run_once()
    output = container.tasks.get(first["task"]["id"]).output
    assert "FUTURE_SENTINEL" not in output
    assert container.tasks.get(second["task"]["id"]).status == TaskStatus.QUEUED
    container.task_service.request_cancel(second["task"]["id"])
    assert container.tasks.get(second["task"]["id"]).status == TaskStatus.CANCELLED


def test_web_continuation_does_not_reply_to_feishu(container):
    command = ChatIngressCommand(
        channel="feishu",
        tenant_key="test",
        workspace_id="local",
        default_agent_id="local-assistant",
        external_message_id=uuid4().hex,
        external_user_id="ou-test",
        display_name="Test",
        conversation_id=uuid4().hex,
        thread_id=None,
        chat_type="p2p",
        body="飞书消息",
    )
    session_id = container.chat_ingress.submit(command)
    service = WebChatService(container)
    result = service.send("网页消息", uuid4().hex, "local-user", session_id=session_id)
    task_id = result["task"]["id"]
    assert len(service.tasks(session_id)) == 2
    assert service.tasks(session_id)[0].id != task_id
    assert all(not m.channel_ingested for m in container.chats.input_messages(task_id))
    container.daemon.run_once()
    container.daemon.run_once()
    container.channel_worker.projector.run_once()
    events = container.events.list(task_id)
    for event in events:
        # Projecting Web terminal events explicitly must produce no outbound message.
        assert container.channel_worker.projector._project(event) == 0
    container.chat_batcher.drain()


def test_history_pagination_and_workspace_boundary(container):
    client = TestClient(create_app(container))
    result = create(client).json()
    url = f"/api/chat/sessions/{result['session']['id']}"
    for i in range(5):
        client.post(url + "/messages", json={"body": str(i), "request_id": uuid4().hex})
    page = client.get(url + "/messages?limit=2").json()
    assert len(page["messages"]) == 2 and page["has_more"]
    older = client.get(url + "/messages", params={"limit": 2, "before": page["next_cursor"]}).json()
    assert not {m["id"] for m in page["messages"]} & {m["id"] for m in older["messages"]}
    assert client.get(url + "?workspace_id=other").status_code == 404
    assert client.get(url + "/messages?limit=101").status_code == 422
    assert create(client, body="  ").status_code == 422


def test_message_and_task_roll_back_together(container, monkeypatch):
    service = WebChatService(container)
    before = len(container.chats.list_sessions("local"))

    def fail(*args):
        raise RuntimeError("simulated insert failure")

    monkeypatch.setattr(container.tasks, "add", fail)
    with pytest.raises(RuntimeError):
        service.send("atomic", uuid4().hex, "local-user", agent_id="local-assistant")
    assert len(container.chats.list_sessions("local")) == before


def test_concurrent_retries_create_one_turn(container):
    from concurrent.futures import ThreadPoolExecutor

    service = WebChatService(container)
    request_id = uuid4().hex
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: service.send(
                    "concurrent", request_id, "local-user", agent_id="local-assistant"
                ),
                range(4),
            )
        )
    assert len({r["session"]["id"] for r in results}) == 1
    assert len({r["task"]["id"] for r in results}) == 1
    assert sum(r["created"] for r in results) == 1


def test_cancelled_queue_is_not_part_of_future_context(container):
    service = WebChatService(container)
    first = service.send(
        "CANCELLED_SENTINEL", uuid4().hex, "local-user", agent_id="local-assistant"
    )
    container.task_service.request_cancel(first["task"]["id"])
    service.update(first["session"]["id"], "local", title="context")
    second = service.send("next", uuid4().hex, "local-user", session_id=first["session"]["id"])
    container.daemon.run_once()
    assert "CANCELLED_SENTINEL" not in container.tasks.get(second["task"]["id"]).output


def test_delete_rolls_back_and_cleans_owned_workspace(container, monkeypatch):
    service = WebChatService(container)
    result = service.send("cleanup", uuid4().hex, "local-user", agent_id="local-assistant")
    session_id = result["session"]["id"]
    task_id = result["task"]["id"]
    container.daemon.run_once()
    service.update(session_id, "local", archived=True)
    original = container.chats.delete_session

    def fail(*args):
        raise RuntimeError("simulated deletion failure")

    monkeypatch.setattr(container.chats, "delete_session", fail)
    with pytest.raises(RuntimeError):
        service.delete(session_id, "local")
    assert container.tasks.get(task_id) is not None
    assert container.events.list(task_id)
    assert len(container.chats.list_messages(session_id)) == 2
    monkeypatch.setattr(container.chats, "delete_session", original)
    from pathlib import Path

    directory = Path(container.workspaces.prepare_chat(session_id))
    service.delete(session_id, "local")
    assert not directory.exists()
