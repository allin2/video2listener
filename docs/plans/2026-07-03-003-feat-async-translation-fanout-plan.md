---
title: Async Translation Fan-Out - Plan
type: feat
date: 2026-07-03
topic: async-translation-fanout
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Async Translation Fan-Out - Plan

## Goal Capsule

**Objective**: 将翻译阶段的串行 API 调用替换为并发异步调度，翻译延迟从 `N × 单次延迟` 降至 `单次延迟`，预期 4-6x 加速。同时将 orchestrator 和 TTS 接口全面异步化，为后续 Pipeline 阶段重叠铺路。

**Product authority**: 用户（单开发者本地工具，付费 DeepSeek API）。

**Open blockers**: 无。

## Product Contract

### Summary

翻译批次通过 `AsyncOpenAI` + `asyncio.Semaphore` 并发发出，失败批次独立重试、成功批次保留不浪费。Orchestrator 和 TTS 接口同步异步化。内置 token-bucket 限流器防止并发过高时的 429 风暴。

Product Contract unchanged from ce-brainstorm.

### Problem Frame

当前 `client.py:394` 使用串行 `for` 循环逐批调用 DeepSeek API。40 个 segment 在 `batch_size=5` 下产生 8 次串行 API 调用，每次 ~500ms，翻译阶段纯 I/O 等待超过 4 秒。`client.py:190` 的 `time.sleep()` 在重试时阻塞整线程。DeepSeek 官方支持 500 并发连接，当前只用 1。Orchestrator 整体同步，阻塞了后续 TTS 阶段的启动。

### Requirements

**并发调度**

R1. 翻译批次使用 `AsyncOpenAI` 客户端并发发出，替代当前 `for batch in batches` 串行循环。

R2. 并发数通过 `asyncio.Semaphore` 控制，默认值 10，可通过 `config.yaml` 配置。付费 DeepSeek API（~200 RPM）下 10 并发安全；免费 tier 用户需调低至 3-5。

R3. 每个批次独立维护重试状态——某批次失败时只重试该批次，其余已成功的保留结果。

**非阻塞重试**

R4. 重试退避使用 `asyncio.sleep()`，替代当前的 `time.sleep()`，不阻塞事件循环中的其他协程。

R5. 重试间隔增加随机抖动（jitter），防止多个批次在退避后同时重试产生 thundering herd。

**限流保护**

R6. 所有翻译 API 调用经过一个 token-bucket 限流器，防止并发过高时触发 429 风暴。

R7. 限流器参数（速率、突发容量）可通过 `config.yaml` 配置，默认速率 120 RPM（适配付费 DeepSeek API）。

**Orchestrator 异步化**

R8. `orchestrator.process()` 改为 `async def`，所有阶段方法支持 async/await。

R9. 服务端 SSE 端点直接 await `process()`，移除现有的 ThreadPoolExecutor 包装。

R10. 现有的 `_check_cancel()` 取消机制适配 asyncio——使用 `asyncio.Event` 替代 `threading.Event`。取消检查注入每个批次的迭代内部。

**TTS 接口异步铺垫**

R11. `synthesize()` 函数签名改为 `async def`，内部实现暂通过 `run_in_executor` 桥接现有同步逻辑。

R12. `merge()` 合并接口同步改为 async 签名。

**向后兼容**

R13. `translate()` 函数保持相同的中文文本输出格式——调用方不需修改解析逻辑。

R14. `process()` 保持相同的返回值结构（`{"status": ..., "video_id": ..., "mode": ..., "output_path": ...}`）。

### Key Decisions

**全面异步化而非桥接。** 选择将 orchestrator 整体改为 async，而非在同步 orchestrator 中用 `asyncio.run()` 包裹异步翻译。代价是改动面更大，但彻底消除了事件循环嵌套问题，且直接解锁后续 Producer-Consumer Pipeline 等异步特性。

**只重试失败批次。** 某批次失败时，其余成功的保留。避免因为一个 429 丢弃已付费翻译的 token。segment 之间无顺序依赖，时序差对最终拼接无影响。

