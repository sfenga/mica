from sessions.zh.s06_realtime import RealtimeHub


def test_subscribe_and_publish():
    hub = RealtimeHub()
    q = hub.subscribe("task-1")
    hub.publish("task-1", {"type": "progress", "message": "hi"})
    ev = q.get(timeout=1)
    assert ev["message"] == "hi"


def test_multiple_subscribers_broadcast():
    hub = RealtimeHub()
    q1 = hub.subscribe("task-1")
    q2 = hub.subscribe("task-1")
    hub.publish("task-1", {"type": "done"})
    assert q1.get(timeout=1)["type"] == "done"
    assert q2.get(timeout=1)["type"] == "done"


def test_late_subscriber_no_replay():
    """晚到订阅者不收历史事件(只收订阅之后的新事件)。"""
    hub = RealtimeHub()
    hub.publish("task-1", {"type": "progress", "msg": "old"})  # 先发(无人订阅)
    q = hub.subscribe("task-1")  # 后订阅
    hub.publish("task-1", {"type": "progress", "msg": "new"})
    ev = q.get(timeout=1)
    assert ev["msg"] == "new"  # 只收到订阅之后的
    assert q.empty()           # 没有历史回放


def test_unsubscribe_stops_receiving():
    hub = RealtimeHub()
    q = hub.subscribe("task-1")
    hub.unsubscribe("task-1", q)
    hub.publish("task-1", {"type": "progress"})
    assert q.empty()  # 退订后不收


def test_different_keys_isolated():
    hub = RealtimeHub()
    q1 = hub.subscribe("task-1")
    q2 = hub.subscribe("task-2")
    hub.publish("task-1", {"type": "done"})
    assert not q1.empty()
    assert q2.empty()  # task-2 不收 task-1 的事件


def test_subscriber_count():
    hub = RealtimeHub()
    assert hub.subscriber_count("task-1") == 0
    hub.subscribe("task-1")
    hub.subscribe("task-1")
    assert hub.subscriber_count("task-1") == 2
