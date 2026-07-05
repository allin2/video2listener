---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
created: 2026-07-03
---

# 管道关键路径去阻塞化 — Plan

## Goal Capsule

**Objective**: 将 `summarize` 和 `quality_check` 从管道主路径移除，翻译完成后即刻进入 TTS，不再因摘要 API 挂起而阻塞 MP3 生成。

**Product authority**: 用户（单开发者本地工具，无外部 stakeholder）

**Open blockers**: 无

---

## Product Contract

Product Contract unchanged (R1-R4, scope boundaries preserved from brainstorm).

### R1 — 异步摘要生成

翻译阶段完成后，`llm_summarize` 不阻塞管道。改为在后台线程中执行，管道即刻进入 TTS 阶段。

**Acceptance**: 翻译完成 → TTS 开始之间的延迟从"summarize API 耗时"降为"零"（仅线程启动开销）。

### R2 — 异步质检

`_quality_check` 同样改为后台执行。质检结果写入日志，不影响管道进度。

**Acceptance**: 质检失败不阻塞、不报错、不影响 MP3 输出。

### R3 — 摘要结果延迟展示

摘要生成完成后写入 DB。前端 SSE 在管道状态变为 `done` 后，额外等待摘要结果最多 30 秒。若摘要在此窗口内完成则直接展示；超时则只展示 MP3 下载入口，摘要标记为"生成中"。

**Acceptance**: 用户总能立即下载 MP3；摘要在可用时自动出现，无需手动刷新。

### R4 — 摘要失败优雅降级

后台摘要生成失败时，记录 warning 日志（不报错），DB 中 `summary_path` 留空。历史面板不展示摘要区域。

**Acceptance**: 摘要生成失败不影响管道成功状态，用户仍可正常下载 MP3。

### Scope Boundaries

- **不改**：`summarize` 的 prompt 模板、API 调用逻辑、重试策略
- **不改**：前端 SSE 协议的整体结构（仅增加摘要延迟拉取逻辑）
- **不改**：`quality_check` 的对比逻辑和输出格式
- **不做**：将摘要改为按需懒加载（后续 ideation 可考虑）

---

## Planning Contract

### Key Technical Decisions

**KTD1 — 后台线程模型**: 使用 `threading.Thread`（非 `ThreadPoolExecutor`）启动单次后台任务。管道不需要等待线程结果，线程自行完成后更新 DB。选择理由：改动最小，与管道现有的 `Thread` 模型一致（`orchestrator.py` 主循环已在独立线程中运行）。

**KTD2 — 前端轮询而非 SSE 推送**: 不在 SSE 协议中新增摘要事件。前端在管道 `done` 后通过现有 `/api/status` 端点轮询 `summary_path` 字段（最多 30 秒，2 秒间隔）。选择理由：避免改 SSE 协议，复用现有 HTTP 轮询基础设施。

**KTD3 — 后台任务生命周期**: 后台线程不绑定 cancel_event。即使用户取消管道，已启动的摘要生成仍会完成并写入 DB（或静默失败）。选择理由：摘要仅影响展示，取消耗时操作无收益。

### Patterns to Follow

- `orchestrator.py` 现有的 `threading.Thread` + `_task_states` 模式
- `server.py` 中 `/api/status` 端点已有的 `summary_path` 暴露逻辑
- `client.py:351-410` 中 `summarize()` 的错误处理和返回值格式

---

## Implementation Units

### U1. 后台摘要 + 质检（orchestrator）

**Goal**: 将 `_extract_terms`、`_quality_check`、`llm_summarize` 移入后台线程，管道在启动后台任务后立即进入 TTS。

**Requirements**: R1, R2, R4

**Dependencies**: 无

**Files**:
- `src/pipeline/orchestrator.py` (modify, lines 447-483)

**Approach**: 把现有的顺序调用块替换为：

```
translate → write script_zh.txt → spawn background thread:
  ├── _extract_terms(script_path)
  ├── _quality_check(source_text, script_zh, metadata, llm_config)
  └── llm_summarize(script_zh, metadata, llm_config) → write summary.json → db.update_variant_status
→ progress("翻译完成") → proceed to TTS (line 494)
```

后台线程自行处理异常（logger.warning），不向上传播。`summary.json` 写入和 DB 状态更新在线程内完成，不在主路径上执行。

