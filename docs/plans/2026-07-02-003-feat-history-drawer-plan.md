---
title: Task History Drawer - Plan
type: feat
date: 2026-07-02
topic: history-drawer
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Task History Drawer - Plan

## Goal Capsule

Add a slide-out side drawer listing all historical tasks (queryable from the DB), so the user can browse past results, re-download old MP3s, or clean up storage — without leaving the main submission flow.

- **Product authority:** STRATEGY.md — 「产品体验」track, make the tool a "持续使用的个人播客库" not a one-shot converter
- **Open blockers:** none

## Product Contract

### Summary

A "📋 历史" button in the header opens a right-side drawer. The drawer fetches `GET /api/tasks` to populate a table of past tasks: video title, mode label, status, date, file size, and actions (download / delete). Deleting a task removes its DB record and the downloaded WAV file, but preserves the intermediate transcript/translation artifacts so re-processing is cheap. Multi-part outputs present separate download links per part.

### Problem Frame

Today the user's only way to re-download a past MP3 is to resubmit the same URL and click "下载已有文件" in the duplicate modal — assuming they even remember which videos they've processed. There's no way to browse what's been done, reclaim disk space from abandoned tasks, or see output metadata at a glance. Each processed video is a one-and-done event, invisible after the page refreshes.

### Key Decisions

**K1. Side drawer over tab or inline panel.** The drawer preserves the current single-page flow: the form, progress timeline, and result card stay exactly as they are. Opening history is a secondary action that doesn't displace the primary submit→monitor→download workflow.

**K2. Delete removes DB record + WAV, preserves transcripts.** The WAV file (largest intermediate artifact, often 200-500MB) is deleted to free space. The transcript and translation text files (~50KB each) are kept so resubmission can skip download+transcribe+translate and jump straight to TTS.

**K3. Multi-part outputs show per-part download links.** When a long video (>60 min) produces `Title_Part1.mp3` + `Title_Part2.mp3`, the drawer shows separate download rows or a grouped entry with individual links, rather than hiding Part2+.

### Requirements

**Backend API**

R1. `GET /api/tasks` returns all episodes from the DB, ordered by `updated_at DESC`. Each entry includes: `video_id`, `title_original`, `mode`, `mode_label`, `status`, `duration_seconds`, `audio_zh_path`, `data_dir`, `updated_at`. For entries with an in-memory task state (currently queued/processing), merge the live status into the response.

R2. `DELETE /api/tasks/{video_id}` removes the DB record. It also deletes any `.wav` files in the task's `data_dir`. It leaves `metadata.json`, `transcript_clean.txt`, `transcript_raw.json`, `script_zh.txt`, `summary.json`, `tts_text.txt`, and `tts_segments/` untouched. Returns 200 on success, 404 if the task doesn't exist.

R3. The `GET /api/status/{video_id}` response already includes `output_path`; the history endpoint supplements this by also exposing `data_dir` so the frontend can check for multi-part files when presenting download options.

**Frontend Drawer**

R4. A "📋 历史" button sits in the header (next to the title). Clicking it opens a right-side drawer (320-400px wide) that slides in from the right with a semi-transparent overlay behind it.

R5. The drawer fetches `GET /api/tasks` on open and renders a scrollable list. Each row shows: title (truncated to 40 chars), mode badge (播客/忠实/精华), status dot (green done / red failed / grey cancelled), date (YYYY-MM-DD), and action buttons (download icon, delete icon).

R6. Clicking a row expands it to show metadata: full title, channel, duration, file size (if MP3 exists on disk). The download button triggers the existing `/api/download/{video_id}` endpoint. If the task's `data_dir` contains multiple `*_Part*.mp3` files, all are listed as separate download links.

R7. The delete button shows a brief inline confirmation ("确认删除？") before calling `DELETE /api/tasks/{video_id}`. On success, the row is removed from the list with a brief fade-out animation. If the API call fails, an error toast appears.

