---
title: API Key UX Improvements - Plan
type: feat
date: 2026-07-02
topic: key-ux
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# API Key UX Improvements - Plan

## Goal Capsule

Improve the security feel and usability of the API Key configuration area: show/hide toggle, one-click copy, prominent safety notice, contextual help tooltips, and a TTS voice preview button that plays a 5-second sample before committing to a pipeline run.

- **Product authority:** STRATEGY.md — 「产品体验」track, reduce daily-use friction
- **Open blockers:** none

## Product Contract

### Summary

Three UX improvements to the form card's LLM and TTS configuration sections: (1) password-field show/hide toggle + copy button on every API Key input, (2) a visually distinct security notice on the localStorage persistence area explaining keys never leave the browser, with a help tooltip on each key field, and (3) a "试听" button next to the TTS voice selector that calls a lightweight backend endpoint to synthesize and play a 5-second Chinese test phrase.

### Problem Frame

Today the user stares at masked password fields with no way to verify what they typed short of submitting and failing. The only security indicator is one line of gray 0.75rem text ("包含 API Key，仅保存在当前浏览器"). A first-time user has no way to know whether keys are sent anywhere besides the LLM/TTS API — the localStorage notice is easily missed. When picking a TTS voice, the user commits to a full pipeline run (5-30 minutes) before hearing what the voice actually sounds like.

### Key Decisions

**K1. Show/hide toggle per field, not global.** Each API Key field gets its own 👁️ icon button that toggles `type="password"` ↔ `type="text"`. A global toggle would accidentally reveal both keys at once.

**K2. Copy button uses Clipboard API with fallback.** `navigator.clipboard.writeText()` when available; fall back to `execCommand('copy')` on older browsers. A brief "已复制" feedback replaces the icon for 1.2 seconds.

**K3. Security notice as a colored banner, not more text.** Replace the current single-line gray hint with a light-amber info banner containing a lock icon + "密钥仅存储于此浏览器本地，不会上传至服务器" at the top of the config persistence area. This is visually distinct from the surrounding form text.

**K4. Help tooltip as a `title` attribute + clickable `?` icon.** A small `?` badge next to each API Key label opens a short inline explanation on click/tap. On desktop, `title` provides hover text as a secondary channel.

**K5. TTS preview via dedicated lightweight endpoint.** `POST /api/tts-preview` accepts a voice config (provider, voice, optional api_key/base_url), synthesizes a fixed short phrase ("你好，这是音色试听。"), and returns `audio/wav` inline. The frontend creates an `Audio` element and plays it. No file is persisted on disk — the WAV is generated in a temp directory and streamed back.

### Requirements

**API Key Field UX**

R1. Each password field (LLM API Key, TTS API Key) gains a 👁️ toggle button inside the input area that switches between masked and visible text. Default state is masked.

R2. Each API Key field gains a 📋 copy button that writes the key text to the clipboard. On success, the icon briefly changes to "✓ 已复制" for 1.2 seconds.

R3. A `?` icon sits next to each API Key label. On click/tap it shows a short inline tooltip: "此密钥仅用于调用 LLM/TTS API，不会上传至 video2listener 服务器。每次请求直接从您的浏览器发送至 API 提供商。"

**Security Notice**

R4. The `config-memory` area gains a visible amber info banner: "🔒 密钥仅存储于此浏览器本地，不会上传至服务器". It replaces the current single-line gray `memory-note` text. The banner is always visible when the config persistence checkbox is checked.

**TTS Voice Preview**

R5. A "🔊 试听" button appears next to the TTS voice selector. On click, it sends the current voice config to `POST /api/tts-preview`, receives a WAV audio response, and plays it via `new Audio(URL.createObjectURL(blob))`. The button shows "播放中…" while audio is playing.

R6. `POST /api/tts-preview` accepts `{provider, voice, api_key, base_url, model}`. It synthesizes the fixed phrase "你好，这是音色试听。" using the existing `src/tts/synthesizer.py` module — Edge TTS if no api_key is provided, MiMo if api_key is present. Returns the WAV via `FileResponse` with `media_type="audio/wav"`. Temp file deleted after response.

### Key Flows

**F1. Verify and copy API Key**
```
User types API Key → clicks 👁️ → sees full key → confirms it's correct
→ clicks 📋 → "✓ 已复制" → pastes into password manager
```

**F2. Preview TTS voice**
```
User selects "苏打 · 中文男声" → clicks "🔊 试听" → button shows "播放中…"
→ POST /api/tts-preview (当前 TTS 配置：有 API Key 则用 MiMo，无则用 Edge)
→ ~2-5 秒 → audio plays "你好，这是音色试听。" → button returns to "🔊 试听"
```

