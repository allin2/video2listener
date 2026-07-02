---
title: Pipeline Visualization - Plan
type: feat
date: 2026-07-02
topic: pipeline-visualization
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Pipeline Visualization - Plan

## Goal Capsule

Replace the current text-log polling UI with an SSE-driven vertical timeline that visualizes the 7 pipeline stages in real time. Each stage node shows status (pending/active/done/failed/duration). Clicking a node expands its log; active stages expose a cancel action; failed stages expose a stage-level retry action. On completion the browser delivers a system notification.

- **Product authority:** STRATEGY.md — 「产品体验」track, reduce daily-use friction
- **Open blockers:** none

## Product Contract

### Summary

An SSE endpoint streams structured stage events to the frontend, driving a vertical timeline with 7 stage nodes. The timeline replaces the `progressCard` text log. Stages auto-advance with real-time status changes. The user can cancel an in-progress task (preserving work through the text-cleaning stage) or retry from a failed stage. A 2-second polling fallback preserves compatibility. On completion, a Web Notification alerts the user even if the tab is backgrounded.

### Problem Frame

Today's UI shows a raw text log refreshed every 2 seconds. The user cannot tell which stage is running, how long each stage took, or how many stages remain. A failed pipeline discards all progress — the user sees one error message with no recovery path. The only way to stop a running task is to kill the server process. These frictions make daily use feel fragile.

### Key Decisions

**K1. SSE as primary transport, polling as fallback.** SSE (EventSource) provides sub-second progress granularity with automatic reconnection. A 2-second polling fallback activates when EventSource is unavailable, reusing the current `api_status` endpoint unchanged.

**K2. Vertical timeline layout over horizontal stepper.** 7 stages with Chinese labels plus durations exceed comfortable horizontal width. The vertical layout also integrates stage-specific logs directly below each node (expand-on-click).

**K3. Cancel preserves through text-cleaning.** When the user cancels, the pipeline stops at the next stage boundary and preserves downloaded video + transcript + cleaned text on disk. Resubmission detects completed stages via `check_stage_file` and resumes from translation.

**K4. 7 user-visible stages mapped from internal states.** The internal `TaskStatus` enum has 6 values; user-facing stages (download, transcribe, clean, translate, TTS, merge) are derived from recognizable progress messages. A lightweight mapping layer in the server bridges internal states to UI stages — when the internal state changes, the server emits the corresponding `stage_change` SSE event.

### Requirements

**SSE Transport**

R1. `GET /api/status/{video_id}/stream` streams `text/event-stream` with the following event types: `stage_change`, `stage_progress`, `log`, `pipeline_complete`, `pipeline_error`. See Key Flows for the wire protocol.

R2. The SSE stream begins on first progress event — not at connection open — so the frontend is ready to receive before painting any stage indicator.

R3. When EventSource is unavailable (detected via `typeof EventSource === 'undefined'`), the frontend falls back to 2-second polling on `GET /api/status/{video_id}` without degrading the visual timeline.

**Frontend Pipeline View**

R4. The `progressCard` is replaced by a vertical timeline of 7 stage nodes. Each node displays: a status dot (grey/blue/green/red), a stage number, a stage name in Chinese, and — when complete — elapsed duration. An animated spinner accompanies the active stage.

R5. Clicking a completed or active stage node expands an inline log panel showing that stage's messages. Expanding a different node collapses the previously expanded one. Active-stage logs auto-scroll to the latest entry.

R6. When a stage is active, an inline **取消** (cancel) button appears below its log panel, styled as a secondary/destructive action. When a stage has failed, an inline **从此阶段重试** (retry from this stage) button appears.

R7. The browser tab title updates during processing: `[●●●] video2listener` → `[✓] Video Title · video2listener` on completion → `[✗] Video Title · video2listener` on failure.

**Cancel**

