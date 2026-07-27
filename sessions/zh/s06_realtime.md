# s06 — WebSocket 实时进度

s05 已经能把「评论」变成新的 task,并用上一条 task 的 `session_id` 续会话。到这里,后台执行闭环已经成立:

```text
issue -> task -> daemon 认领 -> runner 执行 -> SessionStore 落盘
```

但用户体验还有一个断点:daemon 正在执行时,外部看不到过程。只能等 task 结束后,再去读 `sessions/<task_id>.jsonl`。这对 coding agent 很别扭,因为真正有价值的信息往往在执行中出现:模型输出、工具调用、失败提示、完成结果。

s06 只补这一层:**把 daemon 执行中的事件实时推给订阅者**。

本章新增三个东西:

```text
RealtimeHub       纯 Python 发布/订阅中心
WS server         把 Hub 事件转成 WebSocket 消息
task watch        一个终端客户端,连 WS 实时打印事件
```

注意,本章不改变 issue/task 模型,也不改变 runner 语义。task 怎么认领、怎么落盘、怎么失败,都沿用 s05。s06 只是在 `on_event` 旁边多接一根实时推送的线。

## 1. 先看事件原来在哪里

daemon 执行 task 时,runner 会不断调用 `on_event(ev)`:

```python
def on_event(ev: dict) -> None:
    print(f"[event] {ev}")
    daemon._record(task.id, ev)
    daemon._publish(task.id, ev)
```

这里有三件事:

```text
print             给当前进程看
_record           写入 s04 的 SessionStore,作为历史
_publish          本章新增,推给实时订阅者
```

所以 s06 的位置非常明确:它不负责产生事件,事件仍由 `StubRunner` 或 `ClaudeCliRunner` 产生;它也不负责保存历史,历史仍由 `SessionStore` 保存。它只负责把已经产生的事件立刻送出去。

## 2. 整体链路

完整链路是这样:

```text
runner 产生事件
   |
   v
Daemon.on_event(ev)
   |
   |-- SessionStore.append(task_id, ev)       历史路径
   |
   `-- RealtimeHub.publish(task_id, ev)       实时路径
          |
          v
      每个订阅 task_id 的 Queue 收到 ev
          |
          v
      WebSocket handler 从 Queue 取出 ev
          |
          v
      websocket.send(json.dumps(ev))
          |
          v
      task watch <id> 实时打印
```

这里最重要的分工是:

```text
历史 = JSONL
实时 = WebSocket
```

晚到的订阅者不会收到旧事件。旧事件已经在 `sessions/<task_id>.jsonl` 里,需要历史时读文件;WebSocket 只关心连接之后的新事件。

## 3. RealtimeHub: 同步世界里的 pub/sub

`RealtimeHub` 是本章核心,但它很小:

```python
class RealtimeHub:
    def __init__(self):
        self._subs: dict[str, set] = {}
        self._lock = threading.Lock()

    def subscribe(self, key: str) -> "queue.Queue":
        q = queue.Queue()
        with self._lock:
            self._subs.setdefault(key, set()).add(q)
        return q

    def publish(self, key: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(key, set()))
        for q in subs:
            q.put(event)