### Scope Boundaries

**不在此范围**

- TTS 实际并发化——只改 async 签名，内部实现保持串行
- Adaptive Concurrency Discovery（TCP AIMD 自适应调优）——独立 ideation idea
- Token-Aware Batching（按 token 数打包批次）——独立 ideation idea
- Producer-Consumer Pipeline（翻译与 TTS 阶段重叠）——独立 ideation idea，但本需求是其前置条件
- 翻译质量改进——本需求只改调度方式，不改 prompt 或模型参数

### Success Criteria

- 翻译阶段延迟：40 段视频的 API 调用耗时从 ~4s（串行 8 批 × 500ms）降至 ~500ms（全部并发）
- 零浪费：单批次失败时不重做已成功的批次
- 测试通过：现有 `tests/test_pipeline.py` 和 `tests/test_translation.py` 全部通过（适配异步后）
- 取消响应：用户点击取消后，当前正在运行的批次在下一个 retry 周期内响应（< 2 秒），不等待全部完成

### Dependencies / Assumptions

- 用户使用付费 DeepSeek API（~200 RPM），默认并发 10 安全
- `openai` 包已包含 `AsyncOpenAI` 客户端（≥1.30），无需新依赖
- uvicorn 原生支持 async handler，移除 ThreadPoolExecutor 包装无兼容性风险
- 现有同步调用方（`_post_translate_async` 后台线程中的 `llm_summarize`、`_quality_check`）不受影响——后台线程保持同步

### Sources / Research

- ideation: `docs/ideation/2026-07-03-pipeline-efficiency-ideation.html` — idea #1 "Async Translation Fan-Out"
- code: `src/translation/client.py:394` — 串行 for 循环；`client.py:190` — time.sleep()
- code: `src/pipeline/orchestrator.py:131-137` — 现有 ThreadPoolExecutor 调度模式
- code: `src/tts/synthesizer.py:74-98` — TTS ThreadPoolExecutor(3)
- external: DeepSeek API 500 并发连接限制；async dispatch 2x 吞吐（187 vs 92 req/min 基准测试）

---

## Planning Contract

### Key Technical Decisions

**KTD1 — AsyncOpenAI + asyncio.Semaphore 并发模型。** 翻译批次通过 `AsyncOpenAI` 原生异步 I/O 并发发出，用 `asyncio.Semaphore` 限制同时进行的 API 调用数。选择理由：原生异步避免 ThreadPoolExecutor 的线程开销（每个线程 ~8MB 栈空间），Semaphore 精确控制并发度，`asyncio.gather(return_exceptions=True)` 天然支持独立重试。

**KTD2 — asyncio.to_thread() 桥接阻塞阶段。** yt-dlp 下载、whisper 转写、ffmpeg 合并不改为异步——它们没有原生 async API。在 `async def process()` 中通过 `asyncio.to_thread()` 将阻塞调用卸载到默认线程池，不阻塞事件循环。选择理由：只改翻译和 TTS（有价值的异步化），其余保持同步但安全卸载。

**KTD3 — Token-bucket 限流器独立于 Semaphore。** 限流器控制 API 调用的时间速率，Semaphore 控制同时进行的数量——两者正交。Token-bucket 算法：令牌以配置速率补充，每次 API 调用消耗一个令牌，桶容量 = 速率 × 2 作为突发缓冲。选择理由：单独的 Semaphore 只能限制并发数，无法限制 RPM；rate limiter 补全了时间维度的保护。

**KTD4 — 同步 translate() 保留为兼容包装。** 现有的 `translate()` 函数变为 `asyncio.run(translate_async(...))`，保持相同的函数签名和返回值。选择理由：orchestrator 外可能存在的调用方不受影响；`_post_translate_async` 后台线程中的 `llm_summarize` 继续使用同步路径。

**KTD5 — asyncio.Event 取消传播。** `threading.Event` 替换为 `asyncio.Event`。取消检查从阶段间（当前）扩展到每个批次的迭代内部和每个 retry 周期。`asyncio.CancelledError` 传播处理 mid-batch 取消：`asyncio.gather` 中的任务被取消后，已完成的批次结果保留，未完成的丢弃。选择理由：用户点击取消应 < 2 秒内响应，而非等待整个翻译阶段完成。