R8. `POST /api/cancel/{video_id}` sets a `threading.Event` flag. The pipeline checks the flag at each stage boundary (before metadata fetch, before translation, before each TTS segment). On detection, it sets the DB status to `cancelled`, removes in-progress artifacts, and emits `pipeline_cancelled` via SSE.

R9. Cancelled tasks preserve: downloaded video, `metadata.json`, `transcript_raw.json`, and `transcript_clean.txt`. Translation artifacts (`script_zh.txt`, `summary.json`), TTS segments, and output MP3s are removed.

**Stage-Level Retry**

R10. When a stage fails, the retry button skips completed stages (detected via `check_stage_file`) and re-runs from the failed stage. The retry reuses the same `POST /api/process` endpoint with an added `resume_from` parameter naming the failed stage.

**Browser Notification**

R11. On `pipeline_complete`, if `Notification.permission === 'granted'`, the frontend fires a desktop notification: "《视频名》已生成，点击下载" with `tag: videoId` to deduplicate. Clicking the notification focuses the tab and scrolls to the download button.

R12. If permission is `'default'` (never asked), the frontend requests it once when the user clicks "开始处理". A denial is silent — the tab title alone conveys status.

### Key Flows

**F1. Normal pipeline run (happy path)**

```
User clicks 开始处理
  → POST /api/process → 202 { video_id }
  → Frontend opens EventSource to /api/status/{video_id}/stream
  → Tab title → "[●●●] video2listener"

Server emits:
  event: stage_change   data: {"stage":1,"name":"下载","status":"active"}
  event: stage_change   data: {"stage":1,"name":"下载","status":"done","duration_s":134}
  event: stage_change   data: {"stage":2,"name":"转写","status":"active"}
  event: log            data: {"stage":2,"message":"Whisper small · 正在转写..."}
  event: stage_progress data: {"stage":2,"pct":45,"message":"已转写 18/42 分钟"}
  event: stage_change   data: {"stage":2,"name":"转写","status":"done","duration_s":512}
  ... (stages 3-7) ...
  event: pipeline_complete data: {"video_id":"abc","output_path":"data/Title/Title.mp3"}

Frontend:
  → Tab title → "[✓] Title · video2listener"
  → System notification fires
  → Download button appears
```

**F2. Cancel during translation**

```
User clicks 取消 on active "翻译" stage
  → POST /api/cancel/videoId
  → Server sets cancel_flag.set()
  → Pipeline checks flag, cleans translation+TTS artifacts
  → DB status → "cancelled"
  → SSE emits: event: pipeline_cancelled data: {"resumable_from":"translated"}
  → Timeline shows: stages 1-3 green ✓, stage 4 grey with "已取消" label
  → Tab title → "[✗] video2listener"
```

**F3. Retry from failed stage**

```
TTS stage fails at segment 18/39
  → SSE emits: event: pipeline_error data: {"stage":5,"message":"TTS 合成失败 (segment 18): ..."}
  → Stage 5 node turns red, log shows error detail
  → Retry button visible

User clicks 从此阶段重试
  → POST /api/process { url, mode, force: false, resume_from: "tts" }
  → Pipeline checks check_stage_file → stages 1-4 already done → skips
  → Stage 5 re-runs → SSE picks up from there
```

### Scope Boundaries

Deferred to follow-up plans (from `docs/ideation/2026-07-02-product-experience-ideation.html`):
- #2 一键从失败阶段恢复 — partially covered by R10; full recovery UX is a separate plan
- #3 自定义确认弹窗 + 直接下载 — separate plan (submit-time UX)
- #5 多标签任务历史面板 — separate plan
- #6 1Password 风格密钥管理 — separate plan
- #7 提交前客户端校验 — separate plan

Deferred for later:
- Multi-video queue
- Per-stage timing analytics dashboard
- Browser notification with inline audio player

Outside this product's identity:
- Mobile push notifications
- Email alerts on completion

### Dependencies / Assumptions