```

`key` 在 s06 里就是 `task_id`。谁想看某个 task 的实时进度,就:

```python
q = hub.subscribe(task.id)
```

daemon 有新事件时:

```python
hub.publish(task.id, ev)
```

所有订阅了这个 task 的 Queue 都会收到同一份事件。

为什么用 `queue.Queue`,而不是 `asyncio.Queue`?因为 daemon 和 runner 是同步代码。`ClaudeCliRunner` 是在 `subprocess.Popen` 的 stdout 循环里一行行读事件,不是 async 函数。`queue.Queue` 可以让同步的 daemon 直接 `put`,WebSocket 那边再用 executor 去 `get`,两边不用互相改造。

## 4. 为什么晚到订阅者不回放

`RealtimeHub` 没有事件缓存。`publish` 只遍历当下已经订阅的 Queue:

```text
先 publish,后 subscribe -> 收不到旧事件
先 subscribe,后 publish -> 收到新事件
```

这是刻意设计的,原因有两个:

```text
1. s04 已经负责历史:SessionStore 会把事件写到 sessions/<task_id>.jsonl
2. Hub 只做实时:不保存、不截断、不判断历史该回放多少
```

这样概念更干净:

```text
想看正在发生什么 -> 连 WebSocket
想查已经发生什么 -> 读 JSONL
```

对应测试是 `test_late_subscriber_no_replay`。

## 5. WS server: 很薄的一层胶水

WebSocket server 不懂 issue,不懂 task 状态机,也不懂 runner。它只做四步:

```text
1. 从 URL `/watch/<task_id>` 解析 task_id
2. `hub.subscribe(task_id)` 得到 Queue
3. 循环从 Queue 取事件
4. 把事件 JSON 发给 WebSocket 客户端
```

代码形状:

```python
async def _ws_handler(websocket, hub: RealtimeHub) -> None:
    path = websocket.request.path
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "watch":
        await websocket.close(code=4000, reason="path must be /watch/<task_id>")
        return

    task_id = parts[1]
    q = hub.subscribe(task_id)
    try:
        loop = asyncio.get_running_loop()
        while True:
            event = await loop.run_in_executor(None, lambda: q.get(timeout=0.5))
            await websocket.send(json.dumps(event, ensure_ascii=False))
    finally:
        hub.unsubscribe(task_id, q)
```

`run_in_executor` 的作用是:不要在 async event loop 里直接阻塞 `q.get()`。Queue 没事件时会等待,所以放到 executor 线程里等;WebSocket handler 自己保持 async。

## 6. Daemon 接入点

Daemon 只多了一个可选参数:

```python
Daemon(..., session_store=sessions, hub=hub)
```

内部新增 `_publish`:

```python
def _publish(self, task_id: str, event: dict) -> None:
    if self.hub is not None:
        self.hub.publish(task_id, event)
```

然后在 `on_event` 里调用。这个接入方式很重要:没有 hub 时,daemon 仍然能跑;有 hub 时,同一批事件除了落盘,还会实时广播。

也就是说,实时功能是旁路增强,不是主流程依赖。

## 7. demo 怎么读

直接运行:

```bash
python sessions/zh/s06_realtime.py
```

这个 demo 不启动真正的 WebSocket server,也不依赖 `claude`。它用 `StubRunner` 演示 Hub 本身:

```text
1. 创建 issue
2. 入队 task
3. 先 `hub.subscribe(task.id)`
4. daemon.run_once()
5. 从 Queue 里取出执行期间收到的事件
```

这能证明最核心的一点:只要订阅发生在执行之前,daemon 执行中的事件会实时进入订阅者 Queue。

## 8. 终端 watch 的使用方式

代码里提供了两个函数:

```python
run_ws_server(hub, port)
watch_task(host, port, task_id)
```

实际产品化时通常是两个进程:

```text
进程 A: daemon + hub + run_ws_server(hub, port)
进程 B: watch_task("127.0.0.1", port, task_id)
```

`watch_task` 会连接:

```text
ws://<host>:<port>/watch/<task_id>
```

然后不断 `recv()` 并打印服务端推来的 JSON 事件。

本章文件没有做完整 CLI 命令分发,因为重点是教学实时链路;自动化测试也主要测 `RealtimeHub` 的语义。

## 9. 测试覆盖了什么

运行:

```bash
python -m pytest tests/test_realtime.py -v
```

测试覆盖的是 Hub 的核心承诺:

```text
subscribe 后 publish 能收到
多个订阅者都能收到
晚到订阅者不回放历史
unsubscribe 后不再收到
不同 key 互相隔离
subscriber_count 返回当前订阅数
```

这些测试不依赖网络,也不依赖 `claude`。这是故意的:实时层最需要先保证的是发布/订阅语义正确,WebSocket 只是传输外壳。

## 10. 本章边界

s06 做了:

```text
daemon 执行事件 -> RealtimeHub -> WebSocket -> watch 客户端
```

s06 不做:

```text
历史回放          交给 s04 SessionStore
Web 看板          下一章 s07
鉴权              后续 workspace/member 章节再讲
多节点广播        mica 保持单进程/单机教学模型
ping/pong 保活    生产化主题,本章先省掉
```

一句话总结:**s06 把“执行中发生了什么”从 daemon 进程里推出来。历史继续落 JSONL,实时新增 RealtimeHub + WebSocket,两条路径互不抢职责。**
