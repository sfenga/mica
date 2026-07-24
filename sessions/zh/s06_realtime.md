# s06 — WebSocket 实时进度(RealtimeHub + WS server + task watch)

s05 让评论能续聊了,但 daemon 执行 task 时用户看不到过程——只能等 task 完成后看结果。s06 加上实时推送:daemon 执行时,每个事件(claim/progress/done/result)经 `RealtimeHub` 推给所有 WebSocket 订阅者;终端 `task watch <id>` 连 WS 实时看进度。晚到的订阅者不回放历史——历史走 s04 的 `sessions/<task_id>.jsonl`。

## 一、核心设计思想

第一个思想:**RealtimeHub 是纯 Python pub/sub,不依赖 WS**。一个 `id→订阅者集合` 的字典,`publish(id, event)` 遍历当前订阅者往它们的 `queue.Queue` 里 put 事件:

```python
class RealtimeHub:
    def __init__(self):
        self._subs: dict[str, set] = {}   # id -> set of Queue
        self._lock = threading.Lock()

    def subscribe(self, key: str) -> queue.Queue:
        q = queue.Queue()
        with self._lock:
            self._subs.setdefault(key, set()).add(q)
        return q

    def publish(self, key: str, event: dict):
        with self._lock:
            subs = list(self._subs.get(key, set()))
        for q in subs:
            q.put(event)
```

用 `queue.Queue`(线程安全,标准库)而非 asyncio Queue——daemon 的 `on_event` 是同步调用(从 `subprocess.Popen` 的 stdout 循环里来的),不用 asyncio。RealtimeHub 可以在同步代码里直接调,WS server 从 Queue 里取(异步)。

第二个思想:**晚到订阅者不回放历史**。`publish` 只推给**当前**订阅者——没有缓冲区存历史。如果订阅者在事件发出后才连上,它只收到订阅之后的新事件。历史走 s04 的 `sessions/<task_id>.jsonl`(用 `SessionStore.load(task_id)` 读)。这分了两条路:实时走 WS(只推新事件),历史走 JSONL(全保留)。这样 daemon 不需要内存缓冲历史(省内存、省复杂度)。

第三个思想:**WS server 是薄胶水**。`websockets.serve` 起一个 WS server,handler 从 URL path 解析 `task_id`(`/watch/<task_id>`),`hub.subscribe(task_id)` 订阅,然后循环 `q.get` → `websocket.send(json.dumps(event))`。断开时 `unsubscribe` 清理。WS server 只负责把 hub 的事件透传给远端客户端,不做业务逻辑。

```python
async def _ws_handler(websocket, hub):
    path = websocket.request.path
    task_id = path.strip("/").split("/")[-1]
    q = hub.subscribe(task_id)
    try:
        while True:
            event = q.get(timeout=0.5)  # 超时后检查连接,避免线程泄漏
            if event:
                await websocket.send(json.dumps(event, ensure_ascii=False))
    finally:
        hub.unsubscribe(task_id, q)
```

第四个思想:**Daemon 加 hub,on_event 推**。Daemon 的 `on_event` 回调里,除了打印 + 落盘(s04),还调 `self._publish(task.id, ev)`,把事件推给 hub 的订阅者:

```python
# Daemon.execute 的 on_event
def on_event(ev: dict) -> None:
    print(f"[event] {ev}")
    self._record(task.id, ev)      # s04: 落盘 SessionStore
    self._publish(task.id, ev)      # s06: 推 RealtimeHub → WS 订阅者
```

daemon 只管执行 + 推事件;谁订阅、怎么展示(终端 print / web 看板 / 其他),由 hub 的订阅者决定。

## 二、为什么这么设计

### 为什么用 `queue.Queue` 而不是 asyncio Queue?