- The existing `on_progress` callback signature is stable; SSE event emission wraps it without changing pipeline module code.
- `threading.Event` is used for the cancel flag (same process, no cross-process IPC needed).
- System notification permission request happens contextually (on first submit), not on page load.
- The 7-stage mapping from internal `TaskStatus` states to user-facing labels is hand-authored; no ML/auto-classification.

### Success Criteria

1. The user can identify the current stage and completed stages at a glance without reading log text.
2. A backgrounded tab conveys completion status via title alone; system notification fires without the user needing to poll.
3. Cancelling a running task does not require killing the server process, and resubmission skips already-completed work.

### Sources / Research

- Grounding dossier: `/tmp/compound-engineering/ce-brainstorm/b7e3a1d2/grounding.md` (codebase scan of current polling, progress, and state mechanisms)
- Web research: SSE as consensus transport for pipeline progress (MDN, dev.to surveys); GitHub Actions workflow visualization as UI reference; 7-state job lifecycle model (idle/pending/in_progress/partial_ready/completed/failed/cancelled) from NNGroup, Fluent 2, PatternFly
- Ideation: `docs/ideation/2026-07-02-product-experience-ideation.html` — Idea #1 (管道可视化控制台) ranked top
- Visual probe: `/tmp/compound-engineering/ce-brainstorm-visual/b7e3a1d2/screens/001-pipeline-layout.html` — Vertical timeline (Variant A) selected

---

## Planning Contract

### Key Technical Decisions

**KTD1. SSE via FastAPI StreamingResponse + asyncio.Queue bridge.** The pipeline runs in a `threading.Thread`; SSE endpoints are async. An `asyncio.Queue` per task receives events from the sync thread via `asyncio.run_coroutine_threadsafe()`. The SSE endpoint awaits the queue and yields `text/event-stream` chunks. This is the standard FastAPI SSE pattern — see Starlette's `StreamingResponse` documentation.

**KTD2. Stage detection wraps existing progress() calls; pipeline module unchanged.** The orchestrator already emits distinct progress messages at stage boundaries (`"获取视频信息..."`, `"清洗文本..."`, `"翻译完成"`, etc.). The server's `_append_progress` is augmented to detect these messages via a mapping dict and emit typed SSE events. The pipeline module (`orchestrator.py`) is unchanged — no new callback signature, no import changes.

**KTD3. Cancel flag lives in `_task_states` dict alongside existing fields.** Each task entry gains a `cancel_event: threading.Event`. The `POST /api/cancel` endpoint sets it. The pipeline thread checks `cancel_event.is_set()` at defined boundaries. This keeps cancel state in the same thread-safe dict already protected by `_task_lock`.

**KTD4. 7 user-facing stages map from internal pipeline progress, not from TaskStatus.** The `TaskStatus` enum has 6 values that serve checkpoint/resume logic. The 7 UI stages (下载, 转写, 清洗, 翻译, TTS, 合并) are derived from the progress messages the pipeline emits. A `STAGE_MAP` constant in `server.py` maps message pattern → `{stage_id, name, icon}`. This avoids coupling the UI to internal state management.

**KTD5. Polling fallback reuses existing `/api/status` endpoint.** When `EventSource` is unavailable, the frontend falls back to the existing 2-second polling loop. The `/api/status` response is augmented with a `stages` array (derived from progress_messages) so the same visual timeline can render from polled data — with degraded real-time granularity but full functional parity.

### High-Level Technical Design

**SSE Event Wire Protocol**

```
event: stage_change
data: {"stage_id":1,"name":"下载","status":"active"}

event: stage_change
data: {"stage_id":1,"name":"下载","status":"done","duration_s":134,"meta":"1080p · 408 MB"}

event: stage_progress
data: {"stage_id":4,"pct":60,"message":"翻译中... 9/15 段"}

event: log
data: {"stage_id":4,"message":"分段 8/15 完成 <- DeepSeek"}

event: pipeline_complete
data: {"video_id":"7HM-rptYdTs","output_path":"data/Title/Title.mp3","title":"Video Title"}

event: pipeline_error
data: {"stage_id":5,"message":"TTS 合成失败 (segment 18): ..."}

event: pipeline_cancelled
data: {"resumable_from":"translated"}
```