### Patterns to Follow

- `src/tts/synthesizer.py:74-98` — 现有的并发调度模式（并发 fan-out + 顺序收集 + 错误时取消其余任务），翻译需用 asyncio 等价实现
- `src/pipeline/orchestrator.py:458-488` — `_post_translate_async` 后台线程模式，保持同步不改
- `src/web/server.py:142,160-174` — `asyncio.Queue` + `loop.call_soon_threadsafe` SSE 事件桥接
- `src/translation/client.py:34-56` — `_resolve_llm()` 返回 client + model + api_key 的三元组模式，异步版本复刻

### High-Level Technical Design

**翻译并发调度流程：**

```
translate_async(text, mode, ...)
  │
  ├─ segments = _split_translation_segments(text)
  ├─ batches = _make_batches(segments, batch_size)
  │
  ├─ sem = asyncio.Semaphore(max_concurrency)
  ├─ limiter = TokenBucket(rate=rpm, burst=rpm*2)
  │
  └─ asyncio.gather(*[
       translate_one_batch(batch, idx, sem, limiter)
       for idx, batch in enumerate(batches)
     ], return_exceptions=True)
       │
       ├─ 成功: 保留结果
       └─ 异常: 记录该批次失败，隔离不传播

translate_one_batch:
  async with sem:           # 并发控制
    await limiter.acquire() # 限流控制
    for attempt in range(retries):
      try:
        resp = await client.chat.completions.create(...)
        return parse(resp)
      except RateLimitError:
        await asyncio.sleep(backoff**attempt * random(0.5, 1.5))
      except Exception:
        if attempt == retries - 1: raise
        await asyncio.sleep(backoff**attempt * random(0.5, 1.5))
```

**Orchestrator 异步化后的线程模型：**

```
uvicorn (async event loop)
  │
  ├─ SSE endpoint: async generator, pushes events via asyncio.Queue
  │
  └─ POST /api/process: async def api_process()
       │
       └─ await process()  ← 直接 await，不再 ThreadPoolExecutor
            │
            ├─ await youtube_extract_async()     → asyncio.to_thread(yt-dlp)
            ├─ await whisper_transcribe_async()    → asyncio.to_thread(faster-whisper)
            ├─ await translate_async()             → AsyncOpenAI (native async)
            ├─ await synthesize_async()            → run_in_executor(ThreadPoolExecutor)
            └─ await merge_async()                 → asyncio.to_thread(ffmpeg)
```

---

## Implementation Units

### U1. 异步翻译客户端核心

**Goal**: 在 `src/translation/client.py` 中新增异步翻译路径——AsyncOpenAI 客户端、async 版本的批次和单段翻译函数、Semaphore 并发调度、token-bucket 限流器、非阻塞重试。

**Requirements**: R1, R2, R3, R4, R5, R6, R7, R13

**Dependencies**: 无

**Files**:
- `src/translation/client.py` (modify) — 新增 async 函数
- `config.yaml` (modify) — 新增 `translation` 配置段

**Approach**:

1. 新增 `_resolve_llm_async()` 函数——与 `_resolve_llm()` 平行，返回 `(AsyncOpenAI, model_name, api_key)`
2. 新增 `async def _translate_single_async()` —— `_translate_single()` 的异步版本。API 调用 → `await client.chat.completions.create()`，退避 → `await asyncio.sleep()`，递归拆分 → `await self`
3. 新增 `async def _translate_batch_async()` —— `_translate_batch()` 的异步版本。同上模式，fallback 调用 `_fallback_sequential_async()`
4. 新增 `async def _fallback_sequential_async()` —— 异步版本的逐段回退
5. 新增 `TokenBucket` 类——速率和容量可配置，`async acquire()` 方法在令牌不足时 `await asyncio.sleep()` 等待补充
6. 新增 `async def translate_async()` —— 入口函数。创建 `asyncio.Semaphore(max_concurrency)` + `TokenBucket`，`asyncio.gather(*tasks, return_exceptions=True)` 并发调度所有批次。收集结果时成功保留、失败独立记录
7. 修改现有 `translate()` —— 内部调用 `asyncio.run(translate_async(...))` 保持兼容
8. 在 `config.yaml` 新增 `translation` 段：`max_concurrency: 10`、`rate_limit_rpm: 120`