daemon 的 `on_event` 是从 `subprocess.Popen` 的 stdout 逐行循环里同步调的——那是个 `for line in proc.stdout` 循环,不是 async。用 `queue.Queue`(标准库,线程安全)让 `publish` 在同步代码里直接调(`q.put`),WS server 从 `q.get` 异步取。两条线程(daemon 同步推 + WS async 取)通过 Queue 解耦,不用把 daemon 改成 async。

### 为什么不缓冲历史(让晚到订阅者收到)?

缓冲历史意味着 daemon 要在内存里存每个 task 的事件,等订阅者连上再回放——内存开销 + 复杂度(存多少?何时清?)。s04 已经把事件落盘到 `sessions/<task_id>.jsonl`,晚到订阅者需要历史时读这个文件即可。实时(WS)和历史(JSONL)各走各的:WS 只推新事件(轻、快),JSONL 存全量(重放、审计、截断)。daemon 不缓冲历史,省内存、省复杂度。

### 为什么 WS server 不做业务逻辑?

WS server 只透传:从 hub 取事件 → 发给客户端。不做"哪些事件该发""事件内容怎么改"等业务判断。这样 WS server 不会成为业务逻辑耦合点——hub 是唯一的事件分发中心,WS 只是它的一种消费方式(终端客户端是另一种,直接从 Queue 取)。

## 三、实时推送怎么流转

```
  daemon 执行 task:
        │  on_event(ev) → print + _record(task_id, ev) + _publish(task_id, ev)
        ▼
  RealtimeHub.publish(task_id, ev)
        │  遍历 task_id 的所有订阅者 → q.put(ev)
        ▼
  WS server handler:
        │  q.get(ev) → websocket.send(json.dumps(ev))
        ▼
  终端 `task watch <id>`:
        │  ws.recv() → print(msg)
  ─── 晚到订阅者 ───
        │  publish 发出时无订阅者 → 事件丢失(不缓冲)
        │  订阅者连上后只收新事件
        │  历史走 SessionStore.load(task_id) 读 JSONL
```

## 试一试

```bash
python -m pytest tests/test_realtime.py -v   # RealtimeHub 单测(不依赖 WS)
python sessions/zh/s06_realtime.py           # demo:StubRunner + hub + 订阅者收到事件
```

demo 用 StubRunner:执行前先 `hub.subscribe(task_id)` 订阅,然后 `daemon.run_once()`,执行后从订阅者 Queue 里取出收到的事件(claim/progress/done/result),打印。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s06 |
|---|---|---|
| WS 库 | gorilla/websocket(`hub.go:18`) | websockets(Python asyncio) |
| 事件分发 | `Broadcaster.BroadcastToScope`(`broadcaster.go:16`)→ Hub rooms(`scopeKey{Type,ID}`)(`hub.go:204`)→ Redis relay XADD/XREADGROUP 多节点(`redis_relay.go:283,290`) | daemon on_event → RealtimeHub → WS |
| 历史回放 | 无(晚到订阅者从 Redis consumer group 当前位置开始,不回放;历史走 API 读 DB) | 无(晚到走 s04 JSONL) |
| 订阅模型 | 首帧 auth JWT/PAT(`hub.go:704`)→ auto-subscribe workspace+user(`hub.go:321`)→ subscribe 帧 task/chat scope(`hub.go:375`) | URL path `/watch/<task_id>` 隐式订阅 |
| ping/pong | pongWait=60s,pingPeriod=54s(`hub.go:195-196`) | 无 |
| 多节点扩展 | Redis stream relay(多节点 XADD/XREADGROUP) | 无(单节点) |
| scope 鉴权 | `ScopeAuthorizer.AuthorizeScope`(DB 查资源归属)(`hub.go:40`) | 无 |

一句话:**s06 把 multiCa「执行事件实时推给 WS 订阅者」做出来了——RealtimeHub 纯 pub/sub + WS server 薄胶水 + daemon on_event 推**;砍掉的是 Redis 多节点 relay、scope 鉴权、ping/pong、首帧 auth——那些是生产化主题。