**Thread-to-Async Bridge Architecture**

```
┌──────────────────────────┐     asyncio.Queue     ┌────────────────────┐
│  Pipeline (sync thread)  │ ──────────────────→   │  SSE endpoint      │
│                          │  put_nowait(event)    │  (async generator) │
│  _append_progress()      │                       │  await queue.get() │
│    → emit_sse_event() ───┤                       │  yield f"event:…" │
└──────────────────────────┘                       └────────────────────┘
     ↑ cancel_event check
     │
  orchestrator.py
  (stage boundary checks)
```

**Stage Detection Mapping** (`server.py` additions)

```python
STAGE_MAP = [
    # (trigger_message_fragment, stage_id, name, icon)
    ("获取视频信息",    1, "下载",  "📥"),
    ("清洗文本",        3, "清洗",  "🧹"),
    ("文本清洗完成",    3, "清洗",  "🧹"),  # done event
    ("开始翻译",        4, "翻译",  "🌐"),
    ("翻译完成",        4, "翻译",  "🌐"),  # done event
    ("TTS 文本清洗",    5, "TTS",   "🔊"),
    ("开始语音合成",    5, "TTS",   "🔊"),
    ("合并",           6, "合并",  "🎵"),
    ("MP3 已生成",      6, "合并",  "🎵"),  # done event
    ("处理完成",        7, "完成",  "✅"),
]
# Stage 2 (转写) is detected implicitly — when "获取视频信息" completes
# and metadata.json exists, the orchestrator emits progress("无字幕，开始语音转写...") or similar
```

**Frontend State Machine Extension**

The current 4-card model (`formCard` / `progressCard` / `resultCard` / `errorCard`) is preserved. The `progressCard` content is replaced: the `<div id="progressLog">` is swapped for a `<div id="pipelineTimeline">` that renders 7 `.stage-node` elements. The existing `startProgressUI()` / `updateUI()` / `showError()` functions are modified to drive the new UI.

### Assumptions

- Starlette/FastAPI `StreamingResponse` handles SSE Content-Type correctly; this is a standard feature documented in FastAPI's streaming responses guide
- The sync pipeline thread's `cancel_event` check interval (per-stage and per-TTS-segment) is sufficient — cancel latency is at most one TTS segment, not sub-second
- Browser Notification API is available in all modern browsers the user would realistically use (Safari 16+, Chrome 86+, Firefox 99+)
- The 7-stage mapping table is maintained as part of this plan; if future pipeline changes add/remove stages, the mapping table is the single place to update

### Sequencing

U1 → U2 → U3-U5 (parallel) → U6

U1 (SSE backend) must ship first — all frontend work depends on the event stream. U2 (cancel) is independent of U1 but depends on the same `_task_states` structure. U3-U5 can be developed in parallel once U1 lands. U6 (notifications) is the smallest unit and can land anytime after U4.

---

## Implementation Units

### U1. Backend: SSE endpoint with staged event emission

**Goal:** Add `GET /api/status/{video_id}/stream` that streams typed SSE events. Augment `_append_progress` to emit `stage_change`, `stage_progress`, `log`, `pipeline_complete`, and `pipeline_error` events based on a stage-detection mapping.

**Requirements:** R1, R2, R3, KTD1, KTD2, KTD4

**Dependencies:** none

**Files:**
- `src/web/server.py` — add SSE endpoint, `STAGE_MAP`, `emit_sse_event()`, modify `_append_progress`, add `asyncio.Queue` to `_task_states` entries
- `src/pipeline/orchestrator.py` — add explicit stage-transition progress messages where currently only logger emits (e.g., whisper transcription start/end) so all 7 stages are detectable