### Scope Boundaries

In scope:
- Show/hide toggle on LLM and TTS API Key fields
- Copy-to-clipboard on both fields
- Security notice banner on config persistence area
- Help tooltip on each API Key label
- TTS preview button + backend endpoint

Deferred to follow-up plans:
- #7 提交前客户端校验 — separate plan

Out of scope:
- Password strength indicator
- Auto-save to browser password manager (browser-native behavior, not controllable)

### Success Criteria

1. User can toggle any API Key field to verify what they typed, then copy it with one click.
2. The security notice is the first thing a user reads in the config persistence area — not buried in fine print.
3. User can audition a TTS voice in under 5 seconds without starting a full pipeline.

---

## Planning Contract

### Key Technical Decisions

**KTD1. Show/hide toggle as a button inside the input wrapper.** Each password field is wrapped in a `position: relative` container. The 👁️ button sits absolutely positioned inside the right edge of the input. Click toggles `input.type` between `"password"` and `"text"`. Pure vanilla JS, no library.

**KTD2. Clipboard API with `execCommand` fallback.** `navigator.clipboard.writeText()` is the primary path. If unavailable (older browsers, insecure context), fall back to creating a temporary `<textarea>`, selecting it, and calling `document.execCommand('copy')`. The "✓ 已复制" feedback uses a 1.2s `setTimeout` to restore the icon.

**KTD3. Security notice as an inline banner replacing `memory-note`.** The current `<p class="memory-note">` is replaced by a `<div class="security-banner">` with amber background, lock icon, and the privacy statement. It sits inside the existing `config-memory` div, keeping the checkbox + clear button layout unchanged.

**KTD4. Help tooltip on click, not hover (mobile-friendly).** A `<span class="help-icon">?</span>` next to each API Key label. On click it toggles a small absolutely-positioned tooltip bubble. Clicking elsewhere or pressing Escape dismisses it. On desktop the `title` attribute provides a hover fallback.

**KTD5. TTS preview reuses existing `synthesize()` with a temp directory.** `POST /api/tts-preview` creates a temp dir via `tempfile.mkdtemp()`, calls `synthesize(test_phrase, tmp_dir, tts_config=tts_config)`, returns the first WAV via `FileResponse`, and schedules cleanup of the temp dir after response. The test phrase is hardcoded as `"你好，这是音色试听。"` — short enough for fast synthesis, long enough to hear voice character.

### Implementation Units

### U1. Backend: `POST /api/tts-preview` endpoint

**Goal:** Add a lightweight endpoint that synthesizes a test phrase and returns the audio, so the frontend can play a TTS voice preview without starting a full pipeline.

**Requirements:** R5, R6

**Dependencies:** none

**Files:**
- `src/web/server.py` — add `POST /api/tts-preview`

**Approach:**
1. Accept `{provider, voice, api_key, base_url, model}` in the request body
2. Build `tts_config` dict: if `api_key` is present → `{"provider": "mimi", "api_key": ..., "base_url": ..., "model": ..., "voice": ...}`; else → `{"provider": "edge", "voice": voice or "zh-CN-XiaoxiaoNeural"}`
3. Create temp dir, write test phrase to `tts_text.txt` inside it
4. Call `synthesize(test_phrase, tmp_dir, tts_config=tts_config)` — this returns a list of WAV segment paths
5. Concatenate segments via ffmpeg if multiple, or return the single WAV directly
6. Return `FileResponse` with `media_type="audio/wav"`
7. Clean up temp dir in a background thread after response

**Patterns to follow:**
- `src/tts/synthesizer.py:synthesize()` — existing synthesis function, reuse as-is
- `src/web/server.py:api_models()` — same request/response pattern

**Test scenarios:**
- POST with no api_key → uses Edge TTS, returns valid WAV
- POST with valid MiMo api_key → uses MiMo, returns valid WAV
- POST with invalid provider → 400 error
- POST with empty body → 400 error
- Synthesizer throws → 502 with error message

**Verification:** `curl -X POST http://127.0.0.1:8080/api/tts-preview -H 'Content-Type: application/json' -d '{"provider":"edge","voice":"zh-CN-XiaoxiaoNeural"}' --output test.wav && file test.wav` shows valid WAV audio.

### U2. Frontend: API Key field UX (toggle, copy, tooltip)

**Goal:** Add show/hide toggle, copy-to-clipboard, and help tooltip to both API Key password fields.

**Requirements:** R1, R2, R3

**Dependencies:** none (pure frontend)

**Files:**
- `src/web/static/index.html` — CSS + HTML + JS for key field enhancements

