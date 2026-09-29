---
title: 后续修复交接清单
date: 2026-09-29
status: 待执行
base_commit: dea0fbc
---

# 后续修复交接清单

每个任务独立可执行，按优先级排列。标 **[需人工]** 的步骤需要真实 API Key，Agent 无法完成，做到那一步停下交还给用户。

## 通用约定（所有任务）

- Python 用 `.venv`（3.11）：`source .venv/bin/activate`
- 测试：`python3 -m pytest tests/ -q`（基线 196 passed，任务完成后不得减少）
- 服务：`python3 -m uvicorn src.web.server:app --host 127.0.0.1 --port 8080`，无热重载，改 Python 代码后需重启；前端静态文件已设 `Cache-Control: no-cache`，刷新即生效
- 真实 Key 只存在用户浏览器本地，不在环境变量里；需要真实调用 LLM/TTS 的验证交给用户在网页上触发
- 不要自行提交或推送，除非用户要求
- 领域词汇见 `CONCEPTS.md`；时长预算与校准见 `src/audio/budget.py`

---

## A1 · [P0] 摘要与质量抽检在思考型模型下全部失效

**现象（已确认）**：9 月以来生成的 6 份 `summary.json` 全部为 `{}`（2 字节），7 月用 `deepseek-chat` 生成的都正常。日志每次出现 `Quality check: no points found in response`。两步每次都在调用 API 花钱，却没有产出。

**根因**：用户在网页选的 `deepseek-flash` 是思考型模型，思考内容计入 `max_tokens`。这两处的额度写死得很小，思考过程就把额度耗尽了，正文为空：
- `src/translation/client.py` 的 `summarize()`：`max_tokens=1024`（约 1200 行）
- `src/translation/client.py` 的 `_quality_check()`：`max_tokens=512`（约 1349 行）

这两处都走同步 `OpenAI` 客户端，没有复用异步翻译路径里已有的提额逻辑。

**做法**：
1. 抽一个同步辅助函数（比如 `_complete_with_reasoning_budget(client, **kwargs)`）：发起请求 → 如果 `finish_reason == "length"`，而且思考内容占了大头（判定规则复用 `TranslationTruncatedError.reasoning_exhausted` 的比例 `_REASONING_EXHAUSTED_RATIO`），就用 `_raised_max_tokens()` 提额重试，直到达到上限 `llm.reasoning_max_tokens`。
2. `summarize()` 和 `_quality_check()` 都改用这个函数。
3. 思考内容在 `message.reasoning_content` 字段里（`getattr` 读取，字段不存在时视为空）。

**验收**：
- 新增单元测试：用假客户端模拟“第一次 length 且只有思考内容，第二次正常”，断言发生了提额重试并拿到结果；模拟非思考型模型的正常响应，断言只调用一次。
- 196 个原有测试全部通过。
- **[需人工]** 用户在网页上重新生成任一变体后，对应的 `summary.json` 不再是 `{}`，日志中不再出现 `no points found`。

**可选**：写一个一次性脚本，为现有的 6 份空摘要补跑摘要（需要 Key，交给用户执行）。

---

## A2 · [P1] Fish 失败时静默改用 Edge，同一期节目可能中途换声音

**现象**：`src/tts/synthesizer.py` 约 249–262 行，Fish 合成失败时 `logger.warning(... falling back to edge-tts)` 后改用 Edge 合成该段。这有三个问题：
- 一期节目里可能混进另一个音色，听感割裂；
- `tts_segments/manifest.json` 里的 `provider` 仍记为 `fish`，事后无从追查；
- 用户完全不知情。

另外，Fish 在海外，走的是代理，实测每期都会出现 1–2 次 `SSL: UNEXPECTED_EOF_WHILE_READING`（目前重试 2 次后能恢复）。

**做法**：
1. 默认**不回退**：Fish 在重试用尽后抛出明确的错误，任务失败，并可“从 TTS 阶段重试”（`resume_from=translated` 的路径已经存在）。
2. 新增配置 `tts.fallback_provider`（默认为空）；只有显式配置时才允许回退，且回退时要：通过 `on_progress` 提示“第 N 段改用 Edge”；在 manifest 中记录 `fallback_segments: [N, ...]`。
3. Fish 的网络重试从 2 次提到 4 次，并加上指数退避。当前在 `_fish_tts` 约 300–328 行。

