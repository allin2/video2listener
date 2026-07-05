# video2listener

Python 环境：使用 `.venv`（Python 3.11），不要用系统 Python 3.9。

运行任何 Python 命令前先激活虚拟环境：
```bash
source .venv/bin/activate
```

启动服务：
```bash
source .venv/bin/activate && python3 -m uvicorn src.web.server:app --host 127.0.0.1 --port 8080
```

运行测试：
```bash
source .venv/bin/activate && python3 -m pytest tests/ -q
```

## 知识库

`docs/solutions/` — 已解决问题的文档化记录，按类别组织，带 YAML frontmatter（`module`, `tags`, `problem_type`）。在实现或调试相关模块时可参考。

`CONCEPTS.md` — 项目共享领域词汇表（实体、流程、状态概念）。在理解代码库或讨论领域概念时可参考。
