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
