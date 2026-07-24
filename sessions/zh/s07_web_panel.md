# s07 — FastAPI 单页看板(create/transition/comment + WS realtime)

s06 让事件能实时推给 WS 订阅者了,但用户没有地方**操作**——只能命令行跑。s07 加一个 FastAPI 单页看板:浏览器打开就能看到 issue 列表、创建+派活、行内改状态、发评论(触发 follow-up task)、选中某条 issue 实时看执行进度。零构建(HTML 内联 JS,无 npm/webpack)。

## 一、核心设计思想

第一个思想:**create_app 把 store 和 hub 闭包进路由**。FastAPI 的路由函数闭包引用传入的 `issue_store` / `task_store` / `hub`——不需要全局变量,测试时传新 store 就能隔离:

```python
def create_app(issue_store, task_store, hub):
    app = FastAPI()

    @app.get("/api/issues")
    def list_issues():
        return [_issue_to_dict(i) for i in issue_store.list()]

    @app.post("/api/issues")
    def create_issue(title: str = "", assignee_type: str = "agent",
                     assignee_id: str = ""):
        issue = issue_store.create("ws-1", title or "(untitled)",
                                    assignee_type=assignee_type,
                                    assignee_id=assignee_id)
        if assignee_type == "agent" and assignee_id:
            task_store.enqueue(issue.id, assignee_type, assignee_id)
        return _issue_to_dict(issue)
    ...
    return app
```

POST /api/issues 创建 issue 后,如果指派给了 agent,立刻入队一条 queued task——issue 一出现在看板上就已经派活了。

第二个思想:**PATCH 状态转移复用 s01 状态机**。看板上行内改状态(`in_progress→in_review` 等)走的是 `IssueStore.transition()`——和 CLI 调用同一个状态机,合法转移放行,非法(如 `todo→done` 跳过 `in_progress`)返回 `{"error": ...}`:

```python
@app.patch("/api/issues/{issue_id}/status")
def update_status(issue_id: str, status: str):
    try:
        issue_store.transition(issue_id, IssueStatus(status))
    except (KeyError, IllegalTransition, ValueError) as e:
        return {"error": str(e)}
    return _issue_to_dict(issue_store.get(issue_id))
```

状态机逻辑只有一处(s01 的 `VALID_TRANSITIONS`),CLI、web 看板、daemon 驱动都走它,不会出现"web 能改但 CLI 不认"的分裂。

第三个思想:**评论入队 follow-up task**。POST /api/issues/{id}/comments 调 s05 的 `comment_and_enqueue` 逻辑——追加评论 + 为 assignee 入队新 queued task,issue 状态**不变**:

```python
@app.post("/api/issues/{issue_id}/comments")
def add_comment(issue_id: str, text: str = ""):
    issue = issue_store.comment(issue_id, text)
    task = task_store.enqueue(issue_id, issue.assignee_type, issue.assignee_id,
                              trigger_comment_id=text)
    return {"issue": _issue_to_dict(issue), "task_id": task.id}
```

评论触发的 follow-up task 和手动创建的 task 走同一条 daemon 认领→执行→标终态链路。

第四个思想:**WS 按 issue_id 订阅实时进度**。s06 的 `RealtimeHub` 按 key 推事件;s07 的 Daemon `_publish` 额外按 `issue_id` 推(除了按 `task_id`),让 WS `/ws/issues/{id}` 能按 issue 级订阅:

```python
@app.websocket("/ws/issues/{issue_id}")
async def ws_issues(websocket: WebSocket, issue_id: str):
    await websocket.accept()
    q = hub.subscribe(issue_id)
    try:
        while True:
            event = q.get(timeout=0.5)
            if event:
                await websocket.send_json(event)
    finally:
        hub.unsubscribe(issue_id, q)
```

前端 JS 连上这个 WS,选中某条 issue 后实时显示该 issue 的 task 执行进度(claim/progress/done/result 事件)。

## 二、为什么这么设计

### 为什么零构建(内联 HTML+JS)?

不引入 npm/webpack/babel 构建链——降低门槛。一个 HTML 字符串(内联 `<script>`)就够:`fetch` 调 API、`WebSocket` 连实时、`<table>` 渲染 issue 列表、`<form>` 创建、`<select>` 状态转移、`<input>` 评论。没有外部文件、没有打包步骤,`uvicorn` 直接服务。

### 为什么 PATCH 非法转移返回 error dict(200)而不是 HTTP 400?

返回 `{"error": "..."}` + HTTP 200 让前端 JS 不需要区分"业务错误"(非法转移)和"HTTP 错误"(404/500)。前端 `fetch().then(res => res.json())` 总是拿到 JSON,合法返回 issue dict、非法返回 `{"error": ...}`,JS 用 `"error" in data` 判断。如果用 HTTP 400,前端要 `res.ok` 分支处理两种 JSON 格式,复杂度更高。

### 为什么 WS 按 issue_id 而不是 task_id?

看板展示的是 issue 行;用户点击一条 issue 看进度——他关心的是"这条 issue 的执行到哪了",不是"某条具体 task 的事件"。按 issue_id 订阅,无论当前是哪条 task 在跑(首次执行、评论 follow-up、重跑),issue 级 WS 都能收到事件。Daemon `_publish` 额外推 issue_id 实现了这个映射。

## 三、看板怎么流转

```
  浏览器打开 http://localhost:8000/
        │  GET / → 返回 HTML_PAGE(内联 JS)
        │  JS fetch GET /api/issues → 渲染 issue 表
        ▼
  用户操作:
        │  创建:POST /api/issues?title=...&assignee_id=... → issue + task 入队
        │  状态:PATCH /api/issues/{id}/status?status=in_review → 状态机校验
        │  评论:POST /api/issues/{id}/comments?text=... → 评论 + follow-up task 入队
        │  watch:WebSocket /ws/issues/{id} → 实时收到 daemon 推的事件
        ▼
  daemon 后台轮询 task 队列 → 认领 → 执行 → on_event → _publish(task_id, ev, issue_id)
        │  hub.publish(task_id, ev)   → task 级订阅者
        │  hub.publish(issue_id, ev)  → issue 级 WS 订阅者(看板 watch)
```

## 试一试

```bash
python -m pytest tests/test_web_panel.py -v   # API 路由单测(TestClient,不调 claude)
python sessions/zh/s07_web_panel.py           # 启动 uvicorn(StubRunner 后台 daemon)
# 浏览器打开 http://localhost:8000/
```

启动后:看板页面列 issue、创建表单(title + agent id)、状态下拉(in_progress/in_review/done/blocked)、watch 按钮(连 WS 看实时进度)。daemon 用 StubRunner 后台轮询,自动认领 task 执行。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s07 |
|---|---|---|
| 前端 | Next.js App Router(全功能) | FastAPI 单页 HTML(内联 JS,零构建) |
| API | Go Chi router + REST | FastAPI(Python) |
| 状态转移 | DB + 应用层校验 | 复用 s01 `IssueStore.transition()` |
| 评论 | `CreateComment` → `triggerTasksForComment` 三路分流 | 简化:comment + enqueue 新 task |
| 实时 | RealtimeHub → Redis relay → WS(多节点) | RealtimeHub → WS(单节点,按 issue_id) |
| auth | JWT + 成员资格闸门 | 无(本地) |

一句话:**s07 把 multiCa「看板 + 创建 + 状态转移 + 评论 + 实时」做出来了——FastAPI 单页、零构建、复用 s01 状态机和 s06 RealtimeHub**;砍掉的是 Next.js 全功能前端、Redis 多节点 relay、JWT auth——那些是生产化主题。
