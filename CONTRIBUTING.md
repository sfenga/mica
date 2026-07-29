# Contributing

感谢你愿意改进 mica。这个仓库的核心目标是“渐进式教学”:每章一个可直接运行的 Python 文件 + 同名中文说明,每章只引入一个新概念。

## 开发约定

- 保持章节可独立阅读。新增概念优先放到新章节,不要把早期章节重构成抽象框架。
- 保持本地优先。Web/WS demo 默认监听 `127.0.0.1`,不要默认暴露到局域网或公网。
- 保持测试覆盖。修改核心行为时,请补充或更新 `tests/` 中的行为测试。
- 保持文档同步。修改章节行为时,同步对应的 `sessions/zh/sNN_*.md` 和必要的 README/OpenSpec 内容。

## 本地验证

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m compileall -q sessions tests
python -m pytest -q
```

如果验证真实 `ClaudeCliRunner`,请只在可信本地目录和可信任务上运行。