**验收**：
- 单元测试：mock `_fish_tts` 一直失败，默认配置下 `synthesize` 抛出错误，且不会调用 `_edge_tts`；配置了 `fallback_provider: edge` 时会回退，并在 manifest 里记录回退的段号。
- 原有测试全部通过。

---

## A3 · [P1] 转写提速：CPU → Apple Silicon 加速

**现状**：转写已经是端到端流程里最大的瓶颈。实测 BV1wn8q6JEKK（23.7 分钟音频）完整流程 8 分 09 秒，其中转写 4 分 35 秒，占 **56%**；BV1Ccbs6SERa（32.4 分钟）转写 6 分 53 秒。
- 配置：`config.yaml` → `asr: provider: whisper, model: large-v3-turbo, device: cpu`
- 机器：Apple M4，`torch.backends.mps.is_available() == True`
- 代码：`src/transcription/transcriber.py`，目前支持 `whisper` 和 `faster-whisper` 两种 provider

**做法**：
1. 新增 provider `mlx-whisper`（`pip install mlx-whisper`，模型用 `mlx-community/whisper-large-v3-turbo`），在 `transcribe()` 里分发，输出格式与 `_transcribe_whisper` 保持一致（`SubtitleEntry` 列表 / 时间戳格式）。依赖要写进 `pyproject.toml` 和 `requirements.lock.txt`。
2. 如果 mlx 不可用，再评估 openai-whisper 的 `device: mps`。已知部分算子不支持 MPS，可能会报错或回退到 CPU，需要实测。
3. 默认值要等对照测试有结论后再切换。

**对照测试（必须做）**：
- 素材：`data/自称救世主？详解Anthropic的前世今生【因势分解】/` 目录下的原始音频（`.m4a` 或 `.wav`，只有 `.wav` 被删掉时需要重新下载），以及现有的 `transcript_raw.json` 作为 CPU 基线。
- 比较：总耗时，以及与 CPU 基线逐字对比的字符差异率（可以用 `difflib.SequenceMatcher` 或编辑距离计算 CER）。
- 在 `docs/reports/` 写一份简短报告，包含耗时、差异率，以及 3 段差异样例。

**验收**：加速比 ≥ 3 倍，与 CPU 基线的字符差异率 ≤ 3%；满足这两条才把 `config.yaml` 默认值切过去，否则只保留为可选的 provider。

---

## A4 · [P2] 失败原因给用户看懂的提示（可用性报告 F7）

**现状**：`src/pipeline/orchestrator.py` 约 841 行 `error_msg = str(e)`，原始异常直接写入数据库并展示给用户，比如“未安装 whisper 模块。请安装 openai-whisper: pip install openai-whisper”“TTS 合成失败 (segment 6): Cannot connect to host speech.platform.bing.com:443 ssl:<ssl.SSLCont…”。前端历史抽屉会展示失败行的首行（`src/web/static/js/history.js` 的 `shortError`），错误页也会展示。

**做法**：
1. 新增 `src/pipeline/errors.py`，提供 `humanize_error(exc) -> tuple[str, str]`，返回（给用户看的一句话原因，下一步建议）。至少覆盖：
   - 缺少模块（`ModuleNotFoundError` / “未安装 … 模块”）→ “运行环境缺少依赖” + 安装命令
   - 未配置 API Key → 保留现有文案
   - 网络类错误（SSL / 超时 / 连接失败）→ “网络连接失败” + 检查代理的建议（当前代理取自 `config.yaml` 的 `network.proxy`）
   - B站 / YouTube 下载失败（视频不可用、需要登录）
   - TTS 失败（区分 Fish 的 HTTP 4xx，例如 Key 无效或额度用尽，和网络错误）
   - 兜底：“处理失败”
2. 数据库里的 `error_message` 存转换后的友好文案；原始异常完整写入日志（`logger.exception`）。如果需要在界面上展开查看原文，可以新增 `error_detail` 字段（需要做数据库迁移，参照 `src/storage/db.py` 现有的 `ALTER TABLE` 写法）。
3. 前端失败行和错误页展示“原因 + 建议”，原文放在可折叠的 `<details>` 里。