**Approach:**
1. Wrap each `<input type="password">` in a `<div class="key-input-wrap">` (position: relative)
2. Add 👁️ toggle button (absolutely positioned, right side of input): `input.type = input.type === 'password' ? 'text' : 'password'`
3. Add 📋 copy button: read `input.value`, write to clipboard, show "✓" for 1.2s
4. Add `<span class="help-icon">?</span>` next to each `<label>`: on click, toggle a tooltip bubble; on outside click/Escape, dismiss
5. CSS for `.key-input-wrap`, `.toggle-btn`, `.copy-btn`, `.help-icon`, `.help-tooltip`

**Patterns to follow:**
- Existing input layout in `index.html` form fields
- `showDuplicateModal` — same overlay-dismiss-on-Escape pattern for tooltip

**Test scenarios:**
- Click 👁️ → password field reveals text → click again → masked
- Click 📋 → key copied to clipboard → "✓ 已复制" shown → icon restores after 1.2s
- Click ❓ → tooltip appears → click elsewhere → tooltip dismisses
- Tooltip text matches R3 specification
- Both LLM and TTS key fields have independent toggles

**Verification:** Open the UI, verify all three controls (toggle, copy, tooltip) work on both API Key fields.

### U3. Frontend: Security banner + TTS preview button

**Goal:** Replace the gray `memory-note` text with an amber security banner, and add a "🔊 试听" button next to the TTS voice selector.

**Requirements:** R4, R5

**Dependencies:** U1 (TTS preview endpoint), U2 (key field refactor should land first to avoid conflicts)

**Files:**
- `src/web/static/index.html` — CSS + HTML + JS for security banner and TTS preview

**Approach:**
1. **Security banner:** Replace `<p class="memory-note">` with `<div class="security-banner">🔒 密钥仅存储于此浏览器本地，不会上传至服务器</div>`. Style: amber background (`#fffbeb`), amber border (`#fcd34d`), dark amber text (`#92400e`), 0.85rem font.
2. **TTS preview button:** Add `<button class="stage-btn" id="previewTtsBtn">🔊 试听</button>` next to the TTS voice `<select>`. On click:
   - Read current TTS config from form fields
   - POST to `/api/tts-preview`
   - Receive WAV blob → `new Audio(URL.createObjectURL(blob))` → play
   - Button shows "播放中…" while audio is loading/playing
   - On audio `ended` → restore button text
   - On error → show "试听失败" for 2s

**Patterns to follow:**
- `fetchModelsBtn` click handler — same async fetch + loading state pattern
- Existing `config-memory` div — keep the checkbox + clear button layout

**Test scenarios:**
- Security banner visible when "记住配置" is checked
- Click "🔊 试听" with Edge TTS → audio plays Chinese test phrase
- Click "🔊 试听" with MiMo API key → audio plays with MiMo voice
- Click during playback → no double-play (button disabled during playback)
- TTS endpoint returns error → "试听失败" shown briefly

**Verification:** Open the UI. Security banner is visible and distinct from surrounding text. Select a voice, click 试听, hear the audio play.

---

## Verification Contract

### Test Commands

- TTS preview: `curl -X POST http://127.0.0.1:8080/api/tts-preview -H 'Content-Type: application/json' -d '{"provider":"edge","voice":"zh-CN-XiaoxiaoNeural"}' --output test.wav`
- UI verification: open `http://127.0.0.1:8080`, interact with toggle/copy/tooltip/preview

### Quality Gates

- **Toggle works independently:** LLM key toggle does not affect TTS key field and vice versa
- **Copy feedback:** "✓ 已复制" appears within 200ms and reverts after 1.2s
- **TTS preview latency:** Audio starts playing within 5 seconds for Edge TTS
- **No regression:** Existing form submit, config save/restore, model fetch, voice fetch all work unchanged

### Edge Cases

- Copy on older browser without Clipboard API → `execCommand` fallback works
- TTS preview with MiMo when API key is invalid → error returned, "试听失败" shown
- Rapid toggle clicks → no visual glitch, input type toggles correctly each time
- Very long API key (>100 chars) → copy captures full text, not truncated

---

## Definition of Done

### Global

- [ ] Both API Key fields have working show/hide toggle + copy button
- [ ] Help tooltip appears on both API Key labels
- [ ] Amber security banner visible in config persistence area
- [ ] TTS preview button synthesizes and plays audio
- [ ] All existing form functionality works without regression
- [ ] Product Contract unchanged

### Per-Unit

- [ ] U1 — `curl` TTS preview endpoint returns playable WAV
- [ ] U2 — toggle, copy, and tooltip work on both key fields
- [ ] U3 — security banner displayed; 试听 button plays audio
