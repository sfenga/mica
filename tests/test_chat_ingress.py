"""Chat ingress engine: routing, binding, dedup, agent binding, guidance."""

from dataclasses import replace

from mica.application.chat import ChatIngressCommand
from mica.bootstrap import build_memory_container
from mica.integrations.feishu import _flatten_content, _strip_noise


def make_command(
    event_id="om-1", body="解释一下这个接口", thread_id=None, chat_type="p2p", **overrides
):
    command = ChatIngressCommand(
        channel="feishu",
        tenant_key="tenant-1",
        workspace_id="feishu:tenant-1",
        default_agent_id="local-assistant",
        external_message_id=event_id,
        external_user_id="ou-user-1",
        display_name="测试用户",
        conversation_id="oc-chat-1",
        thread_id=thread_id,
        chat_type=chat_type,
        body=body,
    )
    return replace(command, **overrides) if overrides else command


def messages_of(container, session_id):
    return container.chats.list_messages(session_id)


def test_first_message_creates_session_bound_to_default_agent():
    container = build_memory_container()

    session_id = container.chat_ingress.submit(make_command())

    session = container.chats.get_session(session_id)
    assert session.agent_id == "local-assistant"
    assert session.status == "active"
    assert session.creator_user_id.startswith("user_")
    stored = messages_of(container, session_id)
    assert [item.role for item in stored] == ["user"]
    assert stored[0].body == "解释一下这个接口"
    assert stored[0].channel_ingested is True
    # Title claims the first user message's first line.
    assert session.title == "解释一下这个接口"


def test_plain_follow_up_reuses_the_same_session():
    container = build_memory_container()
    first = container.chat_ingress.submit(make_command("om-1"))

    second = container.chat_ingress.submit(make_command("om-2", "补充：只看昨天的数据"))

    assert second == first
    assert len(messages_of(container, first)) == 2


def test_topic_messages_isolate_sessions_inside_one_group():
    container = build_memory_container()
    topic_a = container.chat_ingress.submit(
        make_command("om-a", thread_id="omt-a", chat_type="group")
    )
    topic_b = container.chat_ingress.submit(
        make_command("om-b", thread_id="omt-b", chat_type="group")
    )
    plain = container.chat_ingress.submit(make_command("om-c", chat_type="group"))

    assert len({topic_a, topic_b, plain}) == 3
    binding = container.chats.binding_for_session(topic_a)
    assert binding.thread_id == "omt-a"
    assert binding.conversation_id == "oc-chat-1"


def test_duplicate_event_never_stores_a_second_message():
    container = build_memory_container()
    session_id = container.chat_ingress.submit(make_command("om-1"))

    duplicate = container.chat_ingress.submit(make_command("om-1"))

    assert duplicate is None
    assert len(messages_of(container, session_id)) == 1
    assert container.channels.list_outbox() == []


def test_first_message_explicit_agent_binds_the_session():
    container = build_memory_container()
    container.agents.add(replace(container.agents.get("local-assistant"), id="geometry-agent"))

    session_id = container.chat_ingress.submit(
        make_command("om-1", body="画个布局", explicit_target=("agent", "geometry-agent"))
    )

    session = container.chats.get_session(session_id)
    assert session.agent_id == "geometry-agent"
    # The prefix is stripped; the transcript keeps the actual request.
    assert messages_of(container, session_id)[0].body == "画个布局"
    assert container.channels.list_outbox() == []


def test_first_message_invalid_agent_fails_closed_with_guidance():
    container = build_memory_container()

    result = container.chat_ingress.submit(
        make_command("om-1", explicit_target=("agent", "not-exist"))
    )

    assert result is None
    assert container.chats.sessions_with_unowned_messages() == []
    guidance = container.channels.list_outbox()
    assert len(guidance) == 1
    assert "未找到 Agent：not-exist" in guidance[0].payload["text"]
    assert guidance[0].payload["reply_to"] == "om-1"


def test_first_message_squad_guides_to_issue_without_creating_a_session():
    container = build_memory_container()

    result = container.chat_ingress.submit(
        make_command("om-1", explicit_target=("squad", "layout-squad"))
    )

    assert result is None
    assert container.chats.sessions_with_unowned_messages() == []
    guidance = container.channels.list_outbox()
    assert len(guidance) == 1
    assert "/issue squad=layout-squad" in guidance[0].payload["text"]


def test_existing_session_explicit_agent_strips_runs_and_guides():
    container = build_memory_container()
    container.agents.add(replace(container.agents.get("local-assistant"), id="geometry-agent"))
    session_id = container.chat_ingress.submit(make_command("om-1"))

    returned = container.chat_ingress.submit(
        make_command("om-2", body="帮我看看", explicit_target=("agent", "geometry-agent"))
    )

    assert returned == session_id
    session = container.chats.get_session(session_id)
    # The session binding never drifts.
    assert session.agent_id == "local-assistant"
    assert messages_of(container, session_id)[-1].body == "帮我看看"
    guidance = container.channels.list_outbox()
    assert len(guidance) == 1
    assert "已绑定 Agent：local-assistant" in guidance[0].payload["text"]


def test_group_creator_is_the_system_user_not_the_sender():
    container = build_memory_container()

    session_id = container.chat_ingress.submit(make_command("om-1", chat_type="group"))

    session = container.chats.get_session(session_id)
    assert session.creator_user_id.startswith("user_")
    dm_command = make_command("om-2", conversation_id="oc-dm-1", chat_type="p2p")
    dm_session_id = container.chat_ingress.submit(dm_command)
    dm_session = container.chats.get_session(dm_session_id)
    # Same tenant, different conversation shape: the group creator is the
    # shared system user, the p2p creator is the resolved sender.
    assert dm_session.creator_user_id != session.creator_user_id


def test_adapter_flattens_post_and_placeholder_media():
    post_message = type(
        "Message",
        (),
        {
            "message_type": "post",
            "content": __import__("json").dumps(
                {
                    "title": "周报",
                    "content": [
                        [
                            {"tag": "text", "text": "第一段"},
                            {"tag": "a", "text": "链接", "href": "https://x"},
                        ],
                        [{"tag": "img", "image_key": "img_1"}],
                    ],
                }
            ),
        },
    )()
    flattened = _flatten_content(post_message)
    assert flattened.splitlines()[0] == "周报"
    assert "第一段链接 (https://x)" in flattened
    assert "[图片]" in flattened

    image_message = type(
        "Message", (), {"message_type": "image", "content": '{"image_key": "img_2"}'}
    )()
    assert _flatten_content(image_message) == "[图片]"

    sticker = type("Message", (), {"message_type": "sticker", "content": "{}"})()
    assert _flatten_content(sticker) is None
    assert _strip_noise("@_user_1 mica 你好") == "你好"