R8. Closing the drawer (clicking overlay, pressing Escape, or clicking a close × button) does not refresh or lose the current page state. Re-opening the drawer re-fetches the task list to stay current.

### Key Flows

**F1. Browse and re-download**
```
User clicks "📋 历史" → drawer slides in → fetches /api/tasks → 12 tasks listed
→ User clicks on "AI 时代的学习方法" → row expands, shows meta + download button
→ Clicks download → browser downloads /api/download/abc123 → MP3 saved
```

**F2. Delete old task**
```
User clicks "📋 历史" → sees a failed task with 400MB WAV
→ Clicks delete icon → "确认删除？" inline prompt → clicks confirm
→ DELETE /api/tasks/abc123 → WAV deleted, record removed → row fades out
```

### Scope Boundaries

In scope:
- List all historical tasks from DB
- Download past MP3s (including multi-part)
- Delete tasks (DB record + WAV)

Deferred to follow-up plans (from ideation items):
- #6 密钥管理优化 — separate plan
- #7 提交前客户端校验 — separate plan
- Per-stage timing analytics — separate track

Out of scope for this plan:
- Filtering/searching the task list
- Bulk delete
- Resuming a cancelled/failed task from the drawer (that's U4's existing retry flow)
- Showing task progress in the drawer (drawer is for history; live tasks are on the main timeline)

### Success Criteria

1. The user can open the drawer and see every video they've ever processed, with video title and completion status at a glance.
2. Downloading an old MP3 takes two clicks: open drawer → click download.
3. Deleting a task clears the WAV and the list entry, freeing measurable disk space, without destroying hours of translation work.

---

## Planning Contract

### Key Technical Decisions

**KTD1. New DB query functions in `src/storage/db.py`, no schema change.** The `episode` table already has all needed columns. Add `get_all_episodes()` (SELECT * ORDER BY updated_at DESC) and `delete_episode(video_id)` (DELETE WHERE video_id = ?). Follow the existing pattern: each function gets its own connection, executes, commits, closes.

**KTD2. `GET /api/tasks` merges in-memory state for active tasks.** Iterate `_task_states` (under `_task_lock`) and overlay live status/mode/label onto matching DB rows. This keeps the response accurate for currently-running pipelines without adding DB writes.

**KTD3. Delete endpoint handles file cleanup server-side.** `DELETE /api/tasks/{video_id}` reads `data_dir` from the DB record, deletes `*.wav` files in that directory, removes the DB row. MP3s are NOT deleted (the user may want to download first). Intermediate text files are preserved per K2.

**KTD4. Multi-part detection via filesystem glob.** The DB stores only `audio_zh_path` (first part). The `GET /api/tasks` response supplements each task with a `parts` array by globbing `data_dir / *_Part*.mp3` plus the primary `.mp3`. The frontend renders one download link per part.

**KTD5. Drawer is pure CSS transition + vanilla JS, no library.** A fixed-position overlay + right panel with `transform: translateX()` slide animation. The drawer fetches on open and re-fetches each time. No virtual scrolling needed (typical usage is tens of tasks, not thousands).

### Implementation Units

### U1. DB layer: `get_all_episodes` + `delete_episode`

**Goal:** Add two query functions to `src/storage/db.py` so the API can list and delete tasks.

**Requirements:** R1, R2

**Dependencies:** none

**Files:**
- `src/storage/db.py` — add `get_all_episodes()`, `delete_episode()`

**Approach:**
1. `get_all_episodes()` — `SELECT * FROM episode ORDER BY updated_at DESC`, return list of dicts. Same pattern as `get_pending_episodes()`.
2. `delete_episode(video_id)` — `DELETE FROM episode WHERE video_id = ?`. Return True if a row was deleted, False otherwise.

**Patterns to follow:**
- `get_pending_episodes()` at `db.py:142-149` — same connection lifecycle, same `dict(r) for r in rows` pattern
- Connection: `_get_conn()`, commit, close — identical to every existing function