**Patterns to follow**: 现有 `_translate_single()` / `_translate_batch()` 的 prompt 构造和响应解析逻辑；`synthesizer.py:74-98` 的并发 fan-out + 错误隔离模式

**Test scenarios**:
- 并发调度：mock AsyncOpenAI 返回固定响应，验证 `asyncio.gather` 同时发出了 N 个批次
- 独立重试：注入 1 个批次失败、其余成功，验证只重试失败的，成功的保留
- 限流器：设置 rate_limit_rpm=2，连续发 5 个请求，验证实际速率 ≤ 2 RPM
- 非阻塞退避：mock 连续失败 2 次后成功，验证 asyncio.sleep 被调用且其他批次未受影响
- 兼容包装：调用 sync `translate()` 验证返回格式不变
- 配置文件：读取 `translation.max_concurrency` 和 `translation.rate_limit_rpm` 验证默认值
- `_fallback_sequential_async` 在段数不匹配时触发

**Verification**: 新增的 async 翻译路径能正确处理 8 个批次的并发调度，失败隔离生效，限流器控制速率。

---

### U2. Orchestrator 异步化

**Goal**: `process()` 改为 `async def`，所有阶段方法支持 await。阻塞阶段（下载、转写、合并）通过 `asyncio.to_thread()` 桥接。取消机制迁移到 `asyncio.Event`。

**Requirements**: R8, R10, R14

**Dependencies**: U1（需要 `translate_async` 可用）

**Files**:
- `src/pipeline/orchestrator.py` (modify) — `process()` 和内部方法异步化
- `src/pipeline/state.py` (no change) — 状态枚举不变

**Approach**:

1. `process()` → `async def process()`，保持相同的参数签名和返回值结构
2. 阻塞阶段包装：
   - yt-dlp 下载 → `await asyncio.to_thread(youtube_extract, ...)`
   - whisper 转写 → `await asyncio.to_thread(whisper_transcribe, ...)`
   - ffmpeg 合并 → `await asyncio.to_thread(merge, ...)`
3. 翻译阶段调用 U1 的 `translate_async()`：`await translate_async(...)`
4. TTS 阶段调用 U4 的 `await synthesize(...)`
5. `_check_cancel()` 改为接受 `asyncio.Event`：`if cancel_event and cancel_event.is_set()`
6. 取消检查注入翻译循环：在 `translate_async` 内部的每个批次任务中，retry 间隙检查 cancel event
7. `_post_translate_async` 后台线程保持不变（同步，daemon=True）
8. `threading.Lock`（`_lock`）保持——进程级互斥用 `threading.Lock` 足够，不需要 `asyncio.Lock`

**Patterns to follow**: `orchestrator.py:458-488` — `_post_translate_async` daemon thread 模式保持；`orchestrator.py:155-159` — 现有 `_check_cancel` 签名

**Test scenarios**:
- async process 完成完整管道：mock 所有阶段，验证 `await process(...)` 返回 `{"status": "done", ...}`
- 阻塞阶段通过 to_thread 执行：验证 yt-dlp 和 whisper mock 在非主线程中被调用
- 取消响应：在翻译阶段中间 set cancel_event，验证 process 在 2 秒内返回 cancelled 状态
- 返回值结构不变：验证返回 dict 的结构和字段名与同步版本一致
- 后台线程不变：`_post_translate_async` 仍在 daemon thread 中执行

**Verification**: `await process("test_vid", "podcast")` 正常完成，取消在 2 秒内响应，返回值结构兼容。

---

### U3. 服务端异步集成

