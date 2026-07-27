# s13 — Learning Console(Mission Control UI + 25 API + 10 页面)

s01-s12 分别讲了 issue/task/daemon/claude/持久化/resume/realtime/webpanel/skills/autopilot/multicli/workspaces/squads。s13 把它们全部集成进一个**集成控制台**——一个 FastAPI app + 单页 HTML,打开浏览器就能看见 s01-s12 的所有能力及其关系,还可以操作(创建 issue、run daemon、评论 resume、tick autopilot、seed demo)。

## 一、核心设计思想

第一个思想:**一个 app 集成全部**。`create_console_app()` 闭包引用所有 stores + 服务,~25 个 API endpoint 暴露全部能力。

第二个思想:**Mission Control 美学**。深色底(#0d1117)+ 暖琥珀 accent(#f0a050)——仪表盘/CRT 质感,不是通用的黑底+酸绿。

第三个思想:**10 个页面,每个都有实质内容**。Overview/Issues/Tasks/Trace/Skills/Autopilot/Runtimes/Workspaces/Squads/Timeline——不是空壳,每个都有列表+详情+可视化。

第四个思想:**对象可点击跳转**。选中 issue → Inspector 显示 tasks/comments/children;选中 task → 显示 issue/session/prior task。

第五个思想:**一键 demo + reset**。seed 创建示例数据,reset 清空重来。

## 二、为什么这么设计

### 为什么 dark + amber,不用 dark + green?
dark+green 是 AI 生成设计默认。amber on deep ink 是仪表盘色调,和"观察系统运行"对应。

### 为什么零构建?
不该引入 npm/webpack。内联 HTML/CSS/JS,uvicorn 直接服务。

### 为什么默认 StubRunner?
需要可复现。StubRunner 每次结果一样,可以反复 reset 重来。

## 三、怎么用

```
python sessions/zh/s13_console.py → http://localhost:8000/
```
1. 点 Seed → 示例数据
2. Issues → 看板 + 状态 badge
3. 点 issue → Inspector + Timeline
4. Run daemon once → task 执行,issue 推进
5. Trace → session events
6. Reset → 重来

## 试一试

```bash
python -m pytest tests/test_console.py -v
python sessions/zh/s13_console.py
```

## 四、与完整 multiCa 的区别

| 维度 | multiCa | mica s13 |
|---|---|---|
| 前端 | Next.js 全功能 | 单页 HTML(零构建) |
| API | Go Chi | FastAPI |
| 目的 | 生产看板 | 集成观察(s01-s12 全可见) |
| seed/reset | 无 | 有 |
| 默认 runner | 真实 claude | StubRunner(可复现) |
| timeline | 无 | 有(签名) |