`_cleanup_variant_outputs` 调用（line 445）保持在主路径上——它不依赖 summarize 的结果。

**Patterns to follow**: `orchestrator.py:131-137` 中 `_run` 的 `threading.Thread(target=...)` 启动模式。

**Test scenarios**:
- 翻译完成后 TTS 立即开始，无需等待摘要 API 返回（用 mock `llm_summarize` 注入 5 秒延迟验证）
- 后台摘要线程成功时，DB 中 `summary_path` 正确写入（检查 `summary.json` 存在且内容有效）
- 后台摘要线程抛出异常时，管道主路径不受影响，日志含 warning
- 后台摘要超时（模拟 DeepSeek API 120s timeout 触发）时，`summary_path` 为空，管道正常完成
- 术语提取和质检也一并移入后台线程

**Verification**: 运行 `python3 -m pytest tests/test_pipeline.py -q`，原有管道测试通过且无明显延迟增加。手动提交一个短 YouTube 视频，观察翻译完成后 TTS 即刻开始。

---

### U2. 前端摘要延迟拉取

**Goal**: 管道完成后前端自动等待摘要（最多 30 秒），摘要在可用时自动出现。

**Requirements**: R3

**Dependencies**: U1

**Files**:
- `src/web/static/index.html` (modify)

**Approach**: 在 `PipelineTimeline` 的状态轮询逻辑中，当 `status === 'done'` 且 `summary_path` 为空时，启动一个 30 秒倒计时轮询（每 2 秒请求 `/api/status` 检查 `summary_path` 是否已填充）。超时后标记摘要为"生成中"；轮询到结果后更新 UI。

复用现有的 `fetchStatus` 或 SSE 完成回调——不需要新的 HTTP 端点。

**Patterns to follow**: `index.html` 中已有的 `setInterval` + `fetch` 轮询模式。

**Test scenarios**:
- 管道 `done` 时 `summary_path` 已有值 → 直接展示摘要
- 管道 `done` 时 `summary_path` 为空 → 开始 30 秒轮询
- 后台摘要在 10 秒内完成 → 轮询捕获到 `summary_path` → 更新 UI
- 30 秒超时 `summary_path` 仍为空 → 显示"摘要生成中"
- 下载 MP3 入口在 `done` 状态下立即可用，不等摘要

**Verification**: 提交一个视频，管道完成后在浏览器中观察：MP3 下载入口立即可用；摘要在 30 秒内出现或显示"生成中"。

---

### U3. 管道端到端验证

**Goal**: 确保新流程在真实 DeepSeek API 下正常工作。

**Requirements**: R1-R4

**Dependencies**: U1, U2

**Files**:
- `tests/test_pipeline.py` (modify — 新增加速测试)

**Approach**: 添加一个集成级测试：mock `llm_summarize` 为延迟 3 秒后返回，验证管道在翻译完成后 2 秒内进入 TTS（即不等待 summarize 完成）。

**Test scenarios**:
- 集成测试：mock translate 返回固定中文文本，mock summarize sleep 3s 后返回，验证管道 `done` 时间 < translate 结束时间 + 5s（说明 summarize 未阻塞）

**Verification**: `python3 -m pytest tests/test_pipeline.py -q -k "summarize"` 通过。

---

## Verification Contract

| Requirement | Verification |
|---|---|
| R1 — 异步摘要 | 翻译 → TTS 延迟 < 1s（排除 API 调用自身耗时） |
| R2 — 异步质检 | 质检失败不阻塞 MP3 输出 |
| R3 — 摘要延迟展示 | MP3 立即可下载；摘要 < 30s 自动出现 |
| R4 — 摘要失败降级 | 摘要失败 → MP3 正常下载，无报错弹窗 |

## Definition of Done

- [ ] U1 完成：`orchestrator.py` 中 summarize/quality_check/extract_terms 移入后台线程
- [ ] U2 完成：前端在管道 done 后轮询 summary_path 最多 30 秒
- [ ] U3 完成：集成测试验证 summarize 不阻塞管道主路径
- [ ] 现有测试套件全部通过
- [ ] 手动提交一个 YouTube 短视频，确认翻译 → TTS 无明显等待
