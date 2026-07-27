# s07 — FastAPI 单页看板

s06 已经能把 daemon 执行中的事件实时推给 WebSocket 订阅者。但到这里,用户仍然缺一个“操作面”:

```text
怎么创建 issue?
怎么把 issue 派给 agent?
怎么手动推进状态?
怎么发表评论触发 follow-up task?
怎么看某条 issue 的实时进度?
```

s07 给 mica 加一个最小 Web 看板。它不是完整前端工程,而是一个 FastAPI 服务返回单页 HTML:

```text
GET /        返回 HTML_PAGE,里面内联 JS
API 路由     创建 / 列表 / 状态转移 / 评论
WS 路由      按 issue_id 订阅实时事件
```

本章的重点不是 UI 漂亮,而是把前面六章的能力接到浏览器里。

## 1. s07 新增了什么

s07 在 s06 的基础上只新增 Web 层:

```text
create_app(...)        构造 FastAPI app
HTML_PAGE              单页 HTML + 内联 JavaScript
GET /api/issues        列 issue
POST /api/issues       创建 issue,必要时入队 task
PATCH /api/issues/{id}/status
                       走状态机改状态
POST /api/issues/{id}/comments
                       评论并入队 follow-up task
WS /ws/issues/{id}     按 issue 订阅实时事件
```

它没有引入数据库、前端构建、认证或多 workspace 权限。stores 仍然是前几章的 `IssueStore` / `TaskStore` / `SessionStore`。

## 2. create_app: 把 stores 闭包进路由

FastAPI app 由函数创建:

```python
def create_app(issue_store: IssueStore, task_store: TaskStore, hub: RealtimeHub) -> FastAPI:
    app = FastAPI()
    ...
    return app
```

路由函数直接使用传入的 store 和 hub:

```python
@app.get("/api/issues")
def list_issues():
    return [_issue_to_dict(i) for i in issue_store.list()]
```

这种写法有两个好处:

```text
main() 里传真实 store,启动服务
测试里传空 store,每个 TestClient 互相隔离
```

所以 `tests/test_web_panel.py` 不需要启动真实端口,也不需要跑 daemon。

## 3. GET /: 零构建单页

首页路由直接返回一个字符串:

```python
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE
```

`HTML_PAGE` 里内联了 JavaScript。页面做四件事:

```text
refresh()       fetch('/api/issues'),渲染表格
createForm      POST /api/issues
transition()    PATCH /api/issues/{id}/status
watch()         new WebSocket('/ws/issues/' + id)
```

为什么零构建?因为 s07 是教学章节。引入 npm、打包器、组件库会把重点从“后端机制怎么接到页面”转移到前端工程配置。一个 HTML 字符串足够展示闭环。

## 4. 创建 issue 时顺手派活

创建接口:

```python
@app.post("/api/issues")
def create_issue(title: str = "", assignee_type: str = "agent", assignee_id: str = ""):
    issue = issue_store.create("ws-1", title or "(untitled)",
                               assignee_type=assignee_type, assignee_id=assignee_id)
    if assignee_type == "agent" and assignee_id:
        task_store.enqueue(issue.id, assignee_type, assignee_id)
    return _issue_to_dict(issue)
```

这里有一个重要连接:

```text
创建 issue
如果 assignee 是 agent
立刻 enqueue 一条 queued task
```

这样浏览器里创建一张派给 `agent-1` 的卡,后台 daemon 就能在下一轮 poll 认领它。

如果 assignee 不是 agent,或者没有 assignee_id,就只创建 issue,不入队 task。这个规则延续 s01/s02 的多态 assignee。

## 5. 状态修改复用 s01 状态机

状态接口:

```python
@app.patch("/api/issues/{issue_id}/status")
def update_status(issue_id: str, status: str):
    try:
        issue_store.transition(issue_id, IssueStatus(status))
    except (KeyError, IllegalTransition, ValueError) as e:
        return {"error": str(e)}
    return _issue_to_dict(issue_store.get(issue_id))
```

Web 没有重新实现状态规则。它仍然调用 s01 的:

```text
IssueStore.transition()
VALID_TRANSITIONS
```

所以:

```text
in_progress -> in_review  合法
in_review -> done         合法
todo -> done              非法,返回 {"error": "..."}
```

当前实现对非法转移返回 `200 + {"error": ...}`。这不是生产 API 风格,但让本章前端保持简单:所有响应都按 JSON 解析,合法时是 issue dict,非法时是 error dict。