**Goal**: 移除 `server.py` 中的 ThreadPoolExecutor 包装，SSE 端点直接 await `process()`。进度回调适配 async 上下文。

**Requirements**: R9

**Dependencies**: U2（需要 `async def process()` 可用）

**Files**:
- `src/web/server.py` (modify) — `api_process` 端点、`_run` 闭包

**Approach**:

1. `api_process` 端点中的 `_run()` 闭包改为 `async def _run_async()`：直接 `await process(...)` 替代 `ThreadPoolExecutor.submit()`
2. 移除 `threading.Thread(target=_run)` 包装——不再需要后台线程
3. 进度回调：`on_progress` 仍然是同步函数（`_append_progress`），在 async 上下文中直接调用——uvicorn 的 async handler 在主线程的事件循环中运行，同步回调不阻塞
4. `_task_states` 中的 cancel_event 从 `threading.Event` 改为 `asyncio.Event`
5. SSE 端点 `api_status_stream` 不变——它已经是 `async def`，`asyncio.Queue` 模式继续工作
6. `_emit_sse_event` 中的 `loop.call_soon_threadsafe` 简化——不再需要跨线程桥接，直接用 `queue.put_nowait`

**Patterns to follow**: `server.py:142,160-174` — 现有 asyncio.Queue SSE 模式；`server.py:562-633` — 现有 `_run` 闭包结构

**Test scenarios**:
- SSE 端点直接 await process：mock process 返回 done，验证 SSE 事件流完整
- 取消端点：POST /api/cancel 设置 asyncio.Event，验证 process 中的 cancel check 触发
- 并发请求：两个不同 video_id 同时提交，验证 `_lock` 互斥生效
- 进度回调不阻塞事件循环：在 on_progress 中不做异步操作

**Verification**: 提交视频 → SSE 事件流正常 → 完成。取消响应正常。并发请求互斥正常。

---

### U4. TTS 和合并接口异步签名

**Goal**: `synthesize()` 和 `merge()` 函数签名改为 `async def`，内部实现通过 `run_in_executor` 桥接现有同步代码。

**Requirements**: R11, R12

**Dependencies**: U2（orchestrator 需要 await 这些函数）

**Files**:
- `src/tts/synthesizer.py` (modify) — `synthesize()` → `async def`
- `src/audio/merger.py` (modify) — `merge()` → `async def`

**Approach**:

1. `synthesize()` 改为 `async def synthesize()`，内部调用现有同步逻辑：
   ```python
   loop = asyncio.get_running_loop()
   return await loop.run_in_executor(executor, _synthesize_sync, text, output_dir, ...)
   ```
   其中 `_synthesize_sync` 是当前的同步实现（重命名），`executor` 是现有的 `ThreadPoolExecutor(max_workers=concurrency)`

2. `merge()` 同理：`async def merge()` → `await loop.run_in_executor(None, _merge_sync, ...)`

3. 函数参数和返回值类型不变——只改签名加 async

**Patterns to follow**: `synthesizer.py:33-114` — 现有 synthesize 实现全部保留，只加 async 包装

**Test scenarios**:
- async synthesize 返回音频段列表：验证 `await synthesize(...)` 返回 `list[Path]`
- async merge 生成 MP3：验证 `await merge(...)` 生成正确的输出文件
- 现有 TTS 测试适配：`test_tts.py` 中的测试加 `@pytest.mark.asyncio` 和 `await`

**Verification**: 现有 TTS 测试适配 async 后全部通过。返回值结构不变。

---

### U5. 测试适配和端到端验证

**Goal**: 适配现有测试套件到 async 模式，新增并发调度和重试隔离的专项测试。端到端验证新管道。

**Requirements**: R1-R14（全覆盖验证）

**Dependencies**: U1, U2, U3, U4

**Files**:
- `tests/test_translation.py` (modify) — 适配 async + 新增并发测试
- `tests/test_pipeline.py` (modify) — 适配 async pipeline 测试
- `tests/test_tts.py` (modify) — 适配 async synthesize 测试
- `tests/test_server.py` (modify) — 适配 async endpoint 测试

**Approach**:

1. 安装 `pytest-asyncio`（如果尚未安装），配置 `pytestmark = pytest.mark.asyncio` 或使用 `@pytest.mark.asyncio` 装饰器
2. `test_translation.py`：
   - 现有翻译测试加 `async def` + `await translate_async(...)`
   - 新增 `test_concurrent_dispatch`：mock AsyncOpenAI 返回固定响应，验证 `asyncio.gather` 同时发出 N 个批次
   - 新增 `test_independent_retry`：注入 1/3 批次失败，验证只重试失败的、成功的保留
   - 新增 `test_rate_limiter`：token-bucket 限流器单元测试
   - 新增 `test_sync_wrapper`：验证 sync `translate()` 兼容包装正确
3. `test_pipeline.py`：
   - `test_summarize_does_not_block_pipeline` 适配 `await process(...)`
   - 新增 `test_cancel_during_translation`：翻译中途取消，验证 2 秒内返回 cancelled
   - 新增 `test_concurrent_translation_speedup`：mock 5 段翻译，验证并发完成时间 < 串行时间
4. `test_tts.py`：现有测试加 `await synthesize(...)`
5. `test_server.py`：async endpoint 测试适配（FastAPI TestClient 支持 async）

**Patterns to follow**: 现有 `tests/test_pipeline.py:206-266` — `test_summarize_does_not_block_pipeline` 的 mock 模式；`tests/test_tts.py` — TTS mock 模式

**Test scenarios**:
- 并发调度正确性：验证并发翻译的输出与串行翻译相同（内容一致）
- 重试隔离：1 个批次失败、2 个成功，验证最终结果包含 2 个成功段 + 1 个失败 error log
- 限流器速率：设置 rate=5 RPM，连续发 10 个请求，验证耗时 ≥ ~12 秒（10 请求 ÷ 5 RPM）
- 取消响应延迟：翻译中途触发取消，验证 < 2 秒返回
- 兼容包装：sync translate() 返回与 async translate_async() 相同的文本内容
- 端到端：完整管道走通，生成 MP3 输出

**Verification**: 所有测试通过 `python3 -m pytest tests/ -q`。

---

## Verification Contract

| Requirement | Verification |
|---|---|
| R1 — AsyncOpenAI 并发 | mock 验证 asyncio.gather 同时发出 N 个 API 调用 |
| R2 — Semaphore 阈值 | 设置 concurrency=2，验证最多 2 个同时进行 |
| R3 — 独立重试 | 注入部分失败，验证只重试失败的、成功的保留 |
| R4 — 非阻塞退避 | 验证 asyncio.sleep 被调用且不阻塞其他批次 |
| R5 — jitter | 验证两次 retry 的 sleep 时长不同（随机） |
| R6-R7 — 限流器 | 单元测试验证 token-bucket 速率控制 |
| R8 — process async | `await process(...)` 正常完成 |
| R9 — 移除 TPE | server.py 中不再有 ThreadPoolExecutor 包装 process |
| R10 — asyncio.Event 取消 | 取消后 < 2 秒返回 cancelled |
| R11-R12 — TTS/merge async | `await synthesize/merge(...)` 正常返回 |
| R13-R14 — 向后兼容 | 返回格式和结构不变 |

## Definition of Done

- [ ] U1 完成：async 翻译路径在 `client.py` 中实现，Semaphore + TokenBucket + 非阻塞重试生效
- [ ] U2 完成：`process()` 改为 async，阻塞阶段走 to_thread，取消机制适配 asyncio
- [ ] U3 完成：server.py 移除 ThreadPoolExecutor，SSE 直接 await process
- [ ] U4 完成：synthesize/merge 签名改为 async def
- [ ] U5 完成：现有测试适配 async，新增并发/重试/限流/取消测试
- [ ] 翻译阶段实测加速：40 段视频从 ~4s 降至 < 1s（API 调用耗时）
- [ ] 现有测试套件全部通过（`python3 -m pytest tests/ -q`）
- [ ] 手动提交一个 YouTube 视频，确认翻译阶段速度明显提升