**验收**：每类映射都有单元测试；对失败的任务调用 `/api/tasks`，返回的 `error_message` 是友好文案；原有测试全部通过。

---

## A5 · [P2] 进度页显示预计剩余时间（可用性报告 F12）

**现状**：进度页有阶段时间线和已用时间，但没有剩余时间预估；表单下方写死了“5–15 分钟”。

**可用于估算的实测数据**（Apple M4、CPU 转写、Fish 3 并发、`deepseek-flash`）：
| 阶段 | 经验值 |
|---|---|
| 下载 | 40–70 秒 |
| 转写 | 约 0.19–0.21 × 原视频时长 |
| 浓缩 | 60–70 秒（已有校准、一遍完成） |
| 语音合成 | 约 5.5 秒 × 片段数 ÷ 并发数（片段数约为 TTS 文本字数 ÷ 200） |
| 合并 | 10–30 秒 |

**做法**：
1. 在 `src/audio/budget.py`（或新建 `src/pipeline/eta.py`）实现 `estimate_remaining(stage, source_seconds, tts_chars=None) -> seconds`，系数写成模块常量，并注明出处（本文件）。
2. 通过 SSE 进度事件下发 `eta_seconds`（服务端相关实现见 `src/web/server.py` 中 `_append_progress` 与状态推送部分），前端时间线旁显示“预计还需约 X 分钟”。
3. 转写完成后、语音合成开始前，用实际的 TTS 文本长度重新计算一次，这时的估算最准。
4. A3 如果切换了转写 provider，转写系数要跟着更新。

**验收**：`estimate_remaining` 的单元测试覆盖各个阶段；手动跑一期节目时，ETA 与实际剩余时间的偏差在 ±30% 以内。**[需人工]**：真实运行需要 Key。

---

## A6 · [P3] 清理同步翻译死代码

`src/translation/client.py` 中的同步 `_translate_single`、`_translate_batch`、`_fallback_sequential` 只互相调用；对外的同步入口 `translate()` 已经改为调用 `translate_async`。异步路径里新增的提额、按实际内容算字数等逻辑都没有同步到这套死代码里，留着会误导后来的维护者。

**做法**：用 `grep` 确认这三个函数在 `src/`、`scripts/`、`tests/` 中都没有外部调用后删除；如果测试引用了，就改测异步版本。`condensed_overshoot()`（`src/audio/budget.py`）现在只被测试使用，一并评估是否保留。

**验收**：原有测试全部通过（删除死代码对应测试后的数量要在报告里说明）。

---

## 需要用户本人处理（非 Agent 任务）

| # | 事项 | 说明 |
|---|---|---|
| M1 | 重新生成 Obsidian 播客版 | 现有的 MP3 是 9 月 20 日用 Edge 生成的，SSML 标签被念了出来，长达 61 分钟，基本无法收听。网页上粘贴 `https://www.youtube.com/watch?v=4Hvkv_I8QDE`，选“中文播客版”，然后点“重新生成” |
| M2 | 跑一次英文视频浓缩版 | 校准按“模型 + 源语言”记录，英文路径还没有校准数据，第一次会按系数 1.0 起步，超标时会自动重写 |
| M3 | 浓缩目标中值定 40% 还是 45% | BV1wn8q6JEKK 压到了原视频的 33%，句子偏短，像电报。听一下 `data/自称救世主？…/variants/condensed/` 里的成品再决定。要调整的话改 `src/audio/budget.py` 的 `duration_budget` 和 `src/translation/client.py` 的 `CONDENSED_TARGET_RATIO` |
| M4 | 更新 STRATEGY.md | 还停在 7 月 2 日，写的是“只做 YouTube”。现在已经支持 B站、抖音、小红书，中文视频走改写流程。需要定下来：国内平台是主线还是附加功能 |
| M5 | 是否记录使用频率 | STRATEGY 定了三个核心指标，其中“使用频率”至今没有任何数据来源 |

## 本轮已完成（供参考，不需要再做）

截至 `dea0fbc`：验收门禁改为确定性检查；时长预算以原视频时长为基准；合成后检查实际时长；B站元数据 412 的问题及直连；误把非 YouTube 链接当作 YouTube 处理；浓缩分段并行、明确字数目标、思考耗尽时提额、按“模型 + 源语言”校准；历史抽屉改版；静态资源设为 `no-cache`。
