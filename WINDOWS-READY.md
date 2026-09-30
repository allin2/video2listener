# Windows 环境就绪清单（2026-09-30）

## 已自动就绪（无需操作）

| 项目 | 状态 |
|---|---|
| Python 3.11.6 `.venv` + 全部依赖 | ✓ 从零安装流程已验证 |
| `openai-whisper` 包损坏已修复 | ✓ pip 缓存中的坏 wheel 已清除 |
| ffmpeg / ffprobe 8.1.1 | ✓ 在 PATH |
| Edge TTS 直连 | ✓ 已实测合成成功（国内可直连） |
| yt-dlp 2026.8.19 | ✓ |
| whisper large-v3-turbo 权重 | ✓ 已下载并通过 SHA256 校验，实测可加载（`C:\Users\Administrator\.cache\whisper\large-v3-turbo.pt`，1.51 GB） |
| 测试 | 198 passed（唯一失败为远端基线就有的 Windows 路径用例，与本次无关） |

国内平台（B 站 / 抖音 / 小红书）+ Edge 音色 + DeepSeek 改写：**现在就能跑**，不依赖代理。

## 需要你人工处理

### 1. 代理端口对齐为 7892（只有需要 YouTube 或 Fish 音色时才必须）

代理软件已安装（HTTP 模式，当前混合端口 7890），但 [config.yaml](config.yaml) 配的是 `7892`：

- Fish TTS 已改为遵循 `network.proxy` 配置（不再依赖启动时的环境变量）
- yt-dlp（YouTube 下载）本来就遵循 `network.proxy`
- 因此把 Clash 的**混合端口改成 7892** 后，两者即刻可用，无需改代码或配置

自检：`Test-NetConnection 127.0.0.1 -Port 7892`，`TcpTestSucceeded: True` 即可。
（不想改端口的话，也可把 config.yaml 第 5 行改为 7890，但该文件与 Mac 共用，Mac 上 yt-dlp 依赖 7892。）

### 2. 配置 API Key（打开网页即可，1 分钟）

Key 按设计存在浏览器 localStorage，不在环境变量里：

1. 启动服务（见下方命令），浏览器打开 `http://127.0.0.1:8080`；
2. 设置里填入 DeepSeek API Key（必填）；用 Fish 音色再填 Fish API Key；
3. 第一次 `deepseek-flash` 思考型模型会自动提额重试（A1 已修复），摘要不再是 `{}`。

### 3. 生产机（macOS）补跑 6 份空摘要

```bash
git pull
python3 scripts/backfill_summaries.py --dry-run   # 预览
python3 scripts/backfill_summaries.py --api-key sk-xxx
```

## 启动命令

```powershell
.venv\Scripts\python.exe -m uvicorn src.web.server:app --host 127.0.0.1 --port 8080
```