**Approach:**
1. Add `STAGE_MAP` constant mapping progress-message substrings to `{stage_id, name, icon, event_type}`
2. In `_set_task_state`, initialize `sse_queue: asyncio.Queue()` and `sse_loop: asyncio.AbstractEventLoop` per task
3. `_append_progress` matches the message against `STAGE_MAP`; on match, pushes a structured event dict to the asyncio queue via `loop.call_soon_threadsafe(queue.put_nowait, event)`
4. SSE endpoint: `StreamingResponse` with `media_type="text/event-stream"`, async generator polls queue, formats SSE wire protocol, yields chunks
5. `pipeline_complete` and `pipeline_error` events emitted at the same points where status transitions to done/failed (`server.py:242-256`)
6. Augment `/api/status` polling response with derived `stages` array so polling fallback renders the same timeline

**Patterns to follow:**
- FastAPI SSE: Starlette `StreamingResponse` with async generator (standard pattern — no new dependencies)
- Thread-safe event bridge: `loop.call_soon_threadsafe()` for async queue push (standard asyncio pattern)
- Stage detection: message-substring matching — same pattern as existing `current_stage` tracking in `_append_progress`

**Test scenarios:**
- Happy path: Submit task → SSE streams 7 `stage_change` events (active then done for each) → `pipeline_complete` with output_path
- `stage_progress` events fire during long-running stages (transcription percentage, translation segment count)
- `log` events carry non-stage-specific progress messages
- `pipeline_error` fires with stage_id when pipeline fails mid-stage
- SSE stream closes cleanly on `pipeline_complete` (no dangling connection)
- Second browser tab opens SSE to same video_id → receives events (queue is per-task, not per-connection — fan-out or single-consumer design needed)
- Polling fallback: `/api/status` returns `stages` array matching timeline structure
- Task not found → SSE endpoint returns 404

**Verification:** Start a pipeline from the web UI, curl the SSE endpoint in another terminal, observe typed events arriving in real time with correct stage transitions.

### U2. Backend: Cancel mechanism

**Goal:** Add `POST /api/cancel/{video_id}` that sets a `threading.Event` flag. The orchestrator checks the flag at stage boundaries and cleans up post-translation artifacts before emitting `pipeline_cancelled`.

**Requirements:** R8, R9, KTD3

**Dependencies:** U1 (shares `_task_states` structure)

**Files:**
- `src/web/server.py` — add `/api/cancel` endpoint, add `cancel_event` to `_set_task_state` init
- `src/pipeline/orchestrator.py` — add `cancel_event` parameter to `process()`, check at boundaries: before metadata fetch, before translation, before each TTS segment loop iteration
- `src/pipeline/state.py` — add `CANCELLED = "cancelled"` to `TaskStatus`

**Approach:**
1. `_set_task_state` initializes `cancel_event = threading.Event()` per task
2. `POST /api/cancel/{video_id}` → sets `cancel_event.set()` in `_task_states`, returns 200
3. Pass `cancel_event` through `process()` → `_process_impl()` (new parameter, default `None`)
4. Each stage boundary check: `if cancel_event and cancel_event.is_set(): return pipeline_cancelled`
5. On cancel detection: delete post-translation files (matches existing force-cleanup logic at `orchestrator.py:176-190`), set DB status to `cancelled`, push `pipeline_cancelled` SSE event, return
6. Frontend receives `pipeline_cancelled` → timeline shows completed stages green, active stage grey with "已取消"

**Patterns to follow:**
- Cancel check: same pattern as `_active_tasks` duplicate guard (`orchestrator.py:92-102`)
- Artifact cleanup: reuse existing force-cleanup delete logic, scoped to post-translation files only
- DB status: `update_status(video_id, "cancelled")` pattern from existing status transitions

**Test scenarios:**
- Cancel during translation → stages 1-3 green, stage 4 grey "已取消", `transcript_clean.txt` preserved
- Cancel during TTS → stages 1-4 green, stage 5 grey, existing TTS segments cleaned up
- Cancel before any stage → all stages grey, no files left behind (metadata not yet fetched)
- Cancel after pipeline already completed → 409 or no-op (task not in active state)
- Cancel non-existent task → 404
- Resubmit cancelled task → pipeline detects `check_stage_file` progress, resumes from next incomplete stage