**Test scenarios:**
- `get_all_episodes()` with 0 episodes → empty list
- `get_all_episodes()` with 3 episodes → 3 dicts, newest first
- `delete_episode()` existing video_id → returns True, row gone from subsequent SELECT
- `delete_episode()` non-existent video_id → returns False

**Verification:** Call both functions from a Python shell — `get_all_episodes()` returns all rows ordered correctly; `delete_episode()` removes the target and only the target.

### U2. API: `GET /api/tasks` + `DELETE /api/tasks/{video_id}`

**Goal:** Two new endpoints in `src/web/server.py` — list all tasks (with live-state merge) and delete a task (with WAV cleanup).

**Requirements:** R1, R2, R3

**Dependencies:** U1 (DB functions must exist)

**Files:**
- `src/web/server.py` — add `GET /api/tasks`, `DELETE /api/tasks/{video_id}`

**Approach:**
1. `GET /api/tasks`:
   - Call `db.get_all_episodes()`
   - Iterate `_task_states` under `_task_lock`; for each in-memory task, overlay `status`, `mode_label`, `current_stage_id` onto the matching DB row (or append if not in DB yet)
   - For each task, compute `parts`: read `data_dir`, glob `*_Part*.mp3`, build download links. Primary MP3 (`audio_zh_path`) is part 1 if it exists.
   - Return JSON array
2. `DELETE /api/tasks/{video_id}`:
   - Call `db.get_episode(video_id)` → 404 if missing
   - Read `data_dir`, glob and delete `*.wav` files
   - Call `db.delete_episode(video_id)`
   - Return `{"video_id": video_id, "deleted": true}`
   - Refuse to delete if task is currently processing (status in `_task_states` is `processing`)

**Patterns to follow:**
- Existing endpoint structure at `server.py:142-266` (`api_process`) — `await request.json()` for body parsing, `JSONResponse` for errors
- `_task_lock` usage at `server.py:73-83` (`_get_task_state`) — same pattern for thread-safe iteration
- `Path.glob()` for multi-part detection — standard Python, no new dependencies

**Test scenarios:**
- `GET /api/tasks` with empty DB → `[]`
- `GET /api/tasks` with 3 completed episodes → 3 entries, each with `mode_label`, `status`, `parts`
- `GET /api/tasks` with 1 processing task → live status merged into the response
- `GET /api/tasks` with multi-part output → `parts` array lists all `_PartN.mp3` files
- `DELETE /api/tasks/{id}` existing → 200, WAV deleted, DB row removed
- `DELETE /api/tasks/{id}` while task is processing → 409 conflict
- `DELETE /api/tasks/{id}` non-existent → 404

**Verification:** `curl http://127.0.0.1:8080/api/tasks` returns JSON array. `curl -X DELETE http://127.0.0.1:8080/api/tasks/test123` returns 404.

### U3. Frontend: history drawer with task list, download, delete

**Goal:** Add a right-side slide-out drawer triggered by a "📋 历史" button in the header. The drawer lists all tasks, supports expand-for-details, download, and delete with inline confirmation.

**Requirements:** R4, R5, R6, R7, R8

**Dependencies:** U2 (API endpoints must exist)

**Files:**
- `src/web/static/index.html` — add drawer CSS, HTML, and JS