## 6. 评论接口接 s05

评论接口:

```python
@app.post("/api/issues/{issue_id}/comments")
def add_comment(issue_id: str, text: str = ""):
    issue = issue_store.comment(issue_id, text)
    task = task_store.enqueue(issue_id, issue.assignee_type, issue.assignee_id,
                              trigger_comment_id=text)
    return {"issue": _issue_to_dict(issue), "task_id": task.id}
```

它做两件事:

```text
IssueStore.comment(...)      追加评论,不改 issue 状态
TaskStore.enqueue(...)       新建 follow-up task
```

这就是 s05 的模型搬到 Web API 上:

```text
评论不是 reopen issue
评论是制造下一次 agent 执行
```

当前代码没有检查 assignee 是否为空或是否 agent;它按 issue 当前 assignee 入队。这是教学版的简化。生产系统会做更复杂的评论路由、权限和触发目标解析。

## 7. 为什么 WS 按 issue_id 订阅

s06 的终端 watch 是按 task_id 看事件。但看板用户点击的是 issue 行。

所以 s07 让 daemon 同时按两个 key 发布:

```text
task_id    给 task watch 用
issue_id   给 Web 看板用
```

s07 的 `Daemon._publish` 支持 `issue_id`:

```python
def _publish(self, task_id: str, event: dict, issue_id: str | None = None) -> None:
    if self.hub is not None:
        self.hub.publish(task_id, event)
        if issue_id:
            self.hub.publish(issue_id, event)
```

`on_event` 里会传入当前 task 的 issue:

```python
daemon._publish(task.id, ev, issue_id=task.issue_id)
```

然后 WebSocket 路由订阅 issue:

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

这样同一条 issue 的首次执行、评论 follow-up、重跑 task,都能被页面的 issue 级 watch 收到。

## 8. main() 启动了什么

直接运行:

```bash
python sessions/zh/s07_web_panel.py
```

`main()` 会做这些事:

```text
创建 data_dir
创建 IssueStore / TaskStore / SessionStore
创建 RealtimeHub
创建 agent-1
创建 Daemon(..., StubRunner(), hub=hub)
后台线程启动 daemon.loop()
create_app(...)
uvicorn.run(..., port=8000)
```

所以浏览器里创建 issue 后,后台 daemon 会用 `StubRunner` 自动认领执行。这个 demo 不依赖真实 `claude`。

访问:

```text
http://localhost:8000/
```

页面会显示:

```text
issue 表格
创建表单
状态下拉
watch 按钮
progress 文本区域
```

## 9. 整体链路

```text
浏览器打开 /
   |
   v
HTML_PAGE 内联 JS
   |
   |-- GET /api/issues -> 渲染列表
   |
   |-- POST /api/issues
   |      -> IssueStore.create()
   |      -> 如果 assignee 是 agent,TaskStore.enqueue()
   |
   |-- PATCH /api/issues/{id}/status
   |      -> IssueStore.transition()
   |
   |-- POST /api/issues/{id}/comments
   |      -> IssueStore.comment()
   |      -> TaskStore.enqueue(trigger_comment_id=text)
   |
   `-- WS /ws/issues/{id}
          -> hub.subscribe(issue_id)
          -> 收 daemon 发布的事件

后台 daemon.loop()
   |
   v
claim queued task -> StubRunner.run() -> on_event -> hub.publish(task_id + issue_id)
```

## 10. 测试覆盖了什么

运行:

```bash
python -m pytest tests/test_web_panel.py -v
```

测试覆盖:

```text
GET /api/issues 空列表
POST /api/issues 创建 issue 并入队 task
PATCH 合法状态转移
PATCH 非法状态转移返回 error
POST /comments 追加评论并创建 task
GET / 返回 HTML
```

这些测试用 FastAPI `TestClient`,不启动真实 uvicorn,不调 `claude`,也不需要浏览器。

## 11. 本章边界

s07 做了:

```text
FastAPI app
单页 HTML
issue 列表
创建 issue 并派活
状态转移 API
评论 follow-up API
issue 级 WebSocket 实时订阅
StubRunner 后台 daemon demo
```

s07 不做:

```text
前端构建系统
认证/授权
workspace 成员资格
生产级错误码
复杂评论路由
漂亮 UI
```

一句话总结:**s07 把前六章接成一个可点的本地看板。浏览器负责创建、评论、改状态和订阅进度;真正的执行仍然走 task 队列和 daemon。**