**Verification:** Submit a long video, cancel during translation stage, verify `script_zh.txt` does not exist but `transcript_clean.txt` does. Resubmit — pipeline skips download+transcription.

### U3. Frontend: SSE client + vertical timeline UI

**Goal:** Replace the text `progressLog` in `progressCard` with a vertical timeline of 7 stage nodes driven by SSE events. Implement EventSource client with polling fallback. Each node shows status dot + name + duration. Click expands its log panel.

**Requirements:** R3, R4, R5, K2, KTD5

**Dependencies:** U1 (SSE endpoint must exist)

**Files:**
- `src/web/static/index.html` — replace `progressCard` content, add timeline CSS, add `PipelineTimeline` JS class, modify `startProgressUI()` / `updateUI()` / `showError()`
- `src/web/server.py` — augment `/api/status` with `stages` array for polling fallback

**Approach:**
1. Add CSS for vertical timeline: `.pipeline-timeline`, `.stage-node`, `.stage-dot` (grey/blue/green/red states), `.stage-line` (connector), `.stage-info`, `.stage-log` (expandable panel), `@keyframes pulse` for active dot animation
2. `PipelineTimeline` class: initializes 7 nodes with `data-stage-id` attributes, all in `pending` state. Methods: `setActive(id)`, `setDone(id, duration, meta)`, `setFailed(id, message)`, `setCancelled(id)`
3. SSE client: `new EventSource('/api/status/{videoId}/stream')`, `addEventListener` for each event type, delegating to `PipelineTimeline` methods
4. Fallback detection: `typeof EventSource === 'undefined'` → fall back to 2s polling, parse `stages` array from `/api/status` response, update timeline on each poll
5. Click handler: toggle `.stage-log` visibility on the clicked node, collapse others. Active-stage log auto-scrolls via `scrollTop = scrollHeight`
6. Existing `updateUI()` for `status==='done'` transitions to `resultCard` — timeline is already in done state from SSE events
7. Existing `showError()` for `status==='failed'` transitions to `errorCard` — timeline shows the failed stage in red

**Patterns to follow:**
- CSS state machine: `.pending` / `.active` / `.done` / `.failed` class toggling — same pattern as existing `.hidden` toggles on cards
- Elapsed timer: reuse existing `formatElapsed()` function for stage durations
- Expand-on-click: same pattern as existing `returnToForm` / `startProgressUI` UI state management

**Test scenarios:**
- Empty timeline renders 7 grey nodes on page load (before submit)
- SSE `stage_change` active → target node turns blue with pulse animation, connector line below turns blue
- SSE `stage_change` done → node turns green with checkmark, duration displayed
- Sequence: stages 1→2→3→4→5→6→7 all transition correctly
- Click stage 3 → log panel expands below stage 3. Click stage 5 → stage 3 collapses, stage 5 expands
- Page refresh during processing → timeline rebuilt from `/api/status` response (sessionStorage recovery path)
- EventSource falls back to polling when unavailable → timeline still functional
- SSE reconnection on transient network error → timeline catches up (EventSource auto-reconnect)
- Long-running stage with `stage_progress` events → percentage or item count updates inside the active node

**Verification:** Open the UI, submit a real pipeline. Watch the vertical timeline animate through stages. Click a completed stage to verify its log expands.

### U4. Backend + Frontend: Stage-level retry

**Goal:** When a stage fails, provide a "从此阶段重试" button that re-submits the task with `resume_from` and skips already-completed stages.

**Requirements:** R10

**Dependencies:** U1 (SSE events must be streamable on retry), U2 (cancel shares the same cleanup/resume logic)