**Approach:**
1. **CSS** (~60 lines): `.history-drawer-overlay` (fixed, full-screen, semi-transparent bg), `.history-drawer` (fixed right, 360px wide, `translateX(100%)` → `translateX(0)` on `.open`), `.task-row` (flex row with truncated title, mode badge, status dot, action icons), `.task-row.expanded` (shows metadata panel), fade-out animation for delete.
2. **HTML**: Add "📋 历史" button in `<header>`. Add drawer overlay + panel divs after the existing cards. Panel contains a header ("📋 历史记录" + × close), a scrollable `<div id="taskList">`, and a footer with task count.
3. **JavaScript** (~100 lines):
   - `openHistoryDrawer()`: fetch `/api/tasks`, render rows, add `open` class → CSS transition slides it in
   - `closeHistoryDrawer()`: remove `open` class
   - `renderTaskList(tasks)`: build DOM — each row shows title (truncated 40ch), mode badge, status dot, date, download + delete icons
   - Click row → toggle `.expanded`, show metadata (channel, duration, file size)
   - Download icon → `window.location = /api/download/{video_id}`
   - Delete icon → show inline "确认删除？" with confirm/cancel mini-buttons → on confirm: `DELETE /api/tasks/{id}` → fade out row
   - Escape key and overlay click close the drawer
4. **File size**: compute client-side from `duration_seconds` (estimate ~1MB/min at 128kbps) plus actual file size if MP3 path exists and is reachable via a `HEAD` request. Fall back to estimated size.

**Patterns to follow:**
- Duplicate modal at `showDuplicateModal()` — same overlay + close-on-Escape pattern
- PipelineTimeline class at `class PipelineTimeline` — same vanilla JS DOM-building pattern
- Existing download flow: `window.location.href = '/api/download/' + videoId`

**Test scenarios:**
- Click "📋 历史" → drawer slides in, tasks listed, task count in footer
- Drawer empty state → "暂无历史记录" message
- Click row → expands with full title, channel, duration, file size estimate
- Click download icon → browser navigates to `/api/download/{video_id}`
- Click delete icon → inline confirmation appears → confirm → row fades out → count updates
- Click delete icon → inline confirmation appears → cancel → nothing happens
- Press Escape → drawer closes
- Click overlay → drawer closes
- Re-open drawer → fresh fetch (reflects any changes)

**Verification:** Open the UI, click "📋 历史". Verify past tasks appear. Download an old MP3. Delete a test task and confirm the row disappears.

---

## Verification Contract

### Test Commands

- List tasks: `curl http://127.0.0.1:8080/api/tasks`
- Delete task: `curl -X DELETE http://127.0.0.1:8080/api/tasks/{video_id}`

### Quality Gates

- **API correctness:** `GET /api/tasks` returns all episodes with correct `mode_label`, `status`, and `parts` arrays
- **Delete safety:** Active (processing) tasks are rejected with 409
- **Drawer responsiveness:** Open/close animation completes in <300ms
- **Multi-part accuracy:** Tasks with `_Part1.mp3` + `_Part2.mp3` show two separate download links
- **No regression:** Existing submit, timeline, cancel, retry, and download flows work unchanged

### Edge Cases

- Drawer opened while a task is mid-pipeline → that task shows "处理中" status from memory merge
- Task in DB has `data_dir` pointing to a deleted directory → skip file-size check, show estimated size only
- Very long title (>40 chars) → truncated with ellipsis in list view, full title in expanded view
- Rapid open/close → each open triggers a fresh fetch; previous in-flight request is ignored

---

## Definition of Done

### Global

- [ ] `GET /api/tasks` returns all historical tasks with live status merge
- [ ] `DELETE /api/tasks/{video_id}` removes DB record + WAV, preserves transcripts
- [ ] "📋 历史" button opens right-side drawer
- [ ] Drawer lists tasks with title, mode, status, date, download, delete
- [ ] Download and delete actions work end-to-end
- [ ] Multi-part MP3s show separate download links
- [ ] All existing flows (submit, timeline, cancel, retry) work without regression
- [ ] Product Contract unchanged

### Per-Unit

- [ ] U1 — `get_all_episodes()` and `delete_episode()` work correctly from Python shell
- [ ] U2 — `curl /api/tasks` returns JSON; `curl -X DELETE /api/tasks/{id}` cleans up correctly
- [ ] U3 — drawer slides in, tasks render, download/delete work, Escape closes