**Files:**
- `src/web/server.py` — parse `resume_from` in `POST /api/process`, pass to orchestrator
- `src/pipeline/orchestrator.py` — accept `resume_from: str | None` in `process()`, skip stages before `resume_from` using `_should_run` and `check_stage_file`
- `src/web/static/index.html` — add retry button handler in `PipelineTimeline`

**Approach:**
1. `resume_from` maps to the internal `TaskStatus` closest to the UI stage: `"translate"` → `TaskStatus.TEXT_READY`, `"tts"` → `TaskStatus.TRANSLATED`, `"merge"` → `TaskStatus.TTS_DONE`
2. Orchestrator: when `resume_from` is set, force `current_status` to the corresponding internal state, so `_should_run` naturally skips earlier stages
3. Frontend: on `pipeline_error` event, `PipelineTimeline.setFailed(stageId, message)` renders the stage red and shows a retry button below the log
4. Retry click → POST to `/api/process` with original params + `resume_from: "translated"` (the last successfully completed stage's internal name) → opens new SSE stream → timeline resets for the retry

**Patterns to follow:**
- `resume_from` mapping reverses the stage-detection mapping from U1 — same key-value shape inverted
- `force` mode already resets DB and skips stages via file-system checkpoints — `resume_from` is a narrower variant

**Test scenarios:**
- TTS fails at segment 18 → retry button appears under stage 5 → click → new task starts from TTS stage (stages 1-4 already green on resubmit)
- Translate fails → retry from translate → transcription preserved, translation re-runs
- Retry with no `resume_from` → normal pipeline run (all stages)
- Invalid `resume_from` value → server returns 400 with clear error

**Verification:** Trigger a pipeline error (e.g., disconnect proxy mid-TTS), click retry, verify the new SSE stream starts from the correct stage.

### U5. Frontend: Cancel button in active stage

**Goal:** Show an inline "取消" button below the currently active stage's log panel. On click, POST to `/api/cancel/{videoId}` and update the timeline to cancelled state.

**Requirements:** R6 (cancel button part), R8, R9

**Dependencies:** U1 (SSE), U2 (backend cancel), U3 (timeline UI)

**Files:**
- `src/web/static/index.html` — add cancel button to `PipelineTimeline`, wire click handler, handle `pipeline_cancelled` SSE event

**Approach:**
1. `PipelineTimeline.setActive(id)` → render cancel button below the stage's log panel with CSS class `stage-cancel-btn`
2. Cancel click handler → `fetch('POST', '/api/cancel/' + videoId)` → on 200, disable the button (show "取消中..." text)
3. On `pipeline_cancelled` SSE event → `PipelineTimeline.setCancelled(event.resumableFrom)` → grey out the cancelled stage and all subsequent stages, show "已取消" label
4. Cancel button styling: secondary/destructive — subtle red outline, positioned inline below the log, not a full-width primary button

**Patterns to follow:**
- Button disable-after-click: same pattern as `submitBtn.disabled = true` in `submitForm` handler
- Inline action placement: same pattern as download button in `resultCard`

**Test scenarios:**
- Cancel during active stage → button changes to "取消中...", SSE `pipeline_cancelled` arrives, timeline updates
- Click cancel twice rapidly → second click is no-op (button disabled)
- Cancel endpoint returns 404 → show error toast, re-enable button

**Verification:** Submit a video, wait for a multi-minute stage (translation/TTS), click cancel, verify the button disables and the timeline eventually shows cancelled.

### U6. Frontend: Browser notifications + tab title

**Goal:** Update browser tab title during processing. Fire desktop notification on pipeline completion. Request notification permission contextually on first submit.

**Requirements:** R7, R11, R12

**Dependencies:** U1 (pipeline_complete event), U3 (timeline and submit flow)

**Files:**
- `src/web/static/index.html` — modify `startProgressUI`, add `requestNotificationPermission()`, `fireCompletionNotification()`, tab title updates

**Approach:**
1. Tab title: on `startProgressUI()` → `document.title = "[●●●] video2listener"`. On `pipeline_complete` → `document.title = "[✓] Title · video2listener"`. On error/cancel → `document.title = "[✗] video2listener"`
2. Notification permission: in submit handler, after successful POST, `Notification.requestPermission()` — only if `Notification.permission === 'default'`. On `'denied'`, skip silently
3. Notification: on `pipeline_complete`, if permission is `'granted'`, `new Notification("视频已生成", { body: "《Title》已生成，点击下载", tag: videoId, icon: null })`. Click handler: `window.focus()` + scroll to download
4. Deduplication: `tag: videoId` ensures duplicate Notifications for the same video are coalesced

**Patterns to follow:**
- Tab title mutation: same pattern as `returnToForm` which resets page state
- Notification: MDN Web Notification API — standard, no dependencies

**Test scenarios:**
- First submit ever → permission prompt appears after clicking "开始处理"
- Permission granted → notification fires on completion, clicking it focuses the tab
- Permission denied → notification never fires, tab title still updates
- Permission previously granted → no prompt, notification fires directly
- Tab in background → notification fires, user sees system notification
- Same video completed twice → notification deduplicated by tag
- Completion while tab is focused → notification still fires (browser default behavior)

**Verification:** Submit a video, deny notification permission on first prompt, verify tab title cycles through `[●●●]` → `[✓]`. On second submit, no repeated permission prompt. In a fresh profile, accept permission, verify notification appears on completion.

---

## Verification Contract

### Test Commands

- Run the server: `python3 -m uvicorn src.web.server:app --host 127.0.0.1 --port 8080`
- Manual SSE verification: `curl -N http://127.0.0.1:8080/api/status/{video_id}/stream`
- Cancel verification: `curl -X POST http://127.0.0.1:8080/api/cancel/{video_id}`
- Existing test suite (if any): `python3 -m pytest tests/ -v`

### Quality Gates

- **SSE stream integrity:** No dropped events across 3 consecutive pipeline runs. Events arrive in stage_id ascending order.
- **Polling fallback:** Disabling EventSource in browser DevTools triggers automatic polling fallback within 2 seconds.
- **Cancel latency:** Cancel request → `pipeline_cancelled` event emitted within (1 TTS segment duration + 2 seconds).
- **Retry correctness:** Retry from a failed stage produces output identical to a clean run from that stage onward.
- **Notification dedup:** Two rapid completion events with the same videoId produce at most one visible notification.
- **No regression:** Existing submission, polling, download, and force-retry flows still work unchanged.

### Edge Cases

- SSE stream opened while task is mid-pipeline (not from start) → emits current stage as first event, then continues normally
- Browser tab closed during SSE → server detects broken connection on next queue push, cleans up dangling queue
- Simultaneous cancel + normal pipeline completion race → cancel wins if it arrives before `pipeline_complete` is emitted
- Extremely fast stage (text cleaning, <1 second) → still emits both `active` and `done` events; timeline shows sub-second transition

---

## Definition of Done

### Global

- [ ] SSE endpoint streams typed events for all 7 stages
- [ ] Vertical timeline renders in `progressCard` with correct state transitions
- [ ] Cancel button interrupts pipeline and preserves pre-translation work
- [ ] Retry button re-submits from failed stage
- [ ] Polling fallback activates when EventSource unavailable
- [ ] Tab title updates through processing lifecycle
- [ ] System notification fires on completion (permission-granted path)
- [ ] All existing flows (submit, download, force-retry) work without regression
- [ ] Product Contract unchanged

### Per-Unit

- [ ] U1 — curl can stream SSE events for a complete pipeline run
- [ ] U2 — cancel during long-running stage preserves transcript_clean.txt
- [ ] U3 — timeline visually transitions 7 stages; click expands log
- [ ] U4 — retry from failed TTS stage produces complete output
- [ ] U5 — cancel button disables on click, timeline shows cancelled state
- [ ] U6 — notification fires on completion; tab title shows `[✓] Title`
